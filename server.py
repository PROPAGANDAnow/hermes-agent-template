"""
Hermes Agent — Railway admin server.

Serves an admin UI on $PORT, manages the Hermes gateway as a subprocess.
The gateway is started automatically on boot if a provider API key is present.
"""

import asyncio
import base64
import json
import os
import re
import secrets
import signal
import time
from collections import deque
from contextlib import asynccontextmanager
from pathlib import Path

from starlette.applications import Starlette
from starlette.authentication import (
    AuthCredentials,
    AuthenticationBackend,
    AuthenticationError,
    SimpleUser,
)
from starlette.middleware import Middleware
from starlette.middleware.authentication import AuthenticationMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse
from starlette.routing import Route
from starlette.templating import Jinja2Templates

from cron.jobs import get_job, list_jobs, pause_job, remove_job, resume_job, trigger_job
from cron.scheduler import tick as cron_tick
from hermes_cli.config import load_config
from tools.process_registry import process_registry
from tools.terminal_tool import terminal_tool

ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;]*m")
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

HERMES_HOME = os.environ.get("HERMES_HOME", str(Path.home() / ".hermes"))
ENV_FILE = Path(HERMES_HOME) / ".env"
PAIRING_DIR = Path(HERMES_HOME) / "pairing"
PAIRING_TTL = 3600
TERMINAL_TASK_ID = "hermes-admin-terminal"
TERMINAL_BOOT_COMMAND = os.environ.get("ADMIN_TERMINAL_COMMAND", "bash -i")
CRON_OUTPUT_DIR = Path(HERMES_HOME) / "cron" / "output"
MAX_CRON_OUTPUT_BYTES = 100_000
MAX_FILE_READ_BYTES = 100_000
FILE_BROWSER_ROOTS = {
    "data": Path("/data"),
    "hermes": Path(HERMES_HOME),
    "app": Path(__file__).parent.resolve(),
}

ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")
if not ADMIN_PASSWORD:
    ADMIN_PASSWORD = secrets.token_urlsafe(16)
    print(f"[server] Admin credentials — username: {ADMIN_USERNAME}  password: {ADMIN_PASSWORD}", flush=True)
else:
    print(f"[server] Admin username: {ADMIN_USERNAME}", flush=True)

# ── Env var registry ──────────────────────────────────────────────────────────
# (key, label, category, is_secret)
ENV_VARS = [
    ("LLM_MODEL",               "Model",                    "model",     False),
    ("OPENROUTER_API_KEY",       "OpenRouter",               "provider",  True),
    ("DEEPSEEK_API_KEY",         "DeepSeek",                 "provider",  True),
    ("DASHSCOPE_API_KEY",        "DashScope",                "provider",  True),
    ("GLM_API_KEY",              "GLM / Z.AI",               "provider",  True),
    ("KIMI_API_KEY",             "Kimi",                     "provider",  True),
    ("MINIMAX_API_KEY",          "MiniMax",                  "provider",  True),
    ("HF_TOKEN",                 "Hugging Face",             "provider",  True),
    ("PARALLEL_API_KEY",         "Parallel (search)",        "tool",      True),
    ("FIRECRAWL_API_KEY",        "Firecrawl (scrape)",       "tool",      True),
    ("TAVILY_API_KEY",           "Tavily (search)",          "tool",      True),
    ("FAL_KEY",                  "FAL (image gen)",          "tool",      True),
    ("BROWSERBASE_API_KEY",      "Browserbase key",          "tool",      True),
    ("BROWSERBASE_PROJECT_ID",   "Browserbase project",      "tool",      False),
    ("GITHUB_TOKEN",             "GitHub token",             "tool",      True),
    ("VOICE_TOOLS_OPENAI_KEY",   "OpenAI (voice/TTS)",       "tool",      True),
    ("HONCHO_API_KEY",           "Honcho (memory)",          "tool",      True),
    ("TELEGRAM_BOT_TOKEN",       "Bot Token",                "telegram",  True),
    ("TELEGRAM_ALLOWED_USERS",   "Allowed User IDs",         "telegram",  False),
    ("DISCORD_BOT_TOKEN",        "Bot Token",                "discord",   True),
    ("DISCORD_ALLOWED_USERS",    "Allowed User IDs",         "discord",   False),
    ("SLACK_BOT_TOKEN",          "Bot Token (xoxb-...)",     "slack",     True),
    ("SLACK_APP_TOKEN",          "App Token (xapp-...)",     "slack",     True),
    ("WHATSAPP_ENABLED",         "Enable WhatsApp",          "whatsapp",  False),
    ("EMAIL_ADDRESS",            "Email Address",            "email",     False),
    ("EMAIL_PASSWORD",           "Email Password",           "email",     True),
    ("EMAIL_IMAP_HOST",          "IMAP Host",                "email",     False),
    ("EMAIL_SMTP_HOST",          "SMTP Host",                "email",     False),
    ("MATTERMOST_URL",           "Server URL",               "mattermost",False),
    ("MATTERMOST_TOKEN",         "Bot Token",                "mattermost",True),
    ("MATRIX_HOMESERVER",        "Homeserver URL",           "matrix",    False),
    ("MATRIX_ACCESS_TOKEN",      "Access Token",             "matrix",    True),
    ("MATRIX_USER_ID",           "User ID",                  "matrix",    False),
    ("GATEWAY_ALLOW_ALL_USERS",  "Allow all users",          "gateway",   False),
    ("ADMIN_USERNAME",           "Admin username",           "admin",     False),
    ("ADMIN_PASSWORD",           "Admin password",           "admin",     True),
]

