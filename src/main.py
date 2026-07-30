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
import datetime as dt
import logging
import sys
import uuid

from src.logging_config import log_event, setup_logging
from src.models import ClassifiedDisclosure, Disclosure, Tier
from src.settings import (
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


def run_monitor(settings: Settings, *, dry_run: bool, tdnet_only: bool) -> int:
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

    client = TDnetClient()
    result = client.fetch_window(since, started)
    disclosures = [row_to_disclosure(row, retrieved_at=started) for row in result.rows]
    new_items = select_new_disclosures(disclosures, state)
    classified = classify_and_label(new_items, Classifier.from_yaml(settings.rules_path), settings)
    to_notify = [c for c in classified if c.classification.tier in (Tier.TIER1, Tier.TIER2)]

    if dry_run:
        _print_dry_run(since, started, result.errors, disclosures, classified, to_notify)
        return 0

    posted = 0
    slack = None
    if to_notify:
        from src.slack.client import SlackClient

        slack = SlackClient(settings.slack_bot_token)
    for item in to_notify:
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
                "research_status": "not_requested",
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

    # 通知対象外(Tier3・除外)も処理済みとして記録し、再判定を防ぐ
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

    if result.complete:
        state.set_last_successful_tdnet_check(started)
    state.prune(started)
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
        state_changed=changed,
        errors=result.errors,
    )
    if tdnet_only:
        return 0
    # Slackスレッド監視は Phase 2 で実装
    return 0


def _print_dry_run(
    since: dt.datetime,
    until: dt.datetime,
    errors: list[str],
    all_disclosures: list[Disclosure],
    classified: list[ClassifiedDisclosure],
    to_notify: list[ClassifiedDisclosure],
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
    print(f"--- Slack投稿予定: {len(to_notify)}件 ---")
    for item in to_notify:
        print()
        print(build_alert_text(item))
        print("-" * 60)
    print()
    print("--- 通知対象外(Tier 3・除外)---")
    for item in classified:
        if item in to_notify:
            continue
        c = item.classification
        d = item.disclosure
        tier_label = "除外" if c.tier == Tier.EXCLUDED else f"TIER{c.tier.value}"
        reason = f" [{c.classification_reason}]" if c.tier == Tier.EXCLUDED else ""
        print(f"  {tier_label} [{d.security_code}] {d.company_name}: {d.title}{reason}")
    print()
    print("リサーチコマンド検知: Phase 2 で実装予定(検知対象スレッドなし)")


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

    for name, configured in [
        ("EDINET DB", bool(settings.edinet_db_api_key)),
        ("J-Quants", bool(settings.jquants_api_key)),
        ("Claude API", bool(settings.anthropic_api_key)),
    ]:
        status = "設定済み(Phase 3で接続実装)" if configured else "未設定(Phase 3で使用)"
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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m src.main")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--tdnet-only", action="store_true")
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
    if args.slack_commands_only or args.research_disclosure_id or args.research_parent_ts:
        print("この機能は Phase 2 / Phase 3 で実装予定です。")
        return 2
    return run_monitor(settings, dry_run=args.dry_run, tdnet_only=args.tdnet_only)


if __name__ == "__main__":
    sys.exit(main())
