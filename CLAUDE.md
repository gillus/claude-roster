# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Claude Roster — a Flask web dashboard for managing, configuring, and monitoring Claude Code sessions across remote VMs (via SSH) and local machines (via subprocess/tmux).

## Running

```bash
pip install -r requirements.txt   # flask, pyyaml
python server.py                  # starts on http://localhost:8420
```

Environment variables: `CCM_PORT` (default 8420), `CCM_CONFIG` (default `./config.yaml`).

## Architecture

Single-file Flask app (`server.py`) + single-page frontend (`static/index.html`).

- **`config.yaml`** — defines instances (remote with SSH creds, or local with working_dir)
- **`server.py`** — all backend logic:
  - `run_cmd()` — unified command execution: SSH for remote instances, `bash -c` for local
  - `read_remote_file()` / `write_remote_file()` — file I/O on instances via `run_cmd`
  - `check_instance()` — gathers status (reachability, tmux session, system stats, git info, context tokens)
  - `poll_loop()` — background thread polling all instances every 15 seconds
  - `instance_cache` / `sessions_meta` — in-memory state (no database)
- **`static/index.html`** — complete SPA frontend (HTML/CSS/JS in one file)

### API Routes (all under `/api/instances/<name>/`)

| Endpoint | Purpose |
|---|---|
| `GET /api/instances` | List all instances with cached status |
| `POST .../start`, `POST .../stop` | Start/stop Claude Code tmux sessions |
| `GET .../connect` | Get SSH/tmux attach command |
| `GET/PUT .../claude-md` | Read/write CLAUDE.md files on instance |
| `GET/PUT .../settings` | Read/write settings.json permissions |
| `GET/PUT/DELETE .../skills` | Manage custom slash commands |
| `GET .../session-log` | Read JSONL transcript entries |
| `GET .../context` | Token/context window usage |
| `GET .../mcp` | MCP server configuration |

### Key Design Decisions

- All instance interaction goes through `run_cmd()` — remote commands are wrapped in SSH, local commands use `bash -c`
- Sessions are managed via tmux (session name: `claude`)
- Context/token data is scraped from `~/.config/claude/logs/*.jsonl` files
- No authentication — intended for local/trusted-network use
- Embedded terminal uses xterm.js + WebSocket (`flask-sock`) with `pty.fork()` to relay I/O to tmux sessions
- WebSocket terminal relay uses a queue pattern: background thread reads PTY into a queue, main thread handles all WS ops (send/receive) for thread safety

## Roadmap

- **Send prompt to session** — use `tmux send-keys -t claude` to inject text into a running session from the dashboard without attaching
- **Multi-instance task orchestration** — start the same prompt across multiple instances simultaneously; parallel infrastructure already exists in `poll_loop`
- **Session persistence** — persist `sessions_meta` to a JSON file so history survives server restarts
- **Cost tracking** — accumulate token usage from JSONL logs per-session and estimate cost by model; `get_context_info` already parses usage entries
- **Alerts / notifications** — watch for session completion (tmux disappears), high context usage (>80%), or log errors; browser notifications or webhooks
- **Instance groups / tags** — group instances by project/environment/team for batch operations
- **Config editor in dashboard** — edit `config.yaml` from the UI to add/remove instances without restart; `load_config()` already re-reads on every call
- **Session duration display** — `started_at` is already in `sessions_meta`; surface elapsed time in the UI
