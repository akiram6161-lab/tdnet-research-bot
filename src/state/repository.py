"""state/state.json の読み書き(atomic write・90日保持・変更検知)。"""

from __future__ import annotations

import copy
import datetime as dt
import json
import os
import tempfile
from pathlib import Path
from typing import Any

from src.settings import JST

SCHEMA_VERSION = 1

_EMPTY_STATE: dict[str, Any] = {
    "schema_version": SCHEMA_VERSION,
    "last_successful_tdnet_check": None,
    "processed_disclosures": {},
    "disclosure_thread_mappings": [],
    "processed_research_reply_ts": [],
    "last_slack_poll_at": None,
    "failed_items": [],
    "pending_digest": [],
    "digest_last_posted_date": None,
    "research_daily": {"date": None, "count": 0},
}


class StateRepository:
    """state.json を管理する。未知キーは保持し後方互換性を維持する。"""

    def __init__(self, path: Path, retention_days: int = 90) -> None:
        self._path = path
        self._retention_days = retention_days
        self._state: dict[str, Any] = copy.deepcopy(_EMPTY_STATE)
        self._loaded_snapshot: str = ""

    def load(self) -> None:
        if self._path.exists():
            raw = self._path.read_text(encoding="utf-8").strip()
            if raw:
                data = json.loads(raw)
                merged = copy.deepcopy(_EMPTY_STATE)
                merged.update(data)  # 既存の未知キーもそのまま保持
                merged["schema_version"] = SCHEMA_VERSION
                self._state = merged
        self._loaded_snapshot = self._serialize()

    def _serialize(self) -> str:
        return json.dumps(self._state, ensure_ascii=False, sort_keys=True, indent=2)

    @property
    def dirty(self) -> bool:
        return self._serialize() != self._loaded_snapshot

    def save(self) -> bool:
        """変更がある場合のみ atomic write する。書き込んだら True。"""
        if not self.dirty:
            return False
        self._path.parent.mkdir(parents=True, exist_ok=True)
        serialized = self._serialize() + "\n"
        fd, tmp_path = tempfile.mkstemp(
            dir=self._path.parent, prefix=".state-", suffix=".tmp"
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(serialized)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp_path, self._path)
        finally:
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)
        self._loaded_snapshot = serialized.rstrip("\n")
        # 再シリアライズとの比較のため snapshot を正規化
        self._loaded_snapshot = self._serialize()
        return True

    # ---- processed disclosures -------------------------------------------

    def is_processed(self, disclosure_id: str) -> bool:
        return disclosure_id in self._state["processed_disclosures"]

    def mark_processed(
        self,
        disclosure_id: str,
        *,
        security_code: str,
        disclosed_at: dt.datetime,
        tier: int,
        language: str,
        posted: bool,
    ) -> None:
        self._state["processed_disclosures"][disclosure_id] = {
            "security_code": security_code,
            "disclosed_at": disclosed_at.isoformat(),
            "tier": tier,
            "language": language,
            "posted": posted,
            "first_seen": dt.datetime.now(tz=JST).isoformat(),
        }

    def processed_ja_exists(self, security_code: str, disclosed_at: dt.datetime) -> bool:
        """同一コード・同一時刻の日本語版が処理済みかどうか(英語版の重複排除用)。"""
        iso = disclosed_at.isoformat()
        for entry in self._state["processed_disclosures"].values():
            if (
                entry.get("security_code") == security_code
                and entry.get("disclosed_at") == iso
                and entry.get("language") == "ja"
            ):
                return True
        return False

    # ---- run bookkeeping ---------------------------------------------------

    @property
    def last_successful_tdnet_check(self) -> dt.datetime | None:
        value = self._state.get("last_successful_tdnet_check")
        return dt.datetime.fromisoformat(value) if value else None

    def set_last_successful_tdnet_check(self, moment: dt.datetime) -> None:
        self._state["last_successful_tdnet_check"] = moment.isoformat()

    def add_thread_mapping(self, mapping: dict[str, Any]) -> None:
        self._state["disclosure_thread_mappings"].append(mapping)

    def thread_mappings(self, research_status: str | None = None) -> list[dict[str, Any]]:
        mappings: list[dict[str, Any]] = self._state["disclosure_thread_mappings"]
        if research_status is None:
            return mappings
        return [m for m in mappings if m.get("research_status") == research_status]

    # ---- ダイジェスト(閾値未満のTier 1/2) --------------------------------

    def add_to_digest(self, entry: dict[str, Any]) -> None:
        self._state["pending_digest"].append(entry)

    @property
    def pending_digest(self) -> list[dict[str, Any]]:
        return list(self._state["pending_digest"])

    def digest_posted_today(self, today: str) -> bool:
        return bool(self._state.get("digest_last_posted_date") == today)

    def mark_digest_posted(self, today: str) -> None:
        self._state["digest_last_posted_date"] = today
        self._state["pending_digest"] = []

    # ---- 自動リサーチの日次カウンタ ---------------------------------------

    def research_count_today(self, today: str) -> int:
        daily = self._state.get("research_daily") or {}
        if daily.get("date") != today:
            return 0
        return int(daily.get("count", 0))

    def increment_research_count(self, today: str) -> None:
        count = self.research_count_today(today)
        self._state["research_daily"] = {"date": today, "count": count + 1}

    # ---- retention -----------------------------------------------------------

    def prune(self, now: dt.datetime | None = None) -> int:
        """保持期間(既定90日)を超えたエントリを削除する。削除件数を返す。"""
        now = now or dt.datetime.now(tz=JST)
        cutoff = now - dt.timedelta(days=self._retention_days)
        removed = 0

        processed: dict[str, Any] = self._state["processed_disclosures"]
        for key in list(processed):
            first_seen = processed[key].get("first_seen") or processed[key].get("disclosed_at")
            try:
                seen_at = dt.datetime.fromisoformat(first_seen)
            except (TypeError, ValueError):
                continue
            if seen_at < cutoff:
                del processed[key]
                removed += 1

        mappings = self._state["disclosure_thread_mappings"]
        kept = []
        for item in mappings:
            posted_at = item.get("posted_at")
            try:
                if posted_at and dt.datetime.fromisoformat(posted_at) < cutoff:
                    removed += 1
                    continue
            except ValueError:
                pass
            kept.append(item)
        self._state["disclosure_thread_mappings"] = kept
        return removed
