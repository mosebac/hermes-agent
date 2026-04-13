"""Discord thread bridge for Claude Code sessions.

Keeps one Claude Code print-mode session per Discord thread. State is persisted
under ~/.hermes/custom so threads survive gateway restarts.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from hermes_constants import get_hermes_home

logger = logging.getLogger(__name__)

_DEFAULT_MODEL = "sonnet"
_DEFAULT_PERMISSION_MODE = "bypassPermissions"
_DEFAULT_MAX_TURNS = 10
_DEFAULT_TIMEOUT_SECONDS = 900


@dataclass
class ClaudeCodeRunResult:
    text: str
    session_id: Optional[str]
    permission_denials: list[dict]
    raw: dict[str, Any]


class ClaudeCodeThreadBridge:
    """Persistent per-thread Claude Code bridge for Discord."""

    def __init__(
        self,
        *,
        state_path: Path | None = None,
        claude_bin: str = "claude",
        default_model: str = _DEFAULT_MODEL,
        default_permission_mode: str = _DEFAULT_PERMISSION_MODE,
        default_max_turns: int = _DEFAULT_MAX_TURNS,
        default_timeout_seconds: int = _DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        self.state_path = state_path or (
            get_hermes_home() / "custom" / "claude-code" / "discord-thread-sessions.json"
        )
        self.claude_bin = claude_bin
        self.default_model = default_model
        self.default_permission_mode = default_permission_mode
        self.default_max_turns = default_max_turns
        self.default_timeout_seconds = default_timeout_seconds

    def start_session(
        self,
        *,
        thread_id: str,
        channel_id: str,
        user_id: str,
        workdir: str,
        model: str | None = None,
        permission_mode: str | None = None,
        max_turns: int | None = None,
        timeout_seconds: int | None = None,
    ) -> dict[str, Any]:
        resolved = self._resolve_workdir(workdir)
        if not resolved.exists():
            raise FileNotFoundError(f"workdir not found: {resolved}")
        if not resolved.is_dir():
            raise NotADirectoryError(f"workdir is not a directory: {resolved}")

        state = self._load_state()
        thread_state = {
            "thread_id": str(thread_id),
            "channel_id": str(channel_id),
            "user_id": str(user_id),
            "workdir": str(resolved),
            "model": (model or self.default_model).strip() or self.default_model,
            "permission_mode": (permission_mode or self.default_permission_mode).strip() or self.default_permission_mode,
            "max_turns": int(max_turns or self.default_max_turns),
            "timeout_seconds": int(timeout_seconds or self.default_timeout_seconds),
            "session_id": None,
            "started_at": self._now_iso(),
            "updated_at": self._now_iso(),
            "last_error": None,
        }
        state["threads"][str(thread_id)] = thread_state
        self._save_state(state)
        return thread_state

    def get_session(self, thread_id: str) -> Optional[dict[str, Any]]:
        return self._load_state().get("threads", {}).get(str(thread_id))

    def stop_session(self, thread_id: str) -> bool:
        state = self._load_state()
        removed = state.get("threads", {}).pop(str(thread_id), None)
        self._save_state(state)
        return removed is not None

    def is_active(self, thread_id: str) -> bool:
        return self.get_session(thread_id) is not None

    def format_status(self, thread_id: str) -> str:
        session = self.get_session(thread_id)
        if not session:
            return "Claude Code n'est pas attaché à ce thread."
        session_id = session.get("session_id") or "—"
        return (
            "Claude Code actif\n"
            f"workdir: `{session['workdir']}`\n"
            f"model: `{session['model']}`\n"
            f"permission_mode: `{session['permission_mode']}`\n"
            f"session_id: `{session_id}`\n"
            f"updated: `{session.get('updated_at', '—')}`"
        )

    async def run_prompt(self, thread_id: str, prompt: str) -> ClaudeCodeRunResult:
        session = self.get_session(thread_id)
        if not session:
            raise ValueError("no Claude Code session for this thread")
        return await asyncio.to_thread(self._run_prompt_sync, str(thread_id), prompt)

    def _run_prompt_sync(self, thread_id: str, prompt: str) -> ClaudeCodeRunResult:
        session = self.get_session(thread_id)
        if not session:
            raise ValueError("no Claude Code session for this thread")

        cmd = [
            self.claude_bin,
            "-p",
            prompt,
            "--output-format",
            "json",
            "--max-turns",
            str(session.get("max_turns") or self.default_max_turns),
            "--permission-mode",
            session.get("permission_mode") or self.default_permission_mode,
            "--dangerously-skip-permissions",
        ]
        model = (session.get("model") or self.default_model).strip()
        if model:
            cmd.extend(["--model", model])
        prior_session_id = (session.get("session_id") or "").strip()
        if prior_session_id:
            cmd.extend(["--resume", prior_session_id])

        logger.info(
            "[ClaudeCodeBridge] running Claude Code for thread %s in %s",
            thread_id,
            session["workdir"],
        )
        proc = subprocess.run(
            cmd,
            cwd=session["workdir"],
            capture_output=True,
            text=True,
            timeout=int(session.get("timeout_seconds") or self.default_timeout_seconds),
        )

        raw_output = (proc.stdout or "").strip()
        raw_error = (proc.stderr or "").strip()
        payload = self._parse_payload(raw_output or raw_error)
        result_text = (
            payload.get("result")
            or payload.get("error")
            or raw_output
            or raw_error
            or f"Claude Code exited with status {proc.returncode}."
        ).strip()
        session["session_id"] = payload.get("session_id") or session.get("session_id")
        session["updated_at"] = self._now_iso()
        session["last_error"] = None if proc.returncode == 0 else (raw_error or result_text)
        self._update_session(thread_id, session)
        return ClaudeCodeRunResult(
            text=result_text,
            session_id=session.get("session_id"),
            permission_denials=payload.get("permission_denials") or [],
            raw=payload,
        )

    def _parse_payload(self, raw: str) -> dict[str, Any]:
        text = (raw or "").strip()
        if not text:
            return {}
        try:
            payload = json.loads(text)
            return payload if isinstance(payload, dict) else {"result": text}
        except json.JSONDecodeError:
            pass

        for line in reversed(text.splitlines()):
            candidate = line.strip()
            if not candidate.startswith("{"):
                continue
            try:
                payload = json.loads(candidate)
                return payload if isinstance(payload, dict) else {"result": text}
            except json.JSONDecodeError:
                continue
        return {"result": text}

    def _update_session(self, thread_id: str, session: dict[str, Any]) -> None:
        state = self._load_state()
        state.setdefault("threads", {})[str(thread_id)] = session
        self._save_state(state)

    def _load_state(self) -> dict[str, Any]:
        path = self.state_path
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    data.setdefault("threads", {})
                    return data
            except Exception:
                logger.exception("[ClaudeCodeBridge] failed to read %s", path)
        return {"threads": {}}

    def _save_state(self, state: dict[str, Any]) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")

    def _resolve_workdir(self, workdir: str) -> Path:
        expanded = os.path.expandvars(os.path.expanduser((workdir or "").strip()))
        return Path(expanded).resolve()

    def _now_iso(self) -> str:
        return datetime.now(timezone.utc).replace(microsecond=0).isoformat()
