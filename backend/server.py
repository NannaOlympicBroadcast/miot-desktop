# -*- coding: utf-8 -*-
"""
MIoT Desktop backend sidecar.

A small aiohttp server that wraps the `miot_kit` library and exposes a local
REST + WebSocket API consumed by the Electron renderer.

It reuses the OAuth2 credentials that were already cached on this machine by
the `login_miot.py` helper (default: ~/.miot_cache), so no login flow is
needed inside the desktop app.

Bound to 127.0.0.1 on an ephemeral port; the chosen port is printed to stdout
as `MIOT_BACKEND_PORT=<port>` so the Electron main process can pick it up.
"""
import asyncio
import json
import logging
import os
import platform
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

# ---------------------------------------------------------------------------
# Locate and mount miot_kit
#
# - When frozen by PyInstaller, the `miot` package is bundled into the exe, so
#   no external path is needed.
# - In dev, we look for a vendored copy at <repo>/vendor/miot_kit (overridable
#   via MIOT_KIT_PATH).
# ---------------------------------------------------------------------------
_FROZEN = getattr(sys, "frozen", False)
_DEFAULT_KIT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "vendor", "miot_kit"))
MIOT_KIT_PATH = os.environ.get("MIOT_KIT_PATH", _DEFAULT_KIT)
CACHE_PATH = os.environ.get("MIOT_CACHE_PATH", os.path.join(os.path.expanduser("~"), ".miot_cache"))
CLOUD_SERVER = os.environ.get("MIOT_CLOUD_SERVER", "cn")
LANG = os.environ.get("MIOT_LANG", "zh-Hans")

if not _FROZEN and os.path.isdir(MIOT_KIT_PATH):
    sys.path.insert(0, MIOT_KIT_PATH)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

logging.basicConfig(level=logging.WARNING)
_LOGGER = logging.getLogger("miot_desktop")

# ---------------------------------------------------------------------------
# Camera native library detection + Windows monkey patch
#
# The bundled miot_kit ships the camera P2P native lib only for linux/darwin.
# On Windows the lib is absent, so loading it would crash client init. We
# detect whether the real lib exists; if not we patch the loader with a no-op
# fake (same approach the upstream helper scripts use) so the rest of the
# client still initialises. Live video decode is only possible when the real
# lib is present.
# ---------------------------------------------------------------------------

def _native_lib_path() -> Optional[Path]:
    system = platform.system().lower()
    machine = platform.machine().lower()
    # Derive the libs dir from the actually-imported miot package, so it works
    # both in dev (vendored/external) and when frozen by PyInstaller.
    try:
        import miot as _miot_pkg
        base = Path(_miot_pkg.__file__).parent / "libs"
    except Exception:
        base = Path(MIOT_KIT_PATH) / "miot" / "libs"
    if system == "windows":
        arch = "x86_64" if machine in ("x86_64", "amd64") else ("arm64" if machine in ("arm64", "aarch64") else None)
        if not arch:
            return None
        return base / "windows" / arch / "miot_camera_lite.dll"
    if system == "linux":
        arch = "x86_64" if machine in ("x86_64", "amd64") else ("arm64" if machine in ("arm64", "aarch64") else None)
        if not arch:
            return None
        return base / "linux" / arch / "libmiot_camera_lite.so"
    if system == "darwin":
        arch = "x86_64" if machine == "x86_64" else ("arm64" if machine in ("arm64", "aarch64") else None)
        if not arch:
            return None
        return base / "darwin" / arch / "libmiot_camera_lite.dylib"
    return None


_lib_path = _native_lib_path()
CAMERA_NATIVE_AVAILABLE = bool(_lib_path and _lib_path.exists())

if not CAMERA_NATIVE_AVAILABLE:
    # Patch the dynamic-lib loader so MIoTCamera can be constructed without the
    # real native library (all C calls become no-ops returning 0).
    import miot.camera as _mc

    class _FakeLib:
        def __getattr__(self, name):
            if name == "miot_camera_version":
                return lambda *a, **k: b"0.0.0-fake"
            return lambda *a, **k: 0

    _mc._load_dynamic_lib = lambda: _FakeLib()  # type: ignore

from aiohttp import web, WSMsgType  # noqa: E402

