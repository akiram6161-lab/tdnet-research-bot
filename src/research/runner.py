"""Claude Code(サブスクリプション認証)による自動リサーチ実行。

`claude -p` をサブプロセスとして起動し、開示PDF・EDINET DB・J-Quantsの
データ取得と分析を Claude Code に委ねる。Claude API(従量課金)は使わない。
"""

from __future__ import annotations

import logging
import os
import subprocess
from pathlib import Path
from typing import Any

from src.settings import Settings

logger = logging.getLogger(__name__)

RESEARCH_TIMEOUT_SECONDS = 600
HIGHLIGHT_TIMEOUT_SECONDS = 900
PROMPT_PATH = Path(__file__).resolve().parent.parent.parent / "prompts" / "disclosure_research.md"
HIGHLIGHT_PROMPT_PATH = (
    Path(__file__).resolve().parent.parent.parent / "prompts" / "daily_highlight.md"
)


class ResearchError(RuntimeError):
    pass


def build_prompt(job: dict[str, Any]) -> str:
    """スレッドマッピング(state)の情報からリサーチプロンプトを組み立てる。"""
    template = PROMPT_PATH.read_text(encoding="utf-8")
    replacements = {
        "{security_code}": str(job.get("security_code", "")),
        "{company_name}": str(job.get("company_name", "")),
        "{disclosed_at}": str(job.get("disclosed_at", "")),
        "{title}": str(job.get("title", "")),
        "{category}": str(job.get("category", "")),
        "{document_url}": str(job.get("document_url", "")),
    }
    for key, value in replacements.items():
        template = template.replace(key, value)
    return template


def run_research(job: dict[str, Any], settings: Settings) -> str:
    """Claude Code でリサーチを実行し、サマリー本文を返す。失敗時は ResearchError。"""
    prompt = build_prompt(job)
    env = dict(os.environ)
    # サブスクリプション認証トークン(GitHub Actions では Secrets 経由)
    if settings.claude_code_oauth_token:
        env["CLAUDE_CODE_OAUTH_TOKEN"] = settings.claude_code_oauth_token
    if settings.edinet_db_api_key:
        env["EDINET_DB_API_KEY"] = settings.edinet_db_api_key
    if settings.jquants_api_key:
        env["JQUANTS_API_KEY"] = settings.jquants_api_key

    command = [
        settings.claude_cli,
        "-p",
        prompt,
        "--output-format",
        "text",
        "--allowedTools",
        "Bash,Read,WebFetch,WebSearch",
    ]
    try:
        result = subprocess.run(
            command,
            env=env,
            capture_output=True,
            text=True,
            timeout=RESEARCH_TIMEOUT_SECONDS,
            check=False,
        )
    except FileNotFoundError as exc:
        raise ResearchError(f"claude CLI が見つかりません: {settings.claude_cli}") from exc
    except subprocess.TimeoutExpired as exc:
        raise ResearchError(f"リサーチがタイムアウトしました({RESEARCH_TIMEOUT_SECONDS}s)") from exc

    if result.returncode != 0:
        stderr_tail = (result.stderr or "")[-500:]
        raise ResearchError(f"claude CLI がエラー終了 (code={result.returncode}): {stderr_tail}")

    summary = (result.stdout or "").strip()
    if not summary:
        raise ResearchError("claude CLI の出力が空でした")
    return summary


def _run_claude(prompt: str, settings: Settings, timeout: int) -> str:
    env = dict(os.environ)
    if settings.claude_code_oauth_token:
        env["CLAUDE_CODE_OAUTH_TOKEN"] = settings.claude_code_oauth_token
    if settings.edinet_db_api_key:
        env["EDINET_DB_API_KEY"] = settings.edinet_db_api_key
    if settings.jquants_api_key:
        env["JQUANTS_API_KEY"] = settings.jquants_api_key
    command = [
        settings.claude_cli,
        "-p",
        prompt,
        "--output-format",
        "text",
        "--allowedTools",
        "Bash,Read,WebFetch,WebSearch",
    ]
    try:
        result = subprocess.run(
            command, env=env, capture_output=True, text=True, timeout=timeout, check=False
        )
    except FileNotFoundError as exc:
        raise ResearchError(f"claude CLI が見つかりません: {settings.claude_cli}") from exc
    except subprocess.TimeoutExpired as exc:
        raise ResearchError(f"タイムアウトしました({timeout}s)") from exc
    if result.returncode != 0:
        raise ResearchError(
            f"claude CLI がエラー終了 (code={result.returncode}): {(result.stderr or '')[-500:]}"
        )
    output = (result.stdout or "").strip()
    if not output:
        raise ResearchError("claude CLI の出力が空でした")
    return output


def build_highlight_prompt(items: list[dict[str, Any]], target_date: str) -> str:
    lines = []
    for m in sorted(items, key=lambda x: -int(x.get("score", 0))):
        lines.append(
            f"- {m.get('score')}点 [{m.get('security_code')}] {m.get('company_name')}: "
            f"{m.get('title')} ({m.get('document_url')})"
        )
    template = HIGHLIGHT_PROMPT_PATH.read_text(encoding="utf-8")
    return template.replace("{target_date}", target_date).replace("{items}", "\n".join(lines))


def run_daily_highlight(
    items: list[dict[str, Any]], target_date: str, settings: Settings
) -> tuple[str, str | None]:
    """ハイライトを生成し (サマリー, 深掘り) を返す。"""
    output = _run_claude(
        build_highlight_prompt(items, target_date), settings, HIGHLIGHT_TIMEOUT_SECONDS
    )
    if "---DEEPDIVE---" in output:
        summary, deep = output.split("---DEEPDIVE---", 1)
        return summary.strip(), deep.strip() or None
    return output.strip(), None
