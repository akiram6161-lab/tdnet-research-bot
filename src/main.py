"""エントリポイント CLI(仕様 §34)。

Phase 1(速報MVP)実装済み:
  python -m src.main                  # 定期監視(監視時間内のみ)
  python -m src.main --dry-run        # 取得・分類・投稿予定の表示のみ(副作用なし)
  python -m src.main --tdnet-only     # TDnet監視のみ
  python -m src.main --check-connections
  python -m src.main --test-slack
  python -m src.main --classify-title "..."

Phase 2/3 で実装予定(現状は案内を表示して終了):
  --slack-commands-only / --research-disclosure-id / --research-parent-ts
"""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import logging
import sys
import uuid
from typing import Any

from src.logging_config import log_event, setup_logging
from src.models import ClassifiedDisclosure, Disclosure, Tier
from src.settings import (
    JST,
    Settings,
    load_activists,
    load_portfolio,
    load_watchlist,
    now_jst,
    within_monitoring_window,
)
from src.slack.formatter import build_alert_blocks, build_alert_text
from src.state.repository import StateRepository
from src.tdnet.classifier import Classifier
from src.tdnet.client import TDnetClient, row_to_disclosure
from src.tdnet.parser import TDNET_LIST_BASE

logger = logging.getLogger("tdnet_research_bot")

MAX_LOOKBACK_DAYS = 3  # last_successful が極端に古い場合の遡り上限


def select_new_disclosures(
    disclosures: list[Disclosure], state: StateRepository
) -> list[Disclosure]:
    """重複排除(処理済みID・日英同時開示の英語版)を行う。"""
    ja_seen: set[tuple[str, str]] = {
        (d.security_code, d.disclosed_at.isoformat())
        for d in disclosures
        if d.language == "ja"
    }
    selected: list[Disclosure] = []
    seen_ids: set[str] = set()
    for d in disclosures:
        if d.disclosure_id in seen_ids or state.is_processed(d.disclosure_id):
            continue
        seen_ids.add(d.disclosure_id)
        if d.language == "en":
            key = (d.security_code, d.disclosed_at.isoformat())
            if key in ja_seen or state.processed_ja_exists(d.security_code, d.disclosed_at):
                # 英語版は通知せず処理済みとして記録のみ行う
                state.mark_processed(
                    d.disclosure_id,
                    security_code=d.security_code,
                    disclosed_at=d.disclosed_at,
                    tier=Tier.EXCLUDED.value,
                    language="en",
                    posted=False,
                )
                continue
        selected.append(d)
    return selected


def classify_and_label(
    disclosures: list[Disclosure], classifier: Classifier, settings: Settings
) -> list[ClassifiedDisclosure]:
    portfolio = load_portfolio(settings)
    watchlist = load_watchlist(settings)
    activists = load_activists(settings)
    results: list[ClassifiedDisclosure] = []
    for d in disclosures:
        classification = classifier.classify(d.title)
        watch = watchlist.get(d.security_code)
        results.append(
            ClassifiedDisclosure(
                disclosure=d,
                classification=classification,
                in_portfolio=d.security_code in portfolio,
                in_watchlist=watch is not None,
                watchlist_label=watch.label if watch else None,
                in_activist=d.security_code in activists,
            )
        )
    return results


def is_notify_target(item: ClassifiedDisclosure, threshold: int) -> bool:
    """個別速報の対象か。Tier 1/2かつ合計スコアが閾値以上。

    アクティビスト銘柄は+15点ボーナスにより実効的な足切りが下がるため、
    増配・自己株式取得・業績修正など株価に効く資本政策イベント(基礎65点以上)が
    通知対象に入る。決算説明資料・月次などの定例情報は通常どおり落ちる。
    """
    return (
        item.classification.tier in (Tier.TIER1, Tier.TIER2)
        and item.total_score >= threshold
    )


