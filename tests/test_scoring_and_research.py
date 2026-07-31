"""スコアリング・自動リサーチキュー・ダイジェストのテスト。"""

from __future__ import annotations

import datetime as dt
import subprocess
from pathlib import Path
from typing import Any

import pytest

from src.main import is_notify_target, process_research_queue
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
    assert monthly.score == 35
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


def test_stale_queued_jobs_expire(tmp_path: Path, monkeypatch: Any) -> None:
    """72時間を超えてqueuedのままのジョブは失効し、実行されない。"""
    state = make_state(tmp_path)
    queue_job(state, "old")
    state.thread_mappings()[0]["posted_at"] = (NOW - dt.timedelta(hours=80)).isoformat()
    queue_job(state, "fresh")
    slack = FakeSlack()
    monkeypatch.setattr(runner, "run_research", lambda job, settings: "ok")

    stats = process_research_queue(state, make_settings(tmp_path), slack, NOW)
    assert stats["started"] == 1  # freshのみ実行
    statuses = {m["disclosure_id"]: m["research_status"] for m in state.thread_mappings()}
    assert statuses["old"] == "expired"
    assert statuses["fresh"] == "completed"


def test_activist_bonus_lowers_effective_bar() -> None:
    """アクティビスト銘柄は+15点で資本政策イベント(基礎65点以上)が通知対象になる。"""
    # 自己株買い(75)・配当修正(72)・特別損益(68)は+15で閾値80を超える
    assert is_notify_target(make_item(75, in_activist=True), 80)
    assert is_notify_target(make_item(72, in_activist=True), 80)
    assert is_notify_target(make_item(68, in_activist=True), 80)
    # 中計(55)・月次(35)・Tier3(決算短信)は落ちる
    assert not is_notify_target(make_item(55, tier=Tier.TIER2, in_activist=True), 80)
    assert not is_notify_target(make_item(35, tier=Tier.TIER2, in_activist=True), 80)
    assert not is_notify_target(make_item(0, tier=Tier.TIER3, in_activist=True), 80)
    # 非アクティビストは従来どおり
    assert not is_notify_target(make_item(75), 80)
    assert is_notify_target(make_item(85), 80)
    assert make_item(75, in_activist=True).total_score == 90


# ---- デイリーハイライト ------------------------------------------------------


def test_highlight_gating(tmp_path: Path) -> None:
    """当日はhighlight_after以降のみ、取り逃しは翌日リカバリ、二重投稿なし。"""
    from src.main import determine_highlight_date

    state = make_state(tmp_path)
    settings = make_settings(tmp_path)
    queue_job(state, "a")  # posted_at = 2026-07-30 12:00

    noon = NOW  # 7/30 12:00 → 当日はまだ出さない(前日以前に対象なし)
    assert determine_highlight_date(state, settings, noon) is None

    evening = NOW.replace(hour=20, minute=0)  # 7/30 20:00 → 当日分が対象
    assert determine_highlight_date(state, settings, evening) == dt.date(2026, 7, 30)

    # 当日中に出せなかった場合、翌朝の実行でリカバリされる
    next_morning = NOW.replace(day=31, hour=8, minute=0)
    assert determine_highlight_date(state, settings, next_morning) == dt.date(2026, 7, 30)

    # 投稿済みなら二度と対象にならない
    state.set_last_highlight_date("2026-07-30")
    assert determine_highlight_date(state, settings, evening) is None
    assert determine_highlight_date(state, settings, next_morning) is None


def test_highlight_posts_summary_and_deepdive(tmp_path: Path, monkeypatch: Any) -> None:
    from src import main as main_module
    from src.main import post_daily_highlight_if_due

    state = make_state(tmp_path)
    queue_job(state, "a")
    slack = FakeSlack()
    monkeypatch.setattr(
        main_module, "determine_highlight_date", lambda s, st, n: dt.date(2026, 7, 30)
    )
    monkeypatch.setattr(runner, "run_daily_highlight", lambda i, d, s: ("ハイライト", "深掘り"))
    monkeypatch.setattr("shutil.which", lambda cli: "/usr/bin/claude")

    assert post_daily_highlight_if_due(state, make_settings(tmp_path), slack, NOW) is True
    assert slack.posts[0]["text"] == "ハイライト"
    assert slack.posts[1]["text"] == "深掘り"
    assert slack.posts[1]["thread_ts"] == "ts-1"  # 深掘りはハイライトのスレッドに付く
    assert state.last_highlight_date == "2026-07-30"


def test_fresh_jobs_processed_before_stale_high_score(tmp_path: Path, monkeypatch: Any) -> None:
    """当日分(低スコア)が前日の高スコアより先に処理される。"""
    state = make_state(tmp_path)
    queue_job(state, "old_high", score=95)
    state.thread_mappings()[0]["posted_at"] = (NOW - dt.timedelta(days=1)).isoformat()
    queue_job(state, "fresh_low", score=82)
    order: list[str] = []

    def record(job: Any, settings: Any) -> str:
        order.append(job["disclosure_id"])
        return "ok"

    monkeypatch.setattr(runner, "run_research", record)
    settings = make_settings(tmp_path, max_research_jobs_per_run=2)
    process_research_queue(state, settings, FakeSlack(), NOW)
    assert order == ["fresh_low", "old_high"]
