"""TDnet 公式日次リストページ(I_list_NNN_YYYYMMDD.html)のパーサー。

既存実績のある japan-equities-v2 の TDnetListParser を移植。
安定した semantic class (kjTime / kjCode / kjName / kjTitle) を利用する。
"""

from __future__ import annotations

import datetime as dt
import re
import urllib.parse
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import ClassVar

from src.settings import JST

TDNET_LIST_BASE = "https://www.release.tdnet.info/inbs"
TDNET_SOURCE_ID_RE = re.compile(r"(?<!\d)(\d{18})(?!\d)")

_OFFICIAL_HOSTS = {"www.release.tdnet.info", "release.tdnet.info", "www2.jpx.co.jp"}


@dataclass(frozen=True)
class RawDisclosureRow:
    """リストページ 1 行分の生データ。"""

    time_jst: str
    code: str
    issuer: str
    title: str
    pdf_href: str
    source_id: str
    pdf_url: str
    disclosed_at: dt.datetime


@dataclass(frozen=True)
class ParsedListPage:
    total_declared: int | None
    page_numbers: list[int]
    rows: list[RawDisclosureRow]


def extract_source_id(value: str) -> str | None:
    """PDF href 等から 18 桁の安定開示IDを抽出する。"""
    found = TDNET_SOURCE_ID_RE.findall(value)
    return found[0] if found else None


def resolve_pdf_url(pdf_href: str) -> tuple[str, str] | None:
    """href から (source_id, 公式PDF URL) を解決する。非公式ホストは拒否。"""
    source_id = extract_source_id(pdf_href)
    if not source_id:
        return None
    parsed = urllib.parse.urlparse(pdf_href.strip())
    if parsed.scheme and parsed.netloc:
        host = parsed.netloc.lower().split(":", 1)[0]
        if host not in _OFFICIAL_HOSTS:
            return None
    return source_id, f"{TDNET_LIST_BASE}/{source_id}.pdf"


class _ListHTMLParser(HTMLParser):
    FIELD_CLASSES: ClassVar[dict[str, str]] = {
        "kjTime": "time_jst",
        "kjCode": "code",
        "kjName": "issuer",
        "kjTitle": "title",
    }

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: list[dict[str, str]] = []
        self._in_main_table = False
        self._table_depth = 0
        self._row: dict[str, str] | None = None
        self._field: str | None = None
        self._field_text: list[str] = []

    @staticmethod
    def _attrs(attrs: list[tuple[str, str | None]]) -> dict[str, str]:
        return {key.lower(): value or "" for key, value in attrs}

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = self._attrs(attrs)
        if tag == "table" and attributes.get("id") == "main-list-table":
            self._in_main_table = True
            self._table_depth = 1
            return
        if self._in_main_table and tag == "table":
            self._table_depth += 1
        if not self._in_main_table:
            return
        if tag == "tr":
            self._row = {}
        elif tag == "td" and self._row is not None:
            classes = set(attributes.get("class", "").split())
            self._field = next(
                (
                    field
                    for css_class, field in self.FIELD_CLASSES.items()
                    if css_class in classes
                ),
                None,
            )
            self._field_text = []
        elif tag == "a" and self._row is not None and self._field == "title":
            href = attributes.get("href", "").strip()
            if href.lower().endswith(".pdf"):
                self._row["pdf_href"] = href

    def handle_data(self, data: str) -> None:
        if self._in_main_table and self._row is not None and self._field:
            self._field_text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if not self._in_main_table:
            return
        if tag == "td" and self._row is not None and self._field:
            self._row[self._field] = " ".join("".join(self._field_text).split())
            self._field = None
            self._field_text = []
        elif tag == "tr" and self._row is not None:
            required = ("time_jst", "code", "issuer", "title", "pdf_href")
            if all(self._row.get(key) for key in required):
                self.rows.append(self._row)
            self._row = None
        elif tag == "table":
            self._table_depth -= 1
            if self._table_depth <= 0:
                self._in_main_table = False


def parse_list_page(document: str, run_date: dt.date) -> ParsedListPage:
    """リストページ 1 枚をパースする。"""
    parser = _ListHTMLParser()
    parser.feed(document)

    page_names = sorted(
        set(re.findall(rf"I_list_(\d{{3}})_{run_date.strftime('%Y%m%d')}\.html", document))
    )
    total_match = re.search(r"全\s*([0-9,]+)\s*件", document)
    total = int(total_match.group(1).replace(",", "")) if total_match else None

    rows: list[RawDisclosureRow] = []
    for row in parser.rows:
        if not re.fullmatch(r"[0-9A-Za-z]{5}", row["code"]):
            continue
        if not re.fullmatch(r"\d{2}:\d{2}", row["time_jst"]):
            continue
        resolved = resolve_pdf_url(row["pdf_href"])
        if resolved is None:
            continue
        source_id, pdf_url = resolved
        disclosed_at = dt.datetime.combine(
            run_date, dt.time.fromisoformat(row["time_jst"]), tzinfo=JST
        )
        rows.append(
            RawDisclosureRow(
                time_jst=row["time_jst"],
                code=row["code"].upper(),
                issuer=row["issuer"],
                title=row["title"],
                pdf_href=row["pdf_href"],
                source_id=source_id,
                pdf_url=pdf_url,
                disclosed_at=disclosed_at,
            )
        )
    return ParsedListPage(
        total_declared=total,
        page_numbers=[int(value) for value in page_names],
        rows=rows,
    )
