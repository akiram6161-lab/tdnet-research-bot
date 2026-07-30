"""TDnetリストページのパースと開示ID・PDF URL解決のテスト。"""

from __future__ import annotations

import datetime as dt

from src.tdnet.parser import extract_source_id, parse_list_page, resolve_pdf_url

RUN_DATE = dt.date(2026, 7, 30)

SAMPLE_HTML = """
<html><body>
<div>全 3 件</div>
<a href="I_list_001_20260730.html">1</a>
<table id="main-list-table">
  <tr>
    <td class="kjTime">15:30</td>
    <td class="kjCode">12340</td>
    <td class="kjName">ABC株式会社</td>
    <td class="kjTitle"><a href="140120260730512345.pdf">
      通期業績予想の上方修正に関するお知らせ</a></td>
  </tr>
  <tr>
    <td class="kjTime">15:35</td>
    <td class="kjCode">56780</td>
    <td class="kjName">DEF Inc.</td>
    <td class="kjTitle"><a
      href="https://www.release.tdnet.info/inbs/140120260730598765.pdf">
      Notice of Share Buyback</a></td>
  </tr>
  <tr>
    <td class="kjTime">--:--</td>
    <td class="kjCode">99990</td>
    <td class="kjName">壊れた行</td>
    <td class="kjTitle">PDFリンクなし</td>
  </tr>
  <tr>
    <td class="kjTime">16:00</td>
    <td class="kjCode">43210</td>
    <td class="kjName">偽サイト</td>
    <td class="kjTitle"><a href="https://evil.example.com/140120260730511111.pdf">不正ホスト</a></td>
  </tr>
</table>
</body></html>
"""


def test_parse_list_page_extracts_valid_rows() -> None:
    parsed = parse_list_page(SAMPLE_HTML, RUN_DATE)
    assert parsed.total_declared == 3
    assert parsed.page_numbers == [1]
    # 時刻不正の行と非公式ホストの行は除外される
    assert len(parsed.rows) == 2
    first = parsed.rows[0]
    assert first.code == "12340"
    assert first.issuer == "ABC株式会社"
    assert first.title == "通期業績予想の上方修正に関するお知らせ"
    assert first.source_id == "140120260730512345"
    assert first.pdf_url == "https://www.release.tdnet.info/inbs/140120260730512345.pdf"
    assert first.disclosed_at.hour == 15
    assert first.disclosed_at.minute == 30
    assert first.disclosed_at.tzinfo is not None


def test_extract_source_id() -> None:
    assert extract_source_id("140120260730512345.pdf") == "140120260730512345"
    assert extract_source_id("no-id-here.pdf") is None
    # 18桁ちょうどのみ受け付ける
    assert extract_source_id("1401202607305123456789.pdf") is None


def test_resolve_pdf_url_rejects_unofficial_host() -> None:
    assert resolve_pdf_url("https://evil.example.com/140120260730512345.pdf") is None
    resolved = resolve_pdf_url("140120260730512345.pdf")
    assert resolved is not None
    source_id, url = resolved
    assert source_id == "140120260730512345"
    assert url.startswith("https://www.release.tdnet.info/inbs/")
