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
SESSIONS_PATH = os.path.join(os.path.dirname(__file__), "sessions.json")
instance_cache: dict[str, dict] = {}
sessions_meta: dict[str, dict] = {}


def _load_sessions():
    """Load persisted sessions_meta from disk."""
    global sessions_meta
    if os.path.isfile(SESSIONS_PATH):
        try:
            with open(SESSIONS_PATH, "r") as f:
                sessions_meta = json.load(f)
        except (json.JSONDecodeError, IOError):
            sessions_meta = {}


def _save_sessions():
    """Persist sessions_meta to disk."""
    try:
        with open(SESSIONS_PATH, "w") as f:
            json.dump(sessions_meta, f, indent=2)
    except IOError as e:
        print(f"[sessions] Failed to save: {e}", file=sys.stderr)


# ═══════════════════════════════════════════════════════════════════════════════
# CONFIG
# ═══════════════════════════════════════════════════════════════════════════════

def _ensure_config():
    """Create config.yaml with empty instances list if it doesn't exist."""
    if not os.path.isfile(CONFIG_PATH):
        with open(CONFIG_PATH, "w") as f:
            yaml.dump({"instances": []}, f, default_flow_style=False)


def load_config() -> dict:
    _ensure_config()
    with open(CONFIG_PATH, "r") as f:
        return yaml.safe_load(f) or {"instances": []}


def save_config(cfg: dict):
    """Write config back to disk."""
    with open(CONFIG_PATH, "w") as f:
        yaml.dump(cfg, f, default_flow_style=False, sort_keys=False)


def get_instances() -> list[dict]:
    return load_config().get("instances", [])


def find_instance(name: str) -> dict | None:
    return next((i for i in get_instances() if i["name"] == name), None)


def sanitize_name(name: str) -> str:
    """Sanitize instance name for use in tmux sessions and Docker containers."""
    return re.sub(r'[^a-zA-Z0-9_-]', '-', name).strip('-').lower()


def tmux_session_name(inst: dict) -> str:
    """Return the tmux session name for an instance, namespaced to avoid
    collisions when multiple instances share the same host."""
    return f"claude-{sanitize_name(inst['name'])}"


# ═══════════════════════════════════════════════════════════════════════════════
# COMMAND EXECUTION (SSH for remote, subprocess for local)
# ═══════════════════════════════════════════════════════════════════════════════