from miot.client import MIoTClient  # noqa: E402
from miot.storage import MIoTStorage  # noqa: E402
from miot.types import (  # noqa: E402
    MIoTGetPropertyParam,
    MIoTSetPropertyParam,
    MIoTActionParam,
)

import xiaomi_asr_bridge  # noqa: E402
import miloco_controller  # noqa: E402
import ssr_agent_bridge  # noqa: E402


# ---------------------------------------------------------------------------
# Spec serialisation: turn a parsed MIoTSpecDevice into renderer-friendly JSON
# ---------------------------------------------------------------------------

def _serialize_property(siid: int, prop) -> Dict[str, Any]:
    value_range = None
    if prop.value_range:
        value_range = {
            "min": prop.value_range.min_,
            "max": prop.value_range.max_,
            "step": prop.value_range.step,
        }
    value_list = None
    if prop.value_list:
        value_list = [
            {"value": item.value, "description": item.description or item.name}
            for item in prop.value_list
        ]
    return {
        "siid": siid,
        "piid": prop.iid,
        "iid": f"prop.{siid}.{prop.iid}",
        "name": prop.description_trans or prop.description,
        "format": prop.format,
        "access": prop.access,
        "readable": "read" in prop.access,
        "writable": "write" in prop.access,
        "notify": "notify" in prop.access,
        "unit": prop.unit,
        "value_range": value_range,
        "value_list": value_list,
    }


def _serialize_action(siid: int, action) -> Dict[str, Any]:
    return {
        "siid": siid,
        "aiid": action.iid,
        "iid": f"action.{siid}.{action.iid}",
        "name": action.description_trans or action.description,
        "in": [
            {"piid": p.iid, "name": p.description_trans or p.description, "format": p.format}
            for p in action.in_
        ],
    }


def _serialize_spec(spec_device) -> Dict[str, Any]:
    services = []
    for svc in spec_device.services:
        services.append({
            "siid": svc.iid,
            "name": svc.description_trans or svc.description,
            "type": svc.type_,
            "properties": [_serialize_property(svc.iid, p) for p in svc.properties],
            "actions": [_serialize_action(svc.iid, a) for a in svc.actions],
        })
    return {
        "urn": spec_device.urn,
        "name": spec_device.description_trans or spec_device.description,
        "services": services,
    }


# ---------------------------------------------------------------------------
# App state
# ---------------------------------------------------------------------------
class Backend:
    def __init__(self) -> None:
        self.client: Optional[MIoTClient] = None
        self.ready = False
        self.error: Optional[str] = None

    async def init(self) -> None:
        storage = MIoTStorage(CACHE_PATH)
        uuid = await storage.load_async(domain="cloud", name="uuid", type_=str)
        oauth_info = await storage.load_async(domain="cloud", name="oauth_info", type_=dict)
        if not oauth_info or not uuid:
            raise RuntimeError(
                f"未在缓存 {CACHE_PATH} 中找到登录凭证。请先运行 login_miot.py 完成米家 OAuth 授权。")
        self.client = MIoTClient(
            uuid=uuid,
            redirect_uri="http://127.0.0.1",
            cache_path=CACHE_PATH,
            oauth_info=oauth_info,
            cloud_server=CLOUD_SERVER,
            lang=LANG,
        )
        await self.client.init_async()
        self.ready = True

    async def deinit(self) -> None:
        if self.client:
            try:
                await self.client.deinit_async()
            except Exception as err:  # pylint: disable=broad-except
                _LOGGER.warning("deinit error: %s", err)
            self.client = None
        self.ready = False

    async def reinit(self) -> None:
        """Re-create the client (e.g. after a fresh login)."""
        await self.deinit()
        self.error = None
        await self.init()


BE = Backend()

# OAuth login session: holds the temporary MIoTClient created by /api/auth/start
# and the state token needed to complete the exchange.
_AUTH: Dict[str, Any] = {"client": None, "uuid": None, "state": None}

# XiaoAI ASR/TTS bridge (separate Mi passport credential — see xiaomi_asr_bridge.py).
ASR = xiaomi_asr_bridge.XiaomiAsrBridge()

# Miloco Docker controller (Mi Home perceptive gateway — see miloco_controller.py).
MILOCO = miloco_controller.MilocoController()

# SSR agent bridge (chat assistant + Miloco→bus — see ssr_agent_bridge.py).
AGENT = ssr_agent_bridge.SSRAgentBridge()


