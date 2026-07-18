# -*- coding: utf-8 -*-
"""XiaoAI (小爱音箱) ASR → webhook bridge.

This is a *different* credential and API surface than the rest of this app:
`server.py`'s `MIoTClient` uses the MIoT open-platform **OAuth2** login (for
smart-home device control), cached in `~/.miot_cache`. XiaoAI's ASR (what the
user said) and TTS takeover need the Mi **passport** `passToken` instead — the
same one the `miservice_fork`-based tools (and ssr-agent's XiaoAI channel)
use. See the "小爱音箱" tab / README for how this token is obtained (a headed
Electron login window, harvested via `session.cookies`, see main.js).

The on-disk token format is the plain ``miservice`` ``MiTokenStore`` schema
(``deviceId``/``userAgent``/``passToken``/``userId``/``micoapi``), which is
exactly what the standalone `xiaomi-speaker-sdk` package expects — so the
token file this module writes can be pointed at directly from a script built
with that SDK, or its passToken/userId copied into
``xiaomi_speaker_sdk.token_store.import_pass_token()``.
"""
import asyncio
import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import aiohttp

_LOGGER = logging.getLogger("miot_desktop.xiaomi_asr")


def _base_dir() -> Path:
    cache_path = os.environ.get("MIOT_CACHE_PATH", os.path.join(os.path.expanduser("~"), ".miot_cache"))
    return Path(cache_path) / "xiaomi_speaker"


TOKEN_PATH = Path(os.environ.get("XIAOMI_SPEAKER_TOKEN_PATH") or (_base_dir() / "token.json"))
CONFIG_PATH = Path(os.environ.get("XIAOMI_SPEAKER_CONFIG_PATH") or (_base_dir() / "config.json"))