def run_monitor(
    settings: Settings,
    *,
    dry_run: bool,
    tdnet_only: bool,
    tdnet_client: TDnetClient | None = None,
) -> int:
    run_id = uuid.uuid4().hex[:12]
    started = now_jst()

    if not dry_run and not (settings.slack_bot_token and settings.slack_channel_id):
        # Slack未設定のまま定期実行されても失敗を繰り返さないよう、何もせず正常終了する
        log_event(
            logger,
            "SLACK_BOT_TOKEN / SLACK_CHANNEL_ID が未設定のため何も行いません"
            "(動作確認は --dry-run を使用)",
            run_id=run_id,
        )
        return 0

    if not dry_run and not within_monitoring_window(started, settings):
        log_event(logger, "outside monitoring window; exiting", run_id=run_id)
        return 0

    state = StateRepository(settings.state_path, settings.state_retention_days)
    state.load()

    since = started - dt.timedelta(minutes=settings.lookback_minutes)
    last_success = state.last_successful_tdnet_check
    if last_success is not None:
        since = min(since, last_success)
    since = max(since, started - dt.timedelta(days=MAX_LOOKBACK_DAYS))

    client = tdnet_client or TDnetClient()
    result = client.fetch_window(since, started)
    disclosures = [row_to_disclosure(row, retrieved_at=started) for row in result.rows]
    new_items = select_new_disclosures(disclosures, state)
    classified = classify_and_label(new_items, Classifier.from_yaml(settings.rules_path), settings)
    threshold = settings.research_score_threshold
    # スコア閾値以上(+アクティビスト銘柄は全開示)を個別速報の対象とする
    to_notify = [c for c in classified if is_notify_target(c, threshold)]

    if dry_run:
        _print_dry_run(since, started, result.errors, disclosures, classified, to_notify, threshold)
        return 0

    posted = 0
    slack = None
    if to_notify:
        from src.slack.client import SlackClient

        slack = SlackClient(settings.slack_bot_token)
    for item in sorted(to_notify, key=lambda c: -c.total_score):
        d = item.disclosure
        assert slack is not None
        ts = slack.post_parent_message(
            settings.slack_channel_id,
            text=build_alert_text(item),
            blocks=build_alert_blocks(item),
        )
        posted += 1
        state.add_thread_mapping(
            {
                "disclosure_id": d.disclosure_id,
                "parent_ts": ts,
                "posted_at": now_jst().isoformat(),
                "tier": item.classification.tier.value,
                "score": item.total_score,
                "security_code": d.security_code,
                "company_name": d.company_name,
                "title": d.title,
                "category": item.classification.primary_category,
                "document_url": d.document_url,
                "disclosed_at": d.disclosed_at.strftime("%Y-%m-%d %H:%M"),
                # アクティビスト銘柄の低スコア開示は速報のみ(リサーチ枠を消費しない)
                "research_status": (
                    "queued" if item.total_score >= threshold else "not_requested"
                ),
                "research_attempts": 0,
                "request_reply_ts": None,
                "requesting_user_id": None,
                "acknowledgement_ts": None,
                "research_completed_at": None,
            }
        )
        state.mark_processed(
            d.disclosure_id,
            security_code=d.security_code,
            disclosed_at=d.disclosed_at,
            tier=item.classification.tier.value,
            language=d.language,
            posted=True,
        )

    # 通知対象外(閾値未満のTier1/2・Tier3・除外)も処理済みとして記録し、再判定を防ぐ
    for item in classified:
        if item not in to_notify:
            d = item.disclosure
            state.mark_processed(
                d.disclosure_id,
                security_code=d.security_code,
                disclosed_at=d.disclosed_at,
                tier=item.classification.tier.value,
                language=d.language,
                posted=False,
            )

    research_stats = {"started": 0, "completed": 0, "failed": 0}
    highlight_posted = False
    holdings_alerted = 0
    if not tdnet_only:
        holdings_alerted = check_new_holdings(state, settings, slack, started)
        research_stats = process_research_queue(state, settings, slack, started)
        highlight_posted = post_daily_highlight_if_due(state, settings, slack, started)

    state.prune(started)
    if result.complete:
        # 空実行で毎回commitが発生しないよう、最終成功時刻は
        # 「他に状態変更があるとき」または「12時間以上古いとき」のみ永続化する。
        # lookbackは常に最低30分あるため、記録が多少古くても取りこぼしは生じない。
        last = state.last_successful_tdnet_check
        stale = last is None or (started - last) > dt.timedelta(hours=12)
        if state.dirty or stale:
            state.set_last_successful_tdnet_check(started)
    changed = state.save()

    log_event(
        logger,
        "run finished",
        run_id=run_id,
        started_at=started.isoformat(),
        finished_at=now_jst().isoformat(),
        tdnet_records_retrieved=len(disclosures),
        new_disclosures=len(new_items),
        tier_1_count=sum(1 for c in to_notify if c.classification.tier == Tier.TIER1),
        tier_2_count=sum(1 for c in to_notify if c.classification.tier == Tier.TIER2),
        slack_alerts_posted=posted,
        research_jobs_started=research_stats["started"],
        research_jobs_completed=research_stats["completed"],
        failed_research_jobs=research_stats["failed"],
        highlight_posted=highlight_posted,
        holdings_alerted=holdings_alerted,
        state_changed=changed,
        errors=result.errors,
    )
    return 0