def _parse_code_state(body: Dict[str, Any], fallback_state: Optional[str] = None) -> tuple:
    """Extract (code, state) from a request body.

    Accepts any of:
      - {"url": "http://127.0.0.1/?code=xxx&state=yyy"}  — full redirect URL
      - {"code": "xxx", "state": "yyy"}                  — explicit fields
      - {"code": "xxx"}                                   — bare code; state comes from the
                                                            in-flight session (fallback_state)
    """
    from urllib.parse import urlparse, parse_qs
    code = body.get("code")
    state = body.get("state")
    url = body.get("url")
    if url:
        q = parse_qs(urlparse(url).query)
        code = code or (q.get("code") or [None])[0]
        state = state or (q.get("state") or [None])[0]
    # bare code pasted without state — fall back to the session-stored state
    if code and not state and fallback_state:
        state = fallback_state
    return code, state


def _json(data: Any, status: int = 200) -> web.Response:
    return web.json_response(data, status=status, dumps=lambda o: json.dumps(o, ensure_ascii=False, default=str))


def _err(message: str, status: int = 500) -> web.Response:
    return _json({"error": message}, status=status)


def _is_unauthorized(err: Exception) -> bool:
    """Best-effort detection of an expired/invalid access token.

    miot_kit surfaces this as a plain RuntimeError/ValueError whose message
    contains the mihome API's own wording (e.g. "unauthorized(401)"), so we
    match on that rather than a specific exception type.
    """
    msg = str(err).lower()
    return "401" in msg or "unauthorized" in msg or "invalid access token" in msg or "token expired" in msg


def _client_err(err: Exception) -> web.Response:
    """Turn a MIoTClient call failure into a response.

    If the failure looks like an expired/invalid credential, mark the backend
    as not-ready (so /api/health reflects it) and flag the response with
    auth_required so the renderer can jump straight to the login screen
    instead of just printing the raw error text.
    """
    if _is_unauthorized(err):
        message = "登录凭证已过期或失效，请重新登录小米账号。"
        BE.ready = False
        BE.error = message
        return _json({"error": message, "auth_required": True}, status=401)
    return _err(str(err))


# ---------------------------------------------------------------------------
# REST handlers
# ---------------------------------------------------------------------------
async def h_health(request: web.Request) -> web.Response:
    return _json({
        "ready": BE.ready,
        "error": BE.error,
        "cloud_server": CLOUD_SERVER,
        "cache_path": CACHE_PATH,
        "platform": platform.system(),
    })


async def h_auth_status(request: web.Request) -> web.Response:
    return _json({"logged_in": BE.ready, "error": BE.error})


async def h_auth_start(request: web.Request) -> web.Response:
    """Begin an OAuth2 login flow using MIoTClient (matches login_miot.py approach).

    Creates a temporary client, generates the Xiaomi authorize URL, and stashes
    the client in _AUTH so /api/auth/complete can exchange the code for a token.
    Returns {"url": "<authorize_url>", "state": "<oauth_state>"}.
    """
    try:
        import uuid as _uuid
        # Tear down any previous in-flight session.
        prev = _AUTH.get("client")
        if prev:
            try:
                await prev.deinit_async()
            except Exception:  # pylint: disable=broad-except
                pass

        local_uuid = _uuid.uuid4().hex
        tmp_client = MIoTClient(
            uuid=local_uuid,
            redirect_uri="http://127.0.0.1",
            cache_path=CACHE_PATH,
            cloud_server=CLOUD_SERVER,
            lang=LANG,
        )
        await tmp_client.init_async()
        url = await tmp_client.gen_oauth_url_async()
        # Persist the state token so we can verify it (and fall back to it if
        # the user pastes only the bare code without the full redirect URL).
        oauth_state = tmp_client._oauth_client.state  # type: ignore[attr-defined]
        _AUTH["client"] = tmp_client
        _AUTH["uuid"] = local_uuid
        _AUTH["state"] = oauth_state
        return _json({"url": url, "state": oauth_state})
    except Exception as err:  # pylint: disable=broad-except
        return _err(str(err))