def run_cmd(inst: dict, command: str, timeout: int = 10) -> tuple[int, str, str]:
    """Run a command on an instance. SSH for remote, local shell for local."""
    env_vars = inst.get("env", {})

    if inst["type"] == "remote":
        # Prepend env vars as export statements for remote commands
        if env_vars:
            exports = " ".join(f"{k}={v}" for k, v in env_vars.items())
            command = f"export {exports}; {command}"
        cmd = [
            "ssh", "-A",
            "-o", "StrictHostKeyChecking=no",
            "-o", "ConnectTimeout=5",
            "-o", "BatchMode=yes",
        ]
        if "key" in inst:
            cmd += ["-i", os.path.expanduser(inst["key"])]
        cmd += [f"{inst['user']}@{inst['host']}", command]
    else:
        cmd = ["bash", "-c", command]

    # Build subprocess env: inherit current env + instance env vars
    run_env = None
    if env_vars and inst["type"] == "local":
        run_env = {**os.environ, **env_vars}

    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=run_env)
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
        "config_repo": inst.get("repo", ""),
        "env": inst.get("env", {}),
    }

    # Check reachability
    if inst["type"] == "remote":
        rc, _, err = run_cmd(inst, "echo ok", timeout=5)
        if rc != 0:
            return {**base, "reachable": False, "error": err, "has_session": False}
    else:
        base["reachable"] = True

    # Check for tmux claude session
    sess = tmux_session_name(inst)
    rc, out, _ = run_cmd(inst, f"tmux has-session -t {sess} 2>/dev/null && echo yes || echo no")
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
            pane_pid=$(tmux list-panes -t %s -F '#{pane_pid}' 2>/dev/null | head -1)
            if [ -n "$pane_pid" ]; then
                cwd=$(readlink -f /proc/$pane_pid/cwd 2>/dev/null || echo "")
                echo "CWD:${cwd}"
                if [ -n "$cwd" ] && [ -d "$cwd/.git" ]; then
                    echo "REPO:$(basename $cwd)"
                    echo "BRANCH:$(git -C $cwd branch --show-current 2>/dev/null)"
                fi
            fi
        """ % sess)
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

            # Clean up sessions whose tmux session no longer exists
            stale = [n for n in sessions_meta if n in results and not results[n].get("has_session")]
            if stale:
                for n in stale:
                    sessions_meta.pop(n, None)
                _save_sessions()
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


start_status: dict[str, str] = {}

@app.route("/api/instances/<name>/start-status")
def api_start_status(name):
    return jsonify({"stage": start_status.get(name, "")})

@app.route("/api/instances/<name>/start", methods=["POST"])
def api_start(name):
    inst = find_instance(name)
    if not inst:
        return jsonify({"error": "Not found"}), 404

    data = request.json or {}
    repo = data.get("repo", "") or inst.get("repo", "")
    task = data.get("task", "")
    wdir = data.get("working_dir", inst.get("working_dir", ""))

    try:
        # Pre-flight: verify SSH connectivity for remote instances
        if inst["type"] == "remote":
            start_status[name] = "Checking SSH connectivity..."
            rc, _, err = run_cmd(inst, "echo ok", timeout=10)
            if rc != 0:
                return jsonify({"error": f"SSH connection failed: {err}"}), 500

        # Sync repo if configured (clone or pull)
        if repo:
            start_status[name] = "Syncing repository..."
            if inst["type"] == "remote" and repo.startswith("git@"):
                if not _ensure_deploy_key(inst):
                    # No deploy key configured — check if SSH agent forwarding works
                    rc, out, err = run_cmd(inst, "ssh -o StrictHostKeyChecking=no -T git@github.com 2>&1; true", timeout=10)
                    # GitHub returns exit code 1 with "successfully authenticated" on success
                    combined = f"{out} {err}"
                    if "successfully authenticated" not in combined:
                        return jsonify({"error": "Git SSH auth failed: no deploy_key in config and SSH agent forwarding is not working. "
                                        "Either add deploy_key to the instance config or ensure ssh-agent has your key loaded (ssh-add)."}), 400
            repo_name = repo.rstrip("/").split("/")[-1].replace(".git", "")
            if not wdir:
                wdir = f"~/{repo_name}"
            rc, out, err = run_cmd(inst, _build_sync_cmd(repo, wdir), timeout=120)
            action, sync_rc = _parse_sync_result(rc, out)
            if sync_rc != 0:
                return jsonify({"error": f"Git {action} failed: {err or out}"}), 500

        # Ship Claude auth credentials to the VM
        start_status[name] = "Shipping credentials..."
        _ensure_claude_auth(inst)

        start_dir = wdir or "~"
        sess = tmux_session_name(inst)
        # Kill any leftover tmux session and docker container first
        cname = sanitize_name(name)
        run_cmd(inst, f"tmux kill-session -t {sess} 2>/dev/null; true", timeout=5)
        if inst.get("runtime") == "docker":
            run_cmd(inst, f"docker rm -f claude-{cname} 2>/dev/null; true", timeout=10)

        # Build env export prefix for the tmux session
        env_vars = inst.get("env", {})
        env_prefix = ""
        if env_vars:
            exports = " ".join(f'export {k}="{v}";' for k, v in env_vars.items())
            env_prefix = exports + " "

        # Pre-seed workspace trust in .claude.json so the trust dialog is skipped
        if inst.get("runtime") == "docker":
            run_cmd(inst, """python3 -c "
import json, os
p = os.path.expanduser('~/.claude.json')
try:
    d = json.load(open(p))
