"""run_monitor の安全ガード(Slack未設定時は何もしない)のテスト。"""

from __future__ import annotations

from pathlib import Path

from src.main import run_monitor
from src.settings import Settings


def test_run_monitor_without_slack_config_exits_cleanly(tmp_path: Path) -> None:
    """トークン未設定の本番実行は、ネットワークにもstateにも触れず正常終了する。"""
    state_path = tmp_path / "state.json"
    settings = Settings(slack_bot_token="", slack_channel_id="", state_path=state_path)
    assert run_monitor(settings, dry_run=False, tdnet_only=False) == 0
    assert not state_path.exists()  # state変更なし
