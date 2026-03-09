#!/usr/bin/env python3
"""
Claude Roster
Full control plane for Claude Code instances across VMs and local machine.
"""

import os
import sys
import json
import glob
import yaml
import subprocess
import threading
import time
import webbrowser
import re
import pty
import select
import signal
import struct
import fcntl
import termios
import queue
from datetime import datetime, timezone
from pathlib import Path
from flask import Flask, jsonify, request, send_from_directory, abort
from flask_sock import Sock

app = Flask(__name__, static_folder="static")
sock = Sock(app)

CONFIG_PATH = os.environ.get("CCM_CONFIG", os.path.join(os.path.dirname(__file__), "config.yaml"))
instance_cache: dict[str, dict] = {}
sessions_meta: dict[str, dict] = {}


# ═══════════════════════════════════════════════════════════════════════════════
# CONFIG
# ═══════════════════════════════════════════════════════════════════════════════

def load_config() -> dict:
    with open(CONFIG_PATH, "r") as f:
        return yaml.safe_load(f)


def get_instances() -> list[dict]:
    return load_config().get("instances", [])


def find_instance(name: str) -> dict | None:
    return next((i for i in get_instances() if i["name"] == name), None)


# ═══════════════════════════════════════════════════════════════════════════════
# COMMAND EXECUTION (SSH for remote, subprocess for local)
# ═══════════════════════════════════════════════════════════════════════════════

def run_cmd(inst: dict, command: str, timeout: int = 10) -> tuple[int, str, str]:
    """Run a command on an instance. SSH for remote, local shell for local."""
    if inst["type"] == "remote":
        key = os.path.expanduser(inst["key"])
        cmd = [
            "ssh", "-i", key,
            "-o", "StrictHostKeyChecking=no",
            "-o", "ConnectTimeout=5",
            "-o", "BatchMode=yes",
            f"{inst['user']}@{inst['host']}",
            command,
        ]
    else:
        cmd = ["bash", "-c", command]

    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.returncode, r.stdout.strip(), r.stderr.strip()
    except subprocess.TimeoutExpired:
        return -1, "", "timeout"
    except Exception as e:
        return -1, "", str(e)


def read_remote_file(inst: dict, path: str) -> str | None:
    """Read a file from an instance."""
    rc, out, _ = run_cmd(inst, f"cat {path} 2>/dev/null", timeout=10)
    return out if rc == 0 else None


def write_remote_file(inst: dict, path: str, content: str) -> bool:
    """Write content to a file on an instance."""
    # Escape for heredoc
    escaped = content.replace("\\", "\\\\").replace("$", "\\$").replace("`", "\\`")
    cmd = f'mkdir -p "$(dirname {path})" && cat > {path} << \'CCMEOF\'\n{content}\nCCMEOF'
    rc, _, err = run_cmd(inst, cmd, timeout=10)
    return rc == 0


# ═══════════════════════════════════════════════════════════════════════════════
# INSTANCE STATUS
# ═══════════════════════════════════════════════════════════════════════════════