async def h_auth_complete(request: web.Request) -> web.Response:
    """Finish login: exchange authorization code for token, persist, re-init.

    Accepts any of:
      - {"url": "http://127.0.0.1/?code=xxx&state=yyy"}  full redirect URL (auto-captured)
      - {"code": "xxx", "state": "yyy"}                  explicit fields
      - {"code": "xxx"}                                   bare code pasted by user
    """
    try:
        body = await request.json()
        code, state = _parse_code_state(body, fallback_state=_AUTH.get("state"))
        if not code:
            return _err("缺少授权码 (code)，请粘贴完整的跳转 URL 或授权码。", 400)
        if not state:
            return _err("缺少 state 参数，请重新获取登录链接。", 400)

        tmp_client: Optional[MIoTClient] = _AUTH.get("client")
        local_uuid: Optional[str] = _AUTH.get("uuid")
        if not tmp_client or not local_uuid:
            return _err("登录会话已过期，请重新点击「登录」。", 400)

        # Exchange code → token (state validation happens inside get_access_token_async).
        oauth_info = await tmp_client.get_access_token_async(code=code, state=state)

        # Persist credentials to ~/.miot_cache (same paths as login_miot.py).
        await tmp_client.storage.save_async(
            domain="cloud", name="oauth_info", data=oauth_info.model_dump())
        await tmp_client.storage.save_async(
            domain="cloud", name="uuid", data=local_uuid)

        try:
            await tmp_client.deinit_async()
        except Exception:  # pylint: disable=broad-except
            pass
        _AUTH["client"] = None
        _AUTH["uuid"] = None
        _AUTH["state"] = None

        await BE.reinit()
        if not BE.ready:
            return _err(BE.error or "登录后初始化失败", 500)
        return _json({"ok": True})
    except Exception as err:  # pylint: disable=broad-except
        return _err(str(err))


async def h_user(request: web.Request) -> web.Response:
    try:
        info = await BE.client.get_user_info_async()
        return _json(info.model_dump())
    except Exception as err:  # pylint: disable=broad-except
        return _client_err(err)


async def h_homes(request: web.Request) -> web.Response:
    try:
        homes = await BE.client.get_homes_async()
        return _json({hid: h.model_dump() for hid, h in homes.items()})
    except Exception as err:  # pylint: disable=broad-except
        return _client_err(err)


async def h_devices(request: web.Request) -> web.Response:
    try:
        devices = await BE.client.get_devices_async()
        out = []
        for did, dev in devices.items():
            d = dev.model_dump()
            d["did"] = did
            d["device_class"] = dev.model.split(".")[1] if "." in dev.model else dev.model
            out.append(d)
        out.sort(key=lambda x: (not x.get("online", False), x.get("home_name", ""), x.get("name", "")))
        return _json(out)
    except Exception as err:  # pylint: disable=broad-except
        return _client_err(err)


async def h_spec(request: web.Request) -> web.Response:
    did = request.query.get("did")
    urn = request.query.get("urn")
    try:
        if not urn:
            if not did:
                return _err("missing did or urn", 400)
            devices = await BE.client.get_devices_async()
            if did not in devices:
                return _err(f"device not found: {did}", 404)
            urn = devices[did].urn
        spec_device = await BE.client.spec_parser.parse_async(urn=urn)
        if not spec_device:
            return _err(f"failed to parse spec for urn: {urn}", 502)
        return _json(_serialize_spec(spec_device))
    except Exception as err:  # pylint: disable=broad-except
        return _client_err(err)


async def h_prop_get(request: web.Request) -> web.Response:
    try:
        body = await request.json()
        param = MIoTGetPropertyParam(did=body["did"], siid=int(body["siid"]), piid=int(body["piid"]))
        value = await BE.client.http_client.get_prop_async(param, immediately=True)
        return _json({"value": value})
    except Exception as err:  # pylint: disable=broad-except
        return _client_err(err)


async def h_props_get(request: web.Request) -> web.Response:
    """Batch property read: body = {did, props: [[siid, piid], ...]}."""
    try:
        body = await request.json()
        did = body["did"]
        params = [MIoTGetPropertyParam(did=did, siid=int(s), piid=int(p)) for s, p in body["props"]]
        results = await BE.client.http_client.get_props_async(params)
        return _json({"results": results})
    except Exception as err:  # pylint: disable=broad-except
        return _client_err(err)


async def h_prop_set(request: web.Request) -> web.Response:
    try:
        body = await request.json()
        param = MIoTSetPropertyParam(
            did=body["did"], siid=int(body["siid"]), piid=int(body["piid"]), value=body["value"])
        result = await BE.client.http_client.set_prop_async(param)
        return _json({"result": result})
    except Exception as err:  # pylint: disable=broad-except
        return _client_err(err)


