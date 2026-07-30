"""Slack速報フォーマットとポートフォリオ/ウォッチリストラベルのテスト。"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

from src.models import ClassificationResult, ClassifiedDisclosure, Disclosure, Tier
from src.settings import JST, Settings, load_portfolio, load_watchlist
from src.slack.formatter import build_alert_blocks, build_alert_text

AT = dt.datetime(2026, 7, 30, 15, 30, tzinfo=JST)


def make_item(
    tier: Tier = Tier.TIER1,
    in_portfolio: bool = False,
    in_watchlist: bool = False,
    watchlist_label: str | None = None,
) -> ClassifiedDisclosure:
    return ClassifiedDisclosure(
        disclosure=Disclosure(
            disclosure_id="140120260730512345",
            disclosed_at=AT,
            security_code="1234",
            raw_code="12340",
            company_name="ABC株式会社",
            title="通期業績予想の上方修正に関するお知らせ",
            document_url="https://www.release.tdnet.info/inbs/140120260730512345.pdf",
        ),
        classification=ClassificationResult(
            tier=tier,
            primary_category="業績予想修正",
            matched_rule_ids=["t1_guidance_revision"],
            matched_keywords=["上方修正"],
            confidence="high",
            classification_reason="業績予想の修正",
        ),
        in_portfolio=in_portfolio,
        in_watchlist=in_watchlist,
        watchlist_label=watchlist_label,
    )


def test_alert_text_tier1() -> None:
    text = build_alert_text(make_item(Tier.TIER1))
    assert "🔴 TIER 1|TDnet適時開示" in text
    assert "[1234] ABC株式会社" in text
    assert "開示時刻:15:30 JST" in text
    assert "カテゴリー:業績予想修正" in text
    assert "概要:詳細は開示資料をご確認ください。" in text
    assert "https://www.release.tdnet.info/inbs/140120260730512345.pdf" in text
    assert "リサーチ」と返信" in text


def test_alert_text_tier2_emoji() -> None:
    assert "🟠 TIER 2|TDnet適時開示" in build_alert_text(make_item(Tier.TIER2))


def test_labels_in_text_and_blocks() -> None:
    item = make_item(in_portfolio=True, in_watchlist=True, watchlist_label="半導体")
    text = build_alert_text(item)
    assert "💼 PORTFOLIO" in text
    assert "👀 WATCHLIST(半導体)" in text
    blocks = build_alert_blocks(item)
    assert blocks[0]["text"]["text"].startswith("🔴 TIER 1")
    body = blocks[1]["text"]["text"]
    assert "💼 PORTFOLIO" in body


def test_blocks_structure() -> None:
    blocks = build_alert_blocks(make_item())
    types = [b["type"] for b in blocks]
    assert types == ["section", "section", "section", "section", "context"]


def test_portfolio_and_watchlist_loading(tmp_path: Path) -> None:
    portfolio_csv = tmp_path / "portfolio.csv"
    watchlist_csv = tmp_path / "watchlist.csv"
    portfolio_csv.write_text(
        "security_code,company_name,shares,average_cost,active\n"
        "72030,トヨタ自動車,100,2500,true\n"
        "9999,無効銘柄,10,100,false\n",
        encoding="utf-8",
    )
    watchlist_csv.write_text(
        "security_code,company_name,label,active\n6501,日立製作所,重電,true\n",
        encoding="utf-8",
    )
    settings = Settings(portfolio_path=portfolio_csv, watchlist_path=watchlist_csv)
    portfolio = load_portfolio(settings)
    watchlist = load_watchlist(settings)
    assert "7203" in portfolio  # 5桁→4桁正規化
    assert "9999" not in portfolio  # active=false は除外
    assert watchlist["6501"].label == "重電"


def test_missing_csv_returns_empty(tmp_path: Path) -> None:
    settings = Settings(
        portfolio_path=tmp_path / "none.csv", watchlist_path=tmp_path / "none2.csv"
    )
    assert load_portfolio(settings) == {}
    assert load_watchlist(settings) == {}
