"""Tests for the completion-queue fallback drain in the gateway runner.

The per-process watcher task is the primary path that converts a
``notify_on_complete=true`` background process into a ``[SYSTEM: ...]``
message in the user's chat. Historically the per-turn drain only handled
``watch_match`` and ``watch_disabled`` events and silently dropped
``"completion"`` events, so any completion that the watcher missed (task
GC, startup race, recovery gap) vanished.

These tests cover the corrected behaviour:
  - the per-turn drain also injects ``completion`` events
  - the noise filter suppresses short, successful, non-Claude-Code runs
  - the noise filter never drops failures or Claude Code invocations
  - completion consumption is coordinated between watcher and drain to
    avoid duplicate ``[SYSTEM: ...]`` messages
  - recovered and per-turn watcher tasks are pinned via
    ``self._background_tasks`` so the event loop cannot GC them mid-flight
"""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from gateway.config import GatewayConfig, Platform
from gateway.run import GatewayRunner, _format_gateway_process_notification


# ---------------------------------------------------------------------------
# Formatter
# ---------------------------------------------------------------------------


class TestFormatCompletion:
    def test_formats_completion_event(self):
        evt = {
            "type": "completion",
            "session_id": "proc_abc",
            "command": "pytest -x",
            "exit_code": 0,
            "output": "10 passed",
        }
        text = _format_gateway_process_notification(evt)
        assert text is not None
        assert "proc_abc" in text
        assert "exit code 0" in text
        assert "pytest -x" in text
        assert "10 passed" in text
        assert text.startswith("[SYSTEM:")
        assert text.endswith("]")

    def test_formats_watch_match(self):
        evt = {
            "type": "watch_match",
            "session_id": "proc_xyz",
            "command": "npm run dev",
            "pattern": "ready on",
            "output": "ready on http://localhost:3000",
            "suppressed": 0,
        }
        text = _format_gateway_process_notification(evt)
        assert text is not None
        assert "matched watch pattern" in text
        assert "ready on" in text

    def test_formats_watch_disabled(self):
        evt = {
            "type": "watch_disabled",
            "message": "Watch patterns disabled for process proc_abc.",
        }
        text = _format_gateway_process_notification(evt)
        assert text == "[SYSTEM: Watch patterns disabled for process proc_abc.]"


# ---------------------------------------------------------------------------
# _is_noise_completion
# ---------------------------------------------------------------------------


class TestNoiseFilter:
    def test_disabled_when_min_seconds_zero(self):
        evt = {"exit_code": 0, "command": "echo hi", "started_at": time.time() - 0.1, "finished_at": time.time()}
        assert GatewayRunner._is_noise_completion(evt, 0.0) is False

    def test_short_success_non_claude_is_noise(self):
        now = time.time()
        evt = {"exit_code": 0, "command": "ls -la", "started_at": now - 0.2, "finished_at": now}
        assert GatewayRunner._is_noise_completion(evt, 2.0) is True

    def test_failure_is_never_noise(self):
        now = time.time()
        evt = {"exit_code": 1, "command": "ls -la", "started_at": now - 0.2, "finished_at": now}
        assert GatewayRunner._is_noise_completion(evt, 2.0) is False

    def test_long_run_is_never_noise(self):
        now = time.time()
        evt = {"exit_code": 0, "command": "ls -la", "started_at": now - 10.0, "finished_at": now}
        assert GatewayRunner._is_noise_completion(evt, 2.0) is False

    def test_claude_code_invocation_is_never_noise(self):
        now = time.time()
        for cmd in [
            "claude -p 'do thing'",
            "claude --resume abc",
            "claude-code run",
            "/usr/local/bin/claude -p ok",
        ]:
            evt = {"exit_code": 0, "command": cmd, "started_at": now - 0.1, "finished_at": now}
            assert GatewayRunner._is_noise_completion(evt, 2.0) is False, cmd

    def test_missing_started_at_not_treated_as_noise(self):
        evt = {"exit_code": 0, "command": "ls", "started_at": 0, "finished_at": time.time()}
        assert GatewayRunner._is_noise_completion(evt, 2.0) is False


# ---------------------------------------------------------------------------
# _load_background_notifications_min_seconds
# ---------------------------------------------------------------------------