def check_instance(inst: dict) -> dict:
    name = inst["name"]
    base = {
        "name": name,
        "type": inst["type"],
        "provider": inst.get("provider", "unknown"),
        "host": inst.get("host", "localhost"),
        "user": inst.get("user", os.environ.get("USER", "")),
    }

    # Check reachability
    if inst["type"] == "remote":
        rc, _, err = run_cmd(inst, "echo ok", timeout=5)
        if rc != 0:
            return {**base, "reachable": False, "error": err, "has_session": False}
    else:
        base["reachable"] = True

    # Check for tmux claude session
    rc, out, _ = run_cmd(inst, "tmux has-session -t claude 2>/dev/null && echo yes || echo no")
    has_session = out.strip() == "yes"

    # System stats
    rc, stats, _ = run_cmd(inst, """
        echo "CPU:$(top -bn1 2>/dev/null | grep 'Cpu(s)' | awk '{print $2}' || echo 'N/A')"
        echo "MEM:$(free -m 2>/dev/null | awk '/Mem:/{printf "%d/%d", $3, $2}' || echo 'N/A')"
        echo "DISK:$(df -h / 2>/dev/null | awk 'NR==2{printf "%s/%s", $3, $2}' || echo 'N/A')"
    """)

    parsed = {}
    for line in (stats or "").split("\n"):
        if ":" in line:
            k, v = line.split(":", 1)
            parsed[k.strip()] = v.strip()

    # Session details
    repo_info = ""
    branch = ""
    working_dir = ""
    if has_session:
        rc, sess_out, _ = run_cmd(inst, """
            pane_pid=$(tmux list-panes -t claude -F '#{pane_pid}' 2>/dev/null | head -1)
            if [ -n "$pane_pid" ]; then
                cwd=$(readlink -f /proc/$pane_pid/cwd 2>/dev/null || echo "")
                echo "CWD:${cwd}"
                if [ -n "$cwd" ] && [ -d "$cwd/.git" ]; then
                    echo "REPO:$(basename $cwd)"
                    echo "BRANCH:$(git -C $cwd branch --show-current 2>/dev/null)"
                fi
            fi
        """)
        for line in (sess_out or "").split("\n"):
            if line.startswith("CWD:"):
                working_dir = line[4:]
            elif line.startswith("REPO:"):
                repo_info = line[5:]
            elif line.startswith("BRANCH:"):
                branch = line[7:]

    meta = sessions_meta.get(name, {})

    # Context / token monitoring
    context_info = get_context_info(inst) if has_session else {}

    return {
        **base,
        "reachable": True,
        "has_session": has_session,
        "cpu": parsed.get("CPU", "N/A"),
        "memory": parsed.get("MEM", "N/A"),
        "disk": parsed.get("DISK", "N/A"),
        "repo": repo_info or meta.get("repo", ""),
        "branch": branch or meta.get("branch", ""),
        "working_dir": working_dir or inst.get("working_dir", ""),
        "started_at": meta.get("started_at", ""),
        "task": meta.get("task", ""),
        "context": context_info,
    }


def _find_latest_log_cmd(inst: dict) -> str:
    """Build shell command to find the latest JSONL session log for an instance."""
    wdir = inst.get("working_dir", "")
    if wdir:
        # Claude Code stores logs in ~/.claude/projects/<slug>/<session>.jsonl
        # Slug is the absolute working dir path with / replaced by -
        return f"""
            WDIR="{wdir}"
            WDIR="${{WDIR/#~/$HOME}}"
            WDIR=$(cd "$WDIR" 2>/dev/null && pwd || echo "$WDIR")
            SLUG=$(echo "$WDIR" | sed 's|^/||; s|/|-|g')
            PROJ_DIR="${{HOME}}/.claude/projects/-${{SLUG}}"
            if [ -d "$PROJ_DIR" ]; then
                ls -t "$PROJ_DIR"/*.jsonl 2>/dev/null | head -1
            fi
        """
    return ""


def get_context_info(inst: dict) -> dict:
    """Try to read context/token usage from Claude Code session logs."""
    find_cmd = _find_latest_log_cmd(inst)
    if not find_cmd:
        return {}
    rc, out, _ = run_cmd(inst, f"""
        LATEST=$({find_cmd})
        if [ -n "$LATEST" ]; then
            tac "$LATEST" 2>/dev/null | grep -m1 '"usage"' | head -1
        fi
    """, timeout=8)

    if not out:
        return {}

    try:
        entry = json.loads(out)
        usage = entry.get("usage", entry.get("result", {}).get("usage", {}))
        if not usage:
            # Try nested
            for key in ["message", "result"]:
                if key in entry and "usage" in entry[key]:
                    usage = entry[key]["usage"]
                    break

        if usage:
            input_t = usage.get("input_tokens", 0)
            cache_read = usage.get("cache_read_input_tokens", 0)
            cache_create = usage.get("cache_creation_input_tokens", 0)
            output_t = usage.get("output_tokens", 0)
            total_used = input_t + cache_read + cache_create
            context_window = 200000  # default
            pct = round((total_used / context_window) * 100, 1) if context_window > 0 else 0

            return {
                "input_tokens": input_t,
                "cache_read_tokens": cache_read,
                "cache_create_tokens": cache_create,
                "output_tokens": output_t,
                "total_context_used": total_used,
                "context_window": context_window,
                "percent_used": min(pct, 100),
            }
    except (json.JSONDecodeError, KeyError, TypeError):
        pass

    return {}


