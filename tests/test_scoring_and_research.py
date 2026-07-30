"""スコアリング・自動リサーチキュー・ダイジェストのテスト。"""

from __future__ import annotations

import datetime as dt
import subprocess
from pathlib import Path
from typing import Any

import pytest

from src.main import post_digest_if_due, process_research_queue
from src.models import ClassificationResult, ClassifiedDisclosure, Disclosure, Tier
from src.research import runner
from src.settings import JST, REPO_ROOT, Settings
from src.state.repository import StateRepository
from src.tdnet.classifier import Classifier

NOW = dt.datetime(2026, 7, 30, 12, 0, tzinfo=JST)
RULES = REPO_ROOT / "config" / "disclosure_rules.yaml"


class FakeSlack:
    def __init__(self) -> None:
        self.posts: list[dict[str, Any]] = []

    def post_parent_message(
        self,
        channel_id: str,
        text: str,
        blocks: Any = None,
        max_attempts: int = 3,
        thread_ts: str | None = None,
    ) -> str:
        self.posts.append({"channel": channel_id, "text": text, "thread_ts": thread_ts})
        return f"ts-{len(self.posts)}"


def make_state(tmp_path: Path) -> StateRepository:
    repo = StateRepository(tmp_path / "state.json", 90)
    repo.load()
    return repo


def make_settings(tmp_path: Path, **kwargs: Any) -> Settings:
    return Settings(
        slack_bot_token="xoxb-test",
        slack_channel_id="C123",
        state_path=tmp_path / "state.json",
        **kwargs,
    )


# ---- スコアリング ------------------------------------------------------------


@pytest.fixture(scope="module")
def classifier() -> Classifier:
    return Classifier.from_yaml(RULES)


def test_rule_scores(classifier: Classifier) -> None:
    tob = classifier.classify("当社株式に対する公開買付けの開始に関するお知らせ")
    assert tob.score == 95
    monthly = classifier.classify("2026年7月度 月次売上高に関するお知らせ")
    assert monthly.score == 30
    excluded = classifier.classify("自己株式の取得状況に関するお知らせ")
    assert excluded.score == 0


def make_item(score: int, tier: Tier = Tier.TIER1, **kwargs: Any) -> ClassifiedDisclosure:
    return ClassifiedDisclosure(
        disclosure=Disclosure(
            disclosure_id="140120260730500001",
            disclosed_at=NOW,
            security_code="1234",
            raw_code="12340",
            company_name="テスト社",
            title="テスト開示",
            document_url="https://www.release.tdnet.info/inbs/140120260730500001.pdf",
        ),
        classification=ClassificationResult(
            tier=tier, primary_category="テスト", score=score
        ),
        **kwargs,
    )


def test_total_score_bonuses() -> None:
    assert make_item(70).total_score == 70
    assert make_item(70, in_portfolio=True).total_score == 95
    assert make_item(70, in_watchlist=True).total_score == 85
    assert make_item(95, in_portfolio=True, in_watchlist=True).total_score == 100  # 上限


# ---- 自動リサーチキュー ------------------------------------------------------


def queue_job(state: StateRepository, disclosure_id: str, score: int = 90) -> None:
    state.add_thread_mapping(
        {
            "disclosure_id": disclosure_id,
            "channel_id": "C123",
            "parent_ts": f"ts-{disclosure_id}",
            "posted_at": NOW.isoformat(),
            "tier": 1,
            "score": score,
            "security_code": "1234",
            "company_name": "テスト社",
            "title": "テスト開示",
            "category": "テスト",
            "document_url": "https://example/x.pdf",
            "disclosed_at": "2026-07-30 12:00",
            "research_status": "queued",
            "research_attempts": 0,
        }
    )


def test_research_success_posts_to_thread(tmp_path: Path, monkeypatch: Any) -> None:
    state = make_state(tmp_path)
    queue_job(state, "a")
    slack = FakeSlack()
    monkeypatch.setattr(runner, "run_research", lambda job, settings: "分析サマリー本文")

    from src import main as main_module

    stats = process_research_queue(state, make_settings(tmp_path), slack, NOW)
    assert stats == {"started": 1, "completed": 1, "failed": 0}
    assert slack.posts[0]["thread_ts"] == "ts-a"  # スレッド返信になっている
    assert state.thread_mappings()[0]["research_status"] == "completed"
    assert state.research_count_today("2026-07-30") == 1
    del main_module


