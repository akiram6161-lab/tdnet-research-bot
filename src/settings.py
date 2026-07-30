"""環境変数と設定ファイルの読み込み。"""

from __future__ import annotations

import csv
import datetime as dt
import os
from dataclasses import dataclass, field
from pathlib import Path
from zoneinfo import ZoneInfo

JST = ZoneInfo("Asia/Tokyo")

REPO_ROOT = Path(__file__).resolve().parent.parent


def _parse_hhmm(value: str, fallback: str) -> dt.time:
    try:
        return dt.time.fromisoformat(value)
    except ValueError:
        return dt.time.fromisoformat(fallback)


@dataclass(frozen=True)
class Settings:
    """実行時設定。GitHub Actions では Secrets 経由の環境変数で与える。"""

    tdnet_api_key: str = ""
    tdnet_api_base_url: str = ""
    anthropic_api_key: str = ""
    anthropic_model: str = ""
    edinet_db_api_key: str = ""
    edinet_db_base_url: str = "https://edinetdb.jp/v1"
    jquants_api_key: str = ""
    slack_bot_token: str = ""
    slack_user_token: str = ""
    slack_channel_id: str = ""
    slack_allowed_user_ids: list[str] = field(default_factory=list)
    monitor_start: dt.time = dt.time(7, 45)
    monitor_end: dt.time = dt.time(20, 0)
    lookback_minutes: int = 30
    thread_polling_days: int = 14
    max_research_jobs_per_run: int = 2
    state_retention_days: int = 90
    research_score_threshold: int = 80
    max_auto_research_per_day: int = 15
    digest_after: dt.time = dt.time(19, 45)
    claude_cli: str = "claude"
    claude_code_oauth_token: str = ""
    log_level: str = "INFO"
    state_path: Path = REPO_ROOT / "state" / "state.json"
    rules_path: Path = REPO_ROOT / "config" / "disclosure_rules.yaml"
    portfolio_path: Path = REPO_ROOT / "config" / "portfolio.csv"
    watchlist_path: Path = REPO_ROOT / "config" / "watchlist.csv"

    @classmethod
    def from_env(cls) -> Settings:
        env = os.environ
        allowed = [
            user_id.strip()
            for user_id in env.get("SLACK_ALLOWED_USER_IDS", "").split(",")
            if user_id.strip()
        ]
        return cls(
            tdnet_api_key=env.get("TDNET_API_KEY", ""),
            tdnet_api_base_url=env.get("TDNET_API_BASE_URL", ""),
            anthropic_api_key=env.get("ANTHROPIC_API_KEY", ""),
            anthropic_model=env.get("ANTHROPIC_MODEL", ""),
            edinet_db_api_key=env.get("EDINET_DB_API_KEY", env.get("EDINETDB_API_KEY", "")),
            edinet_db_base_url=env.get("EDINET_DB_BASE_URL", "https://edinetdb.jp/v1"),
            jquants_api_key=env.get("JQUANTS_API_KEY", ""),
            slack_bot_token=env.get("SLACK_BOT_TOKEN", ""),
            slack_user_token=env.get("SLACK_USER_TOKEN", ""),
            slack_channel_id=env.get("SLACK_CHANNEL_ID", ""),
            slack_allowed_user_ids=allowed,
            monitor_start=_parse_hhmm(env.get("MONITOR_START", "07:45"), "07:45"),
            monitor_end=_parse_hhmm(env.get("MONITOR_END", "20:00"), "20:00"),
            lookback_minutes=int(env.get("LOOKBACK_MINUTES", "30")),
            thread_polling_days=int(env.get("THREAD_POLLING_DAYS", "14")),
            max_research_jobs_per_run=int(env.get("MAX_RESEARCH_JOBS_PER_RUN", "2")),
            state_retention_days=int(env.get("STATE_RETENTION_DAYS", "90")),
            research_score_threshold=int(env.get("RESEARCH_SCORE_THRESHOLD", "80")),
            max_auto_research_per_day=int(env.get("MAX_AUTO_RESEARCH_PER_DAY", "15")),
            digest_after=_parse_hhmm(env.get("DIGEST_AFTER", "19:45"), "19:45"),
            claude_cli=env.get("CLAUDE_CLI", "claude"),
            claude_code_oauth_token=env.get("CLAUDE_CODE_OAUTH_TOKEN", ""),
            log_level=env.get("LOG_LEVEL", "INFO"),
        )


def now_jst() -> dt.datetime:
    return dt.datetime.now(tz=JST)


def within_monitoring_window(moment: dt.datetime, settings: Settings) -> bool:
    """平日 07:45〜20:00 JST の監視時間内かを判定する。"""
    local = moment.astimezone(JST)
    if local.weekday() >= 5:  # 土日
        return False
    return settings.monitor_start <= local.time() <= settings.monitor_end


@dataclass(frozen=True)
class WatchEntry:
    security_code: str
    company_name: str
    label: str | None = None


def _load_csv_entries(path: Path, label_column: str | None) -> dict[str, WatchEntry]:
    """portfolio.csv / watchlist.csv を読み込む。active=false の行は無視する。"""
    from src.models import normalize_security_code

    entries: dict[str, WatchEntry] = {}
    if not path.exists():
        return entries
    with path.open(encoding="utf-8", newline="") as fh:
        for row in csv.DictReader(fh):
            code_raw = (row.get("security_code") or "").strip()
            if not code_raw:
                continue
            active = (row.get("active") or "true").strip().lower()
            if active in {"false", "0", "no"}:
                continue
            code = normalize_security_code(code_raw)
            entries[code] = WatchEntry(
                security_code=code,
                company_name=(row.get("company_name") or "").strip(),
                label=(row.get(label_column) or "").strip() if label_column else None,
            )
    return entries


def load_portfolio(settings: Settings) -> dict[str, WatchEntry]:
    return _load_csv_entries(settings.portfolio_path, label_column=None)


def load_watchlist(settings: Settings) -> dict[str, WatchEntry]:
    return _load_csv_entries(settings.watchlist_path, label_column="label")