# ═══════════════════════════════════════════════════════════════════════════════
# BACKGROUND POLLER
# ═══════════════════════════════════════════════════════════════════════════════

def poll_loop():
    while True:
        try:
            instances = get_instances()
            threads, results = [], {}

            def check(inst):
                results[inst["name"]] = check_instance(inst)

            for inst in instances:
                t = threading.Thread(target=check, args=(inst,))
                t.start()
                threads.append(t)
            for t in threads:
                t.join(timeout=15)

            instance_cache.update(results)
        except Exception as e:
            print(f"[poller] {e}", file=sys.stderr)
        time.sleep(15)


# ═══════════════════════════════════════════════════════════════════════════════
# API — INSTANCES
# ═══════════════════════════════════════════════════════════════════════════════

@app.route("/api/instances")
def api_instances():
    if not instance_cache:
        for inst in get_instances():
            instance_cache[inst["name"]] = check_instance(inst)
    return jsonify(list(instance_cache.values()))


@app.route("/api/instances/<name>/start", methods=["POST"])
def api_start(name):
    inst = find_instance(name)
    if not inst:
        return jsonify({"error": "Not found"}), 404

    data = request.json or {}
    repo = data.get("repo", "")
    task = data.get("task", "")
    wdir = data.get("working_dir", inst.get("working_dir", ""))

    cmds = []
    if repo:
        repo_name = repo.rstrip("/").split("/")[-1].replace(".git", "")
        cmds.append(f'[ -d ~/{repo_name} ] && (cd ~/{repo_name} && git pull) || git clone {repo} ~/{repo_name}')
        wdir = wdir or f"~/{repo_name}"

    start_dir = os.path.expanduser(wdir) if wdir else "~"
    cmds.append(f"tmux new-session -d -s claude -c {start_dir} 'claude'")

    rc, _, err = run_cmd(inst, " && ".join(cmds), timeout=30)
    if rc != 0:
        return jsonify({"error": err}), 500

    sessions_meta[name] = {
        "repo": repo.rstrip("/").split("/")[-1].replace(".git", "") if repo else "",
        "task": task,
        "started_at": datetime.now(timezone.utc).isoformat(),
    }
    instance_cache[name] = check_instance(inst)
    return jsonify({"ok": True})


@app.route("/api/instances/<name>/stop", methods=["POST"])
def api_stop(name):
    inst = find_instance(name)
    if not inst:
        return jsonify({"error": "Not found"}), 404
    run_cmd(inst, "tmux kill-session -t claude 2>/dev/null; echo done")
    sessions_meta.pop(name, None)
    instance_cache[name] = check_instance(inst)
    return jsonify({"ok": True})


@app.route("/api/instances/<name>/connect")
def api_connect(name):
    inst = find_instance(name)
    if not inst:
        return jsonify({"error": "Not found"}), 404

    if inst["type"] == "remote":
        key = os.path.expanduser(inst["key"])
        cmd = f"ssh -i {key} -t {inst['user']}@{inst['host']} 'tmux attach-session -t claude'"
    else:
        cmd = "tmux attach-session -t claude"

    return jsonify({"ssh_command": cmd, "type": inst["type"]})


@app.route("/api/instances/<name>/refresh", methods=["POST"])
def api_refresh(name):
    inst = find_instance(name)
    if not inst:
        return jsonify({"error": "Not found"}), 404
    status = check_instance(inst)
    instance_cache[name] = status
    return jsonify(status)