except: d = {}
proj = d.setdefault('projects', {})
ws = proj.setdefault('/workspace', {})
ws['hasTrustDialogAccepted'] = True
d['hasCompletedOnboarding'] = True
json.dump(d, open(p, 'w'), indent=2)
" """, timeout=10)

        # Ensure docker image exists on target if needed
        if inst.get("runtime") == "docker":
            start_status[name] = "Checking Docker image..."
            ok, img_err = _ensure_docker_image(inst)
            if not ok:
                return jsonify({"error": f"Docker image setup failed: {img_err}"}), 500

        # Build the claude launch command based on runtime
        start_status[name] = "Launching session..."
        if inst.get("runtime") == "docker":
            # Run Claude Code in Docker, mounting the working dir and config
            # Only mount .claude.json if it exists; use -it for tmux TTY
            claude_cmd = (
                f'SDIR="{start_dir}"; SDIR="${{SDIR/#~/$HOME}}"; '
                f'mkdir -p "$HOME/.claude"; '
                f'MOUNTS="-v $SDIR:/workspace -v $HOME/.claude:/root/.claude"; '
                f'[ -f "$HOME/.ssh/deploy_key" ] && MOUNTS="$MOUNTS -v $HOME/.ssh/deploy_key:/root/.ssh/deploy_key:ro"; '
                f'[ -f "$HOME/.gitconfig" ] && MOUNTS="$MOUNTS -v $HOME/.gitconfig:/root/.gitconfig:ro"; '
                f'[ -f "$HOME/.claude.json" ] && MOUNTS="$MOUNTS -v $HOME/.claude.json:/root/.claude.json"; '
                f'{env_prefix}docker run --rm --init -it --name claude-{cname} $MOUNTS claude-code'
            )
        else:
            claude_cmd = f'{env_prefix}SDIR="{start_dir}"; SDIR="${{SDIR/#~/$HOME}}"; cd "$SDIR" && claude'
        rc, _, err = run_cmd(inst, f"tmux new-session -d -s {sess} '{claude_cmd}'", timeout=30)
        if rc != 0:
            return jsonify({"error": err}), 500
    finally:
        start_status.pop(name, None)

    sessions_meta[name] = {
        "repo": repo.rstrip("/").split("/")[-1].replace(".git", "") if repo else "",
        "task": task,
        "started_at": datetime.now(timezone.utc).isoformat(),
    }
    _save_sessions()
    instance_cache[name] = check_instance(inst)
    return jsonify({"ok": True})


@app.route("/api/instances/<name>/stop", methods=["POST"])
def api_stop(name):
    inst = find_instance(name)
    if not inst:
        return jsonify({"error": "Not found"}), 404
    sess = tmux_session_name(inst)
    cname = sanitize_name(name)
    run_cmd(inst, f"tmux kill-session -t {sess} 2>/dev/null; echo done")
    if inst.get("runtime") == "docker":
        run_cmd(inst, f"docker rm -f claude-{cname} 2>/dev/null; true", timeout=10)
    sessions_meta.pop(name, None)
    _save_sessions()
    instance_cache[name] = check_instance(inst)
    return jsonify({"ok": True})


@app.route("/api/instances/<name>/connect")
def api_connect(name):
    inst = find_instance(name)
    if not inst:
        return jsonify({"error": "Not found"}), 404

    sess = tmux_session_name(inst)
    if inst["type"] == "remote":
        key_part = f"-i {os.path.expanduser(inst['key'])} " if "key" in inst else ""
        cmd = f"ssh -A {key_part}-t {inst['user']}@{inst['host']} 'tmux attach-session -t {sess}'"
    else:
        cmd = f"tmux attach-session -t {sess}"

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
# API — INSTANCE CRUD (config management)
# ═══════════════════════════════════════════════════════════════════════════════

@app.route("/api/instances", methods=["POST"])
def api_create_instance():
    """Create a new instance in config.yaml."""
    data = request.json or {}
    name = data.get("name", "").strip()
    if not name:
        return jsonify({"error": "name is required"}), 400

    cfg = load_config()
    instances = cfg.get("instances", [])

    if any(i["name"] == name for i in instances):
        return jsonify({"error": f"Instance '{name}' already exists"}), 409

    inst_type = data.get("type", "local")
    inst = {"name": name, "type": inst_type}

    if inst_type == "remote":
        for field in ["host", "user", "key"]:
            val = data.get(field, "").strip()
            if not val:
                return jsonify({"error": f"{field} is required for remote instances"}), 400
            inst[field] = val
        inst["provider"] = data.get("provider", "gcp").strip() or "gcp"
    else:
        inst["working_dir"] = data.get("working_dir", "").strip()
        inst["provider"] = "local"

    if data.get("repo"):
        inst["repo"] = data["repo"].strip()
    if data.get("deploy_key"):
        inst["deploy_key"] = data["deploy_key"].strip()
    if data.get("runtime"):
        inst["runtime"] = data["runtime"].strip()
    if data.get("env") and isinstance(data["env"], dict):
        inst["env"] = {k.strip(): v.strip() for k, v in data["env"].items() if k.strip()}

    instances.append(inst)
    cfg["instances"] = instances
    save_config(cfg)

    # Trigger initial status check
    status = check_instance(inst)
    instance_cache[name] = status
    return jsonify({"ok": True, "instance": status}), 201