SECRET_KEYS  = {k for k, _, _, s in ENV_VARS if s}
PROVIDER_KEYS = [k for k, _, c, _ in ENV_VARS if c == "provider"]
CHANNEL_MAP  = {
    "Telegram":    "TELEGRAM_BOT_TOKEN",
    "Discord":     "DISCORD_BOT_TOKEN",
    "Slack":       "SLACK_BOT_TOKEN",
    "WhatsApp":    "WHATSAPP_ENABLED",
    "Email":       "EMAIL_ADDRESS",
    "Mattermost":  "MATTERMOST_TOKEN",
    "Matrix":      "MATRIX_ACCESS_TOKEN",
}


# ── .env helpers ──────────────────────────────────────────────────────────────
def read_env(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    out = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        v = v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in ('"', "'"):
            v = v[1:-1]
        out[k.strip()] = v
    return out


def write_config_yaml(data: dict[str, str]) -> None:
    """Write config.yaml with the settings the template relies on."""
    model = data.get("LLM_MODEL", "")
    config_path = Path(HERMES_HOME) / "config.yaml"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(f"""\
model:
  default: "{model}"
  provider: "auto"

terminal:
  backend: "local"
  timeout: 60
  cwd: "/tmp"

agent:
  max_iterations: 50

aux_models:
  approval:
    provider: "auto"
    model: ""
    base_url: ""
    api_key: ""
    timeout: 30

display:
  compact: true
  personality: "kawaii"
  resume_display: "full"
  busy_input_mode: "interrupt"
  bell_on_complete: false
  show_reasoning: false
  streaming: true
  inline_diffs: true
  show_cost: false
  skin: "default"
  tool_progress: "off"
  interim_assistant_messages: true
  tool_progress_command: false
  tool_preview_length: 0
  background_process_notifications: "all"
  platforms: {{}}

approvals:
  mode: "off"
  timeout: 60

data_dir: "{HERMES_HOME}"
""")


def write_env(path: Path, data: dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    cat_order = ["model", "provider", "tool",
                 "telegram", "discord", "slack", "whatsapp",
                 "email", "mattermost", "matrix", "gateway"]
    cat_labels = {
        "model": "Model", "provider": "Providers", "tool": "Tools",
        "telegram": "Telegram", "discord": "Discord", "slack": "Slack",
        "whatsapp": "WhatsApp", "email": "Email",
        "mattermost": "Mattermost", "matrix": "Matrix", "gateway": "Gateway",
    }
    key_cat = {k: c for k, _, c, _ in ENV_VARS}
    grouped: dict[str, list[str]] = {c: [] for c in cat_order}
    grouped["other"] = []

    for k, v in data.items():
        if not v:
            continue
        cat = key_cat.get(k, "other")
        grouped.setdefault(cat, []).append(f"{k}={v}")

    lines: list[str] = []
    for cat in cat_order:
        entries = sorted(grouped.get(cat, []))
        if entries:
            lines.append(f"# {cat_labels.get(cat, cat)}")
            lines.extend(entries)
            lines.append("")
    if grouped["other"]:
        lines.append("# Other")
        lines.extend(sorted(grouped["other"]))
        lines.append("")

    path.write_text("\n".join(lines))


def mask(data: dict[str, str]) -> dict[str, str]:
    return {
        k: (v[:8] + "***" if len(v) > 8 else "***") if k in SECRET_KEYS and v else v
        for k, v in data.items()
    }


def unmask(new: dict[str, str], existing: dict[str, str]) -> dict[str, str]:
    return {
        k: (existing.get(k, "") if k in SECRET_KEYS and v.endswith("***") else v)
        for k, v in new.items()
    }


# ── Auth ──────────────────────────────────────────────────────────────────────
class BasicAuth(AuthenticationBackend):
    async def authenticate(self, conn):
        if "Authorization" not in conn.headers:
            return None
        try:
            scheme, creds = conn.headers["Authorization"].split()
            if scheme.lower() != "basic":
                return None
            user, _, pw = base64.b64decode(creds).decode().partition(":")
        except Exception:
            raise AuthenticationError("Invalid credentials")
        if user == ADMIN_USERNAME and pw == ADMIN_PASSWORD:
            return AuthCredentials(["authenticated"]), SimpleUser(user)
        raise AuthenticationError("Invalid credentials")


def guard(request: Request):
    if not request.user.is_authenticated:
        return PlainTextResponse(
            "Unauthorized", status_code=401,
            headers={"WWW-Authenticate": 'Basic realm="hermes-admin"'},
        )


# ── Gateway manager ───────────────────────────────────────────────────────────
class Gateway:
    def __init__(self):
        self.proc: asyncio.subprocess.Process | None = None
        self.state = "stopped"
        self.logs: deque[str] = deque(maxlen=500)
        self.started_at: float | None = None
        self.restarts = 0

    async def start(self):
        if self.proc and self.proc.returncode is None:
            return
        self.state = "starting"
        try:
            # .env values take priority over Railway env vars.
            # We build the env this way so hermes's own dotenv loading
            # (which reads the same file) doesn't shadow our values.
            env = {**os.environ, "HERMES_HOME": HERMES_HOME}
            env.update(read_env(ENV_FILE))
            model = env.get("LLM_MODEL", "")
            provider_key = next((env.get(k, "") for k in PROVIDER_KEYS if env.get(k)), "")
            print(f"[gateway] model={model or '⚠ NOT SET'} | provider_key={'set' if provider_key else '⚠ NOT SET'}", flush=True)
            # Write config.yaml so hermes picks up the model (env vars alone aren't always enough)
            write_config_yaml(read_env(ENV_FILE))
            self.proc = await asyncio.create_subprocess_exec(
                "hermes", "gateway",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                env=env,
            )
            self.state = "running"
            self.started_at = time.time()
            asyncio.create_task(self._drain())
        except Exception as e:
            self.state = "error"
            self.logs.append(f"[error] Failed to start: {e}")

    async def stop(self):
        if not self.proc or self.proc.returncode is not None:
            self.state = "stopped"
            return
        self.state = "stopping"
        self.proc.terminate()
        try:
            await asyncio.wait_for(self.proc.wait(), timeout=10)
        except asyncio.TimeoutError:
            self.proc.kill()
            await self.proc.wait()
        self.state = "stopped"
        self.started_at = None

    async def restart(self):
        await self.stop()
        self.restarts += 1
        await self.start()

    async def _drain(self):
        assert self.proc and self.proc.stdout
        async for raw in self.proc.stdout:
            line = ANSI_ESCAPE.sub("", raw.decode(errors="replace").rstrip())
            self.logs.append(line)
        if self.state == "running":
            self.state = "error"
            self.logs.append(f"[error] Gateway exited (code {self.proc.returncode})")

    def status(self) -> dict:
        uptime = int(time.time() - self.started_at) if self.started_at and self.state == "running" else None
        return {
            "state":    self.state,
            "pid":      self.proc.pid if self.proc and self.proc.returncode is None else None,
            "uptime":   uptime,
            "restarts": self.restarts,
        }


gw = Gateway()
cfg_lock = asyncio.Lock()


def _terminal_backend() -> str:
    backend = os.environ.get("TERMINAL_ENV", "").strip()
    if backend:
        return backend
    try:
        config = load_config()
        return (((config or {}).get("terminal") or {}).get("backend") or "local")
    except Exception:
        return "local"


def _terminal_supported() -> bool:
    return _terminal_backend() == "local"


def _terminal_sessions() -> list[dict]:
    sessions = process_registry.list_sessions(task_id=TERMINAL_TASK_ID)
    sessions.sort(key=lambda item: (item.get("status") != "running", item.get("started_at", "")))
    return sessions


def _active_terminal_session_id() -> str | None:
    sessions = _terminal_sessions()
    if not sessions:
        return None
    return sessions[0].get("session_id")


def _terminal_status_payload(session_id: str | None = None) -> dict:
    session_id = session_id or _active_terminal_session_id()
    payload = {
        "backend": _terminal_backend(),
        "interactive_supported": _terminal_supported(),
        "boot_command": TERMINAL_BOOT_COMMAND,
        "session": None,
    }
    if not session_id:
        return payload
    try:
        payload["session"] = process_registry.poll(session_id)
    except Exception as e:
        payload["session"] = {"session_id": session_id, "status": "missing", "error": str(e)}
    return payload


def _cron_job_payload(job: dict) -> dict:
    deliver = job.get("deliver", "local")
    if isinstance(deliver, list):
        deliver = ", ".join(str(item) for item in deliver)
    return {
        "job_id": job.get("id"),
        "name": job.get("name") or job.get("id"),
        "prompt": job.get("prompt", ""),
        "schedule": job.get("schedule_display") or job.get("schedule"),
        "next_run_at": job.get("next_run_at"),
        "last_run_at": job.get("last_run_at"),
        "last_status": job.get("last_status"),
        "last_error": job.get("last_error"),
        "last_delivery_error": job.get("last_delivery_error"),
        "enabled": job.get("enabled", True),
        "state": job.get("state", "scheduled" if job.get("enabled", True) else "paused"),
        "paused_at": job.get("paused_at"),
        "paused_reason": job.get("paused_reason"),
        "deliver": deliver,
        "script": job.get("script"),
    }


def _cron_output_job_dir(job_id: str) -> Path:
    return CRON_OUTPUT_DIR / job_id


def _validate_output_path(job_id: str, filename: str) -> Path:
    job_dir = _cron_output_job_dir(job_id).resolve()
    candidate = (job_dir / filename).resolve()
    if candidate.parent != job_dir or not candidate.is_file():
        raise FileNotFoundError("Output file not found")
    return candidate


def _list_cron_outputs(job_id: str, limit: int = 100) -> list[dict]:
    job_dir = _cron_output_job_dir(job_id)
    if not job_dir.exists():
        return []
    files = sorted(job_dir.glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True)
    outputs = []
    for file_path in files[:limit]:
        try:
            stat = file_path.stat()
            preview = file_path.read_text(encoding="utf-8", errors="replace")[:400]
        except Exception:
            continue
        outputs.append({
            "filename": file_path.name,
            "size": stat.st_size,
            "modified_at": stat.st_mtime,
            "preview": preview,
        })
    return outputs


def _file_browser_root(root_name: str | None) -> tuple[str, Path]:
    root_key = (root_name or "data").strip().lower() or "data"
    if root_key not in FILE_BROWSER_ROOTS:
        raise ValueError("Unknown file root")
    return root_key, FILE_BROWSER_ROOTS[root_key].resolve()


def _resolve_browser_path(root_name: str | None, relative_path: str = "") -> tuple[str, Path, Path]:
    root_key, root_path = _file_browser_root(root_name)
    cleaned = (relative_path or "").strip().lstrip("/")
    candidate = (root_path / cleaned).resolve()
    if candidate != root_path and root_path not in candidate.parents:
        raise ValueError("Path escapes selected root")
    return root_key, root_path, candidate


def _browser_relpath(root_path: Path, target_path: Path) -> str:
    if target_path == root_path:
        return ""
    return str(target_path.relative_to(root_path)).replace(os.sep, "/")


def _list_browser_entries(root_name: str | None, relative_path: str = "") -> dict:
    root_key, root_path, current_path = _resolve_browser_path(root_name, relative_path)
    if not current_path.exists():
        raise FileNotFoundError("Path not found")
    if not current_path.is_dir():
        raise NotADirectoryError("Path is not a directory")
    entries = []
    for child in sorted(current_path.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower())):
        try:
            stat = child.stat()
        except OSError:
            continue
        entries.append({
            "name": child.name,
            "path": _browser_relpath(root_path, child),
            "type": "dir" if child.is_dir() else "file",
            "size": None if child.is_dir() else stat.st_size,
            "modified_at": stat.st_mtime,
        })
    parent = ""
    if current_path != root_path:
        parent = _browser_relpath(root_path, current_path.parent)
    return {
        "root": root_key,
        "root_path": str(root_path),
        "path": _browser_relpath(root_path, current_path),
        "entries": entries,
        "parent": parent,
    }


def _read_browser_file(root_name: str | None, relative_path: str) -> dict:
    root_key, root_path, file_path = _resolve_browser_path(root_name, relative_path)
    if not file_path.exists() or not file_path.is_file():
        raise FileNotFoundError("File not found")
    data = file_path.read_text(encoding="utf-8", errors="replace")
    return {
        "root": root_key,
        "root_path": str(root_path),
        "path": _browser_relpath(root_path, file_path),
        "name": file_path.name,
        "content": data[:MAX_FILE_READ_BYTES],
        "truncated": len(data) > MAX_FILE_READ_BYTES,
        "size": file_path.stat().st_size,
        "modified_at": file_path.stat().st_mtime,
    }


def _save_browser_file(root_name: str | None, relative_path: str, content: str) -> dict:
    root_key, root_path, file_path = _resolve_browser_path(root_name, relative_path)
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_text(content, encoding="utf-8")
    return {
        "root": root_key,
        "root_path": str(root_path),
        "path": _browser_relpath(root_path, file_path),
        "name": file_path.name,
        "size": file_path.stat().st_size,
        "modified_at": file_path.stat().st_mtime,
    }


# ── Route handlers ────────────────────────────────────────────────────────────
async def page_index(request: Request):
    if err := guard(request): return err
    return templates.TemplateResponse(request, "index.html")


async def route_health(request: Request):
    return JSONResponse({"status": "ok", "gateway": gw.state})


async def api_config_get(request: Request):
    if err := guard(request): return err
    async with cfg_lock:
        data = read_env(ENV_FILE)
    defs = [{"key": k, "label": l, "category": c, "secret": s} for k, l, c, s in ENV_VARS]
    return JSONResponse({"vars": mask(data), "defs": defs})


async def api_config_put(request: Request):
    if err := guard(request): return err
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)
    try:
        restart = body.pop("_restart", False)
        new_vars = body.get("vars", {})
        async with cfg_lock:
            existing = read_env(ENV_FILE)
            merged = unmask(new_vars, existing)
            for k, v in existing.items():
                if k not in merged:
                    merged[k] = v
            write_env(ENV_FILE, merged)
            write_config_yaml(merged)
        if restart:
            asyncio.create_task(gw.restart())
        return JSONResponse({"ok": True, "restarting": restart})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


