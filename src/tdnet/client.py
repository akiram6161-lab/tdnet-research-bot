"""TDnet 公式リストの取得クライアント。

TDnet に公開 API は存在しないため、公式開示リストページを取得する
(既存 japan-equities-v2 パイプラインで動作実績のある方式)。
"""

from __future__ import annotations

import datetime as dt
import logging
import math
import time
from dataclasses import dataclass, field

import requests

from src.models import (
    Disclosure,
    fallback_disclosure_id,
    looks_english,
    normalize_security_code,
)
from src.settings import JST, now_jst
from src.tdnet.parser import TDNET_LIST_BASE, RawDisclosureRow, parse_list_page

logger = logging.getLogger(__name__)

USER_AGENT = "tdnet-research-bot/0.1 (personal disclosure monitor)"
PAGE_SIZE = 100
MAX_PAGES = 100
RETRY_STATUSES = {429, 500, 502, 503, 504}


@dataclass(frozen=True)
class DailyListResult:
    run_date: dt.date
    total_declared: int | None
    pages_fetched: list[int]
    rows: list[RawDisclosureRow]
    errors: list[str] = field(default_factory=list)

    @property
    def complete(self) -> bool:
        return not self.errors


class TDnetClient:
    def __init__(self, session: requests.Session | None = None, timeout: float = 40.0) -> None:
        self._session = session or requests.Session()
        self._session.headers.update({"User-Agent": USER_AGENT})
        self._timeout = timeout

    def _get(self, url: str, attempts: int = 3) -> str:
        last_error: Exception | None = None
        for attempt in range(attempts):
            try:
                response = self._session.get(url, timeout=self._timeout)
                response.raise_for_status()
                return response.content.decode("utf-8", errors="replace")
            except requests.HTTPError as exc:
                last_error = exc
                status = exc.response.status_code if exc.response is not None else None
                if status not in RETRY_STATUSES:
                    break
            except requests.RequestException as exc:
                last_error = exc
            if attempt + 1 < attempts:
                time.sleep(2**attempt)
        raise RuntimeError(f"GET failed for {url}: {last_error}") from last_error

    def fetch_daily_list(self, run_date: dt.date) -> DailyListResult:
        """指定日の全リストページを取得し、宣言件数と突合する。"""
        date_token = run_date.strftime("%Y%m%d")
        errors: list[str] = []
        pages_fetched: list[int] = []
        rows: list[RawDisclosureRow] = []
        total_declared: int | None = None
        expected_pages = 1

        for page in range(1, MAX_PAGES + 1):
            if page > expected_pages:
                break
            url = f"{TDNET_LIST_BASE}/I_list_{page:03d}_{date_token}.html"
            try:
                document = self._get(url)
                parsed = parse_list_page(document, run_date)
            except RuntimeError as exc:
                # 開示ゼロの日や休日は 404 になる。1ページ目の404は「開示なし」。
                if page == 1 and "404" in str(exc):
                    return DailyListResult(run_date, 0, [], [])
                errors.append(f"TDnet page {page} fetch/parse failed: {exc}")
                break

            pages_fetched.append(page)
            rows.extend(parsed.rows)
            if page == 1:
                total_declared = parsed.total_declared
                if total_declared is None:
                    errors.append("TDnet first page did not declare the daily total.")
                    break
                expected_pages = max(
                    1,
                    math.ceil(total_declared / PAGE_SIZE),
                    max(parsed.page_numbers, default=1),
                )

        if total_declared is not None and len(rows) != total_declared:
            errors.append(
                f"TDnet declared {total_declared} disclosures but {len(rows)} were parsed."
            )
        return DailyListResult(run_date, total_declared, pages_fetched, rows, errors)

    def fetch_window(self, since: dt.datetime, until: dt.datetime | None = None) -> DailyListResult:
        """since 以降の開示を取得する(lookback 用)。日をまたぐ場合は複数日分を取得。"""
        until = until or now_jst()
        since = since.astimezone(JST)
        until = until.astimezone(JST)

        all_rows: list[RawDisclosureRow] = []
        errors: list[str] = []
        pages: list[int] = []
        total = 0
        day = since.date()
        while day <= until.date():
            result = self.fetch_daily_list(day)
            errors.extend(result.errors)
            pages.extend(result.pages_fetched)
            total += result.total_declared or 0
            all_rows.extend(result.rows)
            day += dt.timedelta(days=1)

        filtered = [row for row in all_rows if since <= row.disclosed_at <= until]
        return DailyListResult(until.date(), total, pages, filtered, errors)


def row_to_disclosure(row: RawDisclosureRow, retrieved_at: dt.datetime | None = None) -> Disclosure:
    """リスト行を Disclosure モデルへ変換する。"""
    disclosure_id = row.source_id or fallback_disclosure_id(
        normalize_security_code(row.code), row.disclosed_at, row.title, row.pdf_url
    )
    return Disclosure(
        disclosure_id=disclosure_id,
        disclosed_at=row.disclosed_at,
        security_code=normalize_security_code(row.code),
        raw_code=row.code,
        company_name=row.issuer,
        title=row.title,
        document_url=row.pdf_url,
        document_type="pdf",
        exchange=None,
        language="en" if looks_english(row.title) else "ja",
        retrieved_at=retrieved_at or now_jst(),
    )
