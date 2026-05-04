# PROBLEMS-AND-FIXES

## 2026-04-13 — Claude Code bridge needed full permissions by default

### Symptom
The Discord Claude Code bridge worked, but Claude Code could still hit permission boundaries that slowed down or blocked autonomous work in attached threads.

### Root Cause
The bridge defaulted to a restrictive permission mode (`acceptEdits`) and did not pass Claude Code's explicit dangerous-permissions bypass flag.

### Fix
Changed `gateway/platforms/claude_code_bridge.py` so Discord-routed Claude Code runs use `permission_mode = "bypassPermissions"` by default and always add `--dangerously-skip-permissions` to the CLI invocation.

### Prevention
When the user wants Claude Code to behave like a fully autonomous local agent, encode that directly in the bridge defaults and cover it with a test that inspects the generated CLI args.

## 2026-04-13 — Discord threads couldn't talk directly to Claude Code

### Symptom
Users could ask Hermes to orchestrate Claude Code, but there was no direct Discord thread workflow where one thread mapped to one Claude Code session.

### Root Cause
The Discord gateway only knew how to route thread messages into Hermes sessions. There was no persistent per-thread Claude Code state, no Discord-native commands to attach/detach Claude Code, and no forwarding path from an active Discord thread into `claude -p --resume ...`.

### Fix
Added a persistent `ClaudeCodeThreadBridge` in `gateway/platforms/claude_code_bridge.py`, plus Discord-native `/cc-start`, `/cc-status`, and `/cc-stop` slash commands. When a thread is attached, normal text messages in that thread are forwarded to Claude Code print mode with persisted `session_id` reuse.

### Prevention
Keep thread-scoped external-agent bridges outside the main Hermes conversation loop, persist their state under `~/.hermes/custom/`, and add adapter tests for slash registration plus active-thread routing before restarting the gateway. Also document every local source patch in `~/.hermes/PATCHES.md` so `hermes update` does not wipe the custom behavior.

## 2026-04-13 — Claude Code bridge dropped Discord photos/audio and leaked media cache leftovers

### Symptom
A `/cc-start` thread only forwarded plain text to Claude Code. Photo and audio attachments did not reach Claude Code at all. On top of that, cached audio files had no scheduled TTL cleanup, and image files would have accumulated if media support had been added without explicit deletion.

### Root Cause
The Discord adapter only routed active Claude Code threads when `msg_type == MessageType.TEXT`. Media caching happened in the normal Hermes pipeline, not in a Claude-Code-specific prompt builder, so there was no bridge path for image/audio attachments and no per-turn cleanup hook. Separately, `gateway/run.py` wired hourly cleanup for image/document caches but not audio.

### Fix
Added Claude-Code-specific media handling in `gateway/platforms/discord.py`: active Claude Code threads now accept text, image, and audio messages; images are cached and passed to Claude Code as temporary local file paths, then deleted immediately after the turn; audio is cached only long enough to run STT, then deleted immediately, and Claude Code receives only the transcript text. Also added `cleanup_audio_cache()` in `gateway/platforms/base.py` and wired it into the hourly gateway cron cleanup in `gateway/run.py`. Added regression tests for photo routing, audio transcription prompt building, per-turn temp-file deletion, and audio-cache TTL cleanup.

### Prevention
When extending an external-agent bridge beyond plain text, do not reuse the generic media pipeline blindly. Build an explicit bridge prompt path for each media type, define file lifecycle up front, and enforce double protection: immediate per-file deletion after use plus hourly TTL cleanup for crash leftovers.

## 2026-04-17 — `/cc-start` slash handler drifted from bridge state and tests

### Symptom
The Discord Claude Code bridge looked implemented, but the targeted gateway tests failed: one slash-command registration test expected the old `_handle_cc_start_slash(...)` signature, and one attach-flow test crashed with `KeyError: 'max_turns'` when the mocked bridge session omitted that field.

### Root Cause
The slash command gained `max_turns` and `effort` parameters, but one regression test still asserted the legacy 3-argument call. Separately, the success message in `gateway/platforms/discord.py` assumed `start_session()` always returned `max_turns`, which is true in production bridge state but not guaranteed in lighter mocks or future partial session payloads.

### Fix
Updated the Discord adapter to display `session.get("max_turns", max_turns)` and updated the slash-command regression test to assert the full current `_handle_cc_start_slash(interaction, workdir, model, 100, "")` call. Re-ran the bridge test slice: `26 passed`.