# ═══════════════════════════════════════════════════════════════════════════════
# API — CLAUDE.md FILES

@app.route("/api/instances/<name>/git-info")
def api_git_info(name):
    """Check if the instance working_dir already has a git repo."""
    inst = find_instance(name)
    if not inst:
        return jsonify({"error": "Not found"}), 404

    wdir = inst.get("working_dir", "")
    if not wdir:
        return jsonify({"has_git": False})

    rc, out, _ = run_cmd(inst, """
        WDIR="%s"
        WDIR="${WDIR/#~/$HOME}"
        if [ -d "$WDIR/.git" ]; then
            echo "HAS_GIT:yes"
            echo "REPO:$(basename $WDIR)"
            echo "BRANCH:$(git -C $WDIR branch --show-current 2>/dev/null || echo unknown)"
            echo "REMOTE:$(git -C $WDIR remote get-url origin 2>/dev/null || echo none)"
            echo "STATUS:$(git -C $WDIR status --porcelain 2>/dev/null | wc -l | tr -d ' ')"
        else
            echo "HAS_GIT:no"
        fi
    """ % wdir, timeout=8)

    info = {"has_git": False, "working_dir": wdir}
    for line in (out or "").split("\n"):
        if line.startswith("HAS_GIT:yes"):
            info["has_git"] = True
        elif line.startswith("REPO:"):
            info["repo"] = line[5:]
        elif line.startswith("BRANCH:"):
            info["branch"] = line[7:]
        elif line.startswith("REMOTE:"):
            info["remote"] = line[7:]
        elif line.startswith("STATUS:"):
            try:
                info["dirty_files"] = int(line[7:])
            except ValueError:
                info["dirty_files"] = 0

    return jsonify(info)


# ═══════════════════════════════════════════════════════════════════════════════

@app.route("/api/instances/<name>/claude-md")
def api_get_claude_md(name):
    """Get all CLAUDE.md files for an instance."""
    inst = find_instance(name)
    if not inst:
        return jsonify({"error": "Not found"}), 404

    status = instance_cache.get(name, {})
    wdir = status.get("working_dir", inst.get("working_dir", "~"))

    files = {}

    # Global CLAUDE.md
    content = read_remote_file(inst, "~/.claude/CLAUDE.md")
    if content is not None:
        files["global"] = {"path": "~/.claude/CLAUDE.md", "content": content}

    # Project CLAUDE.md
    if wdir:
        content = read_remote_file(inst, f"{wdir}/CLAUDE.md")
        if content is not None:
            files["project"] = {"path": f"{wdir}/CLAUDE.md", "content": content}

        # Project local CLAUDE.md
        content = read_remote_file(inst, f"{wdir}/CLAUDE.local.md")
        if content is not None:
            files["project_local"] = {"path": f"{wdir}/CLAUDE.local.md", "content": content}

    return jsonify(files)


@app.route("/api/instances/<name>/claude-md", methods=["PUT"])
def api_save_claude_md(name):
    """Save a CLAUDE.md file."""
    inst = find_instance(name)
    if not inst:
        return jsonify({"error": "Not found"}), 404

    data = request.json or {}
    path = data.get("path", "")
    content = data.get("content", "")

    if not path:
        return jsonify({"error": "path required"}), 400

    ok = write_remote_file(inst, path, content)
    if not ok:
        return jsonify({"error": "Failed to write file"}), 500

    return jsonify({"ok": True})


# ═══════════════════════════════════════════════════════════════════════════════
# API — SETTINGS / PERMISSIONS
# ═══════════════════════════════════════════════════════════════════════════════