def determine_highlight_date(
    state: StateRepository, settings: Settings, now: dt.datetime
) -> dt.date | None:
    """ハイライトを出すべき対象日を返す。対象なしなら None。

    当日は highlight_after(既定19:45)以降にのみ対象となる。
    実行が間引かれて当日中に出せなかった場合は、翌営業日以降の実行で
    直近3日まで遡ってリカバリ投稿する。
    """
    local = now.astimezone(JST)
    last = state.last_highlight_date
    candidates: list[dt.date] = []
    if local.time() >= settings.highlight_after:
        candidates.append(local.date())
    candidates += [local.date() - dt.timedelta(days=back) for back in range(1, 4)]
    for day in candidates:
        day_str = day.isoformat()
        if last and day_str <= last:
            return None
        has_items = any(
            str(m.get("posted_at", "")).startswith(day_str) for m in state.thread_mappings()
        )
        if has_items:
            return day
    return None


def post_daily_highlight_if_due(
    state: StateRepository,
    settings: Settings,
    slack: Any,
    now: dt.datetime,
) -> bool:
    """1日1回、その日の通知済み開示からハイライト+深掘りを投稿する。"""
    import shutil

    from src.research.runner import ResearchError, run_daily_highlight

    target = determine_highlight_date(state, settings, now)
    if target is None:
        return False
    if shutil.which(settings.claude_cli) is None:
        log_event(logger, "claude CLI not available; highlight deferred")
        return False

    day_str = target.isoformat()
    items = [
        m for m in state.thread_mappings() if str(m.get("posted_at", "")).startswith(day_str)
    ]
    try:
        summary, deep_dive = run_daily_highlight(items, day_str, settings)
    except ResearchError as exc:
        # 失敗しても last_highlight_date を進めず、次回実行でリトライする
        log_event(logger, "daily highlight failed", error=str(exc))
        return False

    if slack is None:
        from src.slack.client import SlackClient

        slack = SlackClient(settings.slack_bot_token)
    ts = slack.post_parent_message(settings.slack_channel_id, text=summary)
    if deep_dive:
        slack.post_parent_message(settings.slack_channel_id, text=deep_dive, thread_ts=ts)
    state.set_last_highlight_date(day_str)
    log_event(logger, "daily highlight posted", target_date=day_str, items=len(items))
    return True


HOLDINGS_CHECK_INTERVAL_HOURS = 4
HOLDINGS_ALERT_LIMIT_PER_RUN = 15


def build_holding_alert_text(position: dict[str, Any]) -> str:
    ratio = position.get("holding_ratio")
    ratio_str = f"{float(ratio) * 100:.2f}%" if ratio is not None else "不明"
    category = str(position.get("purpose_category", ""))
    label = "アクティビスト(重要提案行為)" if category == "activist" else "アクティビスト示唆"
    lines = [
        f"🧲 大量保有報告|{label}",
        "",
        f"*{position.get('filer_name')}* → "
        f"*[{position.get('issuer_sec_code')}] {position.get('issuer_name')}*",
        f"保有比率: {ratio_str}|提出日: {position.get('submit_date')}"
        f"|基準日: {position.get('base_date')}",
    ]
    value = position.get("est_holding_value")
    if value:
        lines.append(f"推定保有額: {float(value) / 1e8:.1f}億円")
    return "\n".join(lines)


