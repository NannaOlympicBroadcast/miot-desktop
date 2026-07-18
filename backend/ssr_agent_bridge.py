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
            self._agent = SSRAgent(self._settings)
            # Attach the Miloco activity → bus bridge so home events reach any
            # SSR bus-event handlers. Best-effort: unsupported platform / miloco
            # off simply returns None.
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
            }
        except Exception as e:  # pylint: disable=broad-except
            return {"available": True, "error": str(e)}

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
