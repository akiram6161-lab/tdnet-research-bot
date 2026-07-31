"""EDINET DB の MCP エンドポイントから大量保有(アクティビスト)データを取得する。

REST APIには該当エンドポイントがないため、MCP(JSON-RPC over HTTP)を直接呼ぶ。
認証は既存の EDINET_DB_API_KEY(Bearer)をそのまま使う。
"""

from __future__ import annotations

import json
import logging
from typing import Any

import requests

from src.settings import Settings

logger = logging.getLogger(__name__)

MCP_URL = "https://edinetdb.jp/mcp"
PROTOCOL_VERSION = "2025-03-26"


class EdinetDbError(RuntimeError):
    pass


def _parse_body(response: requests.Response) -> dict[str, Any]:
    body = response.text
    if "text/event-stream" in (response.headers.get("content-type") or ""):
        for line in body.splitlines():
            if line.startswith("data:"):
                body = line[5:].strip()
                break
    result: dict[str, Any] = json.loads(body)
    return result


def fetch_activist_positions(
    settings: Settings,
    purpose_category: str = "activist",
    limit: int = 500,
    timeout: float = 90.0,
) -> list[dict[str, Any]]:
    """アクティビストの大量保有ポジション一覧(提出日降順)を取得する。"""
    if not settings.edinet_db_api_key:
        raise EdinetDbError("EDINET_DB_API_KEY が未設定です")
    headers = {
        "Authorization": f"Bearer {settings.edinet_db_api_key}",
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    try:
        init = requests.post(
            MCP_URL,
            headers=headers,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": {"name": "tdnet-research-bot", "version": "0.1"},
                },
            },
            timeout=timeout,
        )
        init.raise_for_status()
        session = init.headers.get("mcp-session-id")
        if session:
            headers["mcp-session-id"] = session
        requests.post(
            MCP_URL,
            headers=headers,
            json={"jsonrpc": "2.0", "method": "notifications/initialized"},
            timeout=timeout,
        )
        call = requests.post(
            MCP_URL,
            headers=headers,
            json={
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {
                    "name": "get_activist_positions",
                    "arguments": {"purpose_category": purpose_category, "limit": limit},
                },
            },
            timeout=timeout,
        )
        call.raise_for_status()
        data = _parse_body(call)
        if "error" in data:
            raise EdinetDbError(f"MCP error: {data['error']}")
        content = data["result"]["content"][0]["text"]
        positions: list[dict[str, Any]] = json.loads(content)["positions"]
        return positions
    except (requests.RequestException, KeyError, ValueError) as exc:
        raise EdinetDbError(f"EDINET DB 取得に失敗: {exc}") from exc


def holding_key(position: dict[str, Any]) -> str:
    """大量保有の既読判定キー(ファイラー・銘柄・提出日)。"""
    return "|".join(
        [
            str(position.get("filer_name", "")),
            str(position.get("issuer_sec_code", "")),
            str(position.get("submit_date", "")),
        ]
    )