@app.route("/api/instances/<name>", methods=["PUT"])
def api_update_instance(name):
    """Update an existing instance in config.yaml."""
    data = request.json or {}
    cfg = load_config()
    instances = cfg.get("instances", [])

    idx = next((i for i, inst in enumerate(instances) if inst["name"] == name), None)
    if idx is None:
        return jsonify({"error": "Not found"}), 404

    inst = instances[idx]
    inst_type = data.get("type", inst["type"])
    inst["type"] = inst_type

    if inst_type == "remote":
        for field in ["host", "user", "key"]:
            if field in data:
                inst[field] = data[field].strip()
        if "provider" in data:
            inst["provider"] = data["provider"].strip() or "gcp"
    else:
        if "working_dir" in data:
            inst["working_dir"] = data["working_dir"].strip()
        inst["provider"] = "local"
        # Clean up remote-only fields
        for field in ["host", "user", "key"]:
            inst.pop(field, None)

    for field in ["repo", "deploy_key", "runtime"]:
        if field in data:
            val = data[field].strip() if data[field] else ""
            if val:
                inst[field] = val
            else:
                inst.pop(field, None)

    if "env" in data:
        if isinstance(data["env"], dict) and data["env"]:
            inst["env"] = {k.strip(): v.strip() for k, v in data["env"].items() if k.strip()}
        else:
            inst.pop("env", None)

    instances[idx] = inst
    cfg["instances"] = instances
    save_config(cfg)

    # Refresh status
    status = check_instance(inst)
    instance_cache[name] = status
    return jsonify({"ok": True, "instance": status})


@app.route("/api/instances/<name>", methods=["DELETE"])
def api_delete_instance(name):
    """Remove an instance from config.yaml."""
    cfg = load_config()
    instances = cfg.get("instances", [])

    new_instances = [i for i in instances if i["name"] != name]
    if len(new_instances) == len(instances):
        return jsonify({"error": "Not found"}), 404

    cfg["instances"] = new_instances
    save_config(cfg)

    instance_cache.pop(name, None)
    sessions_meta.pop(name, None)
    return jsonify({"ok": True})


@app.route("/api/instances/<name>/config")
def api_get_instance_config(name):
    """Get raw config for an instance (for editing)."""
    inst = find_instance(name)
    if not inst:
        return jsonify({"error": "Not found"}), 404
    return jsonify(inst)


# ═══════════════════════════════════════════════════════════════════════════════
# API — PROVISIONING (auth, keys)
# ═══════════════════════════════════════════════════════════════════════════════

def _ensure_docker_image(inst: dict) -> tuple[bool, str]:
    """Ensure the claude-code Docker image exists on the instance.
    Builds it from Dockerfile.claude if missing. Returns (success, error_msg)."""
    rc, out, _ = run_cmd(inst, "docker image inspect claude-code >/dev/null 2>&1 && echo EXISTS", timeout=10)
    if "EXISTS" in (out or ""):
        return True, ""

    # Read local Dockerfile.claude
    dockerfile_path = os.path.join(os.path.dirname(__file__), "Dockerfile.claude")
    if not os.path.isfile(dockerfile_path):
        return False, "Dockerfile.claude not found locally"

    with open(dockerfile_path, "r") as f:
        dockerfile_content = f.read()

    # Ship Dockerfile to remote and build
    start_status[inst["name"]] = "Building Docker image (this may take a few minutes)..."
    write_remote_file(inst, "/tmp/Dockerfile.claude", dockerfile_content)
    rc, out, err = run_cmd(inst, "docker build -t claude-code -f /tmp/Dockerfile.claude /tmp", timeout=600)
    if rc != 0:
        return False, f"Docker build failed: {err or out}"
    return True, ""