async def h_action(request: web.Request) -> web.Response:
    try:
        body = await request.json()
        in_list = body.get("in", []) or []
        param = MIoTActionParam(
            did=body["did"], siid=int(body["siid"]), aiid=int(body["aiid"]), in_=in_list)
        result = await BE.client.http_client.action_async(param)
        return _json({"result": result})
    except Exception as err:  # pylint: disable=broad-except
        return _client_err(err)


# ---------------------------------------------------------------------------
# XiaoAI (小爱音箱) ASR/TTS bridge handlers
#
# Separate credential from the MIoT OAuth2 login above (see xiaomi_asr_bridge.py
# for why). The Mi passport token is obtained via an in-app browser login
# window (main.js `xiaomi-passport-login`, harvested from session cookies) and
# posted here; the speaker to use is picked from the user's *already known*
# miot devices (h_xiaomi_speaker_candidates), then cross-referenced against
# the MiNA account's speakers.
# ---------------------------------------------------------------------------
async def h_xiaomi_status(request: web.Request) -> web.Response:
    return _json({
        "has_token": ASR.has_token(),
        "device": ASR.device,
        "config": ASR.config,
    })


async def h_xiaomi_token_info(request: web.Request) -> web.Response:
    return _json(ASR.token_info())


async def h_xiaomi_token_post(request: web.Request) -> web.Response:
    try:
        body = await request.json()
        pass_token = body.get("passToken") or body.get("pass_token")
        user_id = body.get("userId") or body.get("user_id")
        if not pass_token or not user_id:
            return _err("缺少 passToken 或 userId", 400)
        await ASR.import_token(pass_token, user_id)
        ok, error = await ASR.test_login()
        return _json({"ok": ok, "error": error})
    except Exception as err:  # pylint: disable=broad-except
        return _err(str(err))


async def h_xiaomi_speaker_candidates(request: web.Request) -> web.Response:
    """Speakers picked from the user's *already logged-in* miot device list
    (not a raw MiNA dropdown) so the name is one they recognise."""
    if not BE.ready:
        return _err("请先在「设备控制」完成小米账号登录，才能按已知设备选择音箱。", 400)
    try:
        devices = await BE.client.get_devices_async()
        pattern = xiaomi_asr_bridge.speaker_candidate_pattern()
        out = []
        for did, dev in devices.items():
            d = dev.model_dump()
            name = d.get("name", "") or ""
            model = d.get("model", "") or ""
            if pattern.search(model) or pattern.search(name):
                d["did"] = did
                out.append(d)
        return _json(out)
    except Exception as err:  # pylint: disable=broad-except
        return _client_err(err)


async def h_xiaomi_select(request: web.Request) -> web.Response:
    try:
        body = await request.json()
        did = body.get("did") or ""
        name = body.get("name") or ""
        if not did and not name:
            return _err("缺少 did", 400)
        device = await ASR.select_by_did(did, fallback_name=name)
        return _json({"device": device})
    except Exception as err:  # pylint: disable=broad-except
        return _err(str(err))


async def h_xiaomi_config_get(request: web.Request) -> web.Response:
    return _json(ASR.config)


async def h_xiaomi_config_post(request: web.Request) -> web.Response:
    try:
        body = await request.json()
        ASR.set_webhook(body.get("webhook_url", ""), body.get("wake_word", ""))
        return _json({"ok": True, "config": ASR.config})
    except Exception as err:  # pylint: disable=broad-except
        return _err(str(err))


async def h_xiaomi_bridge_start(request: web.Request) -> web.Response:
    try:
        await ASR.start_bridge()
        return _json(ASR.bridge_status())
    except Exception as err:  # pylint: disable=broad-except
        return _err(str(err))


async def h_xiaomi_bridge_stop(request: web.Request) -> web.Response:
    await ASR.stop_bridge()
    return _json(ASR.bridge_status())


async def h_xiaomi_bridge_status(request: web.Request) -> web.Response:
    return _json(ASR.bridge_status())