def check_new_holdings(
    state: StateRepository,
    settings: Settings,
    slack: Any,
    now: dt.datetime,
) -> int:
    """EDINET DBの新規大量保有(アクティビスト)を検知して速報+深掘りキュー投入する。"""
    from src.research.edinetdb import EdinetDbError, fetch_activist_positions, holding_key

    if not settings.edinet_db_api_key:
        return 0
    last = state.last_holdings_check_at
    if last is not None and (now - last) < dt.timedelta(hours=HOLDINGS_CHECK_INTERVAL_HOURS):
        return 0

    positions: list[dict[str, Any]] = []
    try:
        for category in ("activist", "activist_implied"):
            positions.extend(fetch_activist_positions(settings, category, limit=200))
    except EdinetDbError as exc:
        log_event(logger, "holdings check failed", error=str(exc))
        return 0
    state.set_last_holdings_check_at(now)

    seen = state.seen_holdings
    if not seen:
        # 初回は既存分をすべて既読にして通知しない(バックログの洪水防止)
        state.add_seen_holdings([holding_key(p) for p in positions])
        log_event(logger, "holdings bootstrap", seeded=len(positions))
        return 0

    new_positions = [p for p in positions if holding_key(p) not in seen]
    new_positions.sort(key=lambda p: str(p.get("submit_date") or ""), reverse=True)
    alerted = 0
    if new_positions and slack is None:
        from src.slack.client import SlackClient

        slack = SlackClient(settings.slack_bot_token)
    # ファイラー別の他保有ポジション一覧(深掘りプロンプト用)
    by_filer: dict[str, list[str]] = {}
    for p in positions:
        ratio = p.get("holding_ratio") or 0
        if ratio >= 0.01:
            by_filer.setdefault(str(p.get("filer_name")), []).append(
                f"[{p.get('issuer_sec_code')}] {p.get('issuer_name')} {ratio * 100:.1f}%"
            )
    for p in new_positions[:HOLDINGS_ALERT_LIMIT_PER_RUN]:
        ts = slack.post_parent_message(
            settings.slack_channel_id, text=build_holding_alert_text(p)
        )
        alerted += 1
        is_activist = p.get("purpose_category") == "activist"
        state.add_thread_mapping(
            {
                "disclosure_id": f"holding:{holding_key(p)}",
                "kind": "holding",
                "parent_ts": ts,
                "posted_at": now_jst().isoformat(),
                "tier": 1,
                "score": 90 if is_activist else 70,
                "security_code": str(p.get("issuer_sec_code", "")),
                "company_name": str(p.get("issuer_name", "")),
                "filer_name": str(p.get("filer_name", "")),
                "ratio": p.get("holding_ratio"),
                "submit_date": str(p.get("submit_date", "")),
                "holding_category": str(p.get("purpose_category", "")),
                "other_positions": by_filer.get(str(p.get("filer_name")), [])[:15],
                "title": f"{p.get('filer_name')}による大量保有報告",
                "document_url": "",
                "disclosed_at": str(p.get("submit_date", "")),
                # アクティビスト明示のみ深掘り。示唆どまりは速報のみ
                "research_status": "queued" if is_activist else "not_requested",
                "research_attempts": 0,
            }
        )
        state.add_seen_holdings([holding_key(p)])
    if len(new_positions) > HOLDINGS_ALERT_LIMIT_PER_RUN:
        log_event(
            logger,
            "holdings alert limit reached; remainder deferred to next run",
            deferred=len(new_positions) - HOLDINGS_ALERT_LIMIT_PER_RUN,
        )
    return alerted