def _ensure_claude_auth(inst: dict) -> bool:
    """Copy local Claude credentials to the remote VM so it can skip login.
    Returns True if credentials were shipped (or already exist remotely)."""
    if inst["type"] != "remote":
        return True

    local_creds = os.path.expanduser("~/.claude/.credentials.json")
    if not os.path.isfile(local_creds):
        return False

    with open(local_creds, "r") as f:
        creds_content = f.read()

    rc, out, _ = run_cmd(inst, """
        mkdir -p ~/.claude
        cat > ~/.claude/.credentials.json << 'CREDEOF'
%s
CREDEOF
        chmod 600 ~/.claude/.credentials.json
        echo "AUTH:ok"
    """ % creds_content, timeout=10)
    return "AUTH:ok" in (out or "")


def _ensure_deploy_key(inst: dict) -> bool:
    """Ship the deploy_key to the remote VM and configure git to use it.
    Returns True if a deploy key is available on the remote, False otherwise."""
    deploy_key = inst.get("deploy_key", "")
    if not deploy_key:
        return False

    local_key = os.path.expanduser(deploy_key)
    if not os.path.isfile(local_key):
        return False

    # Read local key content
    with open(local_key, "r") as f:
        key_content = f.read()

    # Write key to remote ~/.ssh/deploy_key, set permissions, configure ssh
    setup_cmd = """
        mkdir -p ~/.ssh
        cat > ~/.ssh/deploy_key << 'KEYEOF'
%s
KEYEOF
        chmod 600 ~/.ssh/deploy_key
        # Configure git to use this key for all SSH operations
        git config --global core.sshCommand "ssh -i ~/.ssh/deploy_key -o StrictHostKeyChecking=no"
        echo "DEPLOY_KEY:ok"
    """ % key_content
    rc, out, _ = run_cmd(inst, setup_cmd, timeout=15)
    return "DEPLOY_KEY:ok" in (out or "")


def _build_sync_cmd(repo_url: str, working_dir: str) -> str:
    """Build the shell command to clone or pull a repo."""
    return f"""
        WDIR="{working_dir}"
        WDIR="${{WDIR/#~/$HOME}}"
        if [ -d "$WDIR/.git" ]; then
            cd "$WDIR" && git pull 2>&1
            SYNC_EXIT=$?
            echo "SYNC_ACTION:pull"
        elif [ -d "$WDIR" ] && [ ! -w "$WDIR" ]; then
            echo "Directory $WDIR exists but is not writable (owned by $(stat -c '%U' "$WDIR" 2>/dev/null || echo unknown))" 2>&1
            SYNC_EXIT=1
            echo "SYNC_ACTION:clone"
        else
            git clone {repo_url} "$WDIR" 2>&1
            SYNC_EXIT=$?
            echo "SYNC_ACTION:clone"
        fi
        echo "SYNC_RC:$SYNC_EXIT"
    """


def _parse_sync_result(rc: int, out: str) -> tuple[str, int]:
    """Parse action and return code from sync command output."""
    action = "unknown"
    sync_rc = rc
    for line in (out or "").split("\n"):
        if line.startswith("SYNC_ACTION:"):
            action = line[12:]
        elif line.startswith("SYNC_RC:"):
            try:
                sync_rc = int(line[8:])
            except ValueError:
                pass
    return action, sync_rc


