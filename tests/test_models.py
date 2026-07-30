"""正規化・開示ID生成・言語判定のテスト。"""

from __future__ import annotations

import datetime as dt

from src.models import (
    fallback_disclosure_id,
    looks_english,
    normalize_security_code,
    normalize_title,
)
from src.settings import JST


def test_normalize_security_code() -> None:
    assert normalize_security_code("12340") == "1234"
    assert normalize_security_code("1234") == "1234"
    assert normalize_security_code("130A0") == "130A"  # 英字入りコード
    assert normalize_security_code(" 7203 ") == "7203"


def test_normalize_title_zenkaku_hankaku_and_case() -> None:
    assert normalize_title("ＴＯＢに関するお知らせ") == normalize_title("TOBに関するお知らせ")
    assert normalize_title("業績予想の修正  について") == "業績予想の修正について"
    assert normalize_title("(訂正)配当予想") == normalize_title("(訂正)配当予想")
    assert "、" not in normalize_title("合併、株式交換")


def test_fallback_disclosure_id_is_stable_and_normalized() -> None:
    at = dt.datetime(2026, 7, 30, 15, 30, tzinfo=JST)
    id1 = fallback_disclosure_id("1234", at, "業績予想の修正 について", "https://x/a.pdf")
    id2 = fallback_disclosure_id("1234", at, "業績予想の修正について", "https://x/a.pdf")
    id3 = fallback_disclosure_id("1234", at, "別のタイトル", "https://x/a.pdf")
    assert id1 == id2  # タイトル正規化後に同一
    assert id1 != id3
    assert id1.startswith("sha256:")


def test_looks_english() -> None:
    assert looks_english("Notice of Share Repurchase")
    assert not looks_english("業績予想の修正に関するお知らせ")
    assert not looks_english("Notice(和文タイトル併記)")
