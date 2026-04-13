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