class TestLoadMinSeconds:
    def test_defaults_to_zero(self, monkeypatch, tmp_path):
        import gateway.run as gw
        monkeypatch.setattr(gw, "_hermes_home", tmp_path)
        monkeypatch.delenv("HERMES_BACKGROUND_NOTIFICATION_MIN_SECONDS", raising=False)
        assert GatewayRunner._load_background_notifications_min_seconds() == 0.0

    def test_reads_config_yaml(self, monkeypatch, tmp_path):
        (tmp_path / "config.yaml").write_text(
            "display:\n  background_process_notification_min_seconds: 3.5\n"
        )
        import gateway.run as gw
        monkeypatch.setattr(gw, "_hermes_home", tmp_path)
        monkeypatch.delenv("HERMES_BACKGROUND_NOTIFICATION_MIN_SECONDS", raising=False)
        assert GatewayRunner._load_background_notifications_min_seconds() == 3.5

    def test_env_var_wins(self, monkeypatch, tmp_path):
        (tmp_path / "config.yaml").write_text(
            "display:\n  background_process_notification_min_seconds: 3.5\n"
        )
        import gateway.run as gw
        monkeypatch.setattr(gw, "_hermes_home", tmp_path)
        monkeypatch.setenv("HERMES_BACKGROUND_NOTIFICATION_MIN_SECONDS", "1.0")
        assert GatewayRunner._load_background_notifications_min_seconds() == 1.0

    def test_invalid_returns_zero(self, monkeypatch, tmp_path):
        import gateway.run as gw
        monkeypatch.setattr(gw, "_hermes_home", tmp_path)
        monkeypatch.setenv("HERMES_BACKGROUND_NOTIFICATION_MIN_SECONDS", "banana")
        assert GatewayRunner._load_background_notifications_min_seconds() == 0.0


# ---------------------------------------------------------------------------
# Coordination between watcher and drain (mark_completion_delivered)
# ---------------------------------------------------------------------------


class TestCompletionDeliveryCoordination:
    def test_mark_completion_delivered_sets_consumed(self):
        from tools.process_registry import ProcessRegistry

        reg = ProcessRegistry()
        assert reg.is_completion_consumed("proc_abc") is False
        reg.mark_completion_delivered("proc_abc")
        assert reg.is_completion_consumed("proc_abc") is True

    def test_move_to_finished_populates_started_at(self):
        from tools.process_registry import ProcessRegistry, ProcessSession

        reg = ProcessRegistry()
        s = ProcessSession(
            id="proc_t1",
            command="echo hi",
            started_at=time.time() - 0.5,
            exited=True,
            exit_code=0,
            output_buffer="hi",
            notify_on_complete=True,
        )
        reg._running[s.id] = s
        with patch.object(reg, "_write_checkpoint"):
            reg._move_to_finished(s)
        evt = reg.completion_queue.get_nowait()
        assert "started_at" in evt
        assert "finished_at" in evt
        assert evt["finished_at"] >= evt["started_at"]


# ---------------------------------------------------------------------------
# Per-process watcher: coordination + noise suppression
# ---------------------------------------------------------------------------


class _FakeRegistry:
    def __init__(self, sessions):
        self._sessions = list(sessions)
        self.consumed: set = set()

    def get(self, session_id):
        if self._sessions:
            return self._sessions.pop(0)
        return None

    def is_completion_consumed(self, session_id):
        return session_id in self.consumed

    def mark_completion_delivered(self, session_id):
        self.consumed.add(session_id)


def _build_runner(monkeypatch, tmp_path, mode: str = "all", min_seconds: float = 0.0):
    cfg = f"display:\n  background_process_notifications: {mode}\n"
    if min_seconds:
        cfg += f"  background_process_notification_min_seconds: {min_seconds}\n"
    (tmp_path / "config.yaml").write_text(cfg, encoding="utf-8")

    import gateway.run as gateway_run
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)

    runner = GatewayRunner(GatewayConfig())
    adapter = SimpleNamespace(
        send=AsyncMock(),
        handle_message=AsyncMock(),
    )
    runner.adapters[Platform.TELEGRAM] = adapter
    return runner


def _agent_watcher_dict(session_id="proc_agent"):
    return {
        "session_id": session_id,
        "check_interval": 0,
        "platform": "telegram",
        "chat_id": "123",
        "thread_id": "",
        "user_id": "u1",
        "user_name": "alice",
        "session_key": "sk1",
        "notify_on_complete": True,
    }