### Prevention
When a slash command grows new parameters, update both the adapter tests and any human-facing status formatting in the same change. For bridge/session payloads, prefer `.get(..., fallback)` over hard indexing in UI strings so tests and partial mocks don't mask a working runtime path.

## 2026-04-18 — Hermes orchestration could miss Claude Code completion notifications

### Symptom
In Hermes-orchestrated development mode, Claude Code lots could finish successfully without notifying the user. The system could also blur the real Claude development lot with short-lived support process noise, making the reporting feel broken.

### Root Cause
Gateway-side completion handling relied too heavily on the per-process watcher path and the injected event stream. If that watcher was delayed, lost, or raced with recovery, `completion` events sitting in `completion_queue` were not injected as user-visible notifications. At the same time, there was no coordinated de-duplication between watcher delivery and per-turn drain delivery, and no way to suppress tiny successful non-Claude support processes while always preserving real Claude Code completions.

### Fix
Patched `gateway/run.py` and `tools/process_registry.py` so the gateway now drains real `completion` events as a fallback delivery path, records completion delivery/consumption to avoid duplicate injections, pins watcher tasks in `self._background_tasks`, and suppresses only short successful non-Claude noise via `background_process_notification_min_seconds`. Added regression coverage in `tests/gateway/test_completion_fallback_drain.py` and validated the full slice with targeted pytest plus a real tiny background Claude Code run whose completion notification was actually delivered in-channel.

### Prevention
For Hermes → Claude Code orchestration, never trust one notification path. Keep watcher delivery and queue-drain fallback in parity, mark completions as delivered/consumed explicitly, and require a live end-to-end proof (real Claude background proc + actual in-channel completion message) after any reporting patch.

## 2026-05-04 — custom Hermes upgrade rebased cleanly only after conflict-aware patch merges

### Symptom
Upgrading the local `local/custom-patches` branch from `v0.9.0` to upstream `v0.12.0` failed on rebase conflicts and the first merged result left the targeted Discord/Claude-Code regression slice broken.

### Root Cause
Local patches for transcription, Discord Claude Code bridge, media routing, completion reporting, and slash-sync fallback overlapped with large upstream changes in `transcription_tools.py`, `gateway/platforms/discord.py`, `gateway/run.py`, and Discord test scaffolding. The post-rebase test slice also exposed drift between newer upstream slash-command test mocks and local assumptions (`FakeTree.add_command`, watcher legacy payloads with missing `chat_type`, and formatter/default-thread-name expectations).

### Fix
Completed the rebase manually, preserving local bridge/reporting behavior while keeping upstream command-sync logic. Then finished the skipped post-rebase steps (`pip install -e '.[all]'`, patch regeneration), fixed the remaining merge drift, and re-ran the authoritative patch verification slice until it passed: `70 passed` across completion fallback, Claude Code bridge, Discord slash commands, and audio cache tests.

### Prevention
On this machine, never treat Hermes upgrades as a blind `git rebase`. Use `~/.hermes/bin/hermes-update`, expect conflicts in the documented Discord/transcription files, and always run the patch verification slice from `~/.hermes/PATCHES.md` before declaring the upgrade complete.

## 2026-05-04 — Discord audio transcription failed after local Whisper CPU patch

### Symptom
Discord voice/audio messages returned an internal Whisper error. Gateway logs showed `UnboundLocalError: cannot access local variable 'WhisperModel' where it is not associated with a value` in `tools/transcription_tools.py` when loading the local faster-whisper model.

### Root Cause
The local WSL2 patch changed `_transcribe_local()` to instantiate `WhisperModel(...)` directly on CPU/int8, but the function still had a later `from faster_whisper import WhisperModel` inside the CUDA-runtime fallback block. Python therefore treated `WhisperModel` as a function-local variable everywhere, making the earlier direct CPU instantiation read it before assignment.

### Fix
Moved the `from faster_whisper import WhisperModel` import to the top of `_transcribe_local()` after the faster-whisper availability check and removed the nested fallback import. Updated transcription tests so the machine-local patch asserts direct CPU/int8 loading instead of upstream `auto`/CUDA probing.

### Prevention
When a local patch bypasses an upstream helper and calls a lazily imported dependency directly, keep the import before first use in the same function and run the transcription unit tests plus a real local `_transcribe_local()` smoke test before restarting the gateway.
