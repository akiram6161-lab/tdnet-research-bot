"""Tier判定ルールのテスト(Tier 1 / Tier 2 / 除外 / negative優先)。"""

from __future__ import annotations

import pytest

from src.models import Tier
from src.settings import REPO_ROOT
from src.tdnet.classifier import Classifier

RULES = REPO_ROOT / "config" / "disclosure_rules.yaml"


@pytest.fixture(scope="module")
def classifier() -> Classifier:
    return Classifier.from_yaml(RULES)


@pytest.mark.parametrize(
    "title",
    [
        "株式会社ABCによる当社株式に対する公開買付けの開始に関するお知らせ",
        "MBOの実施及び応募推奨に関するお知らせ",
        "通期業績予想の上方修正に関するお知らせ",
        "業績予想の下方修正に関するお知らせ",
        "第三者割当による新株式発行に関するお知らせ",
        "自己株式の取得に係る事項の決定に関するお知らせ",
        "親会社の異動に関するお知らせ",
        "継続企業の前提に関する注記の記載に関するお知らせ",
        "特別損失の計上に関するお知らせ",
        "臨時株主総会招集のための基準日設定に関するお知らせ",
        "第三者委員会の設置に関するお知らせ",
        "監理銘柄(審査中)の指定に関するお知らせ",
        "資本業務提携に関するお知らせ",
        # regression: 2026-07-30 実データで TIER3 になっていた子会社化開示
        "株式会社ライトの株式取得（子会社化）のお知らせ",  # noqa: RUF001
    ],
)
def test_tier1_titles(classifier: Classifier, title: str) -> None:
    assert classifier.classify(title).tier == Tier.TIER1


@pytest.mark.parametrize(
    "title",
    [
        "株式会社XYZとの業務提携に関するお知らせ",
        "大型受注に関するお知らせ",
        "新サービスの提供開始に関するお知らせ",
        "中期経営計画の策定に関するお知らせ",
        "固定資産の譲渡に関するお知らせ",
        "2026年7月度 月次売上高に関するお知らせ",
        "株主優待制度の変更に関するお知らせ",
        "代表取締役の異動に関するお知らせ",
    ],
)
def test_tier2_titles(classifier: Classifier, title: str) -> None:
    assert classifier.classify(title).tier == Tier.TIER2


@pytest.mark.parametrize(
    "title",
    [
        "自己株式の取得状況に関するお知らせ",
        "コーポレート・ガバナンスに関する報告書の提出について",
        "譲渡制限付株式報酬としての新株式の発行に関するお知らせ",
        "ストックオプション(新株予約権)の付与に関するお知らせ",
        "決算説明会開催のお知らせ",
        "定時株主総会招集ご通知の掲載について",
        "新株予約権の行使状況に関するお知らせ",
        "人事異動に関するお知らせ",
    ],
)
def test_excluded_titles(classifier: Classifier, title: str) -> None:
    assert classifier.classify(title).tier == Tier.EXCLUDED


def test_exclusion_beats_tier1_keyword(classifier: Classifier) -> None:
    """「自己株式の取得状況」はTier 1の自己株式キーワードを含むが除外が優先される。"""
    result = classifier.classify("自己株式の取得状況に関するお知らせ")
    assert result.tier == Tier.EXCLUDED
    assert "ex_treasury_buyback_monthly" in result.matched_rule_ids


def test_negative_keyword_blocks_rule(classifier: Classifier) -> None:
    """譲渡制限付株式報酬は新株予約権ルール(Tier 1)のnegativeで弾かれ除外になる。"""
    result = classifier.classify("譲渡制限付株式報酬としての新株予約権の発行に関するお知らせ")
    assert result.tier == Tier.EXCLUDED


def test_default_is_tier3(classifier: Classifier) -> None:
    result = classifier.classify("2027年3月期 第1四半期決算短信〔日本基準〕(連結)")  # noqa: RUF001
    assert result.tier == Tier.TIER3
    assert result.confidence == "low"


def test_classification_result_fields(classifier: Classifier) -> None:
    result = classifier.classify("通期業績予想の上方修正に関するお知らせ")
    assert result.primary_category == "業績予想修正"
    assert result.matched_rule_ids
    assert result.matched_keywords
    assert result.classification_reason