def sync_activists(settings: Settings) -> int:
    """EDINET DBからアクティビスト保有銘柄(5%以上)を取得しactivists.csvを再生成する。"""
    import csv

    from src.research.edinetdb import fetch_activist_positions

    positions = fetch_activist_positions(settings, "activist", limit=1000)
    by_issuer: dict[str, dict[str, Any]] = {}
    for p in positions:
        code, ratio = p.get("issuer_sec_code"), p.get("holding_ratio") or 0
        if not code or ratio < 0.05:
            continue
        entry = by_issuer.setdefault(str(code), {"name": p["issuer_name"], "ratio": 0.0})
        entry["ratio"] = max(entry["ratio"], ratio)
    with settings.activists_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(["security_code", "company_name", "active"])
        for code, entry in sorted(by_issuer.items(), key=lambda x: -x[1]["ratio"]):
            writer.writerow([code, entry["name"], "true"])
    print(f"activists.csv updated: {len(by_issuer)} issuers (from {len(positions)} positions)")
    return 0


def run_highlight_only(settings: Settings) -> int:
    """ハイライト投稿のみ実行する(monitor.ymlの分離ステップ用)。"""
    if not (settings.slack_bot_token and settings.slack_channel_id):
        print("SLACK_BOT_TOKEN / SLACK_CHANNEL_ID を設定してください。")
        return 1
    state = StateRepository(settings.state_path, settings.state_retention_days)
    state.load()
    posted = post_daily_highlight_if_due(state, settings, None, now_jst())
    state.save()
    log_event(logger, "highlight-only run finished", highlight_posted=posted)
    return 0


def cmd_highlight_due(settings: Settings) -> int:
    """ハイライトを出すべきか(1/0)を出力する。workflowの条件分岐用。"""
    state = StateRepository(settings.state_path, settings.state_retention_days)
    state.load()
    due = determine_highlight_date(state, settings, now_jst())
    print("1" if due is not None else "0")
    return 0


def process_research_queue(
    state: StateRepository,
    settings: Settings,
    slack: Any,
    now: dt.datetime,
) -> dict[str, int]:
    """queued 状態のスレッドを、実行回数上限の範囲で Claude Code リサーチする。"""
    import shutil

    from src.research.runner import ResearchError, run_research
    from src.slack.formatter import RESEARCH_FAILED_MESSAGE

    stats = {"started": 0, "completed": 0, "failed": 0}
    queued = state.thread_mappings(research_status="queued")
    if not queued:
        return stats
    if shutil.which(settings.claude_cli) is None:
        # CLI未導入の実行(CIのインストール省略時など)ではqueuedのまま次回に持ち越す
        log_event(logger, "claude CLI not available; research jobs stay queued")
        return stats
    if slack is None:
        from src.slack.client import SlackClient

        slack = SlackClient(settings.slack_bot_token)

    today = now.astimezone(JST).strftime("%Y-%m-%d")
    # 72時間を超えて残ったジョブは失効させる(金曜夕方の案件を月曜に処理できる幅)
    cutoff = now - dt.timedelta(hours=72)
    for job in queued:
        posted_at = job.get("posted_at")
        with contextlib.suppress(TypeError, ValueError):
            if posted_at and dt.datetime.fromisoformat(posted_at) < cutoff:
                job["research_status"] = "expired"
    queued = [j for j in queued if j.get("research_status") == "queued"]
    # 当日分を最優先(投稿日の新しい順)、同日内はスコアの高い順に処理する
    queued.sort(
        key=lambda m: (str(m.get("posted_at", ""))[:10], int(m.get("score", 0))),
        reverse=True,
    )
    for job in queued:
        if stats["started"] >= settings.max_research_jobs_per_run:
            break
        if state.research_count_today(today) >= settings.max_auto_research_per_day:
            log_event(logger, "daily research cap reached; remaining jobs stay queued")
            break
        job["research_status"] = "running"
        stats["started"] += 1
        state.increment_research_count(today)
        try:
            if job.get("kind") == "holding":
                from src.research.runner import run_holding_deep_dive

                summary = run_holding_deep_dive(job, settings)
            else:
                summary = run_research(job, settings)
            slack.post_parent_message(
                str(job.get("channel_id") or settings.slack_channel_id),
                text=summary,
                thread_ts=str(job["parent_ts"]),
            )
            job["research_status"] = "completed"
            job["research_completed_at"] = now_jst().isoformat()
            stats["completed"] += 1
        except ResearchError as exc:
            attempts = int(job.get("research_attempts", 0)) + 1
            job["research_attempts"] = attempts
            job["research_status"] = "queued" if attempts < 2 else "failed_permanent"
            stats["failed"] += 1
            log_event(logger, "research failed", error=str(exc), attempts=attempts)
            if attempts >= 2:
                with contextlib.suppress(Exception):
                    slack.post_parent_message(
                        str(job.get("channel_id") or settings.slack_channel_id),
                        text=RESEARCH_FAILED_MESSAGE,
                        thread_ts=str(job["parent_ts"]),
                    )
    return stats



