# -*- coding: utf-8 -*-
"""SSR agent bridge — drives an ``SSRAgent`` as the desktop's chat assistant.

SSR (https://github.com/NannaOlympicBroadcast/ssr-agent) is bundled into this
app's Python runtime. This module:

* lazily builds a single ``SSRAgent`` (rebuilt when the model config changes),
* exposes model configuration (list / set primary / add / set API key), backed
  by SSR's own ``~/.ssr/models.json`` + ``~/.ssr/.env``,
* runs a chat turn from mixed input parts — text plus pasted **images** and
  uploaded **files** — over a WebSocket, streaming the agent's live events
  (``turn_start`` / ``thinking`` / ``tool_call`` / ``tool_result`` / ``reply``)
  back to the UI, and
* starts SSR's **Miloco activity → bus bridge** so Mi Home events land on the
  agent's event bus, where SSR bus-event handlers can react to them.

If SSR is not importable (e.g. a build without it bundled), every entry point
degrades gracefully: ``status()`` reports ``available: false`` with guidance,
and the UI shows how to install it, rather than crashing the backend.
"""
import base64
import logging
import os
import shutil
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

_LOGGER = logging.getLogger("miot_desktop.ssr")

# Allow pointing at a source checkout (dev) via SSR_AGENT_PATH; in a packaged
# build ssr is installed into the runtime and importable directly.
_SSR_PATH = os.environ.get("SSR_AGENT_PATH")
if _SSR_PATH and os.path.isdir(_SSR_PATH) and _SSR_PATH not in sys.path:
    sys.path.insert(0, _SSR_PATH)

# The tool class ssr.integrations.miloco exposes, in the "module:attr" form
# settings.agent_tools takes. Constructed with the ToolKit; its callables()
# returns the four miloco_* tools.
MILOCO_TOOLS_SPEC = "ssr.integrations.miloco:MilocoTools"
MILOCO_TOOL_NAMES = (
    "miloco_im_push", "miloco_notify_bind", "miloco_notify_unbind", "miloco_habit_suggest",
)


def _ssr_importable() -> bool:
    try:
        import ssr  # noqa: F401
        return True
    except Exception:  # pylint: disable=broad-except
        return False