async def api_status(request: Request):
    if err := guard(request): return err
    data = read_env(ENV_FILE)
    providers = {
        k.replace("_API_KEY","").replace("_TOKEN","").replace("HF_","HuggingFace ").replace("_"," ").title():
        {"configured": bool(data.get(k))}
        for k in PROVIDER_KEYS
    }
    channels = {
        name: {"configured": bool(v := data.get(key,"")) and v.lower() not in ("false","0","no")}
        for name, key in CHANNEL_MAP.items()
    }
    return JSONResponse({"gateway": gw.status(), "providers": providers, "channels": channels})


async def api_logs(request: Request):
    if err := guard(request): return err
    return JSONResponse({"lines": list(gw.logs)})


async def api_terminal_status(request: Request):
    if err := guard(request): return err
    session_id = request.query_params.get("session_id") or None
    return JSONResponse(_terminal_status_payload(session_id))


async def api_terminal_session(request: Request):
    if err := guard(request): return err
    if not _terminal_supported():
        return JSONResponse({
            "ok": False,
            "error": f"Interactive terminal requires terminal.backend=local (current: {_terminal_backend()})",
            "backend": _terminal_backend(),
        }, status_code=400)
    try:
        body = await request.json()
    except Exception:
        body = {}
    command = (body.get("command") or TERMINAL_BOOT_COMMAND).strip() or TERMINAL_BOOT_COMMAND
    existing = _active_terminal_session_id()
    if existing:
        try:
            process_registry.kill_process(existing)
        except Exception:
            pass
    try:
        result = json.loads(terminal_tool(command, background=True, task_id=TERMINAL_TASK_ID, pty=True))
        payload = _terminal_status_payload(result.get("session_id"))
        payload.update({"ok": True, **result})
        return JSONResponse(payload)
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