def _print_dry_run(
    since: dt.datetime,
    until: dt.datetime,
    errors: list[str],
    all_disclosures: list[Disclosure],
    classified: list[ClassifiedDisclosure],
    to_notify: list[ClassifiedDisclosure],
    threshold: int,
) -> None:
    print("=" * 72)
    print("DRY-RUN(Slack投稿・state変更・Git commitは行いません)")
    print(f"取得ウィンドウ: {since.isoformat()} 〜 {until.isoformat()}")
    print(f"取得件数: {len(all_disclosures)} / 新規(重複排除後): {len(classified)}")
    if errors:
        print(f"取得エラー: {errors}")
    print("=" * 72)

    tier_counts: dict[Tier, int] = {}
    for item in classified:
        tier_counts[item.classification.tier] = tier_counts.get(item.classification.tier, 0) + 1
    print(
        f"Tier内訳: TIER1={tier_counts.get(Tier.TIER1, 0)} "
        f"TIER2={tier_counts.get(Tier.TIER2, 0)} "
        f"TIER3={tier_counts.get(Tier.TIER3, 0)} "
        f"除外={tier_counts.get(Tier.EXCLUDED, 0)}"
    )
    print()
    print(f"--- 個別速報+自動リサーチ対象(スコア{threshold}点以上): {len(to_notify)}件 ---")
    for item in sorted(to_notify, key=lambda c: -c.total_score):
        print()
        print(build_alert_text(item))
        print("-" * 60)
    print()
    print(f"--- 通知対象外(スコア{threshold}点未満・Tier 3・除外)---")
    for item in classified:
        c = item.classification
        if item in to_notify:
            continue
        d = item.disclosure
        if c.tier in (Tier.TIER1, Tier.TIER2):
            label = f"T{c.tier.value}/{item.total_score}点"
        elif c.tier == Tier.EXCLUDED:
            label = "除外"
        else:
            label = "TIER3"
        print(f"  {label} [{d.security_code}] {d.company_name}: {d.title}")


def cmd_check_connections(settings: Settings) -> int:
    """各外部サービスへの接続確認。Phase 1 では TDnet が必須、他は設定有無を表示。"""
    exit_code = 0
    print("接続確認:")

    client = TDnetClient()
    today = now_jst().date()
    try:
        result = client.fetch_daily_list(today)
        print(
            f"  [OK] TDnet: {TDNET_LIST_BASE} "
            f"(本日 {result.total_declared} 件宣言 / {len(result.rows)} 件取得 / "
            f"{len(result.pages_fetched)} ページ)"
        )
        if result.errors:
            print(f"       警告: {result.errors}")
    except Exception as exc:
        print(f"  [NG] TDnet: {exc}")
        exit_code = 1

    if settings.slack_bot_token:
        try:
            from src.slack.client import SlackClient

            info = SlackClient(settings.slack_bot_token).auth_test()
            print(
                f"  [OK] Slack(Bot): team={info.get('team')} bot_user={info.get('user')}"
            )
        except Exception as exc:
            print(f"  [NG] Slack(Bot): {exc}")
            exit_code = 1
    else:
        print("  [--] Slack(Bot): SLACK_BOT_TOKEN 未設定")

    import shutil

    cli_path = shutil.which(settings.claude_cli)
    if cli_path:
        token_note = (
            "トークン設定済み" if settings.claude_code_oauth_token else "サブスク認証(ローカル)"
        )
        print(f"  [OK] Claude Code CLI: {cli_path}({token_note})")
    else:
        print(
            f"  [--] Claude Code CLI: `{settings.claude_cli}` が見つかりません"
            "(自動リサーチはGitHub Actions上で実行)"
        )

    for name, configured in [
        ("EDINET DB", bool(settings.edinet_db_api_key)),
        ("J-Quants", bool(settings.jquants_api_key)),
    ]:
        status = "設定済み(リサーチ時にClaude Codeが使用)" if configured else "未設定(任意)"
        print(f"  [--] {name}: {status}")
    return exit_code