class SSRAgentBridge:
    def __init__(self) -> None:
        self._agent = None
        self._settings = None
        self._lock = threading.Lock()
        self._miloco_bridge = None
        self._miloco_webhook_fallback = None
        self._import_error: Optional[str] = None

    # ------------------------------------------------------------ availability
    def available(self) -> bool:
        return _ssr_importable()

    def _load_settings(self):
        from ssr.config import load_settings
        return load_settings()

    def _build_agent(self):
        """Build (or return cached) SSRAgent. Caller holds no lock."""
        with self._lock:
            if self._agent is not None:
                return self._agent
            from ssr.agent.core import SSRAgent
            self._settings = self._load_settings()
            # Scope this app's agent to smart-home duty (butler persona +
            # Miloco device-type reference doc) instead of the generic coding-
            # agent default — this *is* the "SSR 助手" driving Mi Home, not a
            # general-purpose assistant. Best-effort: degrades to the base
            # persona (no live device catalog / home profile) when miloco-cli
            # isn't installed or Miloco isn't running yet.
            try:
                from ssr.integrations.miloco import apply_smart_home_scope
                apply_smart_home_scope(self._settings)
            except Exception as e:  # pylint: disable=broad-except
                _LOGGER.info("Miloco smart-home scoping not applied: %s", e)
            # Register Miloco's own tool surface (miloco_im_push /
            # miloco_notify_bind / miloco_notify_unbind / miloco_habit_suggest).
            # apply_smart_home_scope above only supplies *prompt* material — it
            # tells the agent these tools exist without providing them, so
            # without this the agent knows the names, has no way to call them,
            # and (with a small model) answers by inventing results instead.
            # `agent_tools` is a list of "module:attr" specs the ToolKit imports;
            # append rather than assign so a user's own ~/.ssr config survives.
            specs = list(getattr(self._settings, "agent_tools", None) or [])
            if MILOCO_TOOLS_SPEC not in specs:
                specs.append(MILOCO_TOOLS_SPEC)
            self._settings.agent_tools = specs
            self._agent = SSRAgent(self._settings)
            # Attach the Miloco activity → bus bridge so home events reach any
            # SSR bus-event handlers, and start the /miloco/webhook receiver
            # (ssr.integrations.miloco.start_bus_bridge) so Miloco's own
            # perception/rule engine can drive this same agent directly.
            # Best-effort: unsupported platform / miloco off simply returns None.
            try:
                from ssr.integrations.miloco import start_bus_bridge
                self._miloco_bridge = start_bus_bridge(self._agent)
                if self._miloco_bridge:
                    _LOGGER.info("Miloco activity→bus bridge started")
            except Exception as e:  # pylint: disable=broad-except
                _LOGGER.info("Miloco bridge not started: %s", e)
            return self._agent

    def reset_agent(self) -> None:
        """Drop the cached agent so the next turn rebuilds it (e.g. after a
        model-config change)."""
        with self._lock:
            self._agent = None

    # ------------------------------------------------------------ status
    def status(self) -> Dict[str, Any]:
        if not self.available():
            return {
                "available": False,
                "message": (
                    "未检测到 SSR agent。请在后端 Python 环境中安装 ssr-agent"
                    "（pip install ssr-agent），或设置 SSR_AGENT_PATH 指向其源码目录。"
                ),
            }
        try:
            settings = self._settings or self._load_settings()
            from ssr.models import ModelsConfig
            mc = ModelsConfig(settings)
            primary = mc.get_primary()
            missing = []
            if not settings.gemini_api_key and primary.provider == "gemini":
                missing.append("GEMINI_API_KEY")
            return {
                "available": True,
                "home": str(settings.home),
                "primary_model": primary.id,
                "primary_provider": primary.provider,
                "primary_model_name": primary.model,
                "has_api_key": bool(settings.gemini_api_key),
                "missing": missing,
                "miloco_bridge": self._miloco_bridge is not None,
                **self.miloco_tooling(),
            }
        except Exception as e:  # pylint: disable=broad-except
            return {"available": True, "error": str(e)}

    def miloco_tooling(self) -> Dict[str, Any]:
        """Whether the agent can actually reach Miloco, and how.

        Reported because the failure mode is otherwise invisible: an agent with
        no Miloco tools still answers device questions — it just makes them up.
        Only inspects a *already-built* agent; building one here would make a
        status poll expensive.
        """
        loaded: List[str] = []
        agent = self._agent
        if agent is not None:
            try:
                loaded = [getattr(fn, "__name__", "") for fn in agent.toolkit.callables()]
            except Exception:  # pylint: disable=broad-except
                loaded = []
        cli = os.environ.get("MILOCO_CLI_BIN") or shutil.which("miloco-cli")
        return {
            "agent_built": agent is not None,
            "tools": loaded,
            "miloco_tools": [n for n in MILOCO_TOOL_NAMES if n in loaded],
            "miloco_tools_expected": list(MILOCO_TOOL_NAMES),
            # miloco-cli is what the Miloco tools *and* the device-catalog /
            # home-profile context blocks shell out to; without it they all
            # degrade to nothing, which is what leaves the agent guessing.
            "miloco_cli": cli,
            "miloco_home": os.environ.get("MILOCO_HOME"),
            "miloco_server_token": bool(os.environ.get("MILOCO_SERVER_TOKEN")),
        }

    # ------------------------------------------------------------ models
    def list_models(self) -> Dict[str, Any]:
        from ssr.models import ModelsConfig
        settings = self._settings or self._load_settings()
        mc = ModelsConfig(settings)
        return {
            "primary": mc.primary,
            "models": [
                {
                    "id": m.id, "provider": m.provider, "model": m.model,
                    "api_key_env": m.api_key_env, "base_url": m.base_url,
                    "has_api_key": bool(m.api_key or os.environ.get(m.api_key_env or "")),
                }
                for m in mc.list_models()
            ],
        }

    def set_model(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """Add/update a model entry and/or set the primary + API key.

        Body keys: ``id``, ``provider``, ``model``, ``api_key_env``,
        ``base_url``, ``api_key``, ``set_primary`` (bool), ``primary`` (id).
        """
        from ssr.models import ModelEntry, ModelsConfig
        settings = self._settings or self._load_settings()
        mc = ModelsConfig(settings)

        api_key = (data.get("api_key") or "").strip()
        model_id = (data.get("id") or "").strip()
        if model_id:
            provider = (data.get("provider") or "gemini").strip()
            api_key_env = (data.get("api_key_env") or
                           ("GEMINI_API_KEY" if provider == "gemini" else f"{provider.upper()}_API_KEY"))
            entry = ModelEntry(
                id=model_id,
                provider=provider,
                model=(data.get("model") or "").strip() or model_id,
                api_key_env=api_key_env,
                base_url=(data.get("base_url") or None),
                api_key=(api_key or None),
            )
            mc.add_model(entry)
            if data.get("set_primary", True):
                mc.switch_primary(model_id)
            # Persist the API key both to the env file (so a fresh process sees
            # it) and the live environment (so this process does).
            if api_key:
                self._persist_env(settings, api_key_env, api_key)
        elif data.get("primary"):
            mc.switch_primary(data["primary"])
        elif api_key:
            # Just updating the primary provider's key.
            primary = mc.get_primary()
            self._persist_env(settings, primary.api_key_env or "GEMINI_API_KEY", api_key)

        # Rebuild the agent so the change takes effect on the next turn.
        self.reset_agent()
        return self.list_models()

    def _persist_env(self, settings, key: str, value: str) -> None:
        os.environ[key] = value
        if key in ("GEMINI_API_KEY", "GOOGLE_API_KEY"):
            os.environ.setdefault("GOOGLE_API_KEY", value)
            os.environ["GEMINI_API_KEY"] = value
        env_path = Path(settings.home) / ".env"
        try:
            lines: List[str] = []
            found = False
            if env_path.exists():
                for line in env_path.read_text("utf-8").splitlines():
                    if line.startswith(key + "="):
                        lines.append(f"{key}={value}")
                        found = True
                    else:
                        lines.append(line)
            if not found:
                lines.append(f"{key}={value}")
            env_path.parent.mkdir(parents=True, exist_ok=True)
            env_path.write_text("\n".join(lines) + "\n", "utf-8")
            try:
                env_path.chmod(0o600)
            except OSError:
                pass
        except OSError as e:
            _LOGGER.warning("failed to persist %s to .env: %s", key, e)

    # ------------------------------------------------------------ chat turn
    def run_turn(
        self,
        text: str,
        attachments: Optional[List[Dict[str, Any]]],
        emit: Callable[[Dict[str, Any]], None],
    ) -> str:
        """Run one blocking agent turn, forwarding live events via ``emit``.

        ``attachments`` items: ``{kind: 'image'|'file', mime, name, data_b64}``.
        Images are passed inline to the multimodal model; other files are saved
        into the agent's working dir and referenced by path so the agent can
        open them with its filesystem tools.
        """
        agent = self._build_agent()
        parts: List[Dict[str, Any]] = []
        saved_files: List[str] = []

        for att in (attachments or []):
            kind = att.get("kind")
            b64 = att.get("data_b64") or ""
            try:
                raw = base64.b64decode(b64) if b64 else b""
            except Exception:  # pylint: disable=broad-except
                raw = b""
            if kind == "image" and raw:
                parts.append({"type": "image", "mime_type": att.get("mime") or "image/png", "data": raw})
            elif raw:
                path = self._save_upload(agent, att.get("name") or "upload.bin", raw)
                saved_files.append(path)

        prompt = text or ""
        if saved_files:
            listing = "\n".join(f"- {p}" for p in saved_files)
            prompt = (prompt + "\n\n" if prompt else "") + f"[用户上传了以下文件，可用工具读取]\n{listing}"
        parts.insert(0, {"type": "text", "text": prompt})

        observer = None

        def _observer(event: Dict[str, Any]) -> None:
            try:
                emit(event)
            except Exception:  # pylint: disable=broad-except
                pass

        try:
            agent.add_event_observer(_observer)
            observer = _observer
            reply = agent.run_parts(parts)
            return reply
        finally:
            if observer is not None:
                agent.remove_event_observer(observer)

    # ------------------------------------------------------------ miloco webhook
    def handle_miloco_webhook(self, body: Dict[str, Any]) -> Dict[str, Any]:
        """Handle one Miloco agent-webhook request: ``{action, payload}`` ->
        ``{code, message, data}``.

        This is an *alternative* entry point to the same receiver
        ``ssr.integrations.miloco.start_bus_bridge`` already runs on its own
        fixed port (Miloco's default ``agent.webhook_url``); exposing it here
        too lets Miloco reach this backend's own REST port instead, and keeps
        working if that fixed port could not be bound. Reuses the *same*
        handler/session pool as the bus bridge when available, so both
        entry points share consistent per-sessionKey conversation state.
        """
        self._build_agent()  # ensures self._miloco_bridge is set, best-effort
        handler = getattr(self._miloco_bridge, "handler", None)
        if handler is None:
            if self._miloco_webhook_fallback is None:
                from ssr.agent.core import SSRAgent
                from ssr.integrations.miloco import MilocoAgentHandler
                settings = self._settings or self._load_settings()
                self._miloco_webhook_fallback = MilocoAgentHandler(lambda: SSRAgent(settings))
            handler = self._miloco_webhook_fallback
        return handler.handle(body)

    def _save_upload(self, agent, name: str, raw: bytes) -> str:
        base = Path(getattr(agent.settings, "project_dir", Path.cwd())) / "uploads"
        base.mkdir(parents=True, exist_ok=True)
        safe = os.path.basename(name) or f"upload-{int(time.time())}.bin"
        path = base / safe
        # Avoid clobbering: add a numeric suffix if it exists.
        if path.exists():
            stem, ext = os.path.splitext(safe)
            path = base / f"{stem}-{int(time.time())}{ext}"
        path.write_bytes(raw)
        return str(path)
