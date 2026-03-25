#!/usr/bin/env python3
import base64
import json
import os
from urllib.parse import quote, urlparse
import subprocess
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

import requests
from flask import Flask, Response, jsonify, request, send_from_directory, stream_with_context
from flask_cors import CORS


BASE_DIR = Path(__file__).parent
STATIC_DIR = BASE_DIR / "static"
UPLOADS_DIR = BASE_DIR / "uploads"
MEMORY_DIR = BASE_DIR / "memory"
WORKTREES_DIR = BASE_DIR / ".builder_worktrees"
CONFIG_PATH = BASE_DIR / "builder_config.json"
OPENROUTER_MODELS_CACHE_PATH = MEMORY_DIR / "openrouter_models_cache.json"
OPENROUTER_CACHE_TTL_SEC = 6 * 3600
GROQ_MODELS_CACHE_PATH = MEMORY_DIR / "groq_models_cache.json"
GROQ_CACHE_TTL_SEC = 6 * 3600

# IDs permitidos en modo groq_free_only (tier gratuito habitual; ref. console.groq.com/docs/models).
GROQ_FREE_TIER_MODEL_IDS = frozenset(
    {
        "llama-3.3-70b-versatile",
        "llama-3.1-8b-instant",
        "llama-3.1-70b-versatile",
        "llama-3.2-1b-preview",
        "llama-3.2-3b-preview",
        "mixtral-8x7b-32768",
        "gemma2-9b-it",
        "openai/gpt-oss-20b",
        "openai/gpt-oss-120b",
        "meta-llama/llama-4-scout-17b-16e-instruct",
        "qwen/qwen3-32b",
    }
)

UPLOADS_DIR.mkdir(exist_ok=True)
MEMORY_DIR.mkdir(exist_ok=True)
WORKTREES_DIR.mkdir(exist_ok=True)


DEFAULT_CONFIG = {
    "host": "127.0.0.1",
    "port": 7788,
    "ollama_base_url": "http://127.0.0.1:11434",
    "model": "llama2-uncensored:latest",
    "temperature": 0.7,
    # Ventana de contexto: valores más bajos = menos RAM y a veces más rápido (chat corto: 1024 o 512).
    "num_ctx": 1024,
    # Tope de tokens a generar por respuesta (evita respuestas enormes que parecen "lentas").
    "num_predict": 512,
    # Cuánto tiempo dejar el modelo cargado en RAM/VRAM tras cada uso (menos recargas = menos esperas).
    "keep_alive": "30m",
    # System prompt for /api/claude/chat/stream (local UI; no cloud APIs).
    "system_prompt": (
        "You are a capable local assistant running on the user's machine via Ollama. "
        "Be direct, practical, and follow the user's instructions."
    ),
    # Chat engine: puter | groq | openrouter | local (Ollama)
    "chat_engine": "puter",
    "puter_model": "gpt-5.4-nano",
    # Puter.js en el navegador (solo https://js.puter.com — ver Settings)
    "puter_cdn_url": "https://js.puter.com/v2/",
    # Groq OpenAI-compatible API (https://console.groq.com/keys) — también GROQ_API_KEY en el entorno
    "groq_api_key": "",
    "groq_model": "llama-3.3-70b-versatile",
    # Solo modelos de tier gratuito permitidos (lista blanca; Groq no usa cartera tipo OpenRouter).
    "groq_free_only": True,
    # OpenRouter (https://openrouter.ai/keys) — OPENROUTER_API_KEY en el entorno
    "openrouter_api_key": "",
    # Solo modelos gratuitos OpenRouter (validado al llamar a la API y al guardar Settings).
    "openrouter_model": "meta-llama/llama-3.2-3b-instruct:free",
    # Opcional: OpenRouter recomienda HTTP-Referer (tu sitio o http://127.0.0.1:7788)
    "openrouter_http_referer": "http://127.0.0.1:7788",
    "openrouter_app_title": "beehAIve UPDATED",
    # Tope de salida para OpenRouter (independiente de num_predict de Ollama; evita respuestas cortísimas).
    "openrouter_max_tokens": 8192,
    # Máximo de vueltas modelo→herramientas en /api/chat/openrouter/agent (evita bucles infinitos).
    "openrouter_agent_max_rounds": 12,
    # Base API (sin barra final). Vacío = https://openrouter.ai/api/v1 — no uses …/chat/completions/ (404).
    "openrouter_api_base": "",
    "github": {"token": "", "owner": "", "repo": "", "branch": "main"},
    # Rutas permitidas para /api/local/* (vacío = solo tu $HOME).
    "local_roots": [],
    # Carpetas de skills sugeridas para incluir en contexto rápidamente.
    "skill_dirs": ["/Users/alvaro/Downloads/alAIve-main/.claude/skills"],
    # Swarm multi-agente: motor LLM (ollama = local; openrouter / groq = nube con clave en Settings).
    "swarm_llm_backend": "ollama",
}


def _openrouter_id_policy_free_heuristic(model_id: str) -> bool:
    """True si el ID cumple la política «solo gratis» por nombre (sin llamar a la API)."""
    t = (model_id or "").strip().lower()
    return t == "openrouter/free" or ":free" in t


def _openrouter_saved_model_likely_paid_pre_policy(model_id: str) -> bool:
    """IDs habituales de pago guardados antes de forzar solo :free (migración suave)."""
    t = (model_id or "").strip()
    if not t or _openrouter_id_policy_free_heuristic(t):
        return False
    low = t.lower()
    if low.startswith("anthropic/") or low.startswith("moonshotai/"):
        return True
    if low.startswith("openai/gpt-4") or low.startswith("openai/gpt-5") or low.startswith("openai/o1") or low.startswith("openai/o3"):
        return True
    return False


def load_config():
    if not CONFIG_PATH.exists():
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(DEFAULT_CONFIG, f, indent=2, ensure_ascii=False)
        return DEFAULT_CONFIG.copy()
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        data = json.load(f)
    cfg = DEFAULT_CONFIG.copy()
    cfg.update(data)
    if "github" not in cfg:
        cfg["github"] = DEFAULT_CONFIG["github"].copy()
    # Sustituir en disco modelos de pago típicos guardados antes de la política :free (p. ej. Claude en Settings).
    om = (cfg.get("openrouter_model") or "").strip()
    migrate = (not om) or _openrouter_saved_model_likely_paid_pre_policy(om)
    if migrate:
        cfg["openrouter_model"] = DEFAULT_CONFIG["openrouter_model"]
        try:
            with open(CONFIG_PATH, "w", encoding="utf-8") as f:
                json.dump(cfg, f, indent=2, ensure_ascii=False)
        except Exception:
            pass
    return cfg


CONFIG = load_config()
app = Flask(__name__, static_folder="static")
CORS(app)
JOBS = {}
JOBS_LOCK = threading.Lock()

_MAX_LOCAL_FILE = 1_500_000
_MAX_LOCAL_READ = 120_000
_MAX_AGENT_TOOL_RESULT = 28_000
_MAX_BUILDER_WRITE = 600_000

OPENROUTER_AGENT_SYSTEM_SUFFIX = (
    "\n\n=== AGENT TOOLS (server-executed) ===\n"
    "You have function tools; the Uncensored Builder server runs them on the user's machine.\n"
    "Use tools instead of promising to «search» or «edit» in the abstract — actually call the tools.\n"
    "- web_search: real web lookup (DuckDuckGo HTML).\n"
    "- workspace_run: one shell command with cwd = the user's Workspace path from the UI (pip, npm, git, pytest). "
    "If the user wants to run commands inside this app’s own folder, they must set Workspace to that folder (or you use builder_* for source edits).\n"
    "- builder_read_file: read a text file under the app install. Parameter rel_path is relative to app root (e.g. static/builder.html, builder_server.py).\n"
    "- builder_write_file: replace the entire file contents (use for new files or full rewrites).\n"
    "- builder_search_replace: preferred for small edits to large files — provide rel_path, old_string, new_string; "
    "old_string must match exactly once unless replace_all is true.\n"
    "SELF-MODIFICATION: When the user asks to fix, change, or refactor this dashboard, Puter loading, OpenRouter, CSS, i18n, or server routes, "
    "you CAN do it by reading then search_replace or write_file. Typical paths: builder_server.py, static/builder.html, "
    "static/beehaive.css, static/beehaive-i18n.js. Do not edit builder_config.json unless explicitly asked (may contain secrets). "
    "After changing builder_server.py, tell the user to restart the Flask/Builder process so changes load.\n"
    "BREVITY / NO BOILERPLATE: Do not waste tokens on generic scaffolding, repeated class skeletons, or filler lines like "
    "«each class can be extended with more methods» unless the user explicitly asked for stubs only. Prefer minimal concrete code, "
    "one tool call when enough, and short summaries.\n"
    "After tools return, summarize what you changed and any user steps (restart, refresh) in plain language."
)


def _openrouter_agent_max_rounds() -> int:
    v = CONFIG.get("openrouter_agent_max_rounds")
    try:
        n = int(v) if v is not None else 12
    except (TypeError, ValueError):
        n = 12
    return max(1, min(32, n))


def _builder_safe_rel_path(rel: str):
    """Ruta relativa bajo BASE_DIR; rechaza .. y absolutos."""
    rel = (rel or "").strip().replace("\\", "/").lstrip("/")
    if not rel or ".." in rel.split("/"):
        return None
    target = (BASE_DIR / rel).resolve()
    try:
        target.relative_to(BASE_DIR.resolve())
    except ValueError:
        return None
    return target


