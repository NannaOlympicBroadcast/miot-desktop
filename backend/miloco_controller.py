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
import re
import secrets
import shutil
import sqlite3
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

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

# ---------------------------------------------------------------------------
# Docker build mirrors — presets the "构建镜像" UI can pick from (app.js),
# resolved server-side so the client only ever sends a preset name or a raw
# custom URL/host/prefix. "default" leaves the corresponding Dockerfile ARG
# at its upstream default. Mirrors the same preset set ssr-minimal's own
# scripts/docker_build.py documents (not guaranteed to stay up forever —
# just common, currently-working options for restricted/CN networks).
# ---------------------------------------------------------------------------
PIP_MIRRORS = {
    "default": "https://pypi.org/simple",
    "tsinghua": "https://pypi.tuna.tsinghua.edu.cn/simple",
    "aliyun": "https://mirrors.aliyun.com/pypi/simple",
    "ustc": "https://pypi.mirrors.ustc.edu.cn/simple",
    "tencent": "https://mirrors.cloud.tencent.com/pypi/simple",
}
APT_MIRRORS = {
    "default": "",  # leave apt's built-in sources alone
    "aliyun": "mirrors.aliyun.com",
    "tsinghua": "mirrors.tuna.tsinghua.edu.cn",
    "ustc": "mirrors.ustc.edu.cn",
    "tencent": "mirrors.cloud.tencent.com",
}
REGISTRY_MIRRORS = {
    "default": "",  # pull the base image from docker.io directly
    "daocloud": "docker.m.daocloud.io",
}
_BASE_IMAGE = "python:3.11-slim"


def _resolve_mirror(value: str, presets: Dict[str, str]) -> str:
    return presets.get(value, value)


# ---------------------------------------------------------------------------
# `docker build --progress=plain` step parser — turns the `#N <event>` line
# stream into structured per-step state (id/title/status/duration/error) so
# the UI can render a live step list instead of a raw scrolling log. Mirrors
# the parser in ssr-minimal's scripts/docker_build.py (duplicated rather than
# imported: miot-desktop doesn't depend on that repo's dev-tooling package).
# ---------------------------------------------------------------------------
_STEP_RE = re.compile(r"^#(\d+) (.*)$")
_STEP_DONE_RE = re.compile(r"^DONE (\d+(?:\.\d+)?)s$")