@app.route("/api/instances/<name>/settings")
def api_get_settings(name):
    """Get all settings.json files for an instance."""
    inst = find_instance(name)
    if not inst:
        return jsonify({"error": "Not found"}), 404

    status = instance_cache.get(name, {})
    wdir = status.get("working_dir", inst.get("working_dir", "~"))

    result = {}

    # User settings
    content = read_remote_file(inst, "~/.claude/settings.json")
    if content:
        try:
            result["user"] = {"path": "~/.claude/settings.json", "content": json.loads(content)}
        except json.JSONDecodeError:
            result["user"] = {"path": "~/.claude/settings.json", "content": {}, "raw": content}

    # Project settings
    if wdir:
        content = read_remote_file(inst, f"{wdir}/.claude/settings.json")
        if content:
            try:
                result["project"] = {"path": f"{wdir}/.claude/settings.json", "content": json.loads(content)}
            except json.JSONDecodeError:
                result["project"] = {"path": f"{wdir}/.claude/settings.json", "content": {}, "raw": content}

        # Project local settings
        content = read_remote_file(inst, f"{wdir}/.claude/settings.local.json")
        if content:
            try:
                result["project_local"] = {"path": f"{wdir}/.claude/settings.local.json", "content": json.loads(content)}
            except json.JSONDecodeError:
                result["project_local"] = {"path": f"{wdir}/.claude/settings.local.json", "content": {}, "raw": content}

    return jsonify(result)


@app.route("/api/instances/<name>/settings", methods=["PUT"])
def api_save_settings(name):
    """Save a settings.json file."""
    inst = find_instance(name)
    if not inst:
        return jsonify({"error": "Not found"}), 404

    data = request.json or {}
    path = data.get("path", "")
    content = data.get("content", {})

    if not path:
        return jsonify({"error": "path required"}), 400

    json_str = json.dumps(content, indent=2)
    ok = write_remote_file(inst, path, json_str)
    if not ok:
        return jsonify({"error": "Write failed"}), 500

    return jsonify({"ok": True})


# ═══════════════════════════════════════════════════════════════════════════════
# API — SKILLS / CUSTOM COMMANDS
# ═══════════════════════════════════════════════════════════════════════════════

@app.route("/api/instances/<name>/skills")
def api_get_skills(name):
    """List custom commands and skills."""
    inst = find_instance(name)
    if not inst:
        return jsonify({"error": "Not found"}), 404

    status = instance_cache.get(name, {})
    wdir = status.get("working_dir", inst.get("working_dir", "~"))

    skills = []

    # Global custom commands
    rc, out, _ = run_cmd(inst, "ls ~/.claude/commands/*.md 2>/dev/null", timeout=5)
    if rc == 0 and out:
        for fpath in out.split("\n"):
            if fpath.strip():
                content = read_remote_file(inst, fpath.strip())
                skills.append({
                    "scope": "global",
                    "path": fpath.strip(),
                    "name": os.path.basename(fpath.strip()).replace(".md", ""),
                    "content": content or "",
                })

    # Project custom commands
    if wdir:
        rc, out, _ = run_cmd(inst, f"ls {wdir}/.claude/commands/*.md 2>/dev/null", timeout=5)
        if rc == 0 and out:
            for fpath in out.split("\n"):
                if fpath.strip():
                    content = read_remote_file(inst, fpath.strip())
                    skills.append({
                        "scope": "project",
                        "path": fpath.strip(),
                        "name": os.path.basename(fpath.strip()).replace(".md", ""),
                        "content": content or "",
                    })

    return jsonify(skills)


@app.route("/api/instances/<name>/skills", methods=["PUT"])
def api_save_skill(name):
    """Create or update a skill/command."""
    inst = find_instance(name)
    if not inst:
        return jsonify({"error": "Not found"}), 404

    data = request.json or {}
    path = data.get("path", "")
    content = data.get("content", "")

    if not path:
        return jsonify({"error": "path required"}), 400

    ok = write_remote_file(inst, path, content)
    return jsonify({"ok": ok})