async def api_terminal_output(request: Request):
    if err := guard(request): return err
    session_id = request.query_params.get("session_id") or _active_terminal_session_id()
    if not session_id:
        return JSONResponse({"ok": True, "session": None, "output": "", "lines": [], "next_offset": 0})
    try:
        offset = int(request.query_params.get("offset", "0"))
    except ValueError:
        offset = 0
    try:
        log_data = process_registry.read_log(session_id, offset=offset, limit=500)
        status = process_registry.poll(session_id)
        output = ANSI_ESCAPE.sub("", log_data.get("output", ""))
        return JSONResponse({
            "ok": True,
            "session": status,
            "output": output,
            "lines": output.splitlines(),
            "next_offset": log_data.get("total_lines", offset),
            "showing": log_data.get("showing"),
        })
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=404)


async def api_terminal_input(request: Request):
    if err := guard(request): return err
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)
    session_id = body.get("session_id") or _active_terminal_session_id()
    if not session_id:
        return JSONResponse({"error": "No active terminal session"}, status_code=404)
    data = body.get("data", "")
    submit = body.get("submit", True)
    try:
        result = process_registry.submit_stdin(session_id, data) if submit else process_registry.write_stdin(session_id, data)
        return JSONResponse({"ok": True, **result})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)


async def api_terminal_kill(request: Request):
    if err := guard(request): return err
    try:
        body = await request.json()
    except Exception:
        body = {}
    session_id = body.get("session_id") or _active_terminal_session_id()
    if not session_id:
        return JSONResponse({"ok": True, "status": "no_session"})
    try:
        result = process_registry.kill_process(session_id)
        return JSONResponse({"ok": True, **result})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)