@app.route("/api/instances/<name>/sync", methods=["POST"])
def api_sync(name):
    """Clone or pull a git repo on the remote instance."""
    inst = find_instance(name)
    if not inst:
        return jsonify({"error": "Not found"}), 404

    data = request.json or {}
    repo_url = data.get("repo", inst.get("repo", ""))
    working_dir = data.get("working_dir", inst.get("working_dir", ""))

    if not repo_url:
        return jsonify({"error": "No repo URL configured"}), 400

    # Ship deploy key to remote if configured
    if inst["type"] == "remote" and repo_url.startswith("git@"):
        if not _ensure_deploy_key(inst):
            # No deploy key — check SSH agent forwarding
            rc, out, err = run_cmd(inst, "ssh -o StrictHostKeyChecking=no -T git@github.com 2>&1; true", timeout=10)
            combined = f"{out} {err}"
            if "successfully authenticated" not in combined:
                return jsonify({"error": "Git SSH auth failed: no deploy_key in config and SSH agent forwarding is not working. "
                                "Either add deploy_key to the instance config or ensure ssh-agent has your key loaded (ssh-add)."}), 400

    # Derive target directory from repo URL if no working_dir set
    repo_name = repo_url.rstrip("/").split("/")[-1].replace(".git", "")
    if not working_dir:
        working_dir = f"~/{repo_name}"

    rc, out, err = run_cmd(inst, _build_sync_cmd(repo_url, working_dir), timeout=120)
    action, sync_rc = _parse_sync_result(rc, out)

    if sync_rc != 0:
        return jsonify({"error": f"Git {action} failed: {err or out}"}), 500

    # Refresh instance status
    instance_cache[name] = check_instance(inst)
    return jsonify({"ok": True, "action": action, "working_dir": working_dir, "output": out})


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

    # Global skills
    rc, out, _ = run_cmd(inst, "ls ~/.claude/skills/*/SKILL.md 2>/dev/null", timeout=5)
    if rc == 0 and out:
        for fpath in out.split("\n"):
            if fpath.strip():
                content = read_remote_file(inst, fpath.strip())
                skills.append({
                    "scope": "global",
                    "path": fpath.strip(),
                    "name": Path(fpath.strip()).parent.name,
                    "content": content or "",
                })

    # Project skills
    if wdir:
        rc, out, _ = run_cmd(inst, f"ls {wdir}/.claude/skills/*/SKILL.md 2>/dev/null", timeout=5)
        if rc == 0 and out:
            for fpath in out.split("\n"):
                if fpath.strip():
                    content = read_remote_file(inst, fpath.strip())
                    skills.append({
                        "scope": "project",
                        "path": fpath.strip(),
                        "name": Path(fpath.strip()).parent.name,
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

    # Ensure the skill directory exists (e.g. .claude/skills/my-skill/)
    parent = str(Path(path).parent)
    run_cmd(inst, f"mkdir -p {parent}", timeout=5)
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

    # Remove the entire skill directory (e.g. .claude/skills/my-skill/)
    skill_dir = str(Path(path).parent)
    rc, _, _ = run_cmd(inst, f"rm -rf {skill_dir}")
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

    sess = tmux_session_name(inst)
    if inst["type"] == "remote":
        cmd = ["ssh", "-A", "-tt",
               "-o", "StrictHostKeyChecking=no",
               "-o", "ConnectTimeout=10"]
        if "key" in inst:
            cmd += ["-i", os.path.expanduser(inst["key"])]
        cmd += [f"{inst['user']}@{inst['host']}",
                f"tmux attach-session -t {sess} || tmux new-session -s {sess}"]
    else:
        cmd = ["tmux", "attach-session", "-t", sess]

    child_pid, fd = pty.fork()
    if child_pid == 0:
        os.execvp(cmd[0], cmd)

    # Set a sane default PTY size immediately (before client resize arrives)
    try:
        winsize = struct.pack("HHHH", 24, 80, 0, 0)
        fcntl.ioctl(fd, termios.TIOCSWINSZ, winsize)
    except OSError:
        pass

    # Wait briefly for the client's initial RESIZE message with actual dimensions
    try:
        msg = ws.receive(timeout=2)
        if msg and isinstance(msg, str) and msg.startswith("\x01RESIZE:"):
            parts = msg[8:].split(",")
            cols, rows = int(parts[0]), int(parts[1])
            winsize = struct.pack("HHHH", rows, cols, 0, 0)
            fcntl.ioctl(fd, termios.TIOCSWINSZ, winsize)
    except Exception:
        pass

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

    _load_sessions()
    if sessions_meta:
        print(f"  Restored {len(sessions_meta)} session(s) from disk")

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