def test_research_respects_per_run_and_daily_caps(tmp_path: Path, monkeypatch: Any) -> None:
    state = make_state(tmp_path)
    for i in range(5):
        queue_job(state, f"job{i}")
    slack = FakeSlack()
    monkeypatch.setattr(runner, "run_research", lambda job, settings: "ok")

    settings = make_settings(tmp_path, max_research_jobs_per_run=2, max_auto_research_per_day=3)
    stats = process_research_queue(state, settings, slack, NOW)
    assert stats["started"] == 2  # 1回の実行では2件まで

    stats = process_research_queue(state, settings, slack, NOW)
    assert stats["started"] == 1  # 日次上限3件で打ち止め
    assert state.research_count_today("2026-07-30") == 3
    assert len(state.thread_mappings(research_status="queued")) == 2


def test_research_failure_retries_then_permanent(tmp_path: Path, monkeypatch: Any) -> None:
    state = make_state(tmp_path)
    queue_job(state, "a")
    slack = FakeSlack()

    def boom(job: Any, settings: Any) -> str:
        raise runner.ResearchError("failed")

    monkeypatch.setattr(runner, "run_research", boom)
    settings = make_settings(tmp_path)

    process_research_queue(state, settings, slack, NOW)
    assert state.thread_mappings()[0]["research_status"] == "queued"  # 1回目はリトライ待ち

    process_research_queue(state, settings, slack, NOW)
    mapping = state.thread_mappings()[0]
    assert mapping["research_status"] == "failed_permanent"
    assert any("失敗" in p["text"] for p in slack.posts)  # 失敗通知がスレッドに投稿される


# ---- 夕方ダイジェスト --------------------------------------------------------


def test_digest_posts_once_after_cutoff(tmp_path: Path) -> None:
    state = make_state(tmp_path)
    state.add_to_digest(
        {
            "security_code": "1234",
            "company_name": "テスト社",
            "title": "株主優待制度の変更",
            "tier": 2,
            "score": 40,
            "document_url": "https://example/x.pdf",
        }
    )
    slack = FakeSlack()
    settings = make_settings(tmp_path)

    noon = NOW  # 12:00 → まだ投稿しない
    assert post_digest_if_due(state, settings, slack, noon) is False

    evening = NOW.replace(hour=19, minute=50)
    assert post_digest_if_due(state, settings, slack, evening) is True
    assert "ダイジェスト" in slack.posts[0]["text"]
    assert state.pending_digest == []

    # 同日2回目は投稿しない
    assert post_digest_if_due(state, settings, slack, evening) is False


def test_digest_skipped_when_empty(tmp_path: Path) -> None:
    state = make_state(tmp_path)
    slack = FakeSlack()
    evening = NOW.replace(hour=19, minute=50)
    assert post_digest_if_due(state, make_settings(tmp_path), slack, evening) is False
    assert slack.posts == []


# ---- runner(subprocessモック) ----------------------------------------------


def test_runner_builds_prompt_and_parses_output(tmp_path: Path, monkeypatch: Any) -> None:
    captured: dict[str, Any] = {}

    def fake_run(command: Any, **kwargs: Any) -> Any:
        captured["command"] = command
        captured["env"] = kwargs.get("env", {})

        class R:
            returncode = 0
            stdout = "サマリー"
            stderr = ""

        return R()

    monkeypatch.setattr(subprocess, "run", fake_run)
    settings = Settings(
        claude_code_oauth_token="tok", edinet_db_api_key="edb", jquants_api_key="jq"
    )
    job = {
        "security_code": "1234",
        "company_name": "テスト社",
        "title": "業績予想の修正",
        "category": "業績予想修正",
        "document_url": "https://example/x.pdf",
        "disclosed_at": "2026-07-30 12:00",
    }
    assert runner.run_research(job, settings) == "サマリー"
    prompt = captured["command"][2]
    assert "テスト社" in prompt
    assert "https://example/x.pdf" in prompt
    assert captured["env"]["CLAUDE_CODE_OAUTH_TOKEN"] == "tok"
    assert captured["env"]["EDINET_DB_API_KEY"] == "edb"


def test_runner_raises_on_empty_output(monkeypatch: Any) -> None:
    def fake_run(command: Any, **kwargs: Any) -> Any:
        class R:
            returncode = 0
            stdout = ""
            stderr = ""

        return R()

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(runner.ResearchError):
        runner.run_research({"title": "x"}, Settings())


def test_research_skipped_when_cli_missing(tmp_path: Path) -> None:
    """claude CLI未導入の実行ではqueuedのまま持ち越す(リトライを消費しない)。"""
    state = make_state(tmp_path)
    queue_job(state, "a")
    slack = FakeSlack()
    settings = make_settings(tmp_path, claude_cli="definitely-missing-cli-xyz")
    stats = process_research_queue(state, settings, slack, NOW)
    assert stats == {"started": 0, "completed": 0, "failed": 0}
    mapping = state.thread_mappings()[0]
    assert mapping["research_status"] == "queued"
    assert mapping["research_attempts"] == 0