async def api_crons(request: Request):
    if err := guard(request): return err
    jobs = [_cron_job_payload(job) for job in list_jobs(include_disabled=True)]
    jobs.sort(key=lambda item: ((not item.get("enabled", True)), str(item.get("name") or ""), str(item.get("job_id") or "")))
    return JSONResponse({"jobs": jobs, "gateway_running": gw.state == "running"})


async def api_cron_outputs(request: Request):
    if err := guard(request): return err
    job_id = request.path_params["job_id"]
    if not get_job(job_id):
        return JSONResponse({"error": "Job not found"}, status_code=404)
    return JSONResponse({"job_id": job_id, "outputs": _list_cron_outputs(job_id)})


async def api_cron_output_get(request: Request):
    if err := guard(request): return err
    job_id = request.path_params["job_id"]
    filename = request.path_params["filename"]
    if not get_job(job_id):
        return JSONResponse({"error": "Job not found"}, status_code=404)
    try:
        output_path = _validate_output_path(job_id, filename)
        content = output_path.read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return JSONResponse({"error": "Output file not found"}, status_code=404)
    return JSONResponse({
        "job_id": job_id,
        "filename": filename,
        "content": content[:MAX_CRON_OUTPUT_BYTES],
        "truncated": len(content) > MAX_CRON_OUTPUT_BYTES,
    })


