"""ルールベースの Tier 判定。

config/disclosure_rules.yaml のルールを正規化済みタイトルに適用する。
除外ルール(tier: exclude)と negative_keywords を一般キーワードより優先する。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

from src.models import ClassificationResult, Tier, normalize_title


@dataclass(frozen=True)
class Rule:
    rule_id: str
    tier: Tier
    category: str
    positive_keywords: list[str]
    negative_keywords: list[str]
    priority: int
    explanation: str
    score: int = 0


def _tier_from_value(value: object) -> Tier:
    if isinstance(value, str) and value.lower() in {"exclude", "excluded"}:
        return Tier.EXCLUDED
    if isinstance(value, int) and value in {1, 2, 3}:
        return Tier(value)
    raise ValueError(f"Unsupported tier value in rules yaml: {value!r}")


def load_rules(path: Path) -> list[Rule]:
    with path.open(encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    rules: list[Rule] = []
    for item in data.get("rules", []):
        rules.append(
            Rule(
                rule_id=str(item["id"]),
                tier=_tier_from_value(item["tier"]),
                category=str(item.get("category", "")),
                positive_keywords=[normalize_title(k) for k in item.get("positive_keywords", [])],
                negative_keywords=[normalize_title(k) for k in item.get("negative_keywords", [])],
                priority=int(item.get("priority", 0)),
                explanation=str(item.get("explanation", "")),
                score=int(item.get("score", 0)),
            )
        )
    if not rules:
        raise ValueError(f"No rules loaded from {path}")
    return rules


@dataclass(frozen=True)
class _Match:
    rule: Rule
    keywords: list[str]


class Classifier:
    def __init__(self, rules: list[Rule]) -> None:
        self._rules = rules

    @classmethod
    def from_yaml(cls, path: Path) -> Classifier:
        return cls(load_rules(path))

    def classify(self, title: str) -> ClassificationResult:
        normalized = normalize_title(title)
        matches: list[_Match] = []
        for rule in self._rules:
            hit = [kw for kw in rule.positive_keywords if kw and kw in normalized]
            if not hit:
                continue
            if any(neg and neg in normalized for neg in rule.negative_keywords):
                continue
            matches.append(_Match(rule=rule, keywords=hit))

        if not matches:
            return ClassificationResult(
                tier=Tier.TIER3,
                primary_category="その他",
                confidence="low",
                classification_reason="該当ルールなし(Tier 3)",
            )

        # 除外ルールは一般ルールより常に優先する(仕様 §13)。
        exclusions = [m for m in matches if m.rule.tier == Tier.EXCLUDED]
        pool = exclusions if exclusions else matches
        best = sorted(
            pool,
            key=lambda m: (-m.rule.priority, m.rule.tier, m.rule.rule_id),
        )[0]

        matched_rule_ids = [m.rule.rule_id for m in pool]
        matched_keywords = sorted({kw for m in pool for kw in m.keywords})
        score = 0 if best.rule.tier == Tier.EXCLUDED else max(m.rule.score for m in pool)
        if best.rule.tier == Tier.EXCLUDED or len(matched_keywords) >= 2 or len(pool) >= 2:
            confidence = "high"
        else:
            confidence = "medium"

        return ClassificationResult(
            tier=best.rule.tier,
            primary_category=best.rule.category,
            matched_rule_ids=matched_rule_ids,
            matched_keywords=matched_keywords,
            confidence=confidence,
            classification_reason=best.rule.explanation or f"rule:{best.rule.rule_id}",
            score=score,
        )