async def h_xiaomi_speak(request: web.Request) -> web.Response:
    try:
        body = await request.json()
        text = (body.get("text") or "").strip()
        if not text:
            return _err("缺少 text", 400)
        await ASR.speak(text)
        return _json({"ok": True})
    except Exception as err:  # pylint: disable=broad-except
        return _err(str(err))


# ---------------------------------------------------------------------------
# Miloco Docker controller handlers
# ---------------------------------------------------------------------------
async def h_miloco_status(request: web.Request) -> web.Response:
    try:
        return _json(await MILOCO.status())
    except Exception as err:  # pylint: disable=broad-except
        return _err(str(err))


async def h_miloco_build(request: web.Request) -> web.Response:
    try:
        await MILOCO.build_image()
        return _json(MILOCO.build_status())
    except Exception as err:  # pylint: disable=broad-except
        return _err(str(err))


async def h_miloco_build_status(request: web.Request) -> web.Response:
    return _json(MILOCO.build_status())


async def h_miloco_start(request: web.Request) -> web.Response:
    try:
        return _json(await MILOCO.start())
    except Exception as err:  # pylint: disable=broad-except
        return _err(str(err))


async def h_miloco_stop(request: web.Request) -> web.Response:
    try:
        return _json(await MILOCO.stop())
    except Exception as err:  # pylint: disable=broad-except
        return _err(str(err))


async def h_miloco_logs(request: web.Request) -> web.Response:
    try:
        tail = int(request.query.get("tail", "200"))
        return _json({"logs": await MILOCO.logs(tail=tail)})
    except Exception as err:  # pylint: disable=broad-except
        return _err(str(err))


# ---------------------------------------------------------------------------
# SSR agent handlers (chat assistant + model config)
# ---------------------------------------------------------------------------
async def h_agent_status(request: web.Request) -> web.Response:
    try:
        return _json(AGENT.status())
    except Exception as err:  # pylint: disable=broad-except
        return _err(str(err))


async def h_agent_models_get(request: web.Request) -> web.Response:
    if not AGENT.available():
        return _err("SSR agent 不可用", 400)
    try:
        return _json(AGENT.list_models())
    except Exception as err:  # pylint: disable=broad-except
        return _err(str(err))


async def h_agent_models_post(request: web.Request) -> web.Response:
    if not AGENT.available():
        return _err("SSR agent 不可用", 400)
    try:
        body = await request.json()
        return _json(AGENT.set_model(body))
    except Exception as err:  # pylint: disable=broad-except
        return _err(str(err))


async def h_miloco_webhook(request: web.Request) -> web.Response:
    """Miloco's agent-webhook contract: ``{action, payload}`` -> ``{code,
    message, data}`` (always HTTP 200; failure is signalled via ``code``).

    Alternative entry point to the fixed-port receiver
    ``ssr.integrations.miloco.start_bus_bridge`` already runs — see
    ``ssr_agent_bridge.SSRAgentBridge.handle_miloco_webhook``.
    """
    if not AGENT.available():
        return _json({"code": 1, "message": "SSR agent 不可用", "data": None})
    try:
        body = await request.json()
    except (json.JSONDecodeError, TypeError):
        return _json({"code": 1, "message": "invalid json", "data": None})
    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(None, AGENT.handle_miloco_webhook, body)
    return _json(result)


async def ws_agent(request: web.Request) -> web.WebSocketResponse:
    """Chat WebSocket. Client sends {type:'chat', text, attachments:[…]}; the
    server streams the agent's live events and a final {type:'done', reply}."""
    ws = web.WebSocketResponse(max_msg_size=64 * 1024 * 1024)
    await ws.prepare(request)
    loop = asyncio.get_event_loop()

    if not AGENT.available():
        await ws.send_json({"type": "error", "message": AGENT.status().get("message", "SSR agent 不可用")})
        await ws.close()
        return ws

    def emit(event: dict) -> None:
        # Called from the worker thread running the (blocking) turn.
        try:
            asyncio.run_coroutine_threadsafe(ws.send_json(_jsonable(event)), loop)
        except Exception:  # pylint: disable=broad-except
            pass

    async for msg in ws:
        if msg.type in (WSMsgType.CLOSE, WSMsgType.ERROR):
            break
        if msg.type != WSMsgType.TEXT:
            continue
        try:
            data = json.loads(msg.data)
        except (json.JSONDecodeError, TypeError):
            await ws.send_json({"type": "error", "message": "invalid json"})
            continue
        if data.get("type") != "chat":
            continue
        text = data.get("text") or ""
        attachments = data.get("attachments") or []
        try:
            reply = await loop.run_in_executor(None, AGENT.run_turn, text, attachments, emit)
            await ws.send_json({"type": "done", "reply": reply})
        except Exception as err:  # pylint: disable=broad-except
            _LOGGER.exception("agent turn failed")
            await ws.send_json({"type": "error", "message": str(err)})
    return ws


