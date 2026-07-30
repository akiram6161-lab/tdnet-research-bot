"""JST監視時間判定とlookbackフィルタのテスト。"""

from __future__ import annotations

import datetime as dt

from src.settings import JST, Settings, within_monitoring_window
from src.tdnet.client import DailyListResult, TDnetClient
from src.tdnet.parser import RawDisclosureRow

SETTINGS = Settings()


def at(year: int, month: int, day: int, hour: int, minute: int) -> dt.datetime:
    return dt.datetime(year, month, day, hour, minute, tzinfo=JST)


def test_weekday_within_window() -> None:
    assert within_monitoring_window(at(2026, 7, 30, 7, 45), SETTINGS)  # 木 開始境界
    assert within_monitoring_window(at(2026, 7, 30, 12, 0), SETTINGS)
    assert within_monitoring_window(at(2026, 7, 30, 20, 0), SETTINGS)  # 終了境界


def test_outside_hours() -> None:
    assert not within_monitoring_window(at(2026, 7, 30, 7, 44), SETTINGS)
    assert not within_monitoring_window(at(2026, 7, 30, 20, 1), SETTINGS)
    assert not within_monitoring_window(at(2026, 7, 30, 23, 30), SETTINGS)


def test_weekend_is_excluded() -> None:
    assert not within_monitoring_window(at(2026, 8, 1, 10, 0), SETTINGS)  # 土
    assert not within_monitoring_window(at(2026, 8, 2, 10, 0), SETTINGS)  # 日


def make_row(hour: int, minute: int, source_id: str) -> RawDisclosureRow:
    disclosed = at(2026, 7, 30, hour, minute)
    return RawDisclosureRow(
        time_jst=f"{hour:02d}:{minute:02d}",
        code="12340",
        issuer="テスト社",
        title="業績予想の修正に関するお知らせ",
        pdf_href=f"{source_id}.pdf",
        source_id=source_id,
        pdf_url=f"https://www.release.tdnet.info/inbs/{source_id}.pdf",
        disclosed_at=disclosed,
    )


class FakeClient(TDnetClient):
    """fetch_daily_list をモックし、実際のHTTPアクセスを行わない。"""

    def __init__(self, rows: list[RawDisclosureRow]) -> None:
        super().__init__()
        self._rows = rows
        self.fetched_dates: list[dt.date] = []

    def fetch_daily_list(self, run_date: dt.date) -> DailyListResult:
        self.fetched_dates.append(run_date)
        rows = [r for r in self._rows if r.disclosed_at.date() == run_date]
        return DailyListResult(run_date, len(rows), [1], rows)


def test_fetch_window_filters_by_lookback() -> None:
    rows = [
        make_row(14, 0, "140120260730000001"),
        make_row(14, 40, "140120260730000002"),
        make_row(15, 0, "140120260730000003"),
    ]
    client = FakeClient(rows)
    since = at(2026, 7, 30, 14, 30)  # 30分lookback想定
    until = at(2026, 7, 30, 15, 0)
    result = client.fetch_window(since, until)
    ids = [r.source_id for r in result.rows]
    assert ids == ["140120260730000002", "140120260730000003"]


def test_fetch_window_spans_multiple_days() -> None:
    client = FakeClient([])
    since = at(2026, 7, 29, 19, 50)
    until = at(2026, 7, 30, 8, 0)
    client.fetch_window(since, until)
    assert client.fetched_dates == [dt.date(2026, 7, 29), dt.date(2026, 7, 30)]
