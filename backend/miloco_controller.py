# -*- coding: utf-8 -*-
"""Miloco (Xiaomi 感知家庭网关) Docker controller.

Lets the desktop GUI launch/stop the official
[Xiaomi Miloco](https://github.com/XiaoMi/xiaomi-miloco) service in Docker
(``--network host``) and preview its web dashboard, **auto-injecting the Mi
account credentials this app already holds** so the user never re-binds.

How the credential injection works
----------------------------------
Miloco persists its Mi OAuth token in a SQLite ``kv`` table (at
``$MILOCO_HOME/miloco.db``) under the key ``MIOT_TOKEN_INFO_KEY``, as the JSON
of a ``MIoTOauthInfo`` (fields ``access_token`` / ``refresh_token`` /
``expires_ts`` / ``user_info``). This desktop app caches the *same*
``MIoTOauthInfo`` (via ``miot_kit``'s ``MIoTStorage``) in ``~/.miot_cache``.
Because both sides use the same upstream ``miot`` library, the shapes are
identical — so we can seed Miloco's DB with our token before its first boot and
it comes up already bound, no OAuth round-trip.

We create the ``kv`` table ourselves (``CREATE TABLE IF NOT EXISTS`` — the same
DDL Miloco uses, so its own boot-time table creation is a no-op) and insert the
token row. On start Miloco reads it in ``init_miot_info_dict`` and is
authenticated immediately.

Docker is driven through the ``docker`` CLI (subprocess), which is what's
available in the packaged app's environment; there is no Python docker SDK
dependency.
"""
import asyncio
import json
import logging
import os
import shutil
import sqlite3
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx

_LOGGER = logging.getLogger("miot_desktop.miloco")

# ---------------------------------------------------------------------------
# Configuration (overridable via env)
# ---------------------------------------------------------------------------
IMAGE_NAME = os.environ.get("MILOCO_IMAGE", "miot-desktop/miloco:latest")
CONTAINER_NAME = os.environ.get("MILOCO_CONTAINER", "miot-desktop-miloco")
MILOCO_PORT = int(os.environ.get("MILOCO_PORT", "1810"))
MILOCO_URL = f"http://127.0.0.1:{MILOCO_PORT}"
CACHE_PATH = os.environ.get("MIOT_CACHE_PATH", os.path.join(os.path.expanduser("~"), ".miot_cache"))
# Host directory bind-mounted into the container as $MILOCO_HOME. Lives next to
# the miot cache so all Mi-account state sits together and survives restarts.
MILOCO_HOME = Path(os.environ.get("MILOCO_HOME_HOST", os.path.join(CACHE_PATH, "miloco_home")))
# Location of the Dockerfile used to build the image when it's missing.
_DOCKERFILE_DIR = Path(os.environ.get(
    "MILOCO_DOCKERFILE_DIR",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "docker", "miloco"),
)).resolve()

MIOT_TOKEN_INFO_KEY = "MIOT_TOKEN_INFO_KEY"


def _now_ms() -> int:
    return int(time.time() * 1000)


def _docker_bin() -> Optional[str]:
    return shutil.which("docker")


async def _run(cmd: List[str], timeout: float = 30.0) -> subprocess.CompletedProcess:
    """Run a command off the event loop, capturing stdout/stderr."""
    def _call() -> subprocess.CompletedProcess:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    return await asyncio.get_event_loop().run_in_executor(None, _call)