async def api_cron_run(request: Request):
    if err := guard(request): return err
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)
    job_id = (body.get("job_id") or "").strip()
    job = trigger_job(job_id)
    if not job:
        return JSONResponse({"error": "Job not found"}, status_code=404)
    await asyncio.to_thread(cron_tick, False)
    refreshed = get_job(job_id) or job
    return JSONResponse({"ok": True, "job": _cron_job_payload(refreshed)})


async def api_cron_pause(request: Request):
    if err := guard(request): return err
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)
    job_id = (body.get("job_id") or "").strip()
    job = pause_job(job_id, reason="Paused from Hermes admin")
    if not job:
        return JSONResponse({"error": "Job not found"}, status_code=404)
    return JSONResponse({"ok": True, "job": _cron_job_payload(job)})


async def api_cron_resume(request: Request):
    if err := guard(request): return err
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)
    job_id = (body.get("job_id") or "").strip()
    job = resume_job(job_id)
    if not job:
        return JSONResponse({"error": "Job not found"}, status_code=404)
    return JSONResponse({"ok": True, "job": _cron_job_payload(job)})


async def api_cron_remove(request: Request):
    if err := guard(request): return err
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)
    job_id = (body.get("job_id") or "").strip()
    if not remove_job(job_id):
        return JSONResponse({"error": "Job not found"}, status_code=404)
    return JSONResponse({"ok": True, "job_id": job_id})