def _parse_build_line(steps: Dict[str, Dict[str, Any]], order: List[str], line: str) -> None:
    m = _STEP_RE.match(line)
    if not m:
        return
    step_id, rest = m.group(1), m.group(2)
    step = steps.get(step_id)
    if step is None:
        step = {"id": step_id, "title": "", "status": "running", "duration": None, "error": None}
        steps[step_id] = step
        order.append(step_id)
    if rest == "CACHED":
        step["status"] = "cached"
    else:
        dm = _STEP_DONE_RE.match(rest)
        if dm is not None:
            step["status"] = "done"
            step["duration"] = float(dm.group(1))
        elif rest.startswith("ERROR"):
            step["status"] = "error"
            step["error"] = rest[len("ERROR"):].strip(": ")
        elif rest.startswith("[") and not step["title"]:
            step["title"] = rest


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
        self._build_steps: Dict[str, Dict[str, Any]] = {}
        self._build_order: List[str] = []
        # `_build_base_seq` is how many lines have already been dropped off the
        # front of `_build_log`, so a line's absolute sequence number stays
        # stable across trimming and clients can resume with ?since= instead of
        # re-fetching (and re-rendering) the whole tail every poll.
        self._build_base_seq = 0
        self._build_started_at: Optional[float] = None
        self._build_finished_at: Optional[float] = None
        self._build_exit_code: Optional[int] = None
        self._build_lock = threading.Lock()
        # Why the last `start()` failed, when docker accepted `run -d` but the
        # container died immediately (bad image, port clash, mount denied…).
        self._start_error: Optional[str] = None

    # ------------------------------------------------------------ build log io
    def _build_log_path(self) -> Path:
        return MILOCO_HOME / "build.log"

    def _append_build_line(self, line: str, sink: Optional[Any] = None) -> None:
        """Record one build output line. Called from the reader thread."""
        with self._build_lock:
            self._build_log.append(line)
            # Keep memory bounded but never renumber: dropping N lines advances
            # the base sequence by exactly N.
            if len(self._build_log) > 4000:
                del self._build_log[:2000]
                self._build_base_seq += 2000
            _parse_build_line(self._build_steps, self._build_order, line)
        if sink is not None:
            try:
                sink.write(line + "\n")
                sink.flush()
            except OSError:
                pass

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

    def _seed_agent_webhook(self) -> bool:
        """Point Miloco's own ``agent.webhook_url`` / ``agent.auth_bearer`` at
        this backend's embedded SSR agent, so Miloco's perception/rule engine
        can drive it directly instead of needing a separate OpenClaw install.

        ``ssr.integrations.miloco.start_bus_bridge`` already listens on
        Miloco's own default agent-webhook port (``18789``) with no config
        needed for the common single-host case; this makes the pairing
        explicit and shares a real bearer token between the two sides.
        Best-effort and order-sensitive: the token only takes effect for a
        webhook server (re)bound *after* this runs (a restart, not a
        hot-reload) — harmless either way since a fresh install's token
        starts unset (open) until the first successful pairing.
        """
        config_path = MILOCO_HOME / "config.json"
        try:
            data = json.loads(config_path.read_text("utf-8")) if config_path.exists() else {}
            if not isinstance(data, dict):
                data = {}
        except (OSError, json.JSONDecodeError):
            data = {}
        agent_cfg = data.get("agent") if isinstance(data.get("agent"), dict) else {}
        token = agent_cfg.get("auth_bearer") or secrets.token_hex(16)
        port = os.environ.get("MILOCO_AGENT_WEBHOOK_PORT", "18789")
        agent_cfg["webhook_url"] = f"http://127.0.0.1:{port}/miloco/webhook"
        agent_cfg["auth_bearer"] = token
        data["agent"] = agent_cfg
        try:
            MILOCO_HOME.mkdir(parents=True, exist_ok=True)
            config_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), "utf-8")
        except OSError as e:
            _LOGGER.warning("failed to seed agent webhook config: %s", e)
            return False
        os.environ["MILOCO_AGENT_AUTH_BEARER"] = token
        os.environ.setdefault("MILOCO_AGENT_WEBHOOK_PORT", port)
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
    async def build_image(
        self, pip_mirror: str = "default", apt_mirror: str = "default",
        registry_mirror: str = "default",
    ) -> None:
        """Build the Miloco image from the bundled Dockerfile (idempotent).

        Args:
            pip_mirror: a key into PIP_MIRRORS, or any raw index URL.
            apt_mirror: a key into APT_MIRRORS, or any raw hostname.
            registry_mirror: a key into REGISTRY_MIRRORS, or any raw
                registry prefix to pull the base image through.
        """
        if self._build_task is not None and not self._build_task.done():
            return
        with self._build_lock:
            self._build_log = []
            self._build_error = None
            self._build_steps = {}
            self._build_order = []
            self._build_base_seq = 0
            self._build_started_at = time.time()
            self._build_finished_at = None
            self._build_exit_code = None
        self._build_task = asyncio.create_task(
            self._build_image_impl(pip_mirror, apt_mirror, registry_mirror))

    async def _build_image_impl(self, pip_mirror: str, apt_mirror: str, registry_mirror: str) -> None:
        docker = _docker_bin()
        if not docker:
            self._fail_build("未检测到 docker，请先安装 Docker。")
            return
        dockerfile = _DOCKERFILE_DIR / "Dockerfile"
        if not dockerfile.exists():
            self._fail_build(f"未找到 Dockerfile：{dockerfile}")
            return

        registry_prefix = _resolve_mirror(registry_mirror, REGISTRY_MIRRORS)
        base_image = f"{registry_prefix}/library/{_BASE_IMAGE}" if registry_prefix else _BASE_IMAGE
        cmd = [
            docker, "build", "--progress=plain",
            "-t", IMAGE_NAME, "-f", str(dockerfile),
            "--build-arg", f"BASE_IMAGE={base_image}",
            "--build-arg", f"PIP_INDEX_URL={_resolve_mirror(pip_mirror, PIP_MIRRORS)}",
            "--build-arg", f"APT_MIRROR={_resolve_mirror(apt_mirror, APT_MIRRORS)}",
            str(_DOCKERFILE_DIR),
        ]
        self._append_build_line("$ " + " ".join(cmd))

        def _call() -> int:
            env = {**os.environ, "DOCKER_BUILDKIT": "1"}
            # Read raw bytes and decode as UTF-8 ourselves. `text=True` would
            # decode with the *locale* codec, which on a zh-CN Windows box is
            # cp936 — and a single byte BuildKit emits that's illegal in GBK
            # (0x80/0xa0/0xff all are) raises UnicodeDecodeError mid-iteration,
            # killing the reader and freezing the log with no output at all.
            proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env)
            assert proc.stdout is not None
            try:
                MILOCO_HOME.mkdir(parents=True, exist_ok=True)
                sink = open(self._build_log_path(), "w", encoding="utf-8", errors="replace")
            except OSError:
                sink = None
            try:
                for raw in proc.stdout:
                    self._append_build_line(
                        raw.decode("utf-8", "replace").rstrip("\r\n"), sink)
            finally:
                if sink is not None:
                    sink.close()
            return proc.wait()

        try:
            code = await asyncio.get_event_loop().run_in_executor(None, _call)
        except Exception as e:  # pylint: disable=broad-except
            self._fail_build(str(e))
            return
        with self._build_lock:
            self._build_exit_code = code
            self._build_finished_at = time.time()
        if code != 0:
            self._build_error = f"镜像构建失败（exit {code}）。请查看构建日志。"
            self._append_build_line(f"$ 构建失败，docker build 退出码 {code}")
        elif not await self.image_exists():
            # docker build reported success but the tag isn't there — treat it
            # as a failure rather than sending the user to a "启动" button that
            # will immediately reject them.
            self._build_error = f"构建进程返回成功，但镜像 {IMAGE_NAME} 不存在。请查看构建日志。"
        else:
            self._append_build_line(f"$ 构建成功，镜像 {IMAGE_NAME} 已就绪")

    def _fail_build(self, message: str) -> None:
        """Record a build failure *in the log too*, so the UI's log pane always
        explains an empty/short build instead of just going quiet."""
        self._build_error = message
        with self._build_lock:
            self._build_finished_at = time.time()
        self._append_build_line("$ " + message)

    def build_status(self, since: int = 0) -> Dict[str, Any]:
        """Snapshot of the build for the UI.

        Args:
            since: absolute sequence number the client already has. Only lines
                at or after it are returned, so the client appends instead of
                repainting (and never silently loses lines that scrolled past a
                fixed-size tail between two polls).
        """
        running = self._build_task is not None and not self._build_task.done()
        with self._build_lock:
            base, total = self._build_base_seq, self._build_base_seq + len(self._build_log)
            start = max(0, min(int(since), total) - base)
            lines = self._build_log[start:]
            steps = [dict(self._build_steps[i]) for i in self._build_order
                     if i in self._build_steps]
            started, finished = self._build_started_at, self._build_finished_at
            exit_code = self._build_exit_code
        return {
            "building": running,
            "error": self._build_error,
            # Absolute sequence of the first returned line, so a client that
            # fell behind the trim window can tell it lost some.
            "first_seq": base + start,
            "next_seq": total,
            "lines": lines,
            # Kept for older clients that render the whole tail each poll.
            "log_tail": self._build_log[-200:],
            "steps": steps,
            "started_at": started,
            "finished_at": finished,
            "exit_code": exit_code,
            "elapsed": (finished or time.time()) - started if started else None,
            "log_path": str(self._build_log_path()),
            "image_name": IMAGE_NAME,
        }

    # ------------------------------------------------------------ run / stop
    async def start(self) -> Dict[str, Any]:
        docker = _docker_bin()
        if not docker:
            raise RuntimeError("未检测到 docker，请先安装 Docker 并确保其正在运行。")
        if not await self.image_exists():
            raise RuntimeError(f"镜像 {IMAGE_NAME} 不存在，请先点击「构建镜像」。")

        # Seed our Mi credentials before first boot so Miloco comes up bound,
        # and point Miloco's agent webhook at this backend's embedded SSR
        # agent so perception/rule automation reaches it directly.
        seeded = self._seed_credentials()
        self._seed_agent_webhook()

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
        self._start_error = None
        r = await _run(cmd, timeout=60)
        if r.returncode != 0:
            self._start_error = (r.stderr or r.stdout or "").strip()
            raise RuntimeError(f"启动容器失败：{self._start_error}")

        # `docker run -d` returns as soon as the container is created — a crash
        # a second later (missing entrypoint, port already bound, mount denied)
        # would otherwise surface as a silent "未运行". Give it a moment and
        # report the real reason from the container's own logs.
        state, exit_code = await self._settle(timeout=3.0)
        if state is not None and state != "running":
            tail = (await self.logs(tail=40)).strip()
            self._start_error = tail or f"容器启动后未能保持运行（state={state}）"
            code = f"exit {exit_code}" if exit_code is not None else f"state={state}"
            raise RuntimeError(f"容器启动后立即退出（{code}）。容器日志：\n{self._start_error}")

        status = await self.status()
        status["seeded_credentials"] = seeded
        return status

    # States meaning "not up": a plain exit, or — because the container runs
    # with `--restart unless-stopped` — a crash loop, which docker reports as
    # `restarting` rather than ever settling on `exited`.
    _DEAD_STATES = ("exited", "dead", "restarting")

    async def _settle(self, timeout: float = 3.0) -> Tuple[Optional[str], Optional[int]]:
        """Poll the container's state briefly after start; return (state, exit
        code). Long enough to catch an immediate crash (bad entrypoint, port
        clash, denied mount all fail within a second) without stalling the
        happy path — a later crash is caught by the periodic status poll."""
        docker = _docker_bin()
        deadline = time.time() + timeout
        state = await self._container_state()
        while time.time() < deadline:
            state = await self._container_state()
            if state in self._DEAD_STATES:
                break
            await asyncio.sleep(0.4)
        code = None
        if docker and state in self._DEAD_STATES:
            r = await _run([docker, "inspect", "-f", "{{.State.ExitCode}}", CONTAINER_NAME], timeout=10)
            if r.returncode == 0:
                try:
                    code = int((r.stdout or "").strip())
                except ValueError:
                    code = None
        return state, code

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
        if not docker:
            return "未检测到 docker，无法读取容器日志。"
        if await self._container_state() is None:
            return f"容器 {CONTAINER_NAME} 尚未创建（还没有启动过 Miloco）。"
        r = await _run([docker, "logs", "--tail", str(tail), CONTAINER_NAME], timeout=20)
        out = (r.stdout or "") + (r.stderr or "")
        if r.returncode != 0 and not out.strip():
            return f"读取容器日志失败（exit {r.returncode}）。"
        return out if out.strip() else "容器已创建但尚未输出任何日志。"

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
        exit_code = None
        if docker and container_state in self._DEAD_STATES:
            r = await _run([docker, "inspect", "-f", "{{.State.ExitCode}}", CONTAINER_NAME], timeout=10)
            if r.returncode == 0:
                try:
                    exit_code = int((r.stdout or "").strip())
                except ValueError:
                    exit_code = None
        return {
            "docker_available": await self.docker_available(),
            "image_exists": await self.image_exists() if docker else False,
            "image_name": IMAGE_NAME,
            "container_state": container_state,
            "running": container_state == "running",
            "healthy": healthy,
            "exit_code": exit_code,
            "start_error": self._start_error,
            "url": MILOCO_URL,
            "preview_url": preview_url,
            "has_server_token": bool(token),
            "has_credentials": self.has_credentials(),
            "miloco_home": str(MILOCO_HOME),
        }
