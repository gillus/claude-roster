# Claude Roster

A web dashboard to manage, configure, and monitor Claude Code sessions across remote VMs and your local machine.

## Features

### Instance Management
- **Remote VMs** (GCP, AWS, any SSH-accessible host)
- **Local instances** on your own machine
- Start/stop Claude Code sessions with one click
- Connect via SSH+tmux (remote) or tmux attach (local)
- Live CPU, memory, disk stats per instance

### CLAUDE.md Editor
- View and edit all CLAUDE.md files per instance:
  - `~/.claude/CLAUDE.md` (global)
  - `./CLAUDE.md` (project)
  - `./CLAUDE.local.md` (personal/local)
- Save changes directly to the remote/local filesystem

### Permissions Manager
- Visual editor for `settings.json` permission rules
- Add/remove allow and deny rules with a click
- Supports user-level and project-level settings
- Toggle between visual mode and raw JSON editing

### Custom Commands (Skills)
- Browse commands in `~/.claude/commands/` and `.claude/commands/`
- Create, edit, and delete custom slash commands
- Supports both global and project scope

### Session Monitor
- View session activity log from JSONL transcript files
- Context window usage meter (input tokens, cache read/write, output)
- Token breakdown and percentage used

### MCP Server Viewer
- See configured MCP servers (global and project level)
- Displays server name, command/URL, and environment variables

## Quick Start

```bash
pip install -r requirements.txt
# Edit config.yaml with your instances
python server.py
```

Dashboard opens at `http://localhost:8420`.

## Configuration

```yaml
instances:
  # Remote VM
  - name: gcp-dev-1
    type: remote
    host: 34.xxx.xxx.xxx
    user: ubuntu
    key: ~/.ssh/gcp-dev.pem
    provider: gcp          # gcp | aws (for badge styling)

  # Local instance
  - name: local-main
    type: local
    working_dir: ~/projects/myapp
    provider: local
```

## Architecture

```
Browser (dashboard)
  │
  ├─ GET /api/instances         → list all + status
  ├─ POST /start, /stop         → manage sessions
  ├─ GET /connect               → get SSH command
  │
  ├─ GET/PUT /claude-md         → read/write CLAUDE.md files
  ├─ GET/PUT /settings          → read/write settings.json
  ├─ GET/PUT/DELETE /skills     → manage custom commands
  ├─ GET /session-log           → transcript entries
  ├─ GET /context               → token usage
  └─ GET /mcp                   → MCP server config
        │
        ▼
  Flask Server (localhost)
    ├─ SSH exec (remote instances)
    └─ subprocess (local instances)
```

## Session Monitoring Notes

Context window data is read from Claude Code's JSONL log files
(`~/.config/claude/logs/`). The accuracy depends on what Claude Code
writes to these logs. For more precise real-time monitoring, you can
also use the `/context` slash command directly in your Claude session.

## Environment Variables

| Variable     | Default         | Description     |
|-------------|-----------------|-----------------|
| `CCM_PORT`  | `8420`          | Server port     |
| `CCM_CONFIG`| `./config.yaml` | Config path     |
