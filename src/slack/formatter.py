"""Slack 速報メッセージのフォーマット(仕様 §17)。"""

from __future__ import annotations

from typing import Any

from src.models import ClassifiedDisclosure, Tier

TIER_EMOJI = {Tier.TIER1: "🔴", Tier.TIER2: "🟠"}

RESEARCH_HINT = "この開示を分析する場合は、この投稿のスレッドに「リサーチ」と返信してください。"
DEFAULT_SUMMARY = "概要:詳細は開示資料をご確認ください。"


def build_alert_text(item: ClassifiedDisclosure) -> str:
    """通知のフォールバックテキスト(および dry-run 表示)を組み立てる。"""
    d = item.disclosure
    c = item.classification
    emoji = TIER_EMOJI.get(c.tier, "⚪")
    lines = [f"{emoji} {item.total_score}点|TIER {c.tier.value}|TDnet適時開示", ""]
    labels = []
    if item.in_activist:
        labels.append("⚡ ACTIVIST")
    if item.in_portfolio:
        labels.append("💼 PORTFOLIO")
    if item.in_watchlist:
        label = f"👀 WATCHLIST({item.watchlist_label})" if item.watchlist_label else "👀 WATCHLIST"
        labels.append(label)
    if labels:
        lines.append(" ".join(labels))
    lines += [
        f"[{d.security_code}] {d.company_name}",
        f"開示時刻:{d.disclosed_at.strftime('%H:%M')} JST",
        f"開示タイトル:{d.title}",
        f"カテゴリー:{c.primary_category}",
        "",
        DEFAULT_SUMMARY,
        "",
        f"資料:{d.document_url}",
        "",
        RESEARCH_HINT,
    ]
    return "\n".join(lines)


def build_alert_blocks(item: ClassifiedDisclosure) -> list[dict[str, Any]]:
    """Block Kit ブロックを組み立てる。"""
    d = item.disclosure
    c = item.classification
    emoji = TIER_EMOJI.get(c.tier, "⚪")

    header = f"{emoji} {item.total_score}点|TIER {c.tier.value}|TDnet適時開示"
    labels = []
    if item.in_activist:
        labels.append("⚡ ACTIVIST")
    if item.in_portfolio:
        labels.append("💼 PORTFOLIO")
    if item.in_watchlist:
        labels.append(
            f"👀 WATCHLIST({item.watchlist_label})" if item.watchlist_label else "👀 WATCHLIST"
        )

    body_lines = [
        f"*[{d.security_code}] {d.company_name}*",
        f"開示時刻:{d.disclosed_at.strftime('%H:%M')} JST",
        f"開示タイトル:{d.title}",
        f"カテゴリー:{c.primary_category}",
    ]
    if labels:
        body_lines.insert(0, " ".join(labels))

    blocks: list[dict[str, Any]] = [
        {"type": "section", "text": {"type": "mrkdwn", "text": header}},
        {"type": "section", "text": {"type": "mrkdwn", "text": "\n".join(body_lines)}},
        {"type": "section", "text": {"type": "mrkdwn", "text": DEFAULT_SUMMARY}},
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"資料:<{d.document_url}|TDnet開示資料(PDF)>"},
        },
        {"type": "context", "elements": [{"type": "mrkdwn", "text": RESEARCH_HINT}]},
    ]
    return blocks


RESEARCH_FAILED_MESSAGE = (
    "⚠️ 自動リサーチに失敗しました。再実行可能な状態で保存しました。"
)
