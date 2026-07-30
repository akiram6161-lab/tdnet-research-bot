"""重複排除(処理済みID・日英同時開示)のテスト。"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

from src.main import select_new_disclosures
from src.models import Disclosure
from src.settings import JST
from src.state.repository import StateRepository

AT = dt.datetime(2026, 7, 30, 15, 30, tzinfo=JST)


def make_disclosure(
    disclosure_id: str,
    code: str = "1234",
    title: str = "業績予想の修正に関するお知らせ",
    language: str = "ja",
    at: dt.datetime = AT,
) -> Disclosure:
    return Disclosure(
        disclosure_id=disclosure_id,
        disclosed_at=at,
        security_code=code,
        raw_code=code + "0",
        company_name="テスト社",
        title=title,
        document_url=f"https://www.release.tdnet.info/inbs/{disclosure_id}.pdf",
        language=language,
    )


def make_state(tmp_path: Path) -> StateRepository:
    repo = StateRepository(tmp_path / "state.json", 90)
    repo.load()
    return repo


def test_processed_ids_are_skipped(tmp_path: Path) -> None:
    state = make_state(tmp_path)
    state.mark_processed(
        "a", security_code="1234", disclosed_at=AT, tier=1, language="ja", posted=True
    )
    items = [make_disclosure("a"), make_disclosure("b")]
    selected = select_new_disclosures(items, state)
    assert [d.disclosure_id for d in selected] == ["b"]


def test_same_batch_duplicate_ids_are_skipped(tmp_path: Path) -> None:
    state = make_state(tmp_path)
    items = [make_disclosure("a"), make_disclosure("a")]
    assert len(select_new_disclosures(items, state)) == 1


def test_english_version_in_same_batch_is_suppressed(tmp_path: Path) -> None:
    state = make_state(tmp_path)
    ja = make_disclosure("ja1", title="業績予想の修正に関するお知らせ", language="ja")
    en = make_disclosure("en1", title="Notice of Revision of Earnings Forecast", language="en")
    selected = select_new_disclosures([ja, en], state)
    assert [d.disclosure_id for d in selected] == ["ja1"]
    # 英語版は処理済みとして記録され、次回以降も再通知されない
    assert state.is_processed("en1")


def test_english_version_against_processed_state(tmp_path: Path) -> None:
    state = make_state(tmp_path)
    state.mark_processed(
        "ja1", security_code="1234", disclosed_at=AT, tier=1, language="ja", posted=True
    )
    en = make_disclosure("en1", title="Notice of Revision of Earnings Forecast", language="en")
    selected = select_new_disclosures([en], state)
    assert selected == []


def test_standalone_english_disclosure_is_kept(tmp_path: Path) -> None:
    """日本語版が存在しない英文開示は通常フローに乗せる(Tier判定に委ねる)。"""
    state = make_state(tmp_path)
    en = make_disclosure("en1", code="9999", title="Notice of Something", language="en")
    selected = select_new_disclosures([en], state)
    assert [d.disclosure_id for d in selected] == ["en1"]
