import json
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from gateway.platforms.claude_code_bridge import ClaudeCodeThreadBridge


def test_start_session_persists_thread_state(tmp_path):
    workdir = tmp_path / "repo"
    workdir.mkdir()
    state_path = tmp_path / "state.json"
    bridge = ClaudeCodeThreadBridge(state_path=state_path)

    session = bridge.start_session(
        thread_id="thread-1",
        channel_id="thread-1",
        user_id="42",
        workdir=str(workdir),
        model="sonnet",
    )

    assert session["workdir"] == str(workdir.resolve())
    assert session["model"] == "sonnet"
    assert session["permission_mode"] == "bypassPermissions"
    assert bridge.get_session("thread-1")["model"] == "sonnet"
    assert state_path.exists()


@pytest.mark.asyncio
async def test_run_prompt_updates_session_id_and_reuses_resume(tmp_path):
    workdir = tmp_path / "repo"
    workdir.mkdir()
    bridge = ClaudeCodeThreadBridge(state_path=tmp_path / "state.json")
    bridge.start_session(
        thread_id="thread-1",
        channel_id="thread-1",
        user_id="42",
        workdir=str(workdir),
        model="sonnet",
    )

    first_payload = {
        "result": "first",
        "session_id": "sess-1",
        "permission_denials": [],
    }
    second_payload = {
        "result": "second",
        "session_id": "sess-1",
        "permission_denials": [],
    }

    calls = []

    def fake_run(cmd, **kwargs):
        calls.append((cmd, kwargs))
        payload = first_payload if len(calls) == 1 else second_payload
        return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(payload), stderr="")

    with patch("gateway.platforms.claude_code_bridge.subprocess.run", side_effect=fake_run):
        first = await bridge.run_prompt("thread-1", "hello")
        second = await bridge.run_prompt("thread-1", "again")

    assert first.text == "first"
    assert second.text == "second"
    assert bridge.get_session("thread-1")["session_id"] == "sess-1"
    first_cmd = calls[0][0]
    second_cmd = calls[1][0]
    assert "--dangerously-skip-permissions" in first_cmd
    assert first_cmd[first_cmd.index("--permission-mode") + 1] == "bypassPermissions"
    assert "--resume" not in first_cmd
    assert second_cmd[second_cmd.index("--resume") + 1] == "sess-1"
    assert "--dangerously-skip-permissions" in second_cmd
    assert calls[0][1]["cwd"] == str(workdir.resolve())


@pytest.mark.asyncio
async def test_run_prompt_falls_back_to_plain_text_when_json_missing(tmp_path):
    workdir = tmp_path / "repo"
    workdir.mkdir()
    bridge = ClaudeCodeThreadBridge(state_path=tmp_path / "state.json")
    bridge.start_session(
        thread_id="thread-1",
        channel_id="thread-1",
        user_id="42",
        workdir=str(workdir),
    )

    with patch(
        "gateway.platforms.claude_code_bridge.subprocess.run",
        return_value=subprocess.CompletedProcess(["claude"], 0, stdout="plain text output", stderr=""),
    ):
        result = await bridge.run_prompt("thread-1", "hello")

    assert result.text == "plain text output"
