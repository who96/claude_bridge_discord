# CLI Discord Bridge

Single-file Discord bridge for Codex-first operation with legacy Claude compatibility.

This repository keeps the original Claude bridge lineage, but the actively maintained runtime targets Codex: one Discord channel connects to one Codex CLI process. Legacy Claude artifacts remain only for migration and rollback.

This repository keeps command compatibility with the previous Claude bridge:
`/new /status /connect /handoff /help`.

## Deployment Topology

- Single process, single channel, single vendor (Codex)
- One process listens to exactly one `CHANNEL_ID`
- Multi-channel or multi-vendor HA is done by duplicating processes, not by mixing routes in one process

## Runtime Flow

```
Discord message -> bridge.py -> codex exec/resume --json -> parse JSON events -> Discord reply
```

Bridge state:

- State dir: `~/.cli-discord-bridge/`
- Session file: `~/.cli-discord-bridge/session.json`
  - Primary key: `thread_id`
  - Backward-compatible read: legacy `session_id`
- Handoff dir: `~/.cli-discord-bridge/handoffs/`

## Codex Session Protocol

- New thread:
  - `codex exec --json --skip-git-repo-check [--dangerously-bypass-approvals-and-sandbox] [-m MODEL] "<prompt>"`
- Resume thread:
  - `codex exec resume --json --skip-git-repo-check [--dangerously-bypass-approvals-and-sandbox] [-m MODEL] <thread_id> "<prompt>"`
- Parser rules:
  - Parse JSON lines only, ignore non-JSON noise
  - Use `thread.started.thread_id` as authoritative thread ID
  - Aggregate `item.completed.item.type=="agent_message"` text as reply body

`/connect` safety:

- Explicit `/connect <thread_id>` is strict.
- If resume returns a different `thread.started.thread_id`, bridge treats it as failure and blocks auto-fallback.

Implicit fallback:

- In non-explicit mode, resume failure may switch to a new thread and update local state.

## Environment Variables

| Variable | Required | Default | Description |
|---|---|---|---|
| `DISCORD_TOKEN` | Yes | — | Discord bot token |
| `CHANNEL_ID` | Yes | — | Discord channel ID |
| `CODEX_BIN` | No | `codex` | Codex CLI binary path |
| `CODEX_MODEL` | No | empty | Optional model override |
| `CODEX_TIMEOUT` | No | `300` | Max seconds per Codex call |
| `CODEX_CWD` | No | `$HOME` | Working directory for Codex call |
| `CODEX_FULL_ACCESS` | No | `0` | `1` enables `--dangerously-bypass-approvals-and-sandbox` |
| `SELFTEST_ON_START` | No | `0` | `1` runs startup selftest |

Compatibility fallback (for smooth migration):

- `CLAUDE_CWD` can still be used if `CODEX_CWD` is not set
- `CLAUDE_TIMEOUT` can still be used if `CODEX_TIMEOUT` is not set

## Commands

| Command | Behavior |
|---|---|
| `/new` | Clear current thread; next message creates a new thread |
| `/status` | Show current thread, cwd, timeout, backend |
| `/connect [thread-id]` | Bind explicit thread (no arg = reset) |
| `/handoff` | Summarize old thread -> coldstart new thread; rollback old thread if new bootstrap fails |
| `/help` | Show command help |

## Local Run

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

DISCORD_TOKEN="xxx" \
CHANNEL_ID="1234567890" \
CODEX_FULL_ACCESS="1" \
.venv/bin/python3 bridge.py
```

## LaunchAgent (CLI)

Keep old example for rollback:

- `com.claude.discord-bridge.plist.example` (legacy)

Use the active CLI bridge example:

- `com.cli.discord-bridge.plist.example`

Install example:

```bash
cp com.cli.discord-bridge.plist.example ~/Library/LaunchAgents/com.cli.discord-bridge.plist
# edit token/channel/paths
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.cli.discord-bridge.plist
```

Stop:

```bash
launchctl bootout gui/$(id -u)/com.cli.discord-bridge
```

Logs:

```bash
tail -f ~/.cli-discord-bridge/bridge.log
tail -f ~/.cli-discord-bridge/bridge.err.log
```

## Gray Release and Rollback

Recommended migration sequence:

1. Start `com.cli.discord-bridge` in a new Discord channel.
2. Keep old `com.claude.discord-bridge` running in parallel for observation.
3. Observe 24h for stuck process, leaks, thread loss, and command compatibility.
4. Switch OpenClaw DR entry to the new CLI bridge channel.
5. If needed, rollback by stopping `com.cli.discord-bridge` and continuing with legacy service.

## Notes

- This project does not implement dynamic multi-vendor switching in one process.
- If you need more HA lanes, copy service instances with different `CHANNEL_ID` and state dirs.
