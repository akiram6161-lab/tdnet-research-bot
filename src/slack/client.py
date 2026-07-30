"""Slack 投稿クライアント(Bot トークン使用)。"""

from __future__ import annotations

import logging
import time
from typing import Any

from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

logger = logging.getLogger(__name__)


class SlackPostError(RuntimeError):
    pass


class SlackClient:
    def __init__(self, bot_token: str) -> None:
        self._client = WebClient(token=bot_token)

    def auth_test(self) -> dict[str, Any]:
        response = self._client.auth_test()
        return dict(response.data) if isinstance(response.data, dict) else {}

    def post_parent_message(
        self,
        channel_id: str,
        text: str,
        blocks: list[dict[str, Any]] | None = None,
        max_attempts: int = 3,
    ) -> str:
        """チャンネルへ親メッセージを投稿し ts を返す。レート制限は Retry-After に従う。"""
        for _attempt in range(max_attempts):
            try:
                response = self._client.chat_postMessage(
                    channel=channel_id,
                    text=text,
                    blocks=blocks,
                    unfurl_links=False,
                    unfurl_media=False,
                )
                ts = response.get("ts")
                if not isinstance(ts, str):
                    raise SlackPostError("Slack response did not contain ts")
                return ts
            except SlackApiError as exc:
                if exc.response is not None and exc.response.status_code == 429:
                    retry_after = int(exc.response.headers.get("Retry-After", "1"))
                    logger.warning("Slack rate limited; retrying in %ss", retry_after)
                    time.sleep(retry_after)
                    continue
                raise SlackPostError(f"Slack post failed: {exc.response['error']}") from exc
        raise SlackPostError("Slack post failed after retries (rate limited)")