async def api_files_list(request: Request):
    if err := guard(request): return err
    root = request.query_params.get("root") or "data"
    relpath = request.query_params.get("path") or ""
    try:
        payload = _list_browser_entries(root, relpath)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    except FileNotFoundError as e:
        return JSONResponse({"error": str(e)}, status_code=404)
    except NotADirectoryError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    payload["roots"] = {name: str(path.resolve()) for name, path in FILE_BROWSER_ROOTS.items()}
    return JSONResponse(payload)


async def api_files_read(request: Request):
    if err := guard(request): return err
    root = request.query_params.get("root") or "data"
    relpath = request.query_params.get("path") or ""
    try:
        payload = _read_browser_file(root, relpath)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    except FileNotFoundError as e:
        return JSONResponse({"error": str(e)}, status_code=404)
    return JSONResponse(payload)


async def api_files_save(request: Request):
    if err := guard(request): return err
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)
    root = body.get("root") or "data"
    relpath = (body.get("path") or "").strip()
    content = body.get("content") or ""
    if not relpath:
        return JSONResponse({"error": "path is required"}, status_code=400)
    try:
        payload = _save_browser_file(root, relpath, content)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    return JSONResponse({"ok": True, **payload})


async def api_gw_start(request: Request):
    if err := guard(request): return err
    asyncio.create_task(gw.start())
    return JSONResponse({"ok": True})


async def api_gw_stop(request: Request):
    if err := guard(request): return err
    asyncio.create_task(gw.stop())
    return JSONResponse({"ok": True})


async def api_gw_restart(request: Request):
    if err := guard(request): return err
    asyncio.create_task(gw.restart())
    return JSONResponse({"ok": True})


async def api_config_reset(request: Request):
    if err := guard(request): return err
    asyncio.create_task(gw.stop())
    async with cfg_lock:
        if ENV_FILE.exists():
            ENV_FILE.unlink()
        write_config_yaml({})
    return JSONResponse({"ok": True})


# ── Pairing ───────────────────────────────────────────────────────────────────
def _pjson(path: Path) -> dict:
    try:
        return json.loads(path.read_text()) if path.exists() else {}
    except Exception:
        return {}


def _wjson(path: Path, data: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False))
    try: os.chmod(path, 0o600)
    except OSError: pass


def _platforms(suffix: str) -> list[str]:
    if not PAIRING_DIR.exists(): return []
    return [f.stem.rsplit(f"-{suffix}", 1)[0] for f in PAIRING_DIR.glob(f"*-{suffix}.json")]


async def api_pairing_pending(request: Request):
    if err := guard(request): return err
    now = time.time()
    out = []
    for p in _platforms("pending"):
        for code, info in _pjson(PAIRING_DIR / f"{p}-pending.json").items():
            if now - info.get("created_at", now) <= PAIRING_TTL:
                out.append({"platform": p, "code": code,
                            "user_id": info.get("user_id",""), "user_name": info.get("user_name",""),
                            "age_minutes": int((now - info.get("created_at", now)) / 60)})
    return JSONResponse({"pending": out})


async def api_pairing_approve(request: Request):
    if err := guard(request): return err
    try: body = await request.json()
    except Exception: return JSONResponse({"error": "Invalid JSON"}, status_code=400)
    platform, code = body.get("platform",""), body.get("code","").upper().strip()
    if not platform or not code:
        return JSONResponse({"error": "platform and code required"}, status_code=400)
    pending_path = PAIRING_DIR / f"{platform}-pending.json"
    pending = _pjson(pending_path)
    if code not in pending:
        return JSONResponse({"error": "Code not found"}, status_code=404)
    entry = pending.pop(code)
    _wjson(pending_path, pending)
    approved = _pjson(PAIRING_DIR / f"{platform}-approved.json")
    approved[entry["user_id"]] = {"user_name": entry.get("user_name",""), "approved_at": time.time()}
    _wjson(PAIRING_DIR / f"{platform}-approved.json", approved)
    return JSONResponse({"ok": True})


