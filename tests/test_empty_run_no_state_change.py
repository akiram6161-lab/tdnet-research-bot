"""空実行(新規開示なし)でstateが変更されないことのテスト(仕様 §28)。"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

from src.main import run_monitor
from src.settings import JST, Settings
from src.tdnet.client import DailyListResult, TDnetClient


class EmptyClient(TDnetClient):
    """常に0件を返すフェイク(ネットワークアクセスなし)。"""

    def fetch_daily_list(self, run_date: dt.date) -> DailyListResult:
        return DailyListResult(run_date, 0, [1], [])


def make_settings(tmp_path: Path) -> Settings:
    return Settings(
        slack_bot_token="xoxb-test",
        slack_channel_id="C123",
        state_path=tmp_path / "state.json",
        monitor_start=dt.time(0, 0),
        monitor_end=dt.time(23, 59),
    )


def test_empty_run_does_not_touch_recent_state(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    recent = (dt.datetime.now(tz=JST) - dt.timedelta(minutes=15)).isoformat()
    original = json.dumps(
        {"schema_version": 1, "last_successful_tdnet_check": recent},
        ensure_ascii=False,
    )
    settings.state_path.write_text(original, encoding="utf-8")

    assert run_monitor(settings, dry_run=False, tdnet_only=True, tdnet_client=EmptyClient()) == 0
    # 新規開示ゼロ・最終成功時刻が新しい → stateファイルは書き換えられない
    assert settings.state_path.read_text(encoding="utf-8") == original


def test_empty_run_refreshes_stale_last_check(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    stale = (dt.datetime.now(tz=JST) - dt.timedelta(hours=13)).isoformat()
    settings.state_path.write_text(
        json.dumps({"schema_version": 1, "last_successful_tdnet_check": stale}),
        encoding="utf-8",
    )

    assert run_monitor(settings, dry_run=False, tdnet_only=True, tdnet_client=EmptyClient()) == 0
    data = json.loads(settings.state_path.read_text(encoding="utf-8"))
    assert data["last_successful_tdnet_check"] != stale  # 12時間超は更新される