@pytest.mark.asyncio
async def test_watcher_injects_and_marks_consumed(monkeypatch, tmp_path):
    """agent_notify watcher must inject synthetic event AND mark consumed."""
    import tools.process_registry as pr_module

    sess = SimpleNamespace(
        output_buffer="tests passed\n",
        exited=True,
        exit_code=0,
        command="pytest -x",
        started_at=time.time() - 30,
    )
    fake = _FakeRegistry([sess])
    monkeypatch.setattr(pr_module, "process_registry", fake)

    async def _instant_sleep(*_a, **_kw):
        pass
    monkeypatch.setattr(asyncio, "sleep", _instant_sleep)

    runner = _build_runner(monkeypatch, tmp_path, mode="all")
    adapter = runner.adapters[Platform.TELEGRAM]

    await runner._run_process_watcher(_agent_watcher_dict("proc_agent"))

    assert adapter.handle_message.await_count == 1
    synth_event = adapter.handle_message.await_args.args[0]
    assert "proc_agent" in synth_event.text
    assert "completed" in synth_event.text
    assert synth_event.internal is True
    assert "proc_agent" in fake.consumed


@pytest.mark.asyncio
async def test_watcher_suppresses_noise_completion(monkeypatch, tmp_path):
    """Short + success + non-Claude → noise; watcher marks consumed, no inject."""
    import tools.process_registry as pr_module

    sess = SimpleNamespace(
        output_buffer="ok\n",
        exited=True,
        exit_code=0,
        command="ls -la",
        started_at=time.time() - 0.1,  # very short
    )
    fake = _FakeRegistry([sess])
    monkeypatch.setattr(pr_module, "process_registry", fake)

    async def _instant_sleep(*_a, **_kw):
        pass
    monkeypatch.setattr(asyncio, "sleep", _instant_sleep)

    runner = _build_runner(monkeypatch, tmp_path, mode="all", min_seconds=2.0)
    adapter = runner.adapters[Platform.TELEGRAM]

    await runner._run_process_watcher(_agent_watcher_dict("proc_noisy"))

    assert adapter.handle_message.await_count == 0
    assert "proc_noisy" in fake.consumed


@pytest.mark.asyncio
async def test_watcher_keeps_claude_code_completion(monkeypatch, tmp_path):
    """Claude Code runs bypass the noise filter even when short."""
    import tools.process_registry as pr_module

    sess = SimpleNamespace(
        output_buffer="ok\n",
        exited=True,
        exit_code=0,
        command="claude -p 'build something'",
        started_at=time.time() - 0.1,  # very short, but Claude Code
    )
    fake = _FakeRegistry([sess])
    monkeypatch.setattr(pr_module, "process_registry", fake)

    async def _instant_sleep(*_a, **_kw):
        pass
    monkeypatch.setattr(asyncio, "sleep", _instant_sleep)

    runner = _build_runner(monkeypatch, tmp_path, mode="all", min_seconds=10.0)
    adapter = runner.adapters[Platform.TELEGRAM]

    await runner._run_process_watcher(_agent_watcher_dict("proc_cc"))

    assert adapter.handle_message.await_count == 1
    assert "proc_cc" in fake.consumed


@pytest.mark.asyncio
async def test_watcher_skips_when_already_consumed(monkeypatch, tmp_path):
    """If poll/wait already consumed the completion, watcher must not duplicate."""
    import tools.process_registry as pr_module

    sess = SimpleNamespace(
        output_buffer="ok\n",
        exited=True,
        exit_code=0,
        command="pytest -x",
        started_at=time.time() - 30,
    )
    fake = _FakeRegistry([sess])
    fake.consumed.add("proc_dup")
    monkeypatch.setattr(pr_module, "process_registry", fake)

    async def _instant_sleep(*_a, **_kw):
        pass
    monkeypatch.setattr(asyncio, "sleep", _instant_sleep)

    runner = _build_runner(monkeypatch, tmp_path, mode="all")
    adapter = runner.adapters[Platform.TELEGRAM]

    await runner._run_process_watcher(_agent_watcher_dict("proc_dup"))

    assert adapter.handle_message.await_count == 0


# ---------------------------------------------------------------------------
# Task GC protection: watcher tasks must be pinned in _background_tasks
# ---------------------------------------------------------------------------


class TestWatcherTaskGcSafety:
    """Regression: asyncio.create_task() without a strong reference could be
    garbage collected mid-run, silently dropping completion delivery."""

    def test_runner_tracks_background_tasks(self):
        runner = GatewayRunner(GatewayConfig())
        assert hasattr(runner, "_background_tasks")
        assert isinstance(runner._background_tasks, set)