def cmd_test_slack(settings: Settings) -> int:
    if not settings.slack_bot_token or not settings.slack_channel_id:
        print("SLACK_BOT_TOKEN / SLACK_CHANNEL_ID を設定してください。")
        return 1
    from src.slack.client import SlackClient

    slack = SlackClient(settings.slack_bot_token)
    ts = slack.post_parent_message(
        settings.slack_channel_id,
        text="✅ tdnet-research-bot 接続テスト(このメッセージは手動テストです)",
    )
    print(f"テスト投稿に成功しました: ts={ts}")
    return 0


def cmd_classify_title(settings: Settings, title: str) -> int:
    classifier = Classifier.from_yaml(settings.rules_path)
    result = classifier.classify(title)
    tier_label = "除外" if result.tier == Tier.EXCLUDED else f"TIER {result.tier.value}"
    print(f"タイトル: {title}")
    print(f"判定: {tier_label}")
    print(f"カテゴリー: {result.primary_category}")
    print(f"マッチしたルール: {result.matched_rule_ids}")
    print(f"マッチしたキーワード: {result.matched_keywords}")
    print(f"確信度: {result.confidence}")
    print(f"判定理由: {result.classification_reason}")
    return 0


def run_research_only(settings: Settings) -> int:
    """queuedの自動リサーチのみを処理する(monitor.ymlの分離ステップ用)。"""
    if not (settings.slack_bot_token and settings.slack_channel_id):
        print("SLACK_BOT_TOKEN / SLACK_CHANNEL_ID を設定してください。")
        return 1
    state = StateRepository(settings.state_path, settings.state_retention_days)
    state.load()
    stats = process_research_queue(state, settings, None, now_jst())
    changed = state.save()
    log_event(
        logger,
        "research-only run finished",
        research_jobs_started=stats["started"],
        research_jobs_completed=stats["completed"],
        failed_research_jobs=stats["failed"],
        state_changed=changed,
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m src.main")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--tdnet-only", action="store_true")
    parser.add_argument("--research-only", action="store_true")
    parser.add_argument("--daily-highlight", action="store_true")
    parser.add_argument("--highlight-due", action="store_true")
    parser.add_argument("--sync-activists", action="store_true")
    parser.add_argument("--slack-commands-only", action="store_true")
    parser.add_argument("--check-connections", action="store_true")
    parser.add_argument("--test-slack", action="store_true")
    parser.add_argument("--classify-title", metavar="TITLE")
    parser.add_argument("--research-disclosure-id", metavar="ID")
    parser.add_argument("--research-parent-ts", metavar="TIMESTAMP")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    settings = Settings.from_env()
    setup_logging(settings.log_level)

    if args.check_connections:
        return cmd_check_connections(settings)
    if args.test_slack:
        return cmd_test_slack(settings)
    if args.classify_title:
        return cmd_classify_title(settings, args.classify_title)
    if args.research_only:
        return run_research_only(settings)
    if args.daily_highlight:
        return run_highlight_only(settings)
    if args.highlight_due:
        return cmd_highlight_due(settings)
    if args.sync_activists:
        return sync_activists(settings)
    if args.slack_commands_only or args.research_disclosure_id or args.research_parent_ts:
        print("この機能は Phase 2 / Phase 3 で実装予定です。")
        return 2
    return run_monitor(settings, dry_run=args.dry_run, tdnet_only=args.tdnet_only)


if __name__ == "__main__":
    sys.exit(main())