async def api_pairing_deny(request: Request):
    if err := guard(request): return err
    try: body = await request.json()
    except Exception: return JSONResponse({"error": "Invalid JSON"}, status_code=400)
    platform, code = body.get("platform",""), body.get("code","").upper().strip()
    p = PAIRING_DIR / f"{platform}-pending.json"
    pending = _pjson(p)
    if code in pending:
        del pending[code]
        _wjson(p, pending)
    return JSONResponse({"ok": True})


async def api_pairing_approved(request: Request):
    if err := guard(request): return err
    out = []
    for p in _platforms("approved"):
        for uid, info in _pjson(PAIRING_DIR / f"{p}-approved.json").items():
            out.append({"platform": p, "user_id": uid,
                        "user_name": info.get("user_name",""), "approved_at": info.get("approved_at",0)})
    return JSONResponse({"approved": out})


async def api_pairing_revoke(request: Request):
    if err := guard(request): return err
    try: body = await request.json()
    except Exception: return JSONResponse({"error": "Invalid JSON"}, status_code=400)
    platform, uid = body.get("platform",""), body.get("user_id","")
    if not platform or not uid:
        return JSONResponse({"error": "platform and user_id required"}, status_code=400)
    p = PAIRING_DIR / f"{platform}-approved.json"
    approved = _pjson(p)
    if uid in approved:
        del approved[uid]
        _wjson(p, approved)
    return JSONResponse({"ok": True})


# ── App lifecycle ─────────────────────────────────────────────────────────────
async def auto_start():
    data = read_env(ENV_FILE)
    if any(data.get(k) for k in PROVIDER_KEYS):
        asyncio.create_task(gw.start())
    else:
        print("[server] No provider key found — gateway not started. Configure one in the admin UI.", flush=True)


@asynccontextmanager
async def lifespan(app):
    await auto_start()
    yield
    await gw.stop()


routes = [
    Route("/",                          page_index),
    Route("/health",                    route_health),
    Route("/api/config",                api_config_get,      methods=["GET"]),
    Route("/api/config",                api_config_put,      methods=["PUT"]),
    Route("/api/status",                api_status),
    Route("/api/logs",                  api_logs),
    Route("/api/terminal/status",       api_terminal_status),
    Route("/api/terminal/session",      api_terminal_session, methods=["POST"]),
    Route("/api/terminal/output",       api_terminal_output),
    Route("/api/terminal/input",        api_terminal_input, methods=["POST"]),
    Route("/api/terminal/kill",         api_terminal_kill, methods=["POST"]),
    Route("/api/crons",                 api_crons),
    Route("/api/crons/run",             api_cron_run, methods=["POST"]),
    Route("/api/crons/pause",           api_cron_pause, methods=["POST"]),
    Route("/api/crons/resume",          api_cron_resume, methods=["POST"]),
    Route("/api/crons/remove",          api_cron_remove, methods=["POST"]),
    Route("/api/crons/{job_id:str}/outputs", api_cron_outputs),
    Route("/api/crons/{job_id:str}/outputs/{filename:str}", api_cron_output_get),
    Route("/api/files",                 api_files_list),
    Route("/api/files/read",            api_files_read),
    Route("/api/files/save",            api_files_save, methods=["PUT"]),
    Route("/api/gateway/start",         api_gw_start,        methods=["POST"]),
    Route("/api/gateway/stop",          api_gw_stop,         methods=["POST"]),
    Route("/api/gateway/restart",       api_gw_restart,      methods=["POST"]),
    Route("/api/config/reset",          api_config_reset,    methods=["POST"]),
    Route("/api/pairing/pending",       api_pairing_pending),
    Route("/api/pairing/approve",       api_pairing_approve, methods=["POST"]),
    Route("/api/pairing/deny",          api_pairing_deny,    methods=["POST"]),
    Route("/api/pairing/approved",      api_pairing_approved),
    Route("/api/pairing/revoke",        api_pairing_revoke,  methods=["POST"]),
]

app = Starlette(
    routes=routes,
    middleware=[Middleware(AuthenticationMiddleware, backend=BasicAuth())],
    lifespan=lifespan,
)

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", "8080"))
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    config = uvicorn.Config(app, host="0.0.0.0", port=port, log_level="info", loop="asyncio")
    server = uvicorn.Server(config)

    def _shutdown():
        loop.create_task(gw.stop())
        server.should_exit = True

    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, _shutdown)

    loop.run_until_complete(server.serve())