def _jsonable(event: dict) -> dict:
    """Drop/encode non-JSON-serialisable fields (e.g. raw bytes) from an event."""
    out = {}
    for k, v in event.items():
        if isinstance(v, (bytes, bytearray)):
            continue
        try:
            json.dumps(v, default=str)
            out[k] = v
        except (TypeError, ValueError):
            out[k] = str(v)
    return out


async def on_startup(app: web.Application) -> None:
    try:
        await BE.init()
    except Exception as err:  # pylint: disable=broad-except
        BE.error = str(err)
        _LOGGER.error("backend init failed: %s", err)


async def on_cleanup(app: web.Application) -> None:
    await BE.deinit()
    await ASR.close()


def build_app() -> web.Application:
    app = web.Application()
    app.router.add_get("/api/health", h_health)
    app.router.add_get("/api/auth/status", h_auth_status)
    app.router.add_post("/api/auth/start", h_auth_start)
    app.router.add_post("/api/auth/complete", h_auth_complete)
    app.router.add_get("/api/user", h_user)
    app.router.add_get("/api/homes", h_homes)
    app.router.add_get("/api/devices", h_devices)
    app.router.add_get("/api/spec", h_spec)
    app.router.add_post("/api/prop/get", h_prop_get)
    app.router.add_post("/api/props/get", h_props_get)
    app.router.add_post("/api/prop/set", h_prop_set)
    app.router.add_post("/api/action", h_action)
    app.router.add_get("/api/xiaomi/status", h_xiaomi_status)
    app.router.add_get("/api/xiaomi/token", h_xiaomi_token_info)
    app.router.add_post("/api/xiaomi/token", h_xiaomi_token_post)
    app.router.add_get("/api/xiaomi/speaker-candidates", h_xiaomi_speaker_candidates)
    app.router.add_post("/api/xiaomi/select", h_xiaomi_select)
    app.router.add_get("/api/xiaomi/config", h_xiaomi_config_get)
    app.router.add_post("/api/xiaomi/config", h_xiaomi_config_post)
    app.router.add_post("/api/xiaomi/bridge/start", h_xiaomi_bridge_start)
    app.router.add_post("/api/xiaomi/bridge/stop", h_xiaomi_bridge_stop)
    app.router.add_get("/api/xiaomi/bridge/status", h_xiaomi_bridge_status)
    app.router.add_post("/api/xiaomi/speak", h_xiaomi_speak)
    app.router.add_get("/api/miloco/status", h_miloco_status)
    app.router.add_post("/api/miloco/build", h_miloco_build)
    app.router.add_get("/api/miloco/build/status", h_miloco_build_status)
    app.router.add_post("/api/miloco/start", h_miloco_start)
    app.router.add_post("/api/miloco/stop", h_miloco_stop)
    app.router.add_get("/api/miloco/logs", h_miloco_logs)
    app.router.add_get("/api/agent/status", h_agent_status)
    app.router.add_get("/api/agent/models", h_agent_models_get)
    app.router.add_post("/api/agent/models", h_agent_models_post)
    app.router.add_get("/ws/agent", ws_agent)
    app.router.add_post("/miloco/webhook", h_miloco_webhook)
    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    return app


def main() -> None:
    if platform.system() == "Windows":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    app = build_app()
    runner = web.AppRunner(app)
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(runner.setup())
    site = web.TCPSite(runner, "127.0.0.1", 0)
    loop.run_until_complete(site.start())
    # Report the actual port to the parent process.
    port = None
    for server in runner.sites:
        sock = list(server._server.sockets)[0]  # type: ignore[attr-defined]
        port = sock.getsockname()[1]
        break
    print(f"MIOT_BACKEND_PORT={port}", flush=True)
    try:
        loop.run_forever()
    except KeyboardInterrupt:
        pass
    finally:
        loop.run_until_complete(runner.cleanup())


if __name__ == "__main__":
    main()
