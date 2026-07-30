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
            )
        )
    return results


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
    tier12 = [c for c in classified if c.classification.tier in (Tier.TIER1, Tier.TIER2)]
    # スコア閾値以上のみ個別速報+自動リサーチ対象。未満は夕方ダイジェストへ。
    to_notify = [c for c in tier12 if c.total_score >= threshold]

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
                "channel_id": settings.slack_channel_id,
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
                "research_status": "queued",
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
    if not tdnet_only:
        research_stats = process_research_queue(state, settings, slack, started)

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
        state_changed=changed,
        errors=result.errors,
    )
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
    # 24時間を超えて残ったジョブは失効させ、翌日の新規開示を優先する
    cutoff = now - dt.timedelta(hours=24)
    for job in queued:
        posted_at = job.get("posted_at")
        with contextlib.suppress(TypeError, ValueError):
            if posted_at and dt.datetime.fromisoformat(posted_at) < cutoff:
                job["research_status"] = "expired"
    queued = [j for j in queued if j.get("research_status") == "queued"]
    # スコアの高い順に処理する
    queued.sort(key=lambda m: -int(m.get("score", 0)))
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
    if args.slack_commands_only or args.research_disclosure_id or args.research_parent_ts:
        print("この機能は Phase 2 / Phase 3 で実装予定です。")
        return 2
    return run_monitor(settings, dry_run=args.dry_run, tdnet_only=args.tdnet_only)


if __name__ == "__main__":
    sys.exit(main())