class MilocoController:
    def __init__(self) -> None:
        self._build_task: Optional[asyncio.Task] = None
        self._build_log: List[str] = []
        self._build_error: Optional[str] = None

    # ------------------------------------------------------------ docker probe
    async def docker_available(self) -> bool:
        if not _docker_bin():
            return False
        try:
            r = await _run([_docker_bin(), "info", "--format", "{{.ServerVersion}}"], timeout=10)
            return r.returncode == 0
        except Exception:  # pylint: disable=broad-except
            return False

    async def image_exists(self) -> bool:
        if not _docker_bin():
            return False
        try:
            r = await _run([_docker_bin(), "image", "inspect", IMAGE_NAME], timeout=10)
            return r.returncode == 0
        except Exception:  # pylint: disable=broad-except
            return False

    async def _container_state(self) -> Optional[str]:
        """Return the container's docker state string (running/exited/…) or None
        if the container does not exist."""
        if not _docker_bin():
            return None
        try:
            r = await _run(
                [_docker_bin(), "inspect", "-f", "{{.State.Status}}", CONTAINER_NAME], timeout=10)
        except Exception:  # pylint: disable=broad-except
            return None
        if r.returncode != 0:
            return None
        return (r.stdout or "").strip() or None

    # ------------------------------------------------------------ credentials
    def _load_oauth_dict(self) -> Optional[Dict[str, Any]]:
        """Read this app's cached Mi OAuth token from ~/.miot_cache.

        MIoTStorage may append a 32-byte sha256 integrity digest after the JSON
        payload, so we strip a trailing digest if the whole blob doesn't parse.
        """
        base = Path(CACHE_PATH) / "cloud"
        for name in ("oauth_info.dict", "oauth_info.json", "oauth_info"):
            p = base / name
            if not p.exists():
                continue
            try:
                raw = p.read_bytes()
            except OSError:
                continue
            for end in (len(raw), len(raw) - 32):
                if end <= 0:
                    continue
                try:
                    data = json.loads(raw[:end].decode("utf-8"))
                except (json.JSONDecodeError, UnicodeDecodeError):
                    continue
                if isinstance(data, dict) and data.get("access_token"):
                    return data
        return None

    def has_credentials(self) -> bool:
        return self._load_oauth_dict() is not None

    def _seed_credentials(self) -> bool:
        """Write our Mi OAuth token into Miloco's SQLite kv table so it boots
        already bound. Returns True if a token was seeded."""
        oauth = self._load_oauth_dict()
        if not oauth:
            return False
        MILOCO_HOME.mkdir(parents=True, exist_ok=True)
        db_path = MILOCO_HOME / "miloco.db"
        # Keep only the fields Miloco's MIoTOauthInfo expects (drop extras that a
        # newer cache format might carry, which would fail strict validation).
        payload = {
            "access_token": oauth.get("access_token"),
            "refresh_token": oauth.get("refresh_token"),
            "expires_ts": oauth.get("expires_ts"),
        }
        if oauth.get("user_info"):
            payload["user_info"] = oauth["user_info"]
        value = json.dumps(payload, ensure_ascii=False)
        conn = sqlite3.connect(str(db_path))
        try:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS kv (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    key TEXT UNIQUE NOT NULL,
                    value TEXT,
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_kv_key ON kv(key)")
            now = _now_ms()
            conn.execute(
                """
                INSERT INTO kv (key, value, created_at, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
                """,
                (MIOT_TOKEN_INFO_KEY, value, now, now),
            )
            conn.commit()
        finally:
            conn.close()
        _LOGGER.info("seeded Mi OAuth token into %s", db_path)
        return True

    def server_token(self) -> str:
        """Miloco's auto-generated web/API bearer token from its config.json."""
        try:
            data = json.loads((MILOCO_HOME / "config.json").read_text("utf-8"))
        except (OSError, json.JSONDecodeError):
            return ""
        server = data.get("server") if isinstance(data, dict) else None
        return str(server.get("token")) if isinstance(server, dict) and server.get("token") else ""

    # ------------------------------------------------------------- image build
    async def build_image(self) -> None:
        """Build the Miloco image from the bundled Dockerfile (idempotent)."""
        if self._build_task is not None and not self._build_task.done():
            return
        self._build_log = []
        self._build_error = None
        self._build_task = asyncio.create_task(self._build_image_impl())

    async def _build_image_impl(self) -> None:
        docker = _docker_bin()
        if not docker:
            self._build_error = "未检测到 docker，请先安装 Docker。"
            return
        dockerfile = _DOCKERFILE_DIR / "Dockerfile"
        if not dockerfile.exists():
            self._build_error = f"未找到 Dockerfile：{dockerfile}"
            return
        cmd = [docker, "build", "-t", IMAGE_NAME, "-f", str(dockerfile), str(_DOCKERFILE_DIR)]
        self._build_log.append("$ " + " ".join(cmd))

        def _call() -> int:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
            assert proc.stdout is not None
            for line in proc.stdout:
                self._build_log.append(line.rstrip())
                if len(self._build_log) > 2000:
                    del self._build_log[:1000]
            return proc.wait()

        try:
            code = await asyncio.get_event_loop().run_in_executor(None, _call)
        except Exception as e:  # pylint: disable=broad-except
            self._build_error = str(e)
            return
        if code != 0:
            self._build_error = f"镜像构建失败（exit {code}）。请查看构建日志。"

    def build_status(self) -> Dict[str, Any]:
        running = self._build_task is not None and not self._build_task.done()
        return {
            "building": running,
            "error": self._build_error,
            "log_tail": self._build_log[-200:],
        }

    # ------------------------------------------------------------ run / stop
    async def start(self) -> Dict[str, Any]:
        docker = _docker_bin()
        if not docker:
            raise RuntimeError("未检测到 docker，请先安装 Docker 并确保其正在运行。")
        if not await self.image_exists():
            raise RuntimeError(f"镜像 {IMAGE_NAME} 不存在，请先点击「构建镜像」。")

        # Seed our Mi credentials before first boot so Miloco comes up bound.
        seeded = self._seed_credentials()

        state = await self._container_state()
        if state == "running":
            return await self.status()
        if state is not None:
            # Exists but stopped/created — remove so we can recreate cleanly with
            # the current mounts/env.
            await _run([docker, "rm", "-f", CONTAINER_NAME], timeout=30)

        MILOCO_HOME.mkdir(parents=True, exist_ok=True)
        cmd = [
            docker, "run", "-d",
            "--name", CONTAINER_NAME,
            "--network", "host",
            "--restart", "unless-stopped",
            "-e", "MILOCO_HOME=/data/miloco",
            "-e", f"MILOCO_SERVER__PORT={MILOCO_PORT}",
            "-v", f"{MILOCO_HOME}:/data/miloco",
            IMAGE_NAME,
        ]
        r = await _run(cmd, timeout=60)
        if r.returncode != 0:
            raise RuntimeError(f"启动容器失败：{(r.stderr or r.stdout or '').strip()}")
        status = await self.status()
        status["seeded_credentials"] = seeded
        return status

    async def stop(self) -> Dict[str, Any]:
        docker = _docker_bin()
        if docker and await self._container_state() is not None:
            await _run([docker, "stop", CONTAINER_NAME], timeout=30)
        return await self.status()

    async def remove(self) -> Dict[str, Any]:
        docker = _docker_bin()
        if docker and await self._container_state() is not None:
            await _run([docker, "rm", "-f", CONTAINER_NAME], timeout=30)
        return await self.status()

    async def logs(self, tail: int = 200) -> str:
        docker = _docker_bin()
        if not docker or await self._container_state() is None:
            return ""
        r = await _run([docker, "logs", "--tail", str(tail), CONTAINER_NAME], timeout=20)
        return (r.stdout or "") + (r.stderr or "")

    # ------------------------------------------------------------- health
    async def _health(self) -> bool:
        try:
            async with httpx.AsyncClient(timeout=3.0) as c:
                r = await c.get(f"{MILOCO_URL}/health")
                return r.status_code < 400
        except Exception:  # pylint: disable=broad-except
            return False

    async def status(self) -> Dict[str, Any]:
        docker = _docker_bin()
        container_state = await self._container_state() if docker else None
        healthy = await self._health() if container_state == "running" else False
        token = self.server_token()
        preview_url = MILOCO_URL + (f"/?token={token}" if token else "/")
        return {
            "docker_available": await self.docker_available(),
            "image_exists": await self.image_exists() if docker else False,
            "image_name": IMAGE_NAME,
            "container_state": container_state,
            "running": container_state == "running",
            "healthy": healthy,
            "url": MILOCO_URL,
            "preview_url": preview_url,
            "has_server_token": bool(token),
            "has_credentials": self.has_credentials(),
            "miloco_home": str(MILOCO_HOME),
        }
