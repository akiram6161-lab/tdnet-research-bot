"""state管理(atomic write・変更検知・retention・後方互換)のテスト。"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

from src.settings import JST
from src.state.repository import StateRepository

NOW = dt.datetime(2026, 7, 30, 12, 0, tzinfo=JST)


def make_repo(tmp_path: Path, retention_days: int = 90) -> StateRepository:
    repo = StateRepository(tmp_path / "state.json", retention_days)
    repo.load()
    return repo


def test_save_only_when_dirty(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    assert repo.save() is False  # 変更なし → 書き込まない
    repo.mark_processed(
        "id1", security_code="1234", disclosed_at=NOW, tier=1, language="ja", posted=True
    )
    assert repo.dirty
    assert repo.save() is True
    assert repo.save() is False  # 保存後は再度cleanになる


def test_roundtrip_and_duplicate_prevention(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    repo.mark_processed(
        "id1", security_code="1234", disclosed_at=NOW, tier=1, language="ja", posted=True
    )
    repo.save()

    repo2 = make_repo(tmp_path)
    assert repo2.is_processed("id1")
    assert not repo2.is_processed("id2")
    assert repo2.processed_ja_exists("1234", NOW)
    assert not repo2.processed_ja_exists("9999", NOW)


def test_unknown_keys_preserved(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    path.write_text(
        json.dumps({"future_field": {"a": 1}, "processed_disclosures": {}}),
        encoding="utf-8",
    )
    repo = StateRepository(path, 90)
    repo.load()
    repo.mark_processed(
        "id1", security_code="1234", disclosed_at=NOW, tier=2, language="ja", posted=True
    )
    repo.save()
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["future_field"] == {"a": 1}  # 未知キーは保持される
    assert "id1" in data["processed_disclosures"]


def test_retention_prune(tmp_path: Path) -> None:
    repo = make_repo(tmp_path, retention_days=90)
    old = NOW - dt.timedelta(days=120)
    repo.mark_processed(
        "old", security_code="1111", disclosed_at=old, tier=1, language="ja", posted=True
    )
    # first_seen を古い日時に書き換えて期限切れを再現
    repo._state["processed_disclosures"]["old"]["first_seen"] = old.isoformat()
    repo.mark_processed(
        "new", security_code="2222", disclosed_at=NOW, tier=1, language="ja", posted=True
    )
    repo.add_thread_mapping({"disclosure_id": "old", "posted_at": old.isoformat()})
    repo.add_thread_mapping({"disclosure_id": "new", "posted_at": NOW.isoformat()})

    removed = repo.prune(NOW)
    assert removed == 2
    assert not repo.is_processed("old")
    assert repo.is_processed("new")


def test_atomic_write_leaves_no_tmp_files(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    repo.mark_processed(
        "id1", security_code="1234", disclosed_at=NOW, tier=1, language="ja", posted=True
    )
    repo.save()
    leftovers = [p for p in tmp_path.iterdir() if p.name != "state.json"]
    assert leftovers == []
    assert json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