def _load_json(path: Path) -> Dict[str, Any]:
    try:
        return json.loads(path.read_text("utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return {}


def _save_json(path: Path, data: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), "utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        pass


def _account_user_agent(device_id: str) -> str:
    """Stable Mi-smarthome Android User-Agent keyed on the deviceId (fewer
    security-verification challenges than a random browser UA)."""
    return f"Android-7.1.1-1.0.0-ONEPLUS A3010-136-{device_id} APP/xiaomi.smarthome APPV/62830"


def _is_auth_error(exc: Exception) -> bool:
    msg = str(exc).lower()
    return (
        "401" in msg or "auth" in msg or "login failed" in msg or "unauthorized" in msg
        or "'code': 3" in msg or '"code": 3' in msg
    )


try:
    from miservice import MiAccount, MiIOService, MiNAService, MiTokenStore  # noqa: F401
    MISERVICE_AVAILABLE = True
except ImportError:  # pragma: no cover - guarded, surfaced via API errors
    MISERVICE_AVAILABLE = False

    class MiAccount:  # type: ignore[no-redef]
        pass


class _CustomMiAccount(MiAccount):
    """Same hardened login as ssr-agent's XiaoAI channel: a stable per-device
    User-Agent, and a re-login path that really invalidates an expired
    serviceToken instead of trusting a dead one forever."""

    def __init__(self, session, token_store):
        super().__init__(session, "", "", token_store)

    async def _serviceLogin(self, uri, data=None):
        device_id = self.token["deviceId"]
        user_agent = _account_user_agent(device_id)
        if self.token.get("userAgent") != user_agent:
            self.token["userAgent"] = user_agent
            if self.token_store:
                self.token_store.save_token(self.token)
        self.now_ua = user_agent
        headers = {"User-Agent": user_agent}
        cookies = {"sdkVersion": "3.9", "deviceId": device_id}
        if "passToken" in self.token:
            cookies["userId"] = self.token["userId"]
            cookies["passToken"] = self.token["passToken"]
        else:
            cookies["passToken"] = ""
        url = "https://account.xiaomi.com/pass/" + uri
        method = "GET" if data is None else "POST"
        async with self.session.request(
            method, url, data=data, cookies=cookies, headers=headers, ssl=False,
        ) as r:
            raw = await r.read()
        return json.loads(raw[11:])

    async def login(self, sid):
        if not self.token:
            self.token = self.token_store.load_token() if self.token_store else None
            if not self.token:
                self.token = {}
        if not self.token.get("deviceId"):
            try:
                from miservice.miaccount import get_random
                self.token["deviceId"] = get_random(16).upper()
            except ImportError:
                import secrets
                self.token["deviceId"] = secrets.token_hex(8).upper()
        self.token["userAgent"] = _account_user_agent(self.token["deviceId"])
        device_id = self.token.get("deviceId")
        user_agent = self.token.get("userAgent")
        self.now_ua = user_agent
        if self.token_store and device_id and user_agent:
            self.token_store.save_token({"deviceId": device_id, "userAgent": user_agent})

        if self.token and sid in self.token and self.token.get("userId") and self.token.get("passToken"):
            return True
        if not self.token.get("passToken"):
            # No password login supported here — this bridge is token-only
            # (the token comes from the in-app browser login).
            return False

        try:
            resp = await self._serviceLogin(f"serviceLogin?sid={sid}&_json=true")
            if (resp.get("code") != 0 or resp.get("notificationUrl") or resp.get("captchaUrl")
                    or ("userId" not in resp and not self.token.get("userId"))):
                raise RuntimeError(f"login rejected: {resp}")
            if "userId" in resp:
                self.token["userId"] = resp["userId"]
            if "passToken" in resp:
                self.token["passToken"] = resp["passToken"]
            serviceToken = await self._securityTokenService(resp["location"], resp["nonce"], resp["ssecurity"])
            self.token[sid] = (resp["ssecurity"], serviceToken)
            if self.token_store:
                self.token_store.save_token(self.token)
            return True
        except Exception:
            _LOGGER.exception("XiaoAI login(%s) failed", sid)
            self.token = {"deviceId": device_id, "userAgent": user_agent}
            if self.token_store:
                self.token_store.save_token(self.token)
            return False

    def _invalidate_sid(self, sid):
        if not self.token:
            self.token = (self.token_store.load_token() if self.token_store else None) or {}
        self.token.pop(sid, None)
        if self.token_store:
            self.token_store.save_token(self.token)

    async def mi_request(self, sid, url, data, headers, relogin=True):
        had_sid_token = bool(self.token and sid in self.token)
        try:
            return await super().mi_request(sid, url, data, headers, relogin=False)
        except Exception as e:
            if relogin and had_sid_token and _is_auth_error(e):
                self._invalidate_sid(sid)
                if await self.login(sid):
                    return await super().mi_request(sid, url, data, headers, relogin=False)
            raise


# Per-hardware MiIO play-text action (siid, aiid); mirrors the community
# xiaogpt hardware map. Used when MiNA text_to_speech silently no-ops.
_TTS_MIIO_COMMANDS = {
    "LX04": (5, 1), "LX06": (5, 1), "LX01": (5, 1), "LX5A": (5, 1), "LX05A": (5, 1),
    "L05B": (5, 3), "L05C": (5, 3), "S12A": (5, 1), "S12": (5, 1), "L06A": (5, 1),
    "L07A": (5, 1), "L09A": (3, 1), "L15A": (7, 3), "L17A": (7, 3), "X08E": (7, 3),
    "X10A": (7, 3), "X6A": (7, 3), "X08C": (7, 3),
}


class XiaomiAsrBridge:
    """Owns the token/device/webhook config and the background forwarding loop."""

    def __init__(self) -> None:
        self.session = None
        self.account: Optional[_CustomMiAccount] = None
        self.mina = None
        self.miio = None
        self.device: Optional[Dict[str, str]] = None
        self.config: Dict[str, Any] = _load_json(CONFIG_PATH)
        self.config.setdefault("mina_device_id", "")
        self.config.setdefault("speaker_name", "")
        self.config.setdefault("webhook_url", "")
        self.config.setdefault("wake_word", "")
        self._bridge_task: Optional["asyncio.Task"] = None
        self._bridge_error: Optional[str] = None
        self._forwarded_count = 0
        self._last_forwarded_at: Optional[int] = None

    # ------------------------------------------------------------ token
    def has_token(self) -> bool:
        return bool(_load_json(TOKEN_PATH).get("passToken"))

    def token_info(self) -> Dict[str, Any]:
        tok = _load_json(TOKEN_PATH)
        pass_token = tok.get("passToken") or ""
        return {
            "token_path": str(TOKEN_PATH),
            "has_token": bool(pass_token),
            "user_id": tok.get("userId") or "",
            "pass_token_masked": ("…" + pass_token[-6:]) if len(pass_token) > 6 else ("<set>" if pass_token else ""),
        }

    async def import_token(self, pass_token: str, user_id: str) -> None:
        tok = _load_json(TOKEN_PATH)
        if not tok.get("deviceId"):
            try:
                from miservice.miaccount import get_random
                tok["deviceId"] = get_random(16).upper()
            except ImportError:
                import secrets
                tok["deviceId"] = secrets.token_hex(8).upper()
        tok["userAgent"] = _account_user_agent(tok["deviceId"])
        tok["passToken"] = (pass_token or "").strip()
        tok["userId"] = str(user_id or "").strip()
        tok.pop("micoapi", None)
        _save_json(TOKEN_PATH, tok)
        await self.close()  # force a fresh login with the new token

    async def close(self) -> None:
        await self.stop_bridge()
        if self.session is not None:
            try:
                await self.session.close()
            except Exception:  # pylint: disable=broad-except
                pass
        self.session = None
        self.account = None
        self.mina = None
        self.miio = None
        self.device = None

    async def _ensure_client(self) -> None:
        if not MISERVICE_AVAILABLE:
            raise RuntimeError("缺少依赖 miservice_fork，请安装：pip install miservice_fork")
        if self.session is None:
            self.session = aiohttp.ClientSession()
        if self.account is None:
            self.account = _CustomMiAccount(self.session, MiTokenStore(str(TOKEN_PATH)))
            self.mina = MiNAService(self.account)
            self.miio = MiIOService(self.account)

    async def test_login(self) -> Tuple[bool, Optional[str]]:
        if not self.has_token():
            return False, "尚未提取 Token，请先点击「提取 Token」登录小米账号。"
        await self._ensure_client()
        try:
            ok = await self.account.login("micoapi")
        except Exception as e:  # pylint: disable=broad-except
            return False, str(e)
        if ok:
            return True, None
        return False, "登录失败：Token 无效或已过期，请重新提取 Token。"

    # ----------------------------------------------------------- devices
    async def list_mina_devices(self) -> List[Dict[str, str]]:
        await self._ensure_client()
        devices = await self.mina.device_list() or []
        return [
            {
                "device_id": d.get("deviceID", ""),
                "name": d.get("name", ""),
                "hardware": d.get("hardware", ""),
                "did": str(d.get("miotDID", "")),
            }
            for d in devices
        ]

    async def select_by_did(self, did: str, fallback_name: str = "") -> Dict[str, str]:
        """Pick the speaker for ASR/TTS by cross-referencing a MIoT `did` (from
        the user's already-known miot devices list) against the MiNA account's
        speakers, falling back to matching the device's name."""
        devices = await self.list_mina_devices()
        chosen = next((d for d in devices if d["did"] and did and d["did"] == str(did)), None)
        if chosen is None and fallback_name:
            chosen = next((d for d in devices if fallback_name and fallback_name in d["name"]), None)
        if chosen is None:
            raise RuntimeError(
                "未在小爱账号下找到匹配的音箱。请确认：1) 该设备支持小爱同学；"
                "2) 提取 Token 时登录的小米账号与米家账号一致。"
            )
        self.device = chosen
        self.config["mina_device_id"] = chosen["device_id"]
        self.config["speaker_name"] = chosen["name"]
        _save_json(CONFIG_PATH, self.config)
        return chosen

    async def _ensure_device_selected(self) -> None:
        if self.device is not None:
            return
        mina_device_id = self.config.get("mina_device_id")
        speaker_name = self.config.get("speaker_name")
        if not mina_device_id and not speaker_name:
            raise RuntimeError("尚未选择音箱，请先在列表中选择一个小爱音箱设备。")
        devices = await self.list_mina_devices()
        chosen = None
        if mina_device_id:
            chosen = next((d for d in devices if d["device_id"] == mina_device_id), None)
        if chosen is None and speaker_name:
            chosen = next((d for d in devices if speaker_name in d["name"]), None)
        if chosen is None:
            raise RuntimeError("未找到已选择的音箱（可能已解绑），请重新选择。")
        self.device = chosen

    # ------------------------------------------------------------- ASR (in)
    async def latest_ask(self) -> Optional[Tuple[str, str]]:
        await self._ensure_device_selected()
        ask = await self._conversation_ask()
        if ask is not None:
            return ask
        return await self._nlp_result_ask()

    async def _conversation_ask(self, _retried: bool = False) -> Optional[Tuple[str, str]]:
        acc = self.account
        if not (acc.token and "micoapi" in acc.token):
            try:
                if not await acc.login("micoapi"):
                    return None
            except Exception:  # pylint: disable=broad-except
                return None
        try:
            service_token = acc.token["micoapi"][1]
            user_id = str(acc.token["userId"])
        except (KeyError, IndexError, TypeError):
            return None

        hardware = self.device.get("hardware", "")
        ts = int(time.time() * 1000)
        url = (
            "https://userprofile.mina.mi.com/device_profile/v2/conversation"
            f"?source=dialogu&hardware={hardware}&timestamp={ts}&limit=2"
        )
        cookies = {"userId": user_id, "serviceToken": service_token, "deviceId": self.device["device_id"]}
        headers = {"User-Agent": getattr(acc, "now_ua", "")}
        try:
            async with acc.session.get(url, cookies=cookies, headers=headers) as r:
                resp = await r.json(content_type=None)
        except Exception:  # pylint: disable=broad-except
            return None

        code = resp.get("code") if isinstance(resp, dict) else None
        if code != 0:
            msg = (resp or {}).get("message", "")
            if not _retried and ("auth" in str(msg).lower() or code in (401, 2)):
                try:
                    acc._invalidate_sid("micoapi")
                    if await acc.login("micoapi"):
                        return await self._conversation_ask(_retried=True)
                except Exception:  # pylint: disable=broad-except
                    pass
            return None

        raw = resp.get("data")
        try:
            data = json.loads(raw) if isinstance(raw, str) else (raw or {})
        except (json.JSONDecodeError, TypeError):
            return None
        records = (data or {}).get("records") or []
        best: Optional[Tuple[int, str, str]] = None
        for rec in records:
            q = (rec.get("query") or "").strip()
            t = int(rec.get("time") or 0)
            rid = str(rec.get("requestId") or rec.get("time") or t)
            if q and (best is None or t >= best[0]):
                best = (t, rid, q)
        return (best[1], best[2]) if best else None

    async def _nlp_result_ask(self) -> Optional[Tuple[str, str]]:
        try:
            raw = await self.mina.ubus_request(self.device["device_id"], "nlp_result_get", "mibrain", {})
        except Exception:  # pylint: disable=broad-except
            return None
        data = (raw or {}).get("data") or {}
        if data.get("code") != 0:
            return None
        try:
            result = (json.loads(data.get("info") or "{}") or {}).get("result") or []
        except (json.JSONDecodeError, TypeError):
            return None
        best: Optional[Tuple[int, str, str]] = None
        for item in result:
            if "nlp" not in item:
                continue
            try:
                nlp = json.loads(item["nlp"])
                ts = int(nlp["meta"]["timestamp"])
                rid = str(nlp["meta"]["request_id"])
                for ans in nlp.get("response", {}).get("answer", []) or []:
                    q = ((ans.get("intention") or {}).get("query") or "").strip()
                    if q and (best is None or ts >= best[0]):
                        best = (ts, rid, q)
            except (json.JSONDecodeError, KeyError, TypeError, ValueError):
                continue
        return (best[1], best[2]) if best else None

    # ------------------------------------------------------------ TTS (out)
    def _tts_command(self) -> Optional[Tuple[int, int]]:
        hw = (self.device or {}).get("hardware", "")
        return _TTS_MIIO_COMMANDS.get(hw.upper())

    async def _speak_chunk(self, text: str) -> bool:
        cmd = self._tts_command()
        did = (self.device or {}).get("did", "")
        if cmd and did and self.miio is not None:
            try:
                code = await self.miio.miot_action(did, list(cmd), [text])
                if code == 0:
                    return True
            except Exception:  # pylint: disable=broad-except
                pass
        try:
            await self.mina.text_to_speech(self.device["device_id"], text)
            return True
        except Exception:  # pylint: disable=broad-except
            _LOGGER.exception("MiNA TTS failed")
            return False

    async def speak(self, text: str) -> None:
        await self._ensure_device_selected()
        text = (text or "").strip()
        if not text:
            return
        try:
            await self.mina.player_pause(self.device["device_id"])
        except Exception:  # pylint: disable=broad-except
            pass
        await asyncio.sleep(0.4)
        for chunk in _chunk_for_tts(text):
            if not await self._speak_chunk(chunk):
                break

    # -------------------------------------------------------- webhook bridge
    def set_webhook(self, webhook_url: str, wake_word: str = "") -> None:
        self.config["webhook_url"] = (webhook_url or "").strip()
        self.config["wake_word"] = (wake_word or "").strip()
        _save_json(CONFIG_PATH, self.config)

    def bridge_status(self) -> Dict[str, Any]:
        return {
            "running": self._bridge_task is not None and not self._bridge_task.done(),
            "webhook_url": self.config.get("webhook_url", ""),
            "wake_word": self.config.get("wake_word", ""),
            "device": self.device,
            "forwarded_count": self._forwarded_count,
            "last_forwarded_at": self._last_forwarded_at,
            "error": self._bridge_error,
        }

    async def start_bridge(self) -> None:
        await self._ensure_device_selected()
        if not self.config.get("webhook_url"):
            raise RuntimeError("请先填写 Webhook URL。")
        if self._bridge_task is not None and not self._bridge_task.done():
            return
        self._bridge_error = None
        self._bridge_task = asyncio.create_task(self._bridge_loop())

    async def stop_bridge(self) -> None:
        task, self._bridge_task = self._bridge_task, None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # pylint: disable=broad-except
                pass

    async def _bridge_loop(self) -> None:
        last_request_id: Optional[str] = None
        first_poll = True
        async with aiohttp.ClientSession() as webhook_session:
            while True:
                try:
                    ask = await self.latest_ask()
                    self._bridge_error = None
                except Exception as e:  # pylint: disable=broad-except
                    self._bridge_error = str(e)
                    ask = None
                if ask is not None:
                    request_id, question = ask
                    if request_id != last_request_id:
                        last_request_id = request_id
                        if not first_poll:
                            await self._forward_to_webhook(webhook_session, request_id, question)
                first_poll = False
                await asyncio.sleep(1.0)

    async def _forward_to_webhook(self, webhook_session, request_id: str, question: str) -> None:
        wake_word = self.config.get("wake_word") or ""
        forward_question = question
        if wake_word:
            if wake_word not in question:
                return
            forward_question = question.replace(wake_word, "", 1).strip()
            if not forward_question:
                return
        payload = {
            "request_id": request_id,
            "question": forward_question,
            "raw_question": question,
            "device_id": self.device["device_id"],
            "device_name": self.device["name"],
            "timestamp": int(time.time() * 1000),
        }
        try:
            async with webhook_session.post(
                self.config["webhook_url"], json=payload, timeout=aiohttp.ClientTimeout(total=10),
            ) as r:
                if r.status >= 400:
                    self._bridge_error = f"Webhook 返回 HTTP {r.status}"
            self._forwarded_count += 1
            self._last_forwarded_at = int(time.time() * 1000)
        except Exception as e:  # pylint: disable=broad-except
            self._bridge_error = f"Webhook 推送失败：{e}"


def _chunk_for_tts(text: str, limit: int = 240) -> List[str]:
    text = " ".join(text.split())
    if len(text) <= limit:
        return [text]
    chunks: List[str] = []
    buf = ""
    for token in text.replace("。", "。\n").replace(". ", ".\n").splitlines():
        if len(buf) + len(token) > limit and buf:
            chunks.append(buf.strip())
            buf = ""
        buf += token + " "
    if buf.strip():
        chunks.append(buf.strip())
    return chunks or [text[:limit]]


def speaker_candidate_pattern() -> "re.Pattern":
    return re.compile(r"speaker|wifispeaker|音箱|小爱", re.I)