@app.route("/api/instances/<name>/skills", methods=["DELETE"])
def api_delete_skill(name):
    inst = find_instance(name)
    if not inst:
        return jsonify({"error": "Not found"}), 404

    path = (request.json or {}).get("path", "")
    if not path:
        return jsonify({"error": "path required"}), 400

    rc, _, _ = run_cmd(inst, f"rm -f {path}")
    return jsonify({"ok": rc == 0})


# ═══════════════════════════════════════════════════════════════════════════════
# API — SESSION MONITORING
# ═══════════════════════════════════════════════════════════════════════════════

@app.route("/api/instances/<name>/session-log")
def api_session_log(name):
    """Get recent session activity/log entries."""
    inst = find_instance(name)
    if not inst:
        return jsonify({"error": "Not found"}), 404

    limit = request.args.get("limit", "50")

    # Get the latest log file and extract recent entries
    find_cmd = _find_latest_log_cmd(inst)
    if not find_cmd:
        return jsonify([])

    rc, out, _ = run_cmd(inst, f"""
        LATEST=$({find_cmd})
        if [ -n "$LATEST" ]; then
            tail -n {limit} "$LATEST"
        fi
    """, timeout=10)

    entries = []
    if out:
        for line in out.split("\n"):
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
                # Simplify for the frontend
                simplified = {
                    "type": entry.get("type", "unknown"),
                    "timestamp": entry.get("timestamp", ""),
                }
                if "message" in entry:
                    msg = entry["message"]
                    if isinstance(msg, dict):
                        simplified["role"] = msg.get("role", "")
                        content = msg.get("content", "")
                        if isinstance(content, str):
                            simplified["content_preview"] = content[:200]
                        elif isinstance(content, list):
                            texts = [c.get("text", "")[:100] for c in content if isinstance(c, dict) and c.get("type") == "text"]
                            simplified["content_preview"] = " ".join(texts)[:200]
                if "tool" in entry or "tool_name" in entry:
                    simplified["tool"] = entry.get("tool_name", entry.get("tool", ""))
                if "usage" in entry:
                    simplified["usage"] = entry["usage"]

                entries.append(simplified)
            except json.JSONDecodeError:
                continue

    return jsonify(entries)


@app.route("/api/instances/<name>/context")
def api_context(name):
    """Get detailed context window info."""
    inst = find_instance(name)
    if not inst:
        return jsonify({"error": "Not found"}), 404

    info = get_context_info(inst)
    return jsonify(info)


# ═══════════════════════════════════════════════════════════════════════════════
# API — MCP SERVERS
# ═══════════════════════════════════════════════════════════════════════════════

@app.route("/api/instances/<name>/mcp")
def api_get_mcp(name):
    """Get MCP server configuration."""
    inst = find_instance(name)
    if not inst:
        return jsonify({"error": "Not found"}), 404

    status = instance_cache.get(name, {})
    wdir = status.get("working_dir", inst.get("working_dir", "~"))

    result = {}

    # Global MCP config (~/.claude.json — note: outside .claude/)
    content = read_remote_file(inst, "~/.claude.json")
    if content:
        try:
            parsed = json.loads(content)
            if "mcpServers" in parsed:
                result["global"] = {"path": "~/.claude.json", "servers": parsed["mcpServers"]}
        except json.JSONDecodeError:
            pass

    # Project MCP config
    if wdir:
        content = read_remote_file(inst, f"{wdir}/.mcp.json")
        if content:
            try:
                parsed = json.loads(content)
                if "mcpServers" in parsed:
                    result["project"] = {"path": f"{wdir}/.mcp.json", "servers": parsed["mcpServers"]}
            except json.JSONDecodeError:
                pass

    return jsonify(result)


# ═══════════════════════════════════════════════════════════════════════════════
# TERMINAL WEBSOCKET
# ═══════════════════════════════════════════════════════════════════════════════