def _agent_tool_definitions():
    return [
        {
            "type": "function",
            "function": {
                "name": "web_search",
                "description": "Search the web via DuckDuckGo HTML. Use for documentation, API names, libraries, or up-to-date facts.",
                "parameters": {
                    "type": "object",
                    "properties": {"query": {"type": "string", "description": "Search query"}},
                    "required": ["query"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "workspace_run",
                "description": (
                    "Run one shell command with cwd = the user's workspace directory "
                    "(pip install, npm, git, pytest, ls, etc.). Dangerous if misused; only runs when workspace_path is configured."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "command": {
                            "type": "string",
                            "description": "Single shell command (same as Terminal rápida in the UI)",
                        }
                    },
                    "required": ["command"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "builder_read_file",
                "description": (
                    "Read a text file inside the Uncensored Builder install (e.g. static/builder.html, builder_server.py). "
                    "Path is relative to the app root."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "rel_path": {
                            "type": "string",
                            "description": "Relative path, e.g. static/builder.html",
                        }
                    },
                    "required": ["rel_path"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "builder_write_file",
                "description": (
                    "Overwrite or create a text file under the Uncensored Builder app directory (self-modify). "
                    "Use for full-file rewrites or new files; for small edits to large files prefer builder_search_replace."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "rel_path": {"type": "string"},
                        "content": {"type": "string", "description": "Full new file contents"},
                    },
                    "required": ["rel_path", "content"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "builder_search_replace",
                "description": (
                    "Replace a unique substring in a file under the app directory (safer than rewriting huge files). "
                    "If old_string is not unique, either narrow the snippet or set replace_all to true to replace every occurrence."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "rel_path": {"type": "string"},
                        "old_string": {"type": "string"},
                        "new_string": {"type": "string", "description": "Replacement text (may be empty to delete old_string)"},
                        "replace_all": {
                            "type": "boolean",
                            "description": "If true, replace every occurrence of old_string (default false = exactly one occurrence required)",
                        },
                    },
                    "required": ["rel_path", "old_string", "new_string"],
                },
            },
        },
    ]


def _duckduckgo_search_snippets(query: str):
    """Reutiliza la lógica de /api/web/search."""
    r = requests.get(
        "https://duckduckgo.com/html/",
        params={"q": query},
        headers={"User-Agent": "Mozilla/5.0"},
        timeout=15,
    )
    r.raise_for_status()
    html = r.text
    snippets = []
    for chunk in html.split("result__a")[:6]:
        if 'href="' in chunk:
            href = chunk.split('href="', 1)[1].split('"', 1)[0]
            title = chunk.split(">", 1)[1].split("<", 1)[0] if ">" in chunk else href
            snippets.append({"title": title.strip(), "url": href})
    return snippets


def _execute_openrouter_agent_tool(name: str, raw_args: str, workspace_path: str) -> str:
    try:
        args = json.loads(raw_args) if (raw_args or "").strip() else {}
    except Exception:
        return json.dumps({"ok": False, "error": "invalid JSON in tool arguments"})
    try:
        if name == "web_search":
            q = (args.get("query") or "").strip()
            if not q:
                return json.dumps({"ok": False, "error": "query required"})
            snippets = _duckduckgo_search_snippets(q)
            return json.dumps({"ok": True, "query": q, "results": snippets}, ensure_ascii=False)

        if name == "workspace_run":
            cmd = (args.get("command") or "").strip()
            if not cmd:
                return json.dumps({"ok": False, "error": "command required"})
            if not workspace_path:
                return json.dumps(
                    {
                        "ok": False,
                        "error": "No workspace_path for this request. Set the Workspace path in the Builder UI (Workspace tab) and retry.",
                    }
                )
            ws = Path(workspace_path).resolve()
            if not ws.exists():
                return json.dumps({"ok": False, "error": f"workspace_path not found: {ws}"})
            try:
                proc = subprocess.run(
                    cmd,
                    cwd=str(ws),
                    shell=True,
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=120,
                )
            except subprocess.TimeoutExpired:
                return json.dumps(
                    {
                        "ok": False,
                        "error": "Comando superó 120s (timeout). Usa un comando más corto o evita procesos interactivos.",
                    },
                    ensure_ascii=False,
                )
            out = {
                "ok": proc.returncode == 0,
                "exit_code": proc.returncode,
                "stdout": (proc.stdout or "")[-14000:],
                "stderr": (proc.stderr or "")[-14000:],
            }
            return json.dumps(out, ensure_ascii=False)

        if name == "builder_read_file":
            rel = (args.get("rel_path") or "").strip()
            target = _builder_safe_rel_path(rel)
            if not target:
                return json.dumps({"ok": False, "error": "invalid rel_path"})
            if not target.is_file():
                return json.dumps({"ok": False, "error": f"not a file: {rel}"})
            try:
                data = target.read_text(encoding="utf-8", errors="replace")
            except Exception as e:
                return json.dumps({"ok": False, "error": str(e)})
            if len(data) > 200_000:
                data = data[:200_000] + "\n...[truncated]"
            return json.dumps({"ok": True, "rel_path": rel, "content": data}, ensure_ascii=False)

        if name == "builder_write_file":
            rel = (args.get("rel_path") or "").strip()
            content = args.get("content")
            if content is None:
                return json.dumps({"ok": False, "error": "content required"})
            if not isinstance(content, str):
                content = str(content)
            if len(content) > _MAX_BUILDER_WRITE:
                return json.dumps({"ok": False, "error": "content too large"})
            target = _builder_safe_rel_path(rel)
            if not target:
                return json.dumps({"ok": False, "error": "invalid rel_path"})
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(content, encoding="utf-8")
            except Exception as e:
                return json.dumps({"ok": False, "error": str(e)})
            return json.dumps({"ok": True, "rel_path": rel, "bytes": target.stat().st_size}, ensure_ascii=False)

        if name == "builder_search_replace":
            rel = (args.get("rel_path") or "").strip()
            old = args.get("old_string")
            new = args.get("new_string")
            if old is None or not isinstance(old, str):
                return json.dumps({"ok": False, "error": "old_string (string) required"})
            if new is None:
                new = ""
            if not isinstance(new, str):
                new = str(new)
            replace_all = bool(args.get("replace_all"))
            if not old:
                return json.dumps({"ok": False, "error": "old_string must be non-empty"})
            target = _builder_safe_rel_path(rel)
            if not target:
                return json.dumps({"ok": False, "error": "invalid rel_path"})
            if not target.is_file():
                return json.dumps({"ok": False, "error": f"not a file: {rel}"})
            try:
                data = target.read_text(encoding="utf-8", errors="strict")
            except Exception as e:
                return json.dumps({"ok": False, "error": str(e)})
            n = data.count(old)
            if n == 0:
                return json.dumps({"ok": False, "error": "old_string not found in file"})
            if replace_all:
                new_data = data.replace(old, new)
            elif n != 1:
                return json.dumps(
                    {
                        "ok": False,
                        "error": f"old_string appears {n} times; narrow the snippet, or set replace_all to true",
                    }
                )
            else:
                new_data = data.replace(old, new, 1)
            try:
                target.write_text(new_data, encoding="utf-8")
            except Exception as e:
                return json.dumps({"ok": False, "error": str(e)})
            return json.dumps(
                {
                    "ok": True,
                    "rel_path": rel,
                    "replaced_occurrences": (n if replace_all else 1),
                    "bytes": target.stat().st_size,
                },
                ensure_ascii=False,
            )

        return json.dumps({"ok": False, "error": f"unknown tool: {name}"})
    except Exception as e:
        return json.dumps({"ok": False, "error": str(e)})


def _extract_openai_style_message_text(data: dict) -> str:
    """Extrae texto del primer choice de una respuesta chat/completions (OpenAI/OpenRouter/Groq)."""
    choices = data.get("choices") or []
    if not choices:
        raise ValueError("empty choices from LLM")
    msg = (choices[0] or {}).get("message") or {}
    content = msg.get("content")
    if isinstance(content, list):
        parts = []
        for p in content:
            if isinstance(p, dict) and p.get("type") == "text":
                parts.append(p.get("text") or "")
            elif isinstance(p, str):
                parts.append(p)
        content = "".join(parts)
    if content is None:
        content = ""
    return (content if isinstance(content, str) else str(content)).strip()


def _groq_chat_completion_nonstream(messages: list, model: str | None = None) -> dict:
    """Chat completions Groq sin stream."""
    key = _groq_api_key()
    if not key:
        raise ValueError("Groq API key missing: Settings o GROQ_API_KEY")
    mid = (model or "").strip() or CONFIG.get("groq_model") or "llama-3.3-70b-versatile"
    _enforce_groq_free_tier_model(mid)
    url = "https://api.groq.com/openai/v1/chat/completions"
    payload = {
        "model": mid,
        "messages": messages,
        "stream": False,
        "temperature": float(CONFIG.get("temperature", 0.7)),
        "max_tokens": min(8192, _openrouter_max_tokens()),
    }
    hdrs = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    r = requests.post(url, headers=hdrs, json=payload, timeout=300)
    if r.status_code >= 400:
        try:
            ej = r.json()
            inner = ej.get("error") if isinstance(ej, dict) else ej
            if isinstance(inner, dict):
                msg = inner.get("message", str(inner))
            else:
                msg = str(inner)
        except Exception:
            msg = (r.text or r.reason)[:4000]
        msg = _groq_error_hint(r.status_code, msg)
        raise ValueError(f"Groq {r.status_code}: {msg}")
    return r.json()


def _openrouter_post_with_retries(
    url: str,
    hdrs: dict,
    json_body: dict,
    *,
    stream: bool,
    timeout: float | int,
    max_attempts: int = 4,
):
    """
    POST a chat/completions. Reintenta en 429/503 (frecuente en modelos :free aunque el mensaje sea corto).
    Respeta Retry-After si viene en cabecera.
    """
    last = None
    for attempt in range(max_attempts):
        r = requests.post(url, headers=hdrs, json=json_body, stream=stream, timeout=timeout)
        last = r
        if r.status_code in (429, 503) and attempt < max_attempts - 1:
            ra = r.headers.get("Retry-After") or r.headers.get("retry-after")
            try:
                delay = float(ra) if ra is not None and str(ra).strip() != "" else None
            except (TypeError, ValueError):
                delay = None
            if delay is None:
                delay = min(12.0, 1.8 * (2**attempt))
            try:
                r.close()
            except Exception:
                pass
            time.sleep(delay)
            continue
        return r
    return last


def _openrouter_chat_completion_nonstream(payload: dict, timeout: float | int = 300):
    """POST chat/completions sin stream; devuelve dict JSON o lanza."""
    key = _openrouter_api_key()
    if not key:
        raise ValueError("OpenRouter API key missing")
    mid = (payload.get("model") or "").strip()
    if mid:
        _enforce_openrouter_free_model_id(mid)
    url = _openrouter_endpoint("chat/completions")
    hdrs = {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "User-Agent": "Uncensored-Builder/1.0 (local; https://openrouter.ai)",
    }
    hdrs.update(_openrouter_extra_headers())
    r = _openrouter_post_with_retries(url, hdrs, payload, stream=False, timeout=timeout)
    if r.status_code >= 400:
        try:
            ej = r.json()
            inner = ej.get("error") if isinstance(ej, dict) else None
            if isinstance(inner, dict):
                msg = inner.get("message") or inner.get("type") or str(inner)
            else:
                msg = str(inner or ej)
        except Exception:
            msg = (r.text or r.reason)[:4000]
        msg = _openrouter_error_hint(r.status_code, msg)
        raise ValueError(f"OpenRouter {r.status_code}: {msg}")
    return r.json()


def _allowed_local_roots():
    roots = CONFIG.get("local_roots") or []
    out = []
    for p in roots:
        try:
            rp = Path(p).expanduser().resolve()
            if rp.is_dir():
                out.append(rp)
        except Exception:
            continue
    if not out:
        try:
            out = [Path.home().resolve()]
        except Exception:
            out = []
    return out


def _safe_puter_cdn_url(url: str) -> str:
    """Solo CDN HTTPS de Puter (evita XSS en <script src>)."""
    default = "https://js.puter.com/v2/"
    u = (url or "").strip()
    if not u:
        return default
    if not u.startswith("https://"):
        return default
    try:
        p = urlparse(u)
        host = (p.hostname or "").lower()
        if host == "js.puter.com":
            return u
    except Exception:
        pass
    return default


def _is_path_under_allowed(target: Path) -> bool:
    try:
        rp = target.resolve()
    except Exception:
        return False
    for root in _allowed_local_roots():
        try:
            rp.relative_to(root)
            return True
        except ValueError:
            continue
    return False


def save_config():
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(CONFIG, f, indent=2, ensure_ascii=False)


def _groq_api_key() -> str:
    env = (os.environ.get("GROQ_API_KEY") or "").strip()
    if env:
        return env
    return (CONFIG.get("groq_api_key") or "").strip()


def _groq_error_hint(status: int, message: str) -> str:
    """Texto extra para errores frecuentes de Groq (429 límites, 402 plan)."""
    base = (message or "").strip()
    if status == 429:
        return (
            base
            + "\n\n[Groq 429] Límite de velocidad (TPM/RPM). Espera unos minutos o reduce el tamaño del mensaje. "
            "Tier gratuito: límites en https://console.groq.com/docs/rate-limits"
        )
    if status == 402:
        return (
            base
            + "\n\n[Groq 402] Groq no pide «cargar créditos» como OpenRouter para la clave gratuita habitual; "
            "suele ser plan/límite de facturación o cuenta. Revisa https://console.groq.com/settings/billing y el tier. "
            "Alternativa: motor Ollama local o OpenRouter con modelo :free."
        )
    if status == 403:
        return base + "\n\n[Groq 403] Clave denegada o sin permiso; revisa https://console.groq.com/keys"
    return base


def _enforce_groq_free_tier_model(model_id: str) -> None:
    """Lista blanca de modelos de tier gratuito (groq_free_only)."""
    if not CONFIG.get("groq_free_only", True):
        return
    mid = (model_id or "").strip()
    if not mid:
        raise ValueError("Modelo Groq vacío.")
    if mid in GROQ_FREE_TIER_MODEL_IDS:
        return
    sample = ", ".join(sorted(GROQ_FREE_TIER_MODEL_IDS)[:6])
    raise ValueError(
        f"Modelo «{mid}» no está permitido (solo tier gratuito). Ejemplos: {sample}. "
        "Desactiva «groq_free_only» en builder_config.json solo si necesitas otro modelo (avanzado)."
    )


def _openrouter_api_key() -> str:
    env = (os.environ.get("OPENROUTER_API_KEY") or "").strip()
    if env:
        return env
    return (CONFIG.get("openrouter_api_key") or "").strip()


def _openrouter_max_tokens() -> int:
    """Límite de tokens generados en chat OpenRouter (64–32000)."""
    v = CONFIG.get("openrouter_max_tokens")
    try:
        n = int(v) if v is not None else 8192
    except (TypeError, ValueError):
        n = 8192
    return max(64, min(32000, n))


# Techo de caracteres por petición (≈ tokens×4) para no enviar historiales enormes a Groq/OpenRouter.
_CLOUD_MSG_PER_MESSAGE_MAX = 48000
_CLOUD_GROQ_HISTORY_MAX_CHARS = 22000
_CLOUD_OPENROUTER_HISTORY_MAX_CHARS = 120000
# Por petición al modo agente (varias vueltas modelo↔herramientas; evita bloqueos larguísimos).
_OPENROUTER_AGENT_HTTP_TIMEOUT_SEC = 180


def _truncate_chat_content(s: str, max_len: int) -> str:
    if not isinstance(s, str) or len(s) <= max_len:
        return s
    return s[:max_len] + "\n\n[…contenido truncado por límite de contexto…]"


def _clamp_chat_messages(messages: list, max_total_chars: int, per_message_max: int = _CLOUD_MSG_PER_MESSAGE_MAX) -> list:
    """Conserva system + los últimos turnos hasta max_total_chars (defensa en servidor)."""
    if not isinstance(messages, list) or not messages:
        return messages
    normalized: list = []
    for m in messages:
        if not isinstance(m, dict):
            continue
        role = m.get("role")
        c = m.get("content")
        if isinstance(c, str):
            normalized.append({"role": role, "content": _truncate_chat_content(c, per_message_max)})
        else:
            normalized.append(dict(m))
    if not normalized:
        return messages
    sys: list = []
    rest = normalized
    if normalized[0].get("role") == "system":
        sys = [normalized[0]]
        rest = normalized[1:]
    sys_len = sum(len((m.get("content") or "")) for m in sys)
    budget = max(0, max_total_chars - sys_len)
    kept: list = []
    used = 0
    for m in reversed(rest):
        c = m.get("content") or ""
        clen = len(c) if isinstance(c, str) else 0
        if used + clen > budget and kept:
            break
        kept.append(m)
        used += clen
    kept.reverse()
    dropped = len(rest) > len(kept)
    if not dropped:
        return sys + kept
    note = (
        "[Historial recortado automáticamente para cumplir el límite de tokens de la API (Groq/OpenRouter). "
        "Usa «Nueva conversación» si necesitas contexto limpio.]\n\n"
    )
    if sys and sys[0].get("role") == "system":
        c0 = sys[0].get("content") or ""
        return [{"role": "system", "content": note + c0}] + kept
    return [{"role": "system", "content": note.strip()}] + kept


def _openrouter_extra_headers():
    """Cabeceras recomendadas por OpenRouter (Referer / X-Title)."""
    h = {}
    ref = (CONFIG.get("openrouter_http_referer") or "").strip()
    if ref:
        h["Referer"] = ref
    title = (CONFIG.get("openrouter_app_title") or "").strip()
    if title:
        h["X-Title"] = title
        h["X-OpenRouter-Title"] = title
    return h


OPENROUTER_API_BASE_DEFAULT = "https://openrouter.ai/api/v1"


def _safe_openrouter_api_base(val: str) -> str:
    """Normaliza base https://openrouter.ai/api/v1 (sin barra final). Rechaza rutas raras o URL de endpoint pegada por error."""
    default = OPENROUTER_API_BASE_DEFAULT
    raw = (val or "").strip().rstrip("/")
    if not raw:
        return default
    try:
        p = urlparse(raw)
        if p.scheme != "https":
            return default
        host = (p.hostname or "").lower()
        if host not in ("openrouter.ai", "www.openrouter.ai"):
            return default
        path = (p.path or "").rstrip("/")
        if "/chat/completions" in path:
            path = path.split("/chat/completions")[0].rstrip("/")
        if path.endswith("/models"):
            path = path[: -len("/models")].rstrip("/")
        if path != "/api/v1":
            return default
        return f"https://{host}{path}"
    except Exception:
        return default


def _openrouter_api_base() -> str:
    return _safe_openrouter_api_base(str(CONFIG.get("openrouter_api_base") or ""))


def _openrouter_endpoint(path: str) -> str:
    """Ej. path 'chat/completions' → …/api/v1/chat/completions (nunca barra final: OpenRouter devuelve 404)."""
    p = path.strip().lstrip("/").rstrip("/")
    return f"{_openrouter_api_base()}/{p}"


def _openrouter_error_hint(status: int, message: str) -> str:
    """Texto extra cuando OpenRouter devuelve 4xx/429 frecuentes."""
    base = (message or "").strip()
    if status == 404:
        return (
            base
            + "\n\n[OpenRouter 404] Suele deberse a: (1) URL con barra final …/chat/completions/ — debe ser …/chat/completions; "
            + "(2) modelo inexistente o retirado — comprueba el ID en https://openrouter.ai/models ."
        )
    if status == 401:
        return base + "\n\n[OpenRouter 401] Clave inválida o caducada: Settings → clave sk-or-… → Guardar, o OPENROUTER_API_KEY."
    if status == 402:
        return (
            base
            + "\n\n[OpenRouter 402] «Insufficient credits» es saldo de cuenta en cero, no el modelo. "
            "OpenRouter suele exigir créditos en https://openrouter.ai/settings/credits aunque uses modelos :free (precio $0 por token); "
            "añadir una cantidad mínima suele reactivar la API. Alternativa en este builder: motor Groq o Ollama local (sin OpenRouter)."
        )
    if status == 429:
        return (
            base
            + "\n\n[OpenRouter 429] Suele ser límite del proveedor o cola de modelos :free (incluso con «hola»). "
            "Este builder ya reintenta hasta 4 veces con espera; si sigue fallando: espera unos minutos, prueba otro :free, "
            "o usa Groq/Ollama. Actividad: https://openrouter.ai/activity"
        )
    if status == 503:
        return (
            base
            + "\n\n[OpenRouter 503] Proveedor o OpenRouter saturados. Reintenta en unos minutos o cambia de modelo."
        )
    return base


def ollama_chat(messages, stream=False):
    opts = {
        "temperature": CONFIG.get("temperature", 0.7),
        "num_ctx": CONFIG.get("num_ctx", 1024),
    }
    if CONFIG.get("num_predict") is not None:
        opts["num_predict"] = CONFIG["num_predict"]
    payload = {
        "model": CONFIG["model"],
        "messages": messages,
        "stream": stream,
        "keep_alive": CONFIG.get("keep_alive", "30m"),
        "options": opts,
    }
    return requests.post(
        f"{CONFIG['ollama_base_url']}/api/chat",
        json=payload,
        stream=stream,
        timeout=300,
    )


def conversation_file(conv_id):
    return MEMORY_DIR / f"conv_{conv_id}.json"


def load_conversation(conv_id):
    path = conversation_file(conv_id)
    if not path.exists():
        return {
            "id": conv_id,
            "title": "New chat",
            "updated": datetime.now().isoformat(),
            "messages": [],
            "project": "",
            "archived": False,
        }
    with open(path, "r", encoding="utf-8") as f:
        conv = json.load(f)
    conv.setdefault("project", "")
    conv.setdefault("archived", False)
    return conv


def save_conversation(conv):
    conv["updated"] = datetime.now().isoformat()
    with open(conversation_file(conv["id"]), "w", encoding="utf-8") as f:
        json.dump(conv, f, indent=2, ensure_ascii=False)


def _conv_archived(conv: dict) -> bool:
    return bool(conv.get("archived"))


def list_conversations(project_filter: str | None = None, archived_mode: str = "active"):
    """
    archived_mode: 'active' (solo no archivadas), 'archived' (solo archivadas), 'all'.
    project_filter: None = todos los proyectos; str = coincidencia exacta (vacío = sin proyecto).
    """
    out = []
    for p in MEMORY_DIR.glob("conv_*.json"):
        try:
            with open(p, "r", encoding="utf-8") as f:
                conv = json.load(f)
            ar = _conv_archived(conv)
            if archived_mode == "active" and ar:
                continue
            if archived_mode == "archived" and not ar:
                continue
            proj = (conv.get("project") or "").strip()
            if project_filter is not None and proj != project_filter.strip():
                continue
            out.append(
                {
                    "id": conv["id"],
                    "title": conv.get("title", "Untitled"),
                    "updated": conv.get("updated", ""),
                    "message_count": len(conv.get("messages", [])),
                    "project": proj,
                    "archived": ar,
                }
            )
        except Exception:
            pass
    out.sort(key=lambda x: x["updated"], reverse=True)
    return out


@app.route("/api/conversation-projects", methods=["GET"])
def conversation_projects_list():
    """Nombres de proyecto distintos usados en conversaciones guardadas."""
    seen = set()
    for p in MEMORY_DIR.glob("conv_*.json"):
        try:
            with open(p, "r", encoding="utf-8") as f:
                conv = json.load(f)
            seen.add((conv.get("project") or "").strip())
        except Exception:
            pass
    ordered = sorted(seen, key=lambda x: (x == "", x.lower()))
    return jsonify({"projects": ordered})


@app.route("/")
@app.route("/dashboard")
def dashboard():
    """HTML estático; Puter.js se carga en el cliente desde /api/config (puter_cdn_url)."""
    return send_from_directory(STATIC_DIR, "builder.html")


@app.route("/claude")
@app.route("/claude.html")
def claude_ui():
    """Claude-like chat UI wired to local Ollama (see /api/claude/chat/stream)."""
    return send_from_directory(STATIC_DIR, "claude.html")


@app.route("/api/health")
def health():
    ok = False
    try:
        r = requests.get(f"{CONFIG['ollama_base_url']}/api/tags", timeout=5)
        ok = r.status_code == 200
    except Exception:
        ok = False
    chat_engine = CONFIG.get("chat_engine") or "puter"
    puter_model = CONFIG.get("puter_model") or "gpt-5.4-nano"
    ollama_model = CONFIG["model"]
    gk = _groq_api_key()
    ork = _openrouter_api_key()
    return jsonify(
        {
            "status": "ok" if ok else "degraded",
            "ollama_online": ok,
            "chat_engine": chat_engine,
            "puter_model": puter_model,
            "groq_configured": bool(gk),
            "groq_model": CONFIG.get("groq_model") or "llama-3.3-70b-versatile",
            "groq_free_only": CONFIG.get("groq_free_only", True),
            "groq_no_credit_wallet": True,
            "openrouter_configured": bool(ork),
            "openrouter_model": CONFIG.get("openrouter_model") or "meta-llama/llama-3.2-3b-instruct:free",
            "openrouter_free_only": True,
            "openrouter_api_base": _openrouter_api_base(),
            "puter_cdn_url": _safe_puter_cdn_url(CONFIG.get("puter_cdn_url") or ""),
            "ollama_model": ollama_model,
            # Legacy: "model" is the Ollama model (swarm/local APIs), not the dashboard chat brain.
            "model": ollama_model,
            "base_url": CONFIG["ollama_base_url"],
            "num_ctx": CONFIG.get("num_ctx"),
            "num_predict": CONFIG.get("num_predict"),
            "keep_alive": CONFIG.get("keep_alive"),
            "conversations_count": len(list_conversations()),
        }
    )


@app.route("/api/builder/install", methods=["GET"])
def builder_install_info():
    """Expone la ruta absoluta de esta instalación (Workspace / documentación para el agente)."""
    base = BASE_DIR.resolve()
    return jsonify(
        {
            "base_dir": str(base),
            "static_dir": str((base / "static").resolve()),
            "hint": "Workspace = base_dir para ejecutar shell/git en este repo; builder_* editan el código del builder sin depender del Workspace.",
        }
    )


@app.route("/api/models")
def models():
    try:
        r = requests.get(f"{CONFIG['ollama_base_url']}/api/tags", timeout=10)
        r.raise_for_status()
        return jsonify(r.json().get("models", []))
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/config", methods=["GET", "PUT"])
def config():
    if request.method == "GET":
        safe = dict(CONFIG)
        gh = dict(safe.get("github", {}))
        if gh.get("token"):
            gh["token"] = "***"
        safe["github"] = gh
        if safe.get("groq_api_key"):
            safe["groq_api_key"] = "***"
        if safe.get("openrouter_api_key"):
            safe["openrouter_api_key"] = "***"
        return jsonify(safe)
    data = request.json or {}
    for key in [
        "ollama_base_url",
        "model",
        "temperature",
        "num_ctx",
        "num_predict",
        "keep_alive",
        "system_prompt",
        "chat_engine",
        "puter_model",
        "openrouter_http_referer",
        "openrouter_app_title",
        "openrouter_max_tokens",
        "openrouter_agent_max_rounds",
        "swarm_llm_backend",
    ]:
        if key in data:
            CONFIG[key] = data[key]
    if "groq_free_only" in data:
        CONFIG["groq_free_only"] = bool(data.get("groq_free_only"))
    if "groq_model" in data:
        gm = (data.get("groq_model") or "").strip()
        if gm:
            try:
                _enforce_groq_free_tier_model(gm)
            except ValueError as e:
                return jsonify({"error": str(e)}), 400
        CONFIG["groq_model"] = gm
    if "openrouter_model" in data:
        om = (data.get("openrouter_model") or "").strip()
        if om:
            try:
                _enforce_openrouter_free_model_id(om)
            except ValueError as e:
                return jsonify({"error": str(e)}), 400
        CONFIG["openrouter_model"] = om
    if "openrouter_api_base" in data:
        CONFIG["openrouter_api_base"] = _safe_openrouter_api_base(str(data.get("openrouter_api_base") or ""))
    # Solo actualizar claves si vienen con valor (no borrar al guardar con campo vacío).
    if "groq_api_key" in data:
        gk = (data.get("groq_api_key") or "").strip()
        if gk and gk != "***":
            CONFIG["groq_api_key"] = gk
    if "openrouter_api_key" in data:
        ok = (data.get("openrouter_api_key") or "").strip()
        if ok and ok != "***":
            CONFIG["openrouter_api_key"] = ok
    if data.get("clear_groq_key") is True:
        CONFIG["groq_api_key"] = ""
    if data.get("clear_openrouter_key") is True:
        CONFIG["openrouter_api_key"] = ""
    if "github" in data and isinstance(data["github"], dict):
        incoming = data["github"]
        ghc = CONFIG["github"]
        for key in ("owner", "repo", "branch"):
            if key in incoming:
                v = (incoming.get(key) or "").strip()
                if key == "branch" and not v:
                    v = "main"
                ghc[key] = v
        if "token" in incoming:
            t = (incoming.get("token") or "").strip()
            if t and t != "***":
                ghc["token"] = t
        if incoming.get("clear_github_token") is True:
            ghc["token"] = ""
    if "local_roots" in data and isinstance(data["local_roots"], list):
        CONFIG["local_roots"] = data["local_roots"]
    if "skill_dirs" in data and isinstance(data["skill_dirs"], list):
        CONFIG["skill_dirs"] = data["skill_dirs"]
    if "puter_cdn_url" in data:
        CONFIG["puter_cdn_url"] = _safe_puter_cdn_url(str(data.get("puter_cdn_url") or ""))
    save_config()
    return jsonify({"success": True})


@app.route("/api/chat", methods=["POST"])
def chat():
    data = request.json or {}
    query = (data.get("query") or "").strip()
    if not query:
        return jsonify({"error": "query is required"}), 400
    conv_id = data.get("conv_id") or str(uuid.uuid4())
    conv = load_conversation(conv_id)
    if conv.get("title") == "New chat":
        conv["title"] = query[:60]

    history = conv.get("messages", [])[-10:]
    messages = [{"role": "system", "content": "You are an uncensored local builder assistant. Be direct and practical."}]
    messages.extend(history)
    messages.append({"role": "user", "content": query})

    try:
        r = ollama_chat(messages, stream=False)
        r.raise_for_status()
        resp = r.json()
        text = resp["message"]["content"]
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    conv["messages"].append({"role": "user", "content": query, "timestamp": datetime.now().isoformat()})
    conv["messages"].append({"role": "assistant", "content": text, "timestamp": datetime.now().isoformat()})
    save_conversation(conv)
    return jsonify({"conv_id": conv_id, "response": text})


@app.route("/api/chat/stream", methods=["POST"])
def chat_stream():
    data = request.json or {}
    query = (data.get("query") or "").strip()
    if not query:
        return jsonify({"error": "query is required"}), 400
    conv_id = data.get("conv_id") or str(uuid.uuid4())
    conv = load_conversation(conv_id)
    if conv.get("title") == "New chat":
        conv["title"] = query[:60]

    history = conv.get("messages", [])[-10:]
    messages = [{"role": "system", "content": "You are an uncensored local builder assistant. Be direct and practical."}]
    messages.extend(history)
    messages.append({"role": "user", "content": query})

    def generate():
        final_text = ""
        try:
            r = ollama_chat(messages, stream=True)
            r.raise_for_status()
            for line in r.iter_lines():
                if not line:
                    continue
                obj = json.loads(line.decode("utf-8"))
                token = obj.get("message", {}).get("content", "")
                done = obj.get("done", False)
                if token:
                    final_text += token
                    yield f"data: {json.dumps({'token': token})}\n\n"
                if done:
                    break
            conv["messages"].append({"role": "user", "content": query, "timestamp": datetime.now().isoformat()})
            conv["messages"].append({"role": "assistant", "content": final_text, "timestamp": datetime.now().isoformat()})
            save_conversation(conv)
            yield f"data: {json.dumps({'done': True, 'conv_id': conv_id})}\n\n"
        except Exception as e:
            yield f"data: {json.dumps({'error': str(e)})}\n\n"

    return Response(stream_with_context(generate()), mimetype="text/event-stream")


@app.route("/api/chat/groq/stream", methods=["POST"])
def chat_groq_stream():
    """Chat con Groq (API OpenAI-compatible). Requiere GROQ_API_KEY o groq_api_key en config."""
    data = request.json or {}
    raw_messages = data.get("messages")
    if not raw_messages or not isinstance(raw_messages, list):
        return jsonify({"error": "messages (array) required"}), 400
    raw_messages = _clamp_chat_messages(raw_messages, _CLOUD_GROQ_HISTORY_MAX_CHARS)
    model = (data.get("model") or "").strip() or CONFIG.get("groq_model") or "llama-3.3-70b-versatile"
    key = _groq_api_key()
    if not key:
        return jsonify({"error": "Groq API key missing: set in Settings or export GROQ_API_KEY"}), 400
    try:
        _enforce_groq_free_tier_model(model)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    def generate():
        try:
            url = "https://api.groq.com/openai/v1/chat/completions"
            payload = {
                "model": model,
                "messages": raw_messages,
                "stream": True,
                "temperature": float(CONFIG.get("temperature", 0.7)),
                "max_tokens": min(32768, int(CONFIG.get("num_predict", 4096) or 4096)),
            }
            r = requests.post(
                url,
                headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                json=payload,
                stream=True,
                timeout=180,
            )
            if r.status_code >= 400:
                try:
                    ej = r.json()
                    inner = ej.get("error") if isinstance(ej, dict) else None
                    if isinstance(inner, dict):
                        msg = inner.get("message") or inner.get("type") or str(inner)
                    else:
                        msg = str(inner or ej)
                except Exception:
                    msg = (r.text or r.reason)[:4000]
                msg = _groq_error_hint(r.status_code, msg)
                yield f"data: {json.dumps({'error': f'Groq {r.status_code}: {msg}'})}\n\n"
                return
            for line in r.iter_lines():
                if not line:
                    continue
                if line.startswith(b"data: "):
                    chunk = line[6:]
                    if chunk.strip() == b"[DONE]":
                        break
                    try:
                        obj = json.loads(chunk.decode("utf-8"))
                        choices = obj.get("choices") or []
                        if not choices:
                            continue
                        delta = choices[0].get("delta") or {}
                        content = delta.get("content") or ""
                        if content:
                            yield f"data: {json.dumps({'token': content})}\n\n"
                    except Exception:
                        continue
            yield f"data: {json.dumps({'done': True})}\n\n"
        except Exception as e:
            yield f"data: {json.dumps({'error': str(e)})}\n\n"

    return Response(stream_with_context(generate()), mimetype="text/event-stream")


@app.route("/api/chat/openrouter/stream", methods=["POST"])
def chat_openrouter_stream():
    """Chat vía OpenRouter (API OpenAI-compatible). Requiere OPENROUTER_API_KEY o openrouter_api_key."""
    data = request.json or {}
    raw_messages = data.get("messages")
    if not raw_messages or not isinstance(raw_messages, list):
        return jsonify({"error": "messages (array) required"}), 400
    raw_messages = _clamp_chat_messages(raw_messages, _CLOUD_OPENROUTER_HISTORY_MAX_CHARS)
    model = (data.get("model") or "").strip() or CONFIG.get("openrouter_model") or "meta-llama/llama-3.2-3b-instruct:free"
    key = _openrouter_api_key()
    if not key:
        return jsonify({"error": "OpenRouter API key missing: Settings o export OPENROUTER_API_KEY"}), 400
    try:
        _enforce_openrouter_free_model_id(model)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    def generate():
        try:
            url = _openrouter_endpoint("chat/completions")
            payload = {
                "model": model,
                "messages": raw_messages,
                "stream": True,
                "temperature": float(CONFIG.get("temperature", 0.7)),
                "max_tokens": _openrouter_max_tokens(),
            }
            hdrs = {
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json",
                "User-Agent": "Uncensored-Builder/1.0 (local; https://openrouter.ai)",
            }
            hdrs.update(_openrouter_extra_headers())
            r = _openrouter_post_with_retries(
                url, hdrs, payload, stream=True, timeout=300, max_attempts=4
            )
            if r.status_code >= 400:
                try:
                    ej = r.json()
                    inner = ej.get("error") if isinstance(ej, dict) else None
                    if isinstance(inner, dict):
                        msg = inner.get("message") or inner.get("type") or str(inner)
                    else:
                        msg = str(inner or ej)
                except Exception:
                    msg = (r.text or r.reason)[:4000]
                msg = _openrouter_error_hint(r.status_code, msg)
                yield f"data: {json.dumps({'error': f'OpenRouter {r.status_code}: {msg}'})}\n\n"
                return
            for line in r.iter_lines():
                if not line:
                    continue
                if line.startswith(b"data: "):
                    chunk = line[6:]
                    if chunk.strip() == b"[DONE]":
                        break
                    try:
                        obj = json.loads(chunk.decode("utf-8"))
                        choices = obj.get("choices") or []
                        if not choices:
                            continue
                        delta = choices[0].get("delta") or {}
                        content = delta.get("content") or ""
                        reasoning = delta.get("reasoning") or ""
                        if isinstance(reasoning, dict):
                            reasoning = reasoning.get("text") or json.dumps(reasoning, ensure_ascii=False)[:4000]
                        if isinstance(reasoning, str) and reasoning:
                            yield f"data: {json.dumps({'reasoning': reasoning})}\n\n"
                        if content:
                            yield f"data: {json.dumps({'token': content})}\n\n"
                    except Exception:
                        continue
            yield f"data: {json.dumps({'done': True})}\n\n"
        except Exception as e:
            yield f"data: {json.dumps({'error': str(e)})}\n\n"

    return Response(stream_with_context(generate()), mimetype="text/event-stream")


@app.route("/api/chat/openrouter/agent", methods=["POST"])
def chat_openrouter_agent():
    """
    Bucle agente: OpenRouter con tools (web_search, workspace_run, builder_read/write).
    El modelo debe soportar tool_calls (muchas variantes Claude/GPT en OpenRouter).
    """
    data = request.json or {}
    raw_messages = data.get("messages")
    if not raw_messages or not isinstance(raw_messages, list):
        return jsonify({"error": "messages (array) required"}), 400
    raw_messages = _clamp_chat_messages(raw_messages, _CLOUD_OPENROUTER_HISTORY_MAX_CHARS)
    model = (data.get("model") or "").strip() or CONFIG.get("openrouter_model") or "meta-llama/llama-3.2-3b-instruct:free"
    workspace_path = (data.get("workspace_path") or "").strip()
    if not _openrouter_api_key():
        return jsonify({"error": "OpenRouter API key missing: Settings o export OPENROUTER_API_KEY"}), 400
    try:
        _enforce_openrouter_free_model_id(model)
    except ValueError as e:
        return jsonify({"error": str(e), "steps": []}), 400

    messages = list(raw_messages)
    wp_note = (
        f"\n\nSession workspace_path for workspace_run (cwd): {workspace_path}"
        if workspace_path
        else "\n\nworkspace_path is empty: tell the user to set Workspace path in the Builder UI before running shell commands."
    )
    if messages and messages[0].get("role") == "system":
        c0 = messages[0].get("content") or ""
        if isinstance(c0, str):
            messages[0] = {"role": "system", "content": c0 + OPENROUTER_AGENT_SYSTEM_SUFFIX + wp_note}
        else:
            messages.insert(0, {"role": "system", "content": OPENROUTER_AGENT_SYSTEM_SUFFIX + wp_note})
    else:
        messages.insert(0, {"role": "system", "content": OPENROUTER_AGENT_SYSTEM_SUFFIX + wp_note})

    tools = _agent_tool_definitions()
    max_rounds = _openrouter_agent_max_rounds()
    steps = []

    try:
        for _round in range(max_rounds):
            payload = {
                "model": model,
                "messages": messages,
                "tools": tools,
                "tool_choice": "auto",
                "temperature": float(CONFIG.get("temperature", 0.7)),
                "max_tokens": _openrouter_max_tokens(),
            }
            resp = _openrouter_chat_completion_nonstream(
                payload, timeout=_OPENROUTER_AGENT_HTTP_TIMEOUT_SEC
            )
            choices = resp.get("choices") or []
            if not choices:
                return jsonify({"error": "empty choices from OpenRouter", "steps": steps}), 502
            msg = (choices[0] or {}).get("message") or {}
            finish = (choices[0] or {}).get("finish_reason")

            tcalls = msg.get("tool_calls")
            if tcalls:
                messages.append(msg)
                for tc in tcalls:
                    if not isinstance(tc, dict):
                        continue
                    tid = tc.get("id") or ""
                    fn = (tc.get("function") or {}).get("name") or ""
                    raw_args = (tc.get("function") or {}).get("arguments") or "{}"
                    result = _execute_openrouter_agent_tool(fn, raw_args, workspace_path)
                    if len(result) > _MAX_AGENT_TOOL_RESULT:
                        result = result[: -100] + "\n...[truncated]"
                    steps.append(
                        {
                            "tool": fn,
                            "arguments": raw_args[:4000],
                            "result_preview": result[:2500],
                        }
                    )
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": tid,
                            "content": result,
                        }
                    )
                continue

            content = msg.get("content")
            if isinstance(content, list):
                parts = []
                for p in content:
                    if isinstance(p, dict) and p.get("type") == "text":
                        parts.append(p.get("text") or "")
                    elif isinstance(p, str):
                        parts.append(p)
                content = "".join(parts)
            if content is None:
                content = ""
            return jsonify(
                {
                    "ok": True,
                    "message": content,
                    "finish_reason": finish,
                    "steps": steps,
                    "rounds_used": _round + 1,
                }
            )

        return jsonify(
            {
                "ok": False,
                "error": f"agent_max_rounds ({max_rounds}) reached without final answer",
                "steps": steps,
            }
        ), 502
    except ValueError as e:
        return jsonify({"error": str(e), "steps": steps}), 400
    except requests.exceptions.Timeout:
        return jsonify(
            {
                "error": (
                    f"Timeout esperando a OpenRouter ({_OPENROUTER_AGENT_HTTP_TIMEOUT_SEC}s). "
                    "Prueba otro modelo, acorta el chat (Nueva conversación) o desactiva modo agente."
                ),
                "steps": steps,
            }
        ), 504
    except Exception as e:
        return jsonify({"error": str(e), "steps": steps}), 500


@app.route("/api/groq/test", methods=["GET"])
def groq_test():
    """Comprueba la API key con GET /openai/v1/models (útil si ves 404/Not Found)."""
    key = _groq_api_key()
    if not key:
        return jsonify({"ok": False, "error": "Sin clave: Settings o GROQ_API_KEY"}), 200
    try:
        r = requests.get(
            "https://api.groq.com/openai/v1/models",
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            timeout=20,
        )
        if r.status_code >= 400:
            try:
                ej = r.json()
                inner = ej.get("error") if isinstance(ej, dict) else ej
                if isinstance(inner, dict):
                    msg = inner.get("message", str(inner))
                else:
                    msg = str(inner)
            except Exception:
                msg = (r.text or "")[:800]
            return jsonify({"ok": False, "http": r.status_code, "error": msg}), 200
        data = r.json()
        ids = [m.get("id") for m in (data.get("data") or []) if m.get("id")]
        return jsonify({"ok": True, "model_ids_sample": ids[:40], "total": len(ids)}), 200
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 200


@app.route("/api/openrouter/test", methods=["GET"])
def openrouter_test():
    """Lista modelos disponibles (GET /api/v1/models) para validar la clave OpenRouter."""
    key = _openrouter_api_key()
    if not key:
        return jsonify({"ok": False, "error": "Sin clave: Settings o OPENROUTER_API_KEY"}), 200
    try:
        hdrs = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
        hdrs.update(_openrouter_extra_headers())
        r = requests.get(
            _openrouter_endpoint("models"),
            headers=hdrs,
            timeout=30,
        )
        if r.status_code >= 400:
            try:
                ej = r.json()
                inner = ej.get("error") if isinstance(ej, dict) else ej
                if isinstance(inner, dict):
                    msg = inner.get("message", str(inner))
                else:
                    msg = str(inner)
            except Exception:
                msg = (r.text or "")[:800]
            msg = _openrouter_error_hint(r.status_code, msg)
            return jsonify({"ok": False, "http": r.status_code, "error": msg}), 200
        data = r.json()
        ids = [m.get("id") for m in (data.get("data") or []) if m.get("id")]
        return jsonify({"ok": True, "model_ids_sample": ids[:50], "total": len(ids)}), 200
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 200


def _is_free_openrouter_model(m: dict) -> bool:
    """Gratis: variante :free, router openrouter/free, o precio prompt+completion = 0."""
    mid = (m.get("id") or "").lower()
    if mid == "openrouter/free" or ":free" in mid:
        return True
    pr = m.get("pricing") or {}

    def _f(x):
        try:
            if x is None or x == "":
                return 0.0
            return float(x)
        except (TypeError, ValueError):
            return 0.0

    return _f(pr.get("prompt")) == 0.0 and _f(pr.get("completion")) == 0.0


def _pricing_json_safe(pr: dict) -> dict:
    """Evita que pricing anidado rompa jsonify o supere tamaño."""
    if not isinstance(pr, dict):
        return {}
    out = {}
    for k in ("prompt", "completion", "request"):
        v = pr.get(k)
        if v is None:
            continue
        if isinstance(v, (int, float, str, bool)):
            out[k] = v
        elif isinstance(v, dict):
            out[k] = json.dumps(v, ensure_ascii=False)[:160]
        else:
            out[k] = str(v)[:160]
    return out


def _openrouter_cache_read(ignore_ttl: bool = False):
    if not OPENROUTER_MODELS_CACHE_PATH.exists():
        return None
    try:
        with open(OPENROUTER_MODELS_CACHE_PATH, "r", encoding="utf-8") as f:
            pack = json.load(f)
        models = pack.get("models")
        if not isinstance(models, list):
            return None
        if not ignore_ttl:
            ts = pack.get("fetched_at_ts") or 0
            if time.time() - float(ts) > OPENROUTER_CACHE_TTL_SEC:
                return None
        return models
    except Exception:
        return None


def _openrouter_cache_write(models: list):
    try:
        pack = {"fetched_at_ts": time.time(), "fetched_at": datetime.now().isoformat(), "models": models}
        with open(OPENROUTER_MODELS_CACHE_PATH, "w", encoding="utf-8") as f:
            json.dump(pack, f, ensure_ascii=False)
    except Exception:
        pass


def _openrouter_fetch_models_raw():
    """Descarga lista cruda desde OpenRouter. Devuelve (models_list|None, error_str|None)."""
    key = _openrouter_api_key()
    if not key:
        return None, "Sin clave: Settings o OPENROUTER_API_KEY"
    hdrs = {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": "Uncensored-Builder/1.0 (local; https://openrouter.ai)",
    }
    hdrs.update(_openrouter_extra_headers())
    try:
        r = requests.get(
            _openrouter_endpoint("models"),
            headers=hdrs,
            timeout=(25, 150),
        )
    except requests.exceptions.Timeout as e:
        return None, f"Timeout OpenRouter: {e}"
    except requests.exceptions.RequestException as e:
        return None, f"Red OpenRouter: {e}"
    if r.status_code >= 400:
        try:
            ej = r.json()
            inner = ej.get("error") if isinstance(ej, dict) else ej
            msg = inner.get("message", str(inner)) if isinstance(inner, dict) else str(inner)
        except Exception:
            msg = (r.text or "")[:800]
        msg = _openrouter_error_hint(r.status_code, msg)
        return None, f"HTTP {r.status_code}: {msg}"
    try:
        raw = r.json()
    except json.JSONDecodeError as e:
        return None, f"JSON inválido de OpenRouter: {e}"
    models = raw.get("data")
    if not isinstance(models, list):
        models = raw.get("models")
    if not isinstance(models, list):
        return None, "Respuesta sin lista data/models"
    return models, None


def _openrouter_models_catalog() -> list:
    """Catálogo para validar precio: caché (sin caducar si hace falta) o red."""
    raw = _openrouter_cache_read(ignore_ttl=True)
    if isinstance(raw, list) and raw:
        return raw
    models, _err = _openrouter_fetch_models_raw()
    if isinstance(models, list) and models:
        try:
            _openrouter_cache_write(models)
        except Exception:
            pass
        return models
    return []


def _enforce_openrouter_free_model_id(model_id: str) -> None:
    """
    Solo modelos gratuitos en OpenRouter: :free, openrouter/free, o precio prompt+completion = 0 en el catálogo.
    Si el ID no está en caché/red, solo se acepta si el nombre contiene :free (convención OpenRouter).
    """
    mid = (model_id or "").strip()
    if not mid:
        raise ValueError("Modelo OpenRouter vacío.")
    models = _openrouter_models_catalog()
    for m in models:
        if not isinstance(m, dict) or m.get("id") != mid:
            continue
        if _is_free_openrouter_model(m):
            return
        raise ValueError(
            f"El modelo «{mid}» tiene coste en OpenRouter. Solo se permiten modelos gratuitos; "
            "elige uno con :free o precio 0 en la lista (solo gratis)."
        )
    mlow = mid.lower()
    if mlow == "openrouter/free" or ":free" in mlow:
        return
    raise ValueError(
        f"Modelo «{mid}» no está en el catálogo local o no es claramente gratuito. "
        "Abre la lista de modelos con «solo gratis», elige un ID, o usa un modelo que termine en :free."
    )


@app.route("/api/openrouter/models", methods=["GET"])
def openrouter_models_list():
    """
    Lista modelos OpenRouter (misma fuente que la consola).
    ?free_only=1 → solo gratuitos (:free, openrouter/free o precio 0).
    ?refresh=1 → ignora caché y vuelve a pedir la red.
    """
    key = _openrouter_api_key()
    if not key:
        return jsonify({"ok": False, "error": "Sin clave: Settings o OPENROUTER_API_KEY"}), 200
    free_only = request.args.get("free_only", "0").lower() in ("1", "true", "yes", "on")
    force_refresh = request.args.get("refresh", "0").lower() in ("1", "true", "yes", "on")

    models_raw = None
    stale = False
    err_net = None

    if not force_refresh:
        models_raw = _openrouter_cache_read(ignore_ttl=False)

    if models_raw is None:
        models_raw, err_net = _openrouter_fetch_models_raw()
        if models_raw is not None:
            _openrouter_cache_write(models_raw)
        elif err_net:
            models_raw = _openrouter_cache_read(ignore_ttl=True)
            if models_raw is not None:
                stale = True

    if models_raw is None:
        return jsonify({"ok": False, "error": err_net or "No se pudo obtener ni leer caché de modelos"}), 200

    out = []
    for m in models_raw:
        if not isinstance(m, dict):
            continue
        mid = m.get("id")
        if not mid:
            continue
        if free_only and not _is_free_openrouter_model(m):
            continue
        pr = m.get("pricing") or {}
        out.append(
            {
                "id": mid,
                "name": m.get("name") or mid,
                "free": _is_free_openrouter_model(m),
                "pricing": _pricing_json_safe(pr),
            }
        )
    out.sort(key=lambda x: (not x["free"], (x["name"] or "").lower()))
    hint = None
    if free_only and len(out) == 0:
        hint = "Con «solo gratis» no hay coincidencias; prueba sin el filtro o revisa openrouter.ai/models."
    return jsonify(
        {
            "ok": True,
            "free_only": free_only,
            "total": len(out),
            "models": out,
            "stale": stale,
            "hint": hint,
        }
    ), 200


@app.route("/api/claude/chat/stream", methods=["POST"])
def claude_chat_stream():
    """
    Claude-style UI: full message history from the browser, streamed via local Ollama only.
    Does not use Puter, Anthropic, or any remote Claude API.
    """
    data = request.json or {}
    raw_messages = data.get("messages") or []
    conv_id = data.get("conv_id") or str(uuid.uuid4())
    system_override = (data.get("system") or "").strip()
    system_content = system_override or CONFIG.get("system_prompt") or DEFAULT_CONFIG["system_prompt"]

    ollama_messages = [{"role": "system", "content": system_content}]
    for m in raw_messages:
        role = m.get("role")
        content = (m.get("content") or "").strip()
        if role not in ("user", "assistant") or not content:
            continue
        ollama_messages.append({"role": role, "content": content})

    if len(ollama_messages) < 2:
        return jsonify({"error": "At least one user message is required."}), 400

    def generate():
        final_text = ""
        try:
            r = ollama_chat(ollama_messages, stream=True)
            r.raise_for_status()
            for line in r.iter_lines():
                if not line:
                    continue
                obj = json.loads(line.decode("utf-8"))
                token = obj.get("message", {}).get("content", "")
                done = obj.get("done", False)
                if token:
                    final_text += token
                    yield f"data: {json.dumps({'token': token})}\n\n"
                if done:
                    break
            if final_text.strip():
                conv = load_conversation(conv_id)
                ts = datetime.now().isoformat()
                stored = []
                for m in raw_messages:
                    role = m.get("role")
                    content = (m.get("content") or "").strip()
                    if role in ("user", "assistant") and content:
                        stored.append(
                            {"role": role, "content": content, "timestamp": ts}
                        )
                stored.append(
                    {"role": "assistant", "content": final_text, "timestamp": ts}
                )
                conv["messages"] = stored
                first_user = next(
                    (x.get("content", "") for x in raw_messages if x.get("role") == "user"),
                    "",
                )
                if first_user and conv.get("title") in ("New chat", "Untitled"):
                    conv["title"] = first_user[:60]
                save_conversation(conv)
            yield f"data: {json.dumps({'done': True, 'conv_id': conv_id})}\n\n"
        except Exception as e:
            yield f"data: {json.dumps({'error': str(e)})}\n\n"

    return Response(stream_with_context(generate()), mimetype="text/event-stream")


@app.route("/api/conversations")
def conversations():
    am = (request.args.get("archived") or "active").strip().lower()
    if am in ("1", "true", "yes", "archived"):
        archived_mode = "archived"
    elif am in ("all", "*"):
        archived_mode = "all"
    else:
        archived_mode = "active"
    proj = request.args.get("project")
    if proj is not None:
        proj = proj.strip()
    return jsonify(list_conversations(project_filter=proj, archived_mode=archived_mode))


@app.route("/api/conversations/save", methods=["POST"])
def conversations_save():
    """Crea o actualiza una conversación (chat Puter / OpenRouter / Groq desde el cliente)."""
    data = request.json or {}
    conv_id = (data.get("id") or "").strip() or str(uuid.uuid4())
    conv = load_conversation(conv_id)
    conv["id"] = conv_id
    if "messages" in data and isinstance(data["messages"], list):
        conv["messages"] = data["messages"]
    if "title" in data:
        t = (data.get("title") or "").strip()
        if t:
            conv["title"] = t[:200]
    if "project" in data:
        conv["project"] = (data.get("project") or "").strip()[:120]
    if "archived" in data:
        conv["archived"] = bool(data.get("archived"))
    save_conversation(conv)
    return jsonify({"ok": True, "id": conv_id})


@app.route("/api/conversations/<conv_id>", methods=["GET", "PATCH", "DELETE"])
def conversation_detail(conv_id):
    if request.method == "GET":
        return jsonify(load_conversation(conv_id))
    if request.method == "DELETE":
        path = conversation_file(conv_id)
        if path.exists():
            try:
                path.unlink()
            except OSError as e:
                return jsonify({"ok": False, "error": str(e)}), 500
        return jsonify({"ok": True})
    data = request.json or {}
    conv = load_conversation(conv_id)
    if "title" in data:
        t = (data.get("title") or "").strip()
        if t:
            conv["title"] = t[:200]
    if "project" in data:
        conv["project"] = (data.get("project") or "").strip()[:120]
    if "archived" in data:
        conv["archived"] = bool(data.get("archived"))
    save_conversation(conv)
    return jsonify({"ok": True})


@app.route("/api/upload", methods=["POST"])
def upload():
    if "file" not in request.files:
        return jsonify({"error": "No file uploaded"}), 400
    f = request.files["file"]
    if not f.filename:
        return jsonify({"error": "Invalid filename"}), 400
    dst = UPLOADS_DIR / Path(f.filename).name
    f.save(dst)
    return jsonify({"success": True, "filename": dst.name, "size": dst.stat().st_size})


@app.route("/api/files")
def files():
    out = []
    for p in sorted(UPLOADS_DIR.glob("*"), key=lambda x: x.stat().st_mtime, reverse=True):
        out.append({"name": p.name, "size": p.stat().st_size, "modified": datetime.fromtimestamp(p.stat().st_mtime).isoformat()})
    return jsonify(out)


@app.route("/api/file/<path:name>", methods=["GET", "DELETE"])
def file_get_or_delete(name):
    """GET: sirve archivo de uploads/. DELETE: elimina (sin path traversal)."""
    safe = Path(str(name).replace("\\", "/")).name
    if not safe:
        return jsonify({"error": "invalid name"}), 400
    target = (UPLOADS_DIR / safe).resolve()
    try:
        target.relative_to(UPLOADS_DIR.resolve())
    except ValueError:
        return jsonify({"error": "path not allowed"}), 400
    if request.method == "DELETE":
        if not target.is_file():
            return jsonify({"error": "not found"}), 404
        try:
            target.unlink()
        except OSError as e:
            return jsonify({"error": str(e)}), 500
        return jsonify({"ok": True})
    return send_from_directory(UPLOADS_DIR, safe, as_attachment=False)


@app.route("/api/files/delete", methods=["POST"])
def files_delete_bulk():
    """Elimina varios archivos de uploads/. Body: { \"names\": [\"a.txt\", ...] }"""
    data = request.json or {}
    names = data.get("names")
    if not isinstance(names, list) or not names:
        return jsonify({"error": "names (non-empty array) required"}), 400
    deleted = []
    errors = []
    for raw in names:
        safe = Path(str(raw).replace("\\", "/")).name
        if not safe:
            errors.append({"name": raw, "error": "invalid"})
            continue
        target = (UPLOADS_DIR / safe).resolve()
        try:
            target.relative_to(UPLOADS_DIR.resolve())
        except ValueError:
            errors.append({"name": safe, "error": "path not allowed"})
            continue
        if target.is_file():
            try:
                target.unlink()
                deleted.append(safe)
            except OSError as e:
                errors.append({"name": safe, "error": str(e)})
    return jsonify({"ok": True, "deleted": deleted, "errors": errors})


@app.route("/api/web/search", methods=["POST"])
def web_search():
    query = (request.json or {}).get("query", "").strip()
    if not query:
        return jsonify({"error": "query is required"}), 400
    try:
        r = requests.get(
            "https://duckduckgo.com/html/",
            params={"q": query},
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=15,
        )
        r.raise_for_status()
        html = r.text
        snippets = []
        for chunk in html.split('result__a')[:6]:
            if 'href="' in chunk:
                href = chunk.split('href="', 1)[1].split('"', 1)[0]
                title = chunk.split(">", 1)[1].split("<", 1)[0] if ">" in chunk else href
                snippets.append({"title": title.strip(), "url": href})
        return jsonify({"query": query, "results": snippets})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


def run_role_agent(role, task, context, llm_backend=None, model=None):
    """
    Un rol del swarm. `llm_backend`: ollama | openrouter | groq (o None → config swarm_llm_backend).
    `model`: ID opcional (OpenRouter/Groq); si vacío usa el modelo por defecto de Settings.
    """
    role_prompts = {
        "researcher": "Researcher agent: gather facts, references, and options.",
        "designer": "Designer agent: define UX, UI structure, and interaction flow.",
        "coder": "Coder agent: produce technical implementation plan and code edits.",
        "reviewer": "Reviewer agent: identify risks, bugs, and missing tests.",
    }
    sys = role_prompts.get(role, f"{role} agent: complete your role-specific task.")
    messages = [
        {"role": "system", "content": sys},
        {
            "role": "user",
            "content": f"Task:\n{task}\n\nShared context:\n{context}\n\nReturn concise, actionable output.",
        },
    ]
    backend = (llm_backend or CONFIG.get("swarm_llm_backend") or "ollama").strip().lower()
    if backend not in ("ollama", "openrouter", "groq"):
        backend = "ollama"

    try:
        if backend == "openrouter":
            mid = (model or "").strip() or CONFIG.get("openrouter_model") or "meta-llama/llama-3.2-3b-instruct:free"
            data = _openrouter_chat_completion_nonstream(
                {
                    "model": mid,
                    "messages": messages,
                    "temperature": float(CONFIG.get("temperature", 0.7)),
                    "max_tokens": _openrouter_max_tokens(),
                }
            )
            return _extract_openai_style_message_text(data)

        if backend == "groq":
            data = _groq_chat_completion_nonstream(messages, model=model)
            return _extract_openai_style_message_text(data)

        r = ollama_chat(messages, stream=False)
        r.raise_for_status()
        return r.json()["message"]["content"]
    except Exception as e:
        if backend == "ollama":
            base = CONFIG.get("ollama_base_url", "http://127.0.0.1:11434")
            om = CONFIG.get("model", "?")
            raise RuntimeError(
                f"Swarm (Ollama) requiere daemon en {base} con modelo «{om}». {e}"
            ) from e
        raise RuntimeError(f"Swarm ({backend}): {e}") from e


def git_run(args, cwd):
    return subprocess.run(args, cwd=str(cwd), capture_output=True, text=True, check=False)


def slugify_branch(name):
    out = []
    for ch in name:
        if ch.isalnum() or ch in "-_/.":
            out.append(ch)
        else:
            out.append("-")
    return "".join(out).strip("-") or "agent-branch"


def ensure_branch(workspace: Path, branch: str):
    current = git_run(["git", "branch", "--show-current"], workspace).stdout.strip()
    if current == branch:
        return
    exists = git_run(["git", "branch", "--list", branch], workspace).stdout.strip()
    if exists:
        git_run(["git", "checkout", branch], workspace)
    else:
        git_run(["git", "checkout", "-b", branch], workspace)


def ensure_worktree(workspace: Path, branch: str, base_branch: str):
    slug = slugify_branch(branch).replace("/", "__")
    wt = (WORKTREES_DIR / slug).resolve()
    if wt.exists():
        return wt
    add = git_run(["git", "worktree", "add", "-B", branch, str(wt), base_branch], workspace)
    if add.returncode != 0:
        raise RuntimeError(add.stderr.strip() or add.stdout.strip() or "failed to create worktree")
    return wt


def has_commit(workspace: Path):
    chk = git_run(["git", "rev-parse", "--verify", "HEAD"], workspace)
    return chk.returncode == 0


def run_browser_flow(url, actions):
    """
    Executes real browser interactions via Python Playwright.
    Requires: pip install playwright && python3 -m playwright install chromium
    """
    try:
        from playwright.sync_api import sync_playwright
    except Exception as e:
        raise RuntimeError(f"Playwright not installed: {e}")

    logs = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto(url, wait_until="domcontentloaded", timeout=60000)
        for a in actions:
            t = a.get("type")
            if t == "click":
                page.click(a["selector"], timeout=20000)
                logs.append(f"clicked {a['selector']}")
            elif t == "type":
                page.fill(a["selector"], a.get("text", ""))
                logs.append(f"typed on {a['selector']}")
            elif t == "wait":
                page.wait_for_timeout(int(a.get("ms", 1000)))
                logs.append(f"waited {int(a.get('ms', 1000))}ms")
            elif t == "extract":
                txt = page.text_content(a["selector"]) or ""
                logs.append(f"extract:{a['selector']}={txt[:500]}")
        result = {"ok": True, "title": page.title(), "finalUrl": page.url, "logs": logs}
        browser.close()
    return result


def run_parallel_swarm_job(job_id, task, roles, workspace_path, auto_commit, base_branch, use_worktrees, llm_backend, model):
    with JOBS_LOCK:
        JOBS[job_id]["status"] = "running"
        JOBS[job_id]["started_at"] = datetime.now().isoformat()
    context = f"Workspace: {workspace_path}"
    outputs = []
    executor = ThreadPoolExecutor(max_workers=max(1, min(6, len(roles))))
    futures = {}
    try:
        for role in roles:
            futures[executor.submit(run_role_agent, role, task, context, llm_backend, model)] = role
        for future in as_completed(futures):
            role = futures[future]
            role_output = future.result()
            branch = None
            worktree_path = None
            git_result = {"ran": False}
            if workspace_path:
                ws = Path(workspace_path).resolve()
                if ws.exists() and (ws / ".git").exists():
                    branch = f"agent/{role}-{job_id[:8]}"
                    target_dir = ws
                    if use_worktrees and has_commit(ws):
                        target_dir = ensure_worktree(ws, branch, base_branch)
                        worktree_path = str(target_dir)
                    else:
                        ensure_branch(ws, branch)
                        if use_worktrees and not has_commit(ws):
                            git_result["warning"] = "Repo has no commits yet; worktrees require an initial commit."
                    plan_path = target_dir / f"AGENT_{role.upper()}_{job_id[:8]}.md"
                    plan_path.write_text(role_output, encoding="utf-8")
                    git_result["ran"] = True
                    try:
                        git_result["plan_file"] = str(plan_path.relative_to(target_dir))
                    except Exception:
                        git_result["plan_file"] = plan_path.name
                    if auto_commit:
                        git_run(["git", "add", str(plan_path.name)], target_dir)
                        msg = f"agent({role}): add plan for {task[:60]}"
                        c = git_run(["git", "commit", "-m", msg], target_dir)
                        git_result["commit_stdout"] = c.stdout[-1200:]
                        git_result["commit_stderr"] = c.stderr[-1200:]
                        git_result["commit_code"] = c.returncode
            outputs.append({"role": role, "output": role_output, "branch": branch, "worktree_path": worktree_path, "git": git_result})
            with JOBS_LOCK:
                JOBS[job_id]["outputs"] = outputs
                JOBS[job_id]["progress"] = f"{len(outputs)}/{len(roles)}"
        with JOBS_LOCK:
            JOBS[job_id]["status"] = "done"
            JOBS[job_id]["finished_at"] = datetime.now().isoformat()
    except Exception as e:
        with JOBS_LOCK:
            JOBS[job_id]["status"] = "error"
            JOBS[job_id]["error"] = str(e)
            JOBS[job_id]["finished_at"] = datetime.now().isoformat()
    finally:
        executor.shutdown(wait=False)


def run_pipeline_swarm_job(job_id, task, roles, workspace_path, auto_commit, base_branch, use_worktrees, llm_backend, model):
    """Encadena roles: cada agente ve la salida del anterior (estilo HiveMind / beehAIve pipeline)."""
    with JOBS_LOCK:
        JOBS[job_id]["status"] = "running"
        JOBS[job_id]["started_at"] = datetime.now().isoformat()
    base_ctx = f"Workspace: {workspace_path}"
    previous = ""
    outputs = []
    try:
        for role in roles:
            combined = base_ctx + (
                f"\n\n[OUTPUT FROM PREVIOUS AGENT IN PIPELINE]\n{previous}" if previous else ""
            )
            text = run_role_agent(role, task, combined, llm_backend=llm_backend, model=model)
            previous = text
            branch = None
            worktree_path = None
            git_result = {"ran": False}
            if workspace_path:
                ws = Path(workspace_path).resolve()
                if ws.exists() and (ws / ".git").exists():
                    branch = f"agent-pipe/{role}-{job_id[:8]}"
                    target_dir = ws
                    if use_worktrees and has_commit(ws):
                        target_dir = ensure_worktree(ws, branch, base_branch)
                        worktree_path = str(target_dir)
                    else:
                        ensure_branch(ws, branch)
                        if use_worktrees and not has_commit(ws):
                            git_result["warning"] = "Repo has no commits yet; worktrees require an initial commit."
                    plan_path = target_dir / f"AGENT_PIPE_{role.upper()}_{job_id[:8]}.md"
                    plan_path.write_text(text, encoding="utf-8")
                    git_result["ran"] = True
                    try:
                        git_result["plan_file"] = str(plan_path.relative_to(target_dir))
                    except Exception:
                        git_result["plan_file"] = plan_path.name
                    if auto_commit:
                        git_run(["git", "add", str(plan_path.name)], target_dir)
                        msg = f"agent-pipeline({role}): plan for {task[:60]}"
                        c = git_run(["git", "commit", "-m", msg], target_dir)
                        git_result["commit_stdout"] = c.stdout[-1200:]
                        git_result["commit_stderr"] = c.stderr[-1200:]
                        git_result["commit_code"] = c.returncode
            outputs.append(
                {
                    "role": role,
                    "output": text,
                    "branch": branch,
                    "worktree_path": worktree_path,
                    "git": git_result,
                }
            )
            with JOBS_LOCK:
                JOBS[job_id]["outputs"] = list(outputs)
                JOBS[job_id]["progress"] = f"{len(outputs)}/{len(roles)}"
        with JOBS_LOCK:
            JOBS[job_id]["status"] = "done"
            JOBS[job_id]["finished_at"] = datetime.now().isoformat()
    except Exception as e:
        with JOBS_LOCK:
            JOBS[job_id]["status"] = "error"
            JOBS[job_id]["error"] = str(e)
            JOBS[job_id]["finished_at"] = datetime.now().isoformat()


@app.route("/api/swarm", methods=["POST"])
def swarm():
    data = request.json or {}
    task = (data.get("task") or "").strip()
    roles = data.get("roles") or ["researcher", "designer", "coder", "reviewer"]
    llm_backend = (data.get("llm_backend") or CONFIG.get("swarm_llm_backend") or "ollama").strip()
    model = (data.get("model") or "").strip() or None
    if not task:
        return jsonify({"error": "task is required"}), 400
    outputs = []
    context = "Local builder workspace (swarm)."
    try:
        for role in roles:
            text = run_role_agent(role, task, context, llm_backend=llm_backend, model=model)
            outputs.append({"role": role, "output": text})
            context += f"\n\n[{role.upper()} OUTPUT]\n{text}"
        return jsonify({"success": True, "task": task, "outputs": outputs, "llm_backend": llm_backend})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/swarm/parallel", methods=["POST"])
def swarm_parallel():
    data = request.json or {}
    task = (data.get("task") or "").strip()
    roles = data.get("roles") or ["researcher", "designer", "coder", "reviewer"]
    workspace_path = (data.get("workspace_path") or "").strip()
    auto_commit = bool(data.get("auto_commit", False))
    base_branch = (data.get("base_branch") or "main").strip()
    use_worktrees = bool(data.get("use_worktrees", True))
    llm_backend = (data.get("llm_backend") or CONFIG.get("swarm_llm_backend") or "ollama").strip()
    model = (data.get("model") or "").strip() or None
    if not task:
        return jsonify({"error": "task is required"}), 400
    job_id = str(uuid.uuid4())
    with JOBS_LOCK:
        JOBS[job_id] = {
            "id": job_id,
            "type": "parallel_swarm",
            "status": "queued",
            "task": task,
            "roles": roles,
            "outputs": [],
            "created_at": datetime.now().isoformat(),
            "progress": f"0/{len(roles)}",
            "base_branch": base_branch,
            "workspace_path": workspace_path,
            "use_worktrees": use_worktrees,
            "llm_backend": llm_backend,
            "model": model or "",
        }
    t = threading.Thread(
        target=run_parallel_swarm_job,
        args=(job_id, task, roles, workspace_path, auto_commit, base_branch, use_worktrees, llm_backend, model),
        daemon=False,
        name=f"swarm-{job_id[:8]}",
    )
    t.start()
    return jsonify({"success": True, "job_id": job_id})


@app.route("/api/swarm/pipeline", methods=["POST"])
def swarm_pipeline():
    """Swarm encadenado: cada rol recibe la salida del anterior (similar a beehAIve HiveMind pipeline)."""
    data = request.json or {}
    task = (data.get("task") or "").strip()
    roles = data.get("roles") or ["researcher", "designer", "coder", "reviewer"]
    workspace_path = (data.get("workspace_path") or "").strip()
    auto_commit = bool(data.get("auto_commit", False))
    base_branch = (data.get("base_branch") or "main").strip()
    use_worktrees = bool(data.get("use_worktrees", True))
    llm_backend = (data.get("llm_backend") or CONFIG.get("swarm_llm_backend") or "ollama").strip()
    model = (data.get("model") or "").strip() or None
    if not task:
        return jsonify({"error": "task is required"}), 400
    job_id = str(uuid.uuid4())
    with JOBS_LOCK:
        JOBS[job_id] = {
            "id": job_id,
            "type": "pipeline_swarm",
            "status": "queued",
            "task": task,
            "roles": roles,
            "outputs": [],
            "created_at": datetime.now().isoformat(),
            "progress": f"0/{len(roles)}",
            "base_branch": base_branch,
            "workspace_path": workspace_path,
            "use_worktrees": use_worktrees,
            "llm_backend": llm_backend,
            "model": model or "",
        }
    t = threading.Thread(
        target=run_pipeline_swarm_job,
        args=(job_id, task, roles, workspace_path, auto_commit, base_branch, use_worktrees, llm_backend, model),
        daemon=False,
        name=f"swarm-pipe-{job_id[:8]}",
    )
    t.start()
    return jsonify({"success": True, "job_id": job_id})


@app.route("/api/jobs", methods=["GET"])
def list_jobs():
    with JOBS_LOCK:
        items = list(JOBS.values())
    items.sort(key=lambda x: x.get("created_at", ""), reverse=True)
    return jsonify(items)


@app.route("/api/jobs/<job_id>", methods=["GET"])
def get_job(job_id):
    with JOBS_LOCK:
        job = JOBS.get(job_id)
    if not job:
        return jsonify({"error": "job not found"}), 404
    return jsonify(job)


@app.route("/api/github/connect", methods=["POST"])
def github_connect():
    data = request.json or {}
    gh = CONFIG["github"]
    for key in ["owner", "repo", "branch"]:
        if key in data:
            v = (data.get(key) or "").strip()
            if key == "branch" and not v:
                v = "main"
            gh[key] = v
    # No sobrescribir token con cadena vacía (el GET enmascara "***" y el cliente suele reenviar vacío).
    if "token" in data:
        t = (data.get("token") or "").strip()
        if t and t != "***":
            gh["token"] = t
    if data.get("clear_github_token") is True:
        gh["token"] = ""
    save_config()
    return jsonify({"success": True})


@app.route("/api/github/status")
def github_status():
    gh = CONFIG.get("github", {})
    if not gh.get("token") or not gh.get("owner") or not gh.get("repo"):
        return jsonify({"connected": False})
    try:
        r = requests.get(
            f"https://api.github.com/repos/{gh['owner']}/{gh['repo']}",
            headers={"Authorization": f"Bearer {gh['token']}", "Accept": "application/vnd.github+json"},
            timeout=15,
        )
        if r.status_code >= 300:
            return jsonify({"connected": False, "error": f"GitHub API error {r.status_code}"}), 200
        return jsonify({"connected": True, "repo": f"{gh['owner']}/{gh['repo']}", "default_branch": r.json().get("default_branch")})
    except Exception as e:
        return jsonify({"connected": False, "error": str(e)}), 200


@app.route("/api/github/tree")
def github_tree():
    gh = CONFIG.get("github", {})
    if not gh.get("token") or not gh.get("owner") or not gh.get("repo"):
        return jsonify({"error": "GitHub is not connected"}), 400
    branch = gh.get("branch") or "main"
    try:
        r = requests.get(
            f"https://api.github.com/repos/{gh['owner']}/{gh['repo']}/git/trees/{branch}?recursive=1",
            headers={"Authorization": f"Bearer {gh['token']}", "Accept": "application/vnd.github+json"},
            timeout=20,
        )
        r.raise_for_status()
        tree = r.json().get("tree", [])
        files = [x["path"] for x in tree if x.get("type") == "blob"][:500]
        return jsonify({"files": files})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/github/repos")
def github_list_repos():
    gh = CONFIG.get("github", {})
    if not gh.get("token"):
        return jsonify({"error": "Configura el token de GitHub primero"}), 400
    try:
        r = requests.get(
            "https://api.github.com/user/repos",
            params={"per_page": 40, "sort": "updated"},
            headers={"Authorization": f"Bearer {gh['token']}", "Accept": "application/vnd.github+json"},
            timeout=25,
        )
        r.raise_for_status()
        repos = [
            {
                "full_name": x["full_name"],
                "default_branch": x.get("default_branch") or "main",
                "private": x.get("private", False),
            }
            for x in r.json()
        ]
        return jsonify({"repos": repos})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/github/file")
def github_file_content():
    path = request.args.get("path", "").strip().lstrip("/")
    if not path:
        return jsonify({"error": "path query is required"}), 400
    gh = CONFIG.get("github", {})
    if not gh.get("token") or not gh.get("owner") or not gh.get("repo"):
        return jsonify({"error": "GitHub is not connected"}), 400
    branch = gh.get("branch") or "main"
    try:
        path_enc = "/".join(quote(seg, safe="") for seg in path.split("/") if seg != "")
        r = requests.get(
            f"https://api.github.com/repos/{gh['owner']}/{gh['repo']}/contents/{path_enc}",
            params={"ref": branch},
            headers={"Authorization": f"Bearer {gh['token']}", "Accept": "application/vnd.github+json"},
            timeout=25,
        )
        r.raise_for_status()
        data = r.json()
        if isinstance(data, list):
            return jsonify({"error": "path is a directory on GitHub; pick a file"}), 400
        if data.get("type") != "file":
            return jsonify({"error": "not a file"}), 400
        raw = base64.b64decode(data.get("content", "")).decode("utf-8", errors="ignore")
        truncated = len(raw) > _MAX_LOCAL_READ
        return jsonify({"path": path, "content": raw[:_MAX_LOCAL_READ], "truncated": truncated})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/local/roots")
def local_roots_get():
    return jsonify({"roots": [str(p) for p in _allowed_local_roots()], "configured": CONFIG.get("local_roots") or []})


@app.route("/api/local/browse")
def local_browse():
    path_param = request.args.get("path", "").strip()
    if not path_param:
        return jsonify({"error": "path query is required"}), 400
    target = Path(path_param).expanduser().resolve()
    if not target.exists():
        return jsonify({"error": "path not found"}), 404
    if not target.is_dir():
        return jsonify({"error": "not a directory"}), 400
    if not _is_path_under_allowed(target):
        return jsonify({"error": "ruta fuera de las carpetas permitidas (settings local_roots)"}), 403
    entries = []
    try:
        for x in sorted(target.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))[:200]:
            if x.name.startswith("."):
                continue
            entries.append(
                {
                    "name": x.name,
                    "type": "dir" if x.is_dir() else "file",
                    "path": str(x),
                }
            )
    except PermissionError:
        return jsonify({"error": "permission denied"}), 403
    return jsonify({"path": str(target), "entries": entries})


@app.route("/api/local/read")
def local_read():
    path_param = request.args.get("path", "").strip()
    if not path_param:
        return jsonify({"error": "path query is required"}), 400
    target = Path(path_param).expanduser().resolve()
    if not target.exists() or not target.is_file():
        return jsonify({"error": "file not found"}), 404
    if not _is_path_under_allowed(target):
        return jsonify({"error": "ruta fuera de las carpetas permitidas"}), 403
    try:
        size = target.stat().st_size
    except OSError as e:
        return jsonify({"error": str(e)}), 400
    if size > _MAX_LOCAL_FILE:
        return jsonify({"error": f"archivo demasiado grande (máx {_MAX_LOCAL_FILE // 1_000_000} MB)"}), 400
    try:
        data = target.read_text(encoding="utf-8", errors="ignore")
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    truncated = len(data) > _MAX_LOCAL_READ
    return jsonify({"path": str(target), "content": data[:_MAX_LOCAL_READ], "truncated": truncated})


@app.route("/api/local/git-repos")
def local_git_repos():
    root_param = request.args.get("root", "").strip()
    root = Path(root_param).expanduser().resolve() if root_param else Path.home().resolve()
    if not root.is_dir():
        return jsonify({"error": "root no es un directorio"}), 400
    if not _is_path_under_allowed(root):
        return jsonify({"error": "root fuera de carpetas permitidas"}), 403
    repos = []
    max_depth = 5

    def walk(d: Path, depth: int) -> None:
        if depth > max_depth or len(repos) >= 80:
            return
        try:
            if (d / ".git").is_dir():
                repos.append({"path": str(d), "name": d.name})
                return
            for sub in sorted(d.iterdir(), key=lambda p: p.name.lower()):
                if sub.is_dir() and not sub.name.startswith("."):
                    walk(sub, depth + 1)
        except (PermissionError, OSError):
            pass

    walk(root, 0)
    return jsonify({"root": str(root), "repos": repos})


@app.route("/api/local/skills-pack")
def local_skills_pack():
    dir_param = request.args.get("dir", "").strip()
    skills_dir = Path(dir_param).expanduser().resolve() if dir_param else None
    configured = CONFIG.get("skill_dirs") or []
    if skills_dir is None:
        if configured:
            skills_dir = Path(configured[0]).expanduser().resolve()
        else:
            return jsonify({"error": "No hay skill_dirs configurados"}), 400
    if not skills_dir.exists() or not skills_dir.is_dir():
        return jsonify({"error": "skills dir not found"}), 404
    if not _is_path_under_allowed(skills_dir):
        return jsonify({"error": "skills dir fuera de carpetas permitidas (local_roots)"}), 403

    files = sorted(skills_dir.rglob("SKILL.md"))
    if not files:
        return jsonify({"error": "No se encontraron SKILL.md"}), 404

    # Límite para evitar prompts gigantes.
    max_files = 40
    max_chars = 220_000
    used = 0
    parts = []
    picked = []
    for f in files[:max_files]:
        try:
            rel = str(f.relative_to(skills_dir))
            txt = f.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        block = f"=== SKILL: {rel} ===\n{txt}\n"
        if used + len(block) > max_chars:
            break
        parts.append(block)
        picked.append(rel)
        used += len(block)
    return jsonify(
        {
            "skills_dir": str(skills_dir),
            "skills_count": len(picked),
            "files": picked,
            "content": "\n".join(parts),
            "truncated": len(picked) < len(files),
        }
    )


@app.route("/api/workspace/preview")
def workspace_preview():
    rel = request.args.get("path", "").strip()
    if not rel:
        return jsonify({"error": "path query is required"}), 400
    target = (BASE_DIR / rel).resolve()
    if BASE_DIR.resolve() not in target.parents and target != BASE_DIR.resolve():
        return jsonify({"error": "path out of workspace"}), 400
    if not target.exists() or not target.is_file():
        return jsonify({"error": "file not found"}), 404
    try:
        data = target.read_text(encoding="utf-8", errors="ignore")
        return jsonify({"path": rel, "content": data[:30000]})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/workspace/tests", methods=["POST"])
def workspace_tests():
    data = request.json or {}
    workspace_path = (data.get("workspace_path") or "").strip()
    command = (data.get("command") or "python3 -m pytest -q").strip()
    if not workspace_path:
        return jsonify({"error": "workspace_path is required"}), 400
    ws = Path(workspace_path).resolve()
    if not ws.exists():
        return jsonify({"error": "workspace_path not found"}), 404
    proc = subprocess.run(command, cwd=str(ws), shell=True, capture_output=True, text=True, check=False)
    return jsonify({"exit_code": proc.returncode, "stdout": proc.stdout[-12000:], "stderr": proc.stderr[-12000:]})


@app.route("/api/workspace/git/status", methods=["POST"])
def workspace_git_status():
    data = request.json or {}
    workspace_path = (data.get("workspace_path") or "").strip()
    if not workspace_path:
        return jsonify({"error": "workspace_path is required"}), 400
    ws = Path(workspace_path).resolve()
    if not (ws / ".git").exists():
        return jsonify({"error": "not a git repo"}), 400
    st = git_run(["git", "status", "--short"], ws)
    lg = git_run(["git", "log", "--oneline", "-n", "8"], ws)
    br = git_run(["git", "branch", "--show-current"], ws)
    return jsonify({"branch": br.stdout.strip(), "status": st.stdout, "log": lg.stdout})


@app.route("/api/workspace/git/bootstrap", methods=["POST"])
def workspace_git_bootstrap():
    data = request.json or {}
    workspace_path = (data.get("workspace_path") or "").strip()
    branch = (data.get("branch") or "main").strip()
    message = (data.get("message") or "chore: bootstrap repository").strip()
    if not workspace_path:
        return jsonify({"error": "workspace_path is required"}), 400
    ws = Path(workspace_path).resolve()
    if not ws.exists():
        return jsonify({"error": "workspace_path not found"}), 404
    if not (ws / ".git").exists():
        i = git_run(["git", "init"], ws)
        if i.returncode != 0:
            return jsonify({"error": i.stderr or i.stdout}), 500
    if not has_commit(ws):
        git_run(["git", "checkout", "-b", branch], ws)
        git_run(["git", "add", "."], ws)
        c = git_run(["git", "commit", "-m", message], ws)
        if c.returncode != 0:
            return jsonify({"error": c.stderr or c.stdout}), 500
        return jsonify({"success": True, "bootstrapped": True, "commit": c.stdout[-1200:]})
    return jsonify({"success": True, "bootstrapped": False, "message": "Repository already has commits."})


@app.route("/api/workspace/worktrees", methods=["POST"])
def workspace_worktrees():
    data = request.json or {}
    workspace_path = (data.get("workspace_path") or "").strip()
    if not workspace_path:
        return jsonify({"error": "workspace_path is required"}), 400
    ws = Path(workspace_path).resolve()
    if not (ws / ".git").exists():
        return jsonify({"error": "not a git repo"}), 400
    wt = git_run(["git", "worktree", "list", "--porcelain"], ws)
    return jsonify({"output": wt.stdout})


@app.route("/api/workspace/agent/changes", methods=["POST"])
def workspace_agent_changes():
    data = request.json or {}
    workspace_path = (data.get("workspace_path") or "").strip()
    branch = (data.get("branch") or "").strip()
    base_branch = (data.get("base_branch") or "main").strip()
    if not workspace_path or not branch:
        return jsonify({"error": "workspace_path and branch are required"}), 400
    ws = Path(workspace_path).resolve()
    if not (ws / ".git").exists():
        return jsonify({"error": "not a git repo"}), 400
    if not has_commit(ws):
        return jsonify({"error": "repo has no commits yet. Create an initial commit first."}), 400
    files_proc = git_run(["git", "diff", "--name-only", f"{base_branch}...{branch}"], ws)
    files = [x for x in files_proc.stdout.splitlines() if x.strip()]
    diffs = {}
    for f in files[:30]:
        d = git_run(["git", "diff", f"{base_branch}...{branch}", "--", f], ws)
        diffs[f] = d.stdout[-8000:]
    return jsonify({"branch": branch, "base_branch": base_branch, "files": files, "diffs": diffs})


@app.route("/api/workspace/agent/merge", methods=["POST"])
def workspace_agent_merge():
    data = request.json or {}
    workspace_path = (data.get("workspace_path") or "").strip()
    branch = (data.get("branch") or "").strip()
    base_branch = (data.get("base_branch") or "main").strip()
    cleanup_worktree = bool(data.get("cleanup_worktree", True))
    delete_branch = bool(data.get("delete_branch", False))
    if not workspace_path or not branch:
        return jsonify({"error": "workspace_path and branch are required"}), 400
    ws = Path(workspace_path).resolve()
    if not (ws / ".git").exists():
        return jsonify({"error": "not a git repo"}), 400
    if not has_commit(ws):
        return jsonify({"error": "repo has no commits yet. Create an initial commit first."}), 400

    checkout = git_run(["git", "checkout", base_branch], ws)
    merge = git_run(["git", "merge", "--no-ff", branch, "-m", f"merge agent branch {branch}"], ws)
    result = {
        "checkout_code": checkout.returncode,
        "checkout_stdout": checkout.stdout[-1200:],
        "checkout_stderr": checkout.stderr[-1200:],
        "merge_code": merge.returncode,
        "merge_stdout": merge.stdout[-4000:],
        "merge_stderr": merge.stderr[-4000:],
    }
    if merge.returncode != 0:
        return jsonify(result), 409

    if cleanup_worktree:
        slug = slugify_branch(branch).replace("/", "__")
        wt = (WORKTREES_DIR / slug).resolve()
        if wt.exists():
            rm = git_run(["git", "worktree", "remove", "--force", str(wt)], ws)
            result["worktree_remove_code"] = rm.returncode
            result["worktree_remove_stderr"] = rm.stderr[-1200:]
    if delete_branch:
        db = git_run(["git", "branch", "-d", branch], ws)
        result["delete_branch_code"] = db.returncode
        result["delete_branch_stderr"] = db.stderr[-1200:]
    return jsonify(result)


@app.route("/api/browser/run", methods=["POST"])
def browser_run():
    data = request.json or {}
    url = (data.get("url") or "").strip()
    actions = data.get("actions") or []
    if not url:
        return jsonify({"error": "url is required"}), 400
    try:
        result = run_browser_flow(url, actions)
        return jsonify(result)
    except Exception as e:
        return jsonify(
            {
                "ok": False,
                "error": str(e),
                "hint": "Install Playwright: pip install playwright && python3 -m playwright install chromium",
            }
        ), 500


if __name__ == "__main__":
    base = f"http://{CONFIG['host']}:{CONFIG['port']}"
    ce = CONFIG.get("chat_engine") or "puter"
    pm = CONFIG.get("puter_model") or "?"
    om = CONFIG["model"]
    gm = CONFIG.get("groq_model") or "llama-3.3-70b-versatile"
    orm = CONFIG.get("openrouter_model") or "meta-llama/llama-3.2-3b-instruct:free"
    if ce == "puter":
        chat_line = f"chat Puter ({pm})"
    elif ce == "groq":
        chat_line = f"chat Groq ({gm})"
    elif ce == "openrouter":
        chat_line = f"chat OpenRouter ({orm})"
    else:
        chat_line = f"chat Ollama ({om})"
    print(
        f"Uncensored Builder: {base}/dashboard  ·  {chat_line}  ·  "
        f"Ollama herramientas/swarm: {om} @ {CONFIG['ollama_base_url']}  ·  "
        f"Claude UI (solo Ollama): {base}/claude"
    )
    # threaded: varias pestañas / health + petición larga (modo agente) sin bloquearse mutuamente.
    app.run(host=CONFIG["host"], port=CONFIG["port"], debug=False, threaded=True)
