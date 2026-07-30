"""アプリケーション全体で使う構造化データモデル。"""

from __future__ import annotations

import datetime as dt
import hashlib
import re
import unicodedata
from dataclasses import dataclass, field
from enum import IntEnum


class Tier(IntEnum):
    """開示の重要度区分。EXCLUDED は明示除外、TIER3 はルール非該当。"""

    TIER1 = 1
    TIER2 = 2
    TIER3 = 3
    EXCLUDED = 9


@dataclass(frozen=True)
class Disclosure:
    """TDnet 適時開示 1 件。"""

    disclosure_id: str
    disclosed_at: dt.datetime
    security_code: str  # 4桁に正規化したコード
    raw_code: str  # TDnet 上の5桁コード
    company_name: str
    title: str
    document_url: str
    document_type: str = "pdf"
    exchange: str | None = None
    language: str = "ja"
    category: str | None = None
    retrieved_at: dt.datetime | None = None
    english_url: str | None = None  # 日本語版に対する英語版URL(補足)


@dataclass(frozen=True)
class ClassificationResult:
    """Tier 判定結果。"""

    tier: Tier
    primary_category: str
    matched_rule_ids: list[str] = field(default_factory=list)
    matched_keywords: list[str] = field(default_factory=list)
    confidence: str = "low"
    classification_reason: str = ""
    score: int = 0  # ルール由来の重要度スコア(0〜100)

PORTFOLIO_BONUS = 25
WATCHLIST_BONUS = 15


@dataclass(frozen=True)
class ClassifiedDisclosure:
    """開示と判定結果、ポートフォリオ/ウォッチリストのラベル。"""

    disclosure: Disclosure
    classification: ClassificationResult
    in_portfolio: bool = False
    in_watchlist: bool = False
    watchlist_label: str | None = None

    @property
    def total_score(self) -> int:
        """ルールスコア+保有/監視銘柄ボーナス(上限100)。"""
        score = self.classification.score
        if self.in_portfolio:
            score += PORTFOLIO_BONUS
        if self.in_watchlist:
            score += WATCHLIST_BONUS
        return min(score, 100)


def normalize_security_code(code: str) -> str:
    """証券コードを4桁(または英数字コードはそのまま)に正規化する。"""
    cleaned = re.sub(r"[^0-9A-Za-z]", "", code).upper()
    if len(cleaned) == 5 and cleaned.endswith("0"):
        return cleaned[:4]
    return cleaned


_WS_RE = re.compile(r"\s+")
_PUNCT_RE = re.compile(r"[、。・，．,.！!？?：:；;「」『』【】\[\]()（）<>《》〈〉\"'“”‘’-]")  # noqa: RUF001


def normalize_title(title: str) -> str:
    """開示タイトルを判定用に正規化する(全半角・大小文字・空白・句読点)。"""
    text = unicodedata.normalize("NFKC", title)
    text = text.lower()
    text = _PUNCT_RE.sub("", text)
    text = _WS_RE.sub("", text)
    return text


def fallback_disclosure_id(
    security_code: str,
    disclosed_at: dt.datetime,
    title: str,
    document_url: str,
) -> str:
    """安定IDが得られない場合の SHA256 ベースの開示ID。"""
    payload = "|".join(
        [security_code, disclosed_at.isoformat(), normalize_title(title), document_url]
    )
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def looks_english(title: str) -> bool:
    """タイトルが英文開示(日本語文字を含まない)かを判定する。"""
    for ch in title:
        name = unicodedata.name(ch, "")
        if "CJK" in name or "HIRAGANA" in name or "KATAKANA" in name:
            return False
    return True