@sock.route("/api/instances/<name>/terminal")
def terminal_ws(ws, name):
    """Interactive terminal via WebSocket using pty."""
    print(f"[terminal] WebSocket connected for instance: {name}", file=sys.stderr)
    inst = find_instance(name)
    if not inst:
        print(f"[terminal] Instance not found: {name}", file=sys.stderr)
        ws.send("\r\nInstance not found.\r\n")
        return

    if inst["type"] == "remote":
        key = os.path.expanduser(inst["key"])
        cmd = ["ssh", "-tt", "-i", key,
               "-o", "StrictHostKeyChecking=no",
               "-o", "ConnectTimeout=10",
               f"{inst['user']}@{inst['host']}",
               "tmux attach-session -t claude || tmux new-session -s claude"]
    else:
        cmd = ["tmux", "attach-session", "-t", "claude"]

    child_pid, fd = pty.fork()
    if child_pid == 0:
        os.execvp(cmd[0], cmd)

    # Queue for pty output → main thread sends to WS
    outq = queue.Queue()
    alive = threading.Event()
    alive.set()

    def pty_reader():
        """Background thread: read from pty, enqueue for main thread."""
        try:
            while alive.is_set():
                r, _, _ = select.select([fd], [], [], 0.1)
                if r:
                    try:
                        data = os.read(fd, 16384)
                        if not data:
                            break
                        outq.put(data)
                    except OSError:
                        break
        except Exception:
            pass
        finally:
            alive.clear()

    reader_thread = threading.Thread(target=pty_reader, daemon=True)
    reader_thread.start()

    # Main thread: all WS operations happen here
    print(f"[terminal] Entering main loop for {name}, pty fd={fd}, pid={child_pid}", file=sys.stderr)
    try:
        while alive.is_set():
            # Drain pty output queue → send to browser
            while True:
                try:
                    data = outq.get_nowait()
                    ws.send(data)
                except queue.Empty:
                    break

            # Read from browser → write to pty (short timeout)
            try:
                msg = ws.receive(timeout=0.05)
            except Exception as e:
                etype = type(e).__name__
                if "Closed" in etype or "close" in str(e).lower():
                    print(f"[terminal] WS closed for {name}: {e}", file=sys.stderr)
                    break
                continue
            if msg is None:
                continue
            if isinstance(msg, str):
                if msg.startswith("\x01RESIZE:"):
                    try:
                        parts = msg[8:].split(",")
                        cols, rows = int(parts[0]), int(parts[1])
                        winsize = struct.pack("HHHH", rows, cols, 0, 0)
                        fcntl.ioctl(fd, termios.TIOCSWINSZ, winsize)
                    except (ValueError, IndexError, OSError):
                        pass
                else:
                    os.write(fd, msg.encode())
            else:
                os.write(fd, msg)
    except Exception as e:
        print(f"[terminal] Main loop exception for {name}: {type(e).__name__}: {e}", file=sys.stderr)
    finally:
        print(f"[terminal] Cleaning up for {name}", file=sys.stderr)
        alive.clear()
        reader_thread.join(timeout=2)
        try:
            os.close(fd)
        except OSError:
            pass
        try:
            os.kill(child_pid, signal.SIGTERM)
            os.waitpid(child_pid, 0)
        except (OSError, ChildProcessError):
            pass


# ═══════════════════════════════════════════════════════════════════════════════
# STATIC FILES
# ═══════════════════════════════════════════════════════════════════════════════

@app.route("/")
def index():
    return send_from_directory("static", "index.html")


@app.route("/<path:path>")
def static_files(path):
    return send_from_directory("static", path)


# ═══════════════════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════════════════

def main():
    port = int(os.environ.get("CCM_PORT", 8420))

    poller = threading.Thread(target=poll_loop, daemon=True)
    poller.start()

    print(f"\n  ⚡ Claude Roster — http://localhost:{port}\n")

    def open_browser():
        time.sleep(1.5)
        webbrowser.open(f"http://localhost:{port}")
    threading.Thread(target=open_browser, daemon=True).start()

    app.run(host="0.0.0.0", port=port, debug=False)


if __name__ == "__main__":
    main()
