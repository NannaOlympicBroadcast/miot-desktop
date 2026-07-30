# -*- coding: utf-8 -*-
"""Miloco (Xiaomi 感知家庭网关) bare-metal controller.

Lets the desktop GUI install / launch / stop the official
[Xiaomi Miloco](https://github.com/XiaoMi/xiaomi-miloco) service **directly on
this Windows machine — no Docker, no WSL** — and preview its web dashboard,
**auto-injecting the Mi account credentials this app already holds** so the user
never re-binds.

Why bare metal is possible now (this replaced a Docker deployment)
------------------------------------------------------------------
Upstream refuses a native Windows install — ``scripts/install.ps1`` exits with
"Please install Miloco inside WSL", and ``scripts/install.py`` explains why:
``miloco-miot`` publishes no Windows wheel, because the camera P2P native
library shipped inside it (``miot/libs/<os>/<arch>/``) is only built for
linux/darwin. That is not a cosmetic gap: ``MIoTClient.init_async()``
constructs ``MIoTCamera``, whose ``__init__`` unconditionally CDLLs that
library, so a Windows install used to die during client init — which is exactly
why this app ran Miloco in a Linux container.

This repo now vendors a self-built ``miot_camera_lite.dll``
(``vendor/miot_kit/miot/libs/windows/x86_64/`` — see its NOTICE.md), so the
install below drops it into Miloco's own bundled ``miot`` source tree *before*
pip-installing it, and the native Windows run works.

What ``install()`` does (it replaces docker/miloco/Dockerfile step for step)
---------------------------------------------------------------------------
1. probe the host toolchain: Python >= 3.11 (required), git (required),
   Node + pnpm (only needed for the dashboard).
2. fetch the upstream repo into ``$MILOCO_ROOT/src``. Uses ``git fetch <ref>
   --depth 1`` rather than ``clone --branch`` so a branch, a tag *or* a full
   commit SHA all work — same as the Dockerfile did.
3. build the React/Vite dashboard (``web/``) and copy it into
   ``backend/miloco/src/miloco/static``, where ``miloco.main:spa_handler``
   serves it from (``settings.directories.static_dir``). Skipping this is what
   makes ``GET /`` return 404: the backend's SPA route 404s by design when
   ``static_dir/index.html`` is missing, leaving a working API and no dashboard.
4. copy the Windows camera DLL into ``backend/miot/src/miot/libs/windows/…`` so
   the wheel built in step 6 contains it.
5. copy the perception ONNX models into ``$MILOCO_HOME/models``. They live in
   the repo but are excluded from the wheel (``[tool.hatch.build] exclude``),
   and ``settings.directories.models_dir`` resolves to ``$MILOCO_HOME/models``,
   so without this step every perception feature fails at model load.
6. create ``$MILOCO_ROOT/venv`` and pip-install ``backend/miot`` then
   ``backend/miloco`` into it (same order and same two packages as the image).
7. drop a ``fcntl`` shim into the venv: ``miloco.home_profile.store`` does
   ``import fcntl`` at module scope and ``miloco.main`` imports that module, so
   on Windows the backend cannot even boot without it (see ``_FCNTL_SHIM``).
8. smoke-check the venv: both packages import and ``miloco-backend`` exists.

``start()`` then spawns ``$MILOCO_ROOT/venv/Scripts/miloco-backend.exe`` with
``MILOCO_HOME`` pointed at our state dir, stdout/stderr captured to
``$MILOCO_HOME/backend.log``, and the pid recorded in
``$MILOCO_ROOT/miloco.pid`` — so relaunching this app re-adopts a Miloco that
is still running instead of starting a second one on the same port.

Unlike ``docker run --restart unless-stopped``, a crashed process is **not**
restarted automatically; ``status()`` reports the exit code and ``logs()`` the
tail, and the user restarts from the UI. (No hidden retry loop that would
mask a boot failure as "starting…" forever.)

How the credential injection works (unchanged from the Docker deployment)
------------------------------------------------------------------------
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
"""
import asyncio
import json
import logging
import os
import platform
import secrets
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import httpx

_LOGGER = logging.getLogger("miot_desktop.miloco")

IS_WINDOWS = os.name == "nt"

# ---------------------------------------------------------------------------
# Configuration (overridable via env)
# ---------------------------------------------------------------------------
MILOCO_PORT = int(os.environ.get("MILOCO_PORT", "1810"))
MILOCO_URL = f"http://127.0.0.1:{MILOCO_PORT}"
CACHE_PATH = os.environ.get("MIOT_CACHE_PATH", os.path.join(os.path.expanduser("~"), ".miot_cache"))
# Miloco's own state root ($MILOCO_HOME): config.json, miloco.db, logs, models.
# Lives next to the miot cache so all Mi-account state sits together.
MILOCO_HOME = Path(os.environ.get("MILOCO_HOME_HOST", os.path.join(CACHE_PATH, "miloco_home")))
# Where Miloco itself is installed: the upstream checkout plus the venv we run
# it from. Deliberately *outside* $MILOCO_HOME so wiping state never breaks the
# install, and reinstalling never wipes state.
MILOCO_ROOT = Path(os.environ.get("MILOCO_ROOT_HOST", os.path.join(CACHE_PATH, "miloco_runtime")))
SRC_DIR = MILOCO_ROOT / "src"
VENV_DIR = MILOCO_ROOT / "venv"
INSTALL_INFO_PATH = MILOCO_ROOT / "install.json"
PID_PATH = MILOCO_ROOT / "miloco.pid"
# Captured stdout/stderr of the backend process. Miloco writes its own logs
# under $MILOCO_HOME/logs; this file additionally catches everything that dies
# before logging is up (import errors, port clashes, tracebacks).
BACKEND_LOG_PATH = MILOCO_HOME / "backend.log"

MILOCO_REPO = os.environ.get("MILOCO_REPO", "https://github.com/XiaoMi/xiaomi-miloco.git")
MILOCO_REF = os.environ.get("MILOCO_REF", "main")

MIOT_TOKEN_INFO_KEY = "MIOT_TOKEN_INFO_KEY"

# Minimum interpreter Miloco's own pyproject requires (requires-python>=3.11).
MIN_PYTHON = (3, 11)

# ---------------------------------------------------------------------------
# Install mirrors — presets the "安装 Miloco" UI can pick from (app.js),
# resolved server-side so the client only ever sends a preset name or a raw
# custom URL. "default" leaves the corresponding tool at its upstream default.
# Only two remain relevant on bare metal: pip (Python deps) and npm (the
# dashboard build). The old apt/docker-registry mirrors existed solely for the
# container image and went away with it.
# ---------------------------------------------------------------------------
PIP_MIRRORS = {
    "default": "https://pypi.org/simple",
    "tsinghua": "https://pypi.tuna.tsinghua.edu.cn/simple",
    "aliyun": "https://mirrors.aliyun.com/pypi/simple",
    "ustc": "https://pypi.mirrors.ustc.edu.cn/simple",
    "tencent": "https://mirrors.cloud.tencent.com/pypi/simple",
}
NPM_MIRRORS = {
    "default": "",  # leave pnpm's default registry.npmjs.org alone
    "npmmirror": "https://registry.npmmirror.com",
    "tencent": "https://mirrors.cloud.tencent.com/npm/",
    "huawei": "https://mirrors.huaweicloud.com/repository/npm/",
}


def _resolve_mirror(value: str, presets: Dict[str, str]) -> str:
    return presets.get(value, value)


# ---------------------------------------------------------------------------
# The Windows compatibility layer written into the venv.
#
# Upstream supports Linux/macOS only (its installer refuses Windows outright),
# so a handful of POSIX-only stdlib pieces are used unconditionally *at import
# time* — the backend dies before it can log anything. Two mechanisms fix that
# without touching upstream source (which every update would overwrite):
#
#   1. `fcntl.py` — a module Windows doesn't have at all (see below).
#   2. `miot_desktop_win_compat.py` + a `.pth` that imports it, so it runs at
#      interpreter startup and can fill in missing `os` attributes before any
#      miloco module is imported (see _WIN_COMPAT_SRC).
#
# Both are rewritten on every install *and* every start, so they can't drift and
# don't need a reinstall to arrive.
# ---------------------------------------------------------------------------
_WIN_COMPAT_MODULE = "miot_desktop_win_compat"
_WIN_COMPAT_PTH = "zz-miot-desktop-win-compat.pth"

_WIN_COMPAT_SRC = '''"""Windows compatibility shims for Miloco — installed by MIoT Desktop.

Loaded at interpreter startup by ``zz-miot-desktop-win-compat.pth`` (site.py
executes the ``import`` line of every .pth in site-packages). That timing is the
whole point: the gaps filled here are hit while *importing* miloco, so nothing
inside miloco could apply them.

Upstream Miloco targets Linux/macOS only, so it reaches for POSIX-only stdlib
APIs unconditionally. Each shim fills exactly one such gap with a truthful
Windows answer — nothing is faked into "success" in a way that would hide a real
failure.

Not meant to be imported by name. Safe to delete: MIoT Desktop rewrites it on
every install and every start.
"""
import os


def _install_sysconf():
    """``os.sysconf`` for the names Miloco actually asks for.

    ``miloco.node_monitor.vmmap`` evaluates ``os.sysconf("SC_PAGESIZE")`` at
    module scope, and ``miloco.node_monitor.resource_monitor`` imports that
    module unconditionally even though it is macOS-only code (the platform
    dispatch happens later, at call time). Without this the whole import chain
    from ``miloco.main`` dies with:

        AttributeError: module 'os' has no attribute 'sysconf'

    Unknown names raise ValueError, exactly as CPython does for an unrecognised
    name — so a caller that probes ``hasattr(os, "sysconf")`` expecting a real
    number (page size, CPU count) gets a correct one instead of a fabricated
    value, and anything else fails loudly rather than silently wrong.
    """
    if hasattr(os, "sysconf"):
        return
    import mmap

    values = {
        # mmap.PAGESIZE comes from GetSystemInfo().dwPageSize — the real thing.
        "SC_PAGESIZE": lambda: mmap.PAGESIZE,
        "SC_PAGE_SIZE": lambda: mmap.PAGESIZE,
        "SC_NPROCESSORS_ONLN": lambda: os.cpu_count() or 1,
        "SC_NPROCESSORS_CONF": lambda: os.cpu_count() or 1,
    }

    def sysconf(name):
        getter = values.get(name) if isinstance(name, str) else None
        if getter is None:
            raise ValueError("unrecognized configuration name")
        return int(getter())

    os.sysconf = sysconf
    os.sysconf_names = {name: index for index, name in enumerate(sorted(values))}


_install_sysconf()
'''

# ---------------------------------------------------------------------------
# The `fcntl` shim written into the venv on Windows.
#
# `miloco.home_profile.store` imports fcntl at module scope for its
# flock()-based cross-process lock, and `miloco.main` imports that module while
# building the FastAPI app — so on Windows (no stdlib fcntl) the backend fails
# at import, before it can log anything. Rather than patching upstream source
# (which every reinstall would undo), we add a module named `fcntl` to the
# venv's site-packages; it shadows nothing, because Windows has no real one.
#
# flock() maps onto msvcrt.locking(), a genuine mandatory byte-range lock on
# the same fd, so the store's read-modify-write sections stay serialised across
# processes (CLI + backend both write home-profile). Only the operations that
# module actually uses (LOCK_EX / LOCK_UN) are meaningful.
# ---------------------------------------------------------------------------
_FCNTL_SHIM = '''"""Windows `fcntl` stand-in — installed by MIoT Desktop, not by Miloco.

Miloco imports `fcntl` for one thing: the flock()-based cross-process lock in
`miloco.home_profile.store`, which `miloco.main` pulls in at import time. This
module supplies just enough of the POSIX API for that to work on Windows, by
mapping flock() onto msvcrt.locking() byte-range locks on the same fd.

Do not import this deliberately; it exists so unmodified upstream code runs.
"""
import msvcrt
import os
import time

LOCK_SH = 1
LOCK_EX = 2
LOCK_NB = 4
LOCK_UN = 8

# fcntl.fcntl() command constants, present so an attribute lookup elsewhere
# fails loudly at call time rather than at import time.
F_GETFD = 1
F_SETFD = 2
F_GETFL = 3
F_SETFL = 4
FD_CLOEXEC = 1

# One byte at offset 0 is enough: every locker in this process tree uses the
# same range, which is all a whole-file advisory lock needs. Locking past EOF
# is legal on Windows, so the (always empty) .lock file works unchanged.
_RANGE = 1
# msvcrt's blocking mode gives up after ~10s; loop so LOCK_EX really blocks,
# but not forever — a lock wedged by a crashed holder must not hang the backend.
_MAX_WAIT = 60.0


def flock(fd, operation):
    """POSIX flock() over msvcrt byte-range locks.

    LOCK_SH is treated as exclusive: Windows byte-range locks through msvcrt
    have no shared mode. That is stricter than POSIX, never less safe.
    """
    blocking = not operation & LOCK_NB
    op = operation & ~LOCK_NB
    pos = os.lseek(fd, 0, os.SEEK_CUR)
    os.lseek(fd, 0, os.SEEK_SET)
    try:
        if op == LOCK_UN:
            try:
                msvcrt.locking(fd, msvcrt.LK_UNLCK, _RANGE)
            except OSError:
                pass  # not held (double-unlock) — same no-op POSIX gives
            return
        mode = msvcrt.LK_LOCK if blocking else msvcrt.LK_NBLCK
        deadline = time.monotonic() + _MAX_WAIT
        while True:
            try:
                msvcrt.locking(fd, mode, _RANGE)
                return
            except OSError:
                if not blocking:
                    raise
                if time.monotonic() >= deadline:
                    # Proceed unlocked rather than deadlock the caller; the
                    # writes it guards are atomic (temp file + os.replace),
                    # so the worst case is a lost concurrent update.
                    return
    finally:
        os.lseek(fd, pos, os.SEEK_SET)


def lockf(fd, operation, length=0, start=0, whence=0):
    """Only the flock()-equivalent whole-file form is supported."""
    return flock(fd, operation)


def fcntl(fd, cmd, arg=0):
    raise OSError("fcntl.fcntl() is not available on Windows")


def ioctl(fd, request, arg=0, mutate_flag=True):
    raise OSError("fcntl.ioctl() is not available on Windows")
'''


def _now_ms() -> int:
    return int(time.time() * 1000)


def _no_window() -> int:
    """Creation flag that keeps helper processes from flashing a console window.

    The desktop app is windowed; without this every git/pip/pnpm call would pop
    a black console on screen.
    """
    return getattr(subprocess, "CREATE_NO_WINDOW", 0) if IS_WINDOWS else 0


def _exe(name: str) -> str:
    return f"{name}.exe" if IS_WINDOWS else name


def _venv_bin(name: str) -> Path:
    return VENV_DIR / ("Scripts" if IS_WINDOWS else "bin") / _exe(name)


def _venv_python() -> Path:
    return _venv_bin("python")


def _backend_exe() -> Path:
    return _venv_bin("miloco-backend")


def _site_packages() -> Optional[Path]:
    """The venv's site-packages, without paying for a subprocess.

    Layout is fixed per platform (``Lib\\site-packages`` on Windows,
    ``lib/pythonX.Y/site-packages`` elsewhere), and this is called on every
    start — running the interpreter just to ask sysconfig would be wasteful.
    """
    if IS_WINDOWS:
        p = VENV_DIR / "Lib" / "site-packages"
        return p if p.is_dir() else None
    for p in sorted((VENV_DIR / "lib").glob("python3.*/site-packages")):
        if p.is_dir():
            return p
    return None


def _arch_dir() -> Optional[str]:
    machine = platform.machine().lower()
    if machine in ("x86_64", "amd64"):
        return "x86_64"
    if machine in ("arm64", "aarch64"):
        return "arm64"
    return None


def _camera_dll() -> Optional[Path]:
    """The vendored Windows camera P2P library this app ships.

    Resolved from the *imported* ``miot`` package so it works both in dev
    (vendor/miot_kit) and in the frozen build (sys._MEIPASS/miot/libs/...) —
    the same derivation ``server.py`` uses to decide whether live video works.
    """
    override = os.environ.get("MIOT_CAMERA_DLL")
    if override:
        p = Path(override)
        return p if p.exists() else None
    arch = _arch_dir()
    if not arch:
        return None
    try:
        import miot as _miot_pkg
        base = Path(_miot_pkg.__file__).parent / "libs"
    except Exception:  # pylint: disable=broad-except
        base = Path(os.path.dirname(os.path.abspath(__file__))).parent / "vendor" / "miot_kit" / "miot" / "libs"
    p = base / "windows" / arch / "miot_camera_lite.dll"
    return p if p.exists() else None


def _child_path() -> str:
    """PATH for the Miloco process: its own venv first, our bundle never.

    When this backend is frozen, PyInstaller's bundle directory can end up on
    PATH — and it ships its own python3xx.dll / OpenSSL / zlib. Windows
    resolves a child's DLLs partly through PATH, so leaving it there invites a
    venv interpreter loading our bundled runtime's libraries.
    """
    meipass = getattr(sys, "_MEIPASS", None)
    entries = [str(_venv_python().parent)]
    for part in os.environ.get("PATH", "").split(os.pathsep):
        if not part:
            continue
        if meipass and os.path.normcase(os.path.normpath(part)).startswith(
                os.path.normcase(os.path.normpath(meipass))):
            continue
        entries.append(part)
    return os.pathsep.join(entries)


async def _run(cmd: Sequence[str], timeout: float = 30.0, cwd: Optional[Path] = None,
               env: Optional[Dict[str, str]] = None) -> subprocess.CompletedProcess:
    """Run a command off the event loop, capturing stdout/stderr."""
    def _call() -> subprocess.CompletedProcess:
        return subprocess.run(
            list(cmd), capture_output=True, text=True, timeout=timeout,
            cwd=str(cwd) if cwd else None, env=env, creationflags=_no_window())
    return await asyncio.get_event_loop().run_in_executor(None, _call)


def _kill_tree(pid: int, timeout: float = 8.0) -> None:
    """Terminate a pid and its children, escalating to a hard kill."""
    try:
        import psutil
    except ImportError:  # pragma: no cover — psutil is a hard requirement
        psutil = None  # type: ignore
    if psutil is None:
        if IS_WINDOWS:
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(pid)],
                           capture_output=True, creationflags=_no_window(), check=False)
        else:
            import signal
            try:
                os.kill(pid, signal.SIGTERM)
            except OSError:
                pass
        return
    try:
        proc = psutil.Process(pid)
    except psutil.Error:
        return
    try:
        victims = proc.children(recursive=True) + [proc]
    except psutil.Error:
        victims = [proc]
    for p in victims:
        try:
            p.terminate()
        except psutil.Error:
            pass
    _, alive = psutil.wait_procs(victims, timeout=timeout)
    for p in alive:
        try:
            p.kill()
        except psutil.Error:
            pass


def _pid_is_miloco(pid: int) -> bool:
    """Is `pid` alive *and* actually our Miloco backend?

    Checked against the venv path / executable name, because a bare
    "does this pid exist" test would adopt whatever unrelated process happened
    to inherit a recycled pid from a stale pidfile.
    """
    try:
        import psutil
    except ImportError:  # pragma: no cover
        return False
    try:
        proc = psutil.Process(pid)
        if not proc.is_running() or proc.status() == getattr(psutil, "STATUS_ZOMBIE", "zombie"):
            return False
        needle = str(VENV_DIR).lower()
        try:
            if needle in (proc.exe() or "").lower():
                return True
        except psutil.Error:
            pass  # AccessDenied on some Windows setups — fall through to cmdline
        try:
            joined = " ".join(proc.cmdline()).lower()
        except psutil.Error:
            return False
        return needle in joined or "miloco" in joined
    except psutil.Error:
        return False


class MilocoController:
    def __init__(self) -> None:
        # ---- install job state
        self._install_task: Optional[asyncio.Task] = None
        self._log: List[str] = []
        # `_log_base_seq` is how many lines have already been dropped off the
        # front of `_log`, so a line's absolute sequence number stays stable
        # across trimming and clients can resume with ?since= instead of
        # re-fetching (and re-rendering) the whole tail every poll.
        self._log_base_seq = 0
        self._error: Optional[str] = None
        self._steps: Dict[str, Dict[str, Any]] = {}
        self._step_order: List[str] = []
        self._started_at: Optional[float] = None
        self._finished_at: Optional[float] = None
        self._exit_code: Optional[int] = None  # exit code of the failing command
        self._lock = threading.Lock()
        self._ref = MILOCO_REF
        # ---- runtime state
        # Why the last `start()` failed, when the process was spawned but died
        # immediately (missing dependency, port clash, bad config…).
        self._start_error: Optional[str] = None
        self._proc: Optional[subprocess.Popen] = None
        self._proc_log = None  # type: Optional[Any]
        self._last_exit_code: Optional[int] = None
        # Toolchain probe cache — status() is polled every few seconds by the
        # UI and probing Python costs a subprocess.
        self._env_cache: Optional[Dict[str, Any]] = None
        self._env_cache_at = 0.0
        # Host IANA timezone; "" means "asked and couldn't tell", None means
        # "not asked yet" (see _local_timezone).
        self._tz_cache: Optional[str] = None

    # ------------------------------------------------------------ log io
    def _log_path(self) -> Path:
        return MILOCO_ROOT / "install.log"

    def _append(self, line: str, sink: Optional[Any] = None) -> None:
        """Record one install output line. Called from reader threads too."""
        with self._lock:
            self._log.append(line)
            # Keep memory bounded but never renumber: dropping N lines advances
            # the base sequence by exactly N.
            if len(self._log) > 4000:
                del self._log[:2000]
                self._log_base_seq += 2000
        if sink is not None:
            try:
                sink.write(line + "\n")
                sink.flush()
            except OSError:
                pass

    # ------------------------------------------------------------ steps
    def _step_start(self, step_id: str, title: str) -> None:
        with self._lock:
            if step_id not in self._steps:
                self._step_order.append(step_id)
            self._steps[step_id] = {
                "id": step_id, "title": title, "status": "running",
                "duration": None, "error": None, "_t0": time.time(),
            }
        self._append(f"── {title}")

    def _step_finish(self, step_id: str, status: str, error: Optional[str] = None,
                     note: Optional[str] = None) -> None:
        with self._lock:
            step = self._steps.get(step_id)
            if step is None:
                return
            step["status"] = status
            step["duration"] = round(time.time() - step.pop("_t0", time.time()), 1)
            step["error"] = error
            if note:
                step["title"] = f"{step['title']} — {note}"
        if error:
            self._append(f"✗ {error}")
        elif note:
            self._append(f"· {note}")

    def _step_done(self, step_id: str, note: Optional[str] = None) -> None:
        self._step_finish(step_id, "done", note=note)

    def _step_skip(self, step_id: str, note: str) -> None:
        self._step_finish(step_id, "skipped", note=note)

    def _fail(self, step_id: Optional[str], message: str, exit_code: Optional[int] = None) -> None:
        """Record an install failure *in the log too*, so the UI's log pane
        always explains a short/empty install instead of just going quiet."""
        self._error = message
        with self._lock:
            self._finished_at = time.time()
            self._exit_code = exit_code
        if step_id:
            self._step_finish(step_id, "error", error=message)
        else:
            self._append("✗ " + message)

    # ------------------------------------------------- toolchain probing
    def _probe_python(self, exe: str) -> Optional[Dict[str, Any]]:
        """Return {path, version, version_info} if `exe` is a usable interpreter."""
        try:
            r = subprocess.run(
                [exe, "-c",
                 "import sys,json;print(json.dumps({'p':sys.executable,'v':list(sys.version_info[:3])}))"],
                capture_output=True, text=True, timeout=15, creationflags=_no_window())
        except (OSError, subprocess.SubprocessError):
            return None
        if r.returncode != 0:
            return None
        try:
            data = json.loads((r.stdout or "").strip().splitlines()[-1])
        except (json.JSONDecodeError, IndexError):
            return None
        info = tuple(data.get("v") or ())
        if len(info) < 2 or info[:2] < MIN_PYTHON:
            return None
        return {
            "path": data.get("p") or exe,
            "version": ".".join(str(x) for x in info),
            "version_info": list(info),
        }

    def _find_python(self) -> Optional[Dict[str, Any]]:
        """Locate a host interpreter Miloco can be installed into.

        Note this can never be *our* interpreter in a packaged build: the
        backend is frozen by PyInstaller, so `sys.executable` is
        miot-backend.exe and has no pip. In dev (running under a real python)
        it is a valid last-resort candidate.
        """
        override = os.environ.get("MILOCO_PYTHON")
        if override:
            return self._probe_python(override)
        candidates: List[str] = []
        # Prefer 3.13 → 3.11 through the py launcher: newest-first would land
        # on a just-released 3.x that onnxruntime/scipy have no wheels for yet.
        if shutil.which("py"):
            candidates += ["py -3.13", "py -3.12", "py -3.11"]
        for name in ("python3.13", "python3.12", "python3.11", "python", "python3"):
            if shutil.which(name):
                candidates.append(name)
        if not getattr(sys, "frozen", False):
            candidates.append(sys.executable)
        for cand in candidates:
            # "py -3.12" is the one two-word candidate; probe it as such.
            if cand.startswith("py -"):
                found = self._probe_py_launcher(cand.split(" ", 1)[1])
            else:
                found = self._probe_python(cand)
            if found:
                return found
        return None

    def _probe_py_launcher(self, flag: str) -> Optional[Dict[str, Any]]:
        try:
            r = subprocess.run(["py", flag, "-c", "import sys;print(sys.executable)"],
                               capture_output=True, text=True, timeout=15,
                               creationflags=_no_window())
        except (OSError, subprocess.SubprocessError):
            return None
        if r.returncode != 0:
            return None
        exe = (r.stdout or "").strip().splitlines()[-1] if r.stdout.strip() else ""
        return self._probe_python(exe) if exe else None

    def _pnpm_cmd(self) -> Optional[List[str]]:
        """How to invoke pnpm on this host, or None if we can't.

        Always the *resolved* path from `shutil.which`, never the bare name: on
        Windows both tools are usually `.cmd` shims (`pnpm.cmd`, `npx.cmd`), so
        a hardcoded `.exe` wouldn't exist and a bare name would depend on
        PATHEXT resolution inside CreateProcess.

        `npx --yes pnpm@11` is the fallback because it needs neither a global
        install nor admin rights — unlike `corepack enable`, which writes shims
        into the Node install directory.
        """
        pnpm = shutil.which("pnpm")
        if pnpm:
            return [pnpm]
        npx = shutil.which("npx")
        if npx:
            return [npx, "--yes", "pnpm@11"]
        return None

    def probe_env(self, force: bool = False) -> Dict[str, Any]:
        """Toolchain snapshot, cached because status() polls frequently."""
        if not force and self._env_cache is not None and time.time() - self._env_cache_at < 60:
            return self._env_cache
        py = self._find_python()
        pnpm = self._pnpm_cmd()
        dll = _camera_dll()
        env = {
            "python": py["path"] if py else None,
            "python_version": py["version"] if py else None,
            "git": shutil.which("git"),
            "node": shutil.which("node"),
            # Two forms on purpose: `pnpm` is for display, `pnpm_argv` is what
            # actually gets executed. Joining and re-splitting one string would
            # break on the spaces in "C:\Program Files\nodejs\npx.cmd".
            "pnpm": " ".join(pnpm) if pnpm else None,
            "pnpm_argv": pnpm,
            "camera_dll": str(dll) if dll else None,
            "min_python": ".".join(str(x) for x in MIN_PYTHON),
        }
        self._env_cache, self._env_cache_at = env, time.time()
        return env

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
        config_path = self._config_path()
        data = self._read_config()
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

    # Where Miloco's web/API token has been observed to live in config.json,
    # most specific first. Tried in order so a rename upstream degrades to the
    # next candidate instead of silently yielding an empty token.
    _TOKEN_KEYS: Tuple[Tuple[str, ...], ...] = (
        ("server", "token"),
        ("server", "auth_token"),
        ("server", "access_token"),
        ("server", "auth_bearer"),
        ("web", "token"),
        ("token",),
    )

    def _config_path(self) -> Path:
        return MILOCO_HOME / "config.json"

    def _read_config(self) -> Dict[str, Any]:
        try:
            data = json.loads(self._config_path().read_text("utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            return {}
        return data if isinstance(data, dict) else {}

    def _token_info(self) -> Tuple[str, str]:
        """Miloco's web/API token and the config key it came from.

        Returns ("", "") when no usable token is present. Only str/int values
        are accepted: a dict or None under the expected key previously went
        through ``str()`` and produced a garbage token (``"None"``,
        ``"{'a': 1}"``) that the server then rejected.
        """
        data = self._read_config()
        for path in self._TOKEN_KEYS:
            node: Any = data
            for part in path:
                node = node.get(part) if isinstance(node, dict) else None
                if node is None:
                    break
            if isinstance(node, bool) or not isinstance(node, (str, int)):
                continue
            token = str(node).strip()
            # Guard against placeholders that are technically strings.
            if token and token.lower() not in ("none", "null", "false", "0"):
                return token, ".".join(path)
        return "", ""

    def server_token(self) -> str:
        """Miloco's auto-generated web/API bearer token from its config.json."""
        return self._token_info()[0]

    # ------------------------------------------------------------- install
    def installed(self) -> bool:
        return _backend_exe().exists()

    def install_info(self) -> Dict[str, Any]:
        try:
            data = json.loads(INSTALL_INFO_PATH.read_text("utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            return {}
        return data if isinstance(data, dict) else {}

    async def install(self, pip_mirror: str = "default", npm_mirror: str = "default",
                      ref: Optional[str] = None) -> None:
        """Install (or update) Miloco into $MILOCO_ROOT. Idempotent; safe to
        re-run to pick up a newer upstream commit.

        Args:
            pip_mirror: a key into PIP_MIRRORS, or any raw index URL.
            npm_mirror: a key into NPM_MIRRORS, or any raw registry URL, used
                while building the web dashboard (web/ -> pnpm install).
            ref: upstream branch, tag or commit SHA (default: $MILOCO_REF/main).
        """
        if self._install_task is not None and not self._install_task.done():
            return
        if self._running_pid() is not None:
            raise RuntimeError("Miloco 正在运行，请先点击「停止」再安装/更新（运行中的文件无法覆盖）。")
        with self._lock:
            self._log = []
            self._log_base_seq = 0
            self._error = None
            self._steps = {}
            self._step_order = []
            self._started_at = time.time()
            self._finished_at = None
            self._exit_code = None
        self._ref = (ref or MILOCO_REF).strip() or MILOCO_REF
        self._install_task = asyncio.create_task(self._install_guarded(pip_mirror, npm_mirror))

    async def _install_guarded(self, pip_mirror: str, npm_mirror: str) -> None:
        """Never let an unexpected exception look like a successful install:
        without this the task dies silently and the UI reads `installing: false,
        error: null` as "done"."""
        try:
            await self._install_impl(pip_mirror, npm_mirror)
        except Exception as e:  # pylint: disable=broad-except
            _LOGGER.exception("miloco install crashed")
            running = [i for i in self._step_order
                       if self._steps.get(i, {}).get("status") == "running"]
            self._fail(running[-1] if running else None, f"安装过程异常终止：{e}")

    async def _stream(self, cmd: Sequence[str], cwd: Optional[Path] = None,
                      env: Optional[Dict[str, str]] = None, echo: bool = True) -> int:
        """Run a command, streaming its combined output into the install log."""
        if echo:
            self._append("$ " + " ".join(str(c) for c in cmd))

        def _call() -> int:
            try:
                MILOCO_ROOT.mkdir(parents=True, exist_ok=True)
                sink = open(self._log_path(), "a", encoding="utf-8", errors="replace")
            except OSError:
                sink = None
            try:
                # Read raw bytes and decode as UTF-8 ourselves. `text=True`
                # would decode with the *locale* codec, which on a zh-CN
                # Windows box is cp936 — and a single byte a tool emits that's
                # illegal in GBK (0x80/0xa0/0xff all are) raises
                # UnicodeDecodeError mid-iteration, killing the reader and
                # freezing the log with no output at all.
                proc = subprocess.Popen(
                    [str(c) for c in cmd], stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT, cwd=str(cwd) if cwd else None,
                    env=env, creationflags=_no_window())
                assert proc.stdout is not None
                for raw in proc.stdout:
                    self._append(raw.decode("utf-8", "replace").rstrip("\r\n"), sink)
                return proc.wait()
            except OSError as e:
                self._append(f"! {e}", sink)
                return -1
            finally:
                if sink is not None:
                    sink.close()

        return await asyncio.get_event_loop().run_in_executor(None, _call)

    async def _install_impl(self, pip_mirror: str, npm_mirror: str) -> None:  # noqa: C901
        MILOCO_ROOT.mkdir(parents=True, exist_ok=True)
        # Fresh log file per install (the in-memory ring buffer was reset in
        # install(); keep the two consistent).
        try:
            self._log_path().write_text("", "utf-8")
        except OSError:
            pass

        # -- 1/8 toolchain ---------------------------------------------------
        self._step_start("1", "[1/8] 检测本机环境")
        env_info = self.probe_env(force=True)
        for k, v in env_info.items():
            if k == "pnpm_argv":
                continue  # the "pnpm" key above already shows this, readably
            self._append(f"  {k}: {v or '(未找到)'}")
        if not env_info["python"]:
            self._fail("1", f"未找到可用的 Python（需要 >= {env_info['min_python']}）。"
                            "请安装 Python 后重试，或用环境变量 MILOCO_PYTHON 指定解释器路径。")
            return
        if not env_info["git"]:
            self._fail("1", "未找到 git。Miloco 需要从官方仓库获取源码，请先安装 Git"
                            "（https://git-scm.com/download/win）后重试。")
            return
        if IS_WINDOWS and not env_info["camera_dll"]:
            # Without it Miloco dies in MIoTClient.init_async → nothing works.
            self._fail("1", "缺少 Windows 摄像头原生库 miot_camera_lite.dll，"
                            "Miloco 在 Windows 上无法完成客户端初始化。"
                            "请确认安装包完整，或用 MIOT_CAMERA_DLL 指定该 DLL 路径。")
            return
        build_web = bool(env_info["pnpm_argv"])
        self._step_done("1", f"Python {env_info['python_version']}"
                             + ("" if build_web else "；未找到 Node/pnpm，将跳过 Web 控制台"))

        git = env_info["git"]

        # -- 2/8 source ------------------------------------------------------
        self._step_start("2", f"[2/8] 获取 Miloco 源码 ({self._ref})")
        # `git fetch <arbitrary ref> --depth 1` (rather than `clone --branch`)
        # accepts branch names, tags *and* full commit SHAs. Depth 1 means no
        # tag history reaches hatch-vcs, so the build falls back to the
        # `fallback_version = "0.0.0"` both backend packages declare — an
        # already-accepted degraded case inherited from the Docker build.
        if not (SRC_DIR / ".git").is_dir():
            SRC_DIR.mkdir(parents=True, exist_ok=True)
            code = await self._stream([git, "init", "-q", "."], cwd=SRC_DIR)
            if code == 0:
                code = await self._stream([git, "remote", "add", "origin", MILOCO_REPO], cwd=SRC_DIR)
        else:
            code = await self._stream([git, "remote", "set-url", "origin", MILOCO_REPO], cwd=SRC_DIR)
        if code == 0:
            code = await self._stream([git, "fetch", "--depth", "1", "origin", self._ref], cwd=SRC_DIR)
        if code == 0:
            # reset (not checkout) so a previous install's local modifications
            # can't block the update; untracked files we add ourselves (the
            # camera DLL, the built static/) survive and are redone below.
            code = await self._stream([git, "reset", "--hard", "-q", "FETCH_HEAD"], cwd=SRC_DIR)
        if code != 0:
            self._fail("2", f"获取源码失败（exit {code}）。请检查网络能否访问 {MILOCO_REPO}。", code)
            return
        head = await _run([git, "rev-parse", "HEAD"], cwd=SRC_DIR, timeout=15)
        commit = (head.stdout or "").strip()[:12]
        self._step_done("2", f"commit {commit}" if commit else None)

        # -- 3/8 web dashboard ----------------------------------------------
        self._step_start("3", "[3/8] 构建 Web 控制台")
        if not build_web:
            self._step_skip("3", "缺少 Node.js / pnpm，控制台将不可用（HTTP API 仍可用）")
        else:
            pnpm = list(env_info["pnpm_argv"])
            web_env = {**os.environ, "CI": "true"}
            registry = _resolve_mirror(npm_mirror, NPM_MIRRORS)
            if registry:
                # npm_config_registry is honoured by npm, npx *and* pnpm, and
                # unlike `pnpm config set` it doesn't rewrite the user's global
                # npm config as a side effect of one install.
                web_env["npm_config_registry"] = registry
                web_env["COREPACK_NPM_REGISTRY"] = registry
            code = await self._stream(pnpm + ["install", "--frozen-lockfile"],
                                      cwd=SRC_DIR / "web", env=web_env)
            if code == 0:
                code = await self._stream(pnpm + ["build"], cwd=SRC_DIR / "web", env=web_env)
            if code != 0:
                self._fail("3", f"Web 控制台构建失败（exit {code}）。可配置 NPM 镜像后重试。", code)
                return
            try:
                self._collect_web()
            except (OSError, RuntimeError) as e:
                self._fail("3", f"Web 构建产物不完整：{e}")
                return
            self._step_done("3")

        # -- 4/8 camera DLL --------------------------------------------------
        self._step_start("4", "[4/8] 注入 Windows 摄像头原生库")
        dll = env_info["camera_dll"]
        if not IS_WINDOWS:
            # Upstream ships the linux/darwin builds itself; injecting our DLL
            # here would just litter the checkout.
            self._step_skip("4", "非 Windows 平台，使用上游自带的原生库")
        elif not dll:
            # Unreachable on Windows (step 1 already hard-failed), kept so a
            # future relaxation of that check can't silently install a Miloco
            # that dies at client init.
            self._fail("4", "缺少 Windows 摄像头原生库 miot_camera_lite.dll。")
            return
        else:
            arch = _arch_dir() or "x86_64"
            dest = SRC_DIR / "backend" / "miot" / "src" / "miot" / "libs" / "windows" / arch
            try:
                dest.mkdir(parents=True, exist_ok=True)
                shutil.copy2(dll, dest / "miot_camera_lite.dll")
            except OSError as e:
                self._fail("4", f"复制摄像头原生库失败：{e}")
                return
            self._append(f"  {dll} -> {dest / 'miot_camera_lite.dll'}")
            self._step_done("4")

        # -- 5/8 perception models ------------------------------------------
        self._step_start("5", "[5/8] 部署感知模型")
        try:
            copied, total = self._copy_models()
        except OSError as e:
            # Not fatal: the backend boots without models, only perception
            # features fail — and they fail with their own clear error.
            self._step_skip("5", f"复制模型失败（{e}），感知功能将不可用")
        else:
            if total == 0:
                self._step_skip("5", "源码树中没有模型文件，感知功能将不可用")
            else:
                self._step_done("5", f"{copied}/{total} 个模型 -> {MILOCO_HOME / 'models'}")

        # -- 6/8 venv --------------------------------------------------------
        self._step_start("6", "[6/8] 创建 Python 虚拟环境")
        py = env_info["python"]
        if not _venv_python().exists():
            # `--clear` only when a broken venv is already there: recreating a
            # good one would throw away the installed packages for nothing.
            args = [py, "-m", "venv"] + (["--clear"] if VENV_DIR.exists() else []) + [str(VENV_DIR)]
            code = await self._stream(args)
            if code != 0 or not _venv_python().exists():
                self._fail("6", f"创建虚拟环境失败（exit {code}）：{VENV_DIR}", code)
                return
            self._step_done("6", str(VENV_DIR))
        else:
            self._step_done("6", f"复用已有虚拟环境 {VENV_DIR}")

        # -- 7/8 pip install -------------------------------------------------
        self._step_start("7", "[7/8] 安装 Miloco 及其依赖（首次较慢）")
        pip_env = {
            **os.environ,
            "PIP_INDEX_URL": _resolve_mirror(pip_mirror, PIP_MIRRORS),
            "PIP_DISABLE_PIP_VERSION_CHECK": "1",
            # Don't let a stray VIRTUAL_ENV/PYTHONHOME from our own process
            # leak into the child and confuse pip about the target env.
            "VIRTUAL_ENV": str(VENV_DIR),
        }
        pip_env.pop("PYTHONHOME", None)
        pip_env.pop("PYTHONPATH", None)
        vpy = str(_venv_python())
        code = await self._stream([vpy, "-m", "pip", "install", "--upgrade", "pip", "setuptools", "wheel"],
                                  env=pip_env)
        if code == 0:
            # miot first, then miloco (which depends on it by name,
            # `miloco-miot`) — same order the image installed them in, so the
            # local source build satisfies the dependency instead of pip
            # looking for a wheel that doesn't exist for Windows.
            code = await self._stream([vpy, "-m", "pip", "install", str(SRC_DIR / "backend" / "miot")],
                                      env=pip_env)
        if code == 0:
            code = await self._stream([vpy, "-m", "pip", "install", str(SRC_DIR / "backend" / "miloco")],
                                      env=pip_env)
        if code != 0:
            self._fail("7", f"安装依赖失败（exit {code}）。网络受限时可在「镜像源设置」里选择 PyPI 镜像后重试。", code)
            return
        self._step_done("7")

        # -- 8/8 Windows compat layer + smoke check --------------------------
        self._step_start("8", "[8/8] 写入 Windows 兼容层并校验安装")
        if IS_WINDOWS:
            try:
                for path in self._write_win_compat():
                    self._append(f"  兼容层 -> {path}")
            except (OSError, RuntimeError) as e:
                self._fail("8", f"写入 Windows 兼容层失败：{e}")
                return
        if not _backend_exe().exists():
            self._fail("8", f"安装完成但未生成 {_backend_exe()}，请查看上面的安装日志。")
            return
        # `import miloco.main` is the check that matters: it walks the whole
        # import chain the backend walks at startup, which is where every
        # Windows incompatibility so far has surfaced (fcntl, os.sysconf). Doing
        # it here turns "install succeeds, start crashes" into a failed install
        # with the traceback attached.
        probe = ["import importlib.metadata as m", "import fcntl, miot, miloco"]
        if IS_WINDOWS:
            probe += [
                "import os, zoneinfo",
                "assert os.sysconf('SC_PAGESIZE') > 0, 'sysconf shim inactive'",
                # No /usr/share/zoneinfo on Windows: ZoneInfo only works if the
                # `tzdata` package came along (via apscheduler -> tzlocal).
                "zoneinfo.ZoneInfo('Asia/Shanghai')",
            ]
        probe += [
            "import miloco.main",
            "print('miloco', m.version('miloco'), '| miloco-miot', m.version('miloco-miot'))",
        ]
        probe_env = {**pip_env, "MILOCO_HOME": str(MILOCO_HOME),
                     "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}
        check = await _run([vpy, "-c", "\n".join(probe)], env=probe_env, timeout=300)
        out = ((check.stdout or "") + (check.stderr or "")).strip()
        for line in out.splitlines():
            self._append("  " + line)
        if check.returncode != 0:
            self._fail("8", "安装校验失败：虚拟环境中无法导入 miloco（详见日志中的 traceback）。",
                       check.returncode)
            return
        self._step_done("8", out.splitlines()[-1] if out else None)

        web_static = SRC_DIR / "backend" / "miloco" / "src" / "miloco" / "static" / "index.html"
        info = {
            "ref": self._ref,
            "commit": commit,
            "repo": MILOCO_REPO,
            "python": py,
            "python_version": env_info["python_version"],
            "venv": str(VENV_DIR),
            "src": str(SRC_DIR),
            "web_built": web_static.exists(),
            "camera_dll": env_info["camera_dll"],
            # Resolved here so the first start doesn't have to shell out for it.
            "local_timezone": self._local_timezone(),
            "versions": out.splitlines()[-1] if out else "",
            "installed_at": time.time(),
        }
        try:
            INSTALL_INFO_PATH.write_text(json.dumps(info, ensure_ascii=False, indent=2), "utf-8")
        except OSError as e:
            _LOGGER.warning("failed to write %s: %s", INSTALL_INFO_PATH, e)
        with self._lock:
            self._finished_at = time.time()
            self._exit_code = 0
        self._append(f"$ 安装完成，可执行文件 {_backend_exe()}")

    def _collect_web(self) -> None:
        """Copy the built dashboard where the backend serves it from.

        Mirrors xiaomi-miloco's own scripts/build.sh::build_web — item list,
        sourcemap stripping and the "all of these must exist" check are copied
        from there, because a half-copied static/ yields a dead page at :1810
        that looks like a backend bug.
        """
        dist = SRC_DIR / "web" / "dist"
        static = SRC_DIR / "backend" / "miloco" / "src" / "miloco" / "static"
        if static.exists():
            shutil.rmtree(static)
        static.mkdir(parents=True, exist_ok=True)
        for item in ("index.html", "assets", "fonts", "favicon.svg", "watch.html", "vendor"):
            src = dist / item
            if not src.exists():
                continue
            if src.is_dir():
                shutil.copytree(src, static / item)
            else:
                shutil.copy2(src, static / item)
        for stale in (static / "assets").glob("*.map"):
            stale.unlink()
        missing = [name for name, ok in (
            ("index.html", (static / "index.html").is_file()),
            ("assets/", (static / "assets").is_dir()),
            ("fonts/", (static / "fonts").is_dir()),
            ("watch.html", (static / "watch.html").is_file()),
        ) if not ok]
        if missing:
            raise RuntimeError("缺少 " + ", ".join(missing))
        self._append(f"  {dist} -> {static}")

    def _copy_models(self) -> Tuple[int, int]:
        """Populate $MILOCO_HOME/models from the checkout.

        The ONNX models are in the repo but excluded from the wheel
        (``[tool.hatch.build] exclude``), and Miloco resolves them from
        ``settings.directories.models_dir`` == ``$MILOCO_HOME/models``. Upstream
        downloads a tarball there; we already have the files, so just copy the
        ones that aren't there yet (same name *and* size counts as present).
        """
        src = SRC_DIR / "backend" / "miloco" / "src" / "miloco" / "perception" / "models"
        if not src.is_dir():
            return 0, 0
        files = sorted(p for p in src.iterdir() if p.is_file())
        dest_dir = MILOCO_HOME / "models"
        dest_dir.mkdir(parents=True, exist_ok=True)
        copied = 0
        for f in files:
            dest = dest_dir / f.name
            if dest.exists() and dest.stat().st_size == f.stat().st_size:
                continue
            shutil.copy2(f, dest)
            copied += 1
            self._append(f"  {f.name} ({f.stat().st_size // 1024 // 1024} MB)")
        return copied, len(files)

    def _write_win_compat(self) -> List[Path]:
        """Write the Windows compatibility layer into the venv's site-packages.

        Idempotent: files whose content already matches are left alone, so this
        is cheap enough to run on every start — which is what lets an existing
        install pick up a new shim without being reinstalled. Returns the files
        that actually changed.
        """
        site = _site_packages()
        if site is None:
            raise RuntimeError(f"无法定位虚拟环境的 site-packages（{VENV_DIR}）")
        written: List[Path] = []
        for name, content in (
            ("fcntl.py", _FCNTL_SHIM),
            (f"{_WIN_COMPAT_MODULE}.py", _WIN_COMPAT_SRC),
            # A .pth's `import` line is executed by site.py at startup; the
            # "zz-" prefix just keeps it last in the sorted .pth order.
            (_WIN_COMPAT_PTH, f"import {_WIN_COMPAT_MODULE}\n"),
        ):
            target = site / name
            try:
                if target.exists() and target.read_text("utf-8") == content:
                    continue
            except OSError:
                pass  # unreadable -> rewrite it
            target.write_text(content, "utf-8")
            written.append(target)
        return written

    def _ensure_win_compat(self) -> None:
        """Best-effort refresh of the compat layer before a start.

        Deliberately non-fatal: if it can't be written the spawn below fails
        with the real traceback in the log, which is more informative than a
        pre-emptive error here.
        """
        if not IS_WINDOWS or not self.installed():
            return
        try:
            for path in self._write_win_compat():
                _LOGGER.info("refreshed Windows compat file %s", path)
        except (OSError, RuntimeError) as e:
            _LOGGER.warning("could not refresh Windows compat files: %s", e)

    # ------------------------------------------------------------- timezone
    def _local_timezone(self) -> str:
        """The host's IANA zone name, asked of the venv's own tzlocal.

        Miloco resolves its deploy timezone from ``TZ`` / ``/etc/timezone`` /
        ``/etc/localtime`` — all Unix-only — and otherwise falls back to
        ``Asia/Shanghai`` with a warning, so on Windows every timestamp it shows
        would assume China regardless of where the machine is. tzlocal is
        already installed in the venv (apscheduler depends on it, and on Windows
        it pulls in `tzdata`, which is also what makes ZoneInfo work there at
        all), so we can just ask it and pass the answer as ``MILOCO_TIMEZONE``.

        Cached in install.json: this costs a subprocess, and a machine's zone
        doesn't change while the app is open. Empty string means "couldn't tell",
        in which case Miloco's own fallback stands.
        """
        if self._tz_cache is not None:
            return self._tz_cache
        info = self.install_info()
        tz = str(info.get("local_timezone") or "").strip()
        if not tz and _venv_python().exists():
            try:
                r = subprocess.run(
                    [str(_venv_python()), "-c",
                     # Validate through ZoneInfo as well: a name Miloco can't
                     # load is worse than no name at all.
                     "import tzlocal, zoneinfo;"
                     "n = tzlocal.get_localzone_name() or '';"
                     "zoneinfo.ZoneInfo(n); print(n)"],
                    capture_output=True, text=True, timeout=30,
                    creationflags=_no_window())
                if r.returncode == 0 and (r.stdout or "").strip():
                    tz = r.stdout.strip().splitlines()[-1].strip()
            except (OSError, subprocess.SubprocessError):
                tz = ""
            if tz and info:
                info["local_timezone"] = tz
                try:
                    INSTALL_INFO_PATH.write_text(
                        json.dumps(info, ensure_ascii=False, indent=2), "utf-8")
                except OSError:
                    pass
        self._tz_cache = tz
        return tz

    def install_status(self, since: int = 0) -> Dict[str, Any]:
        """Snapshot of the install for the UI.

        Args:
            since: absolute sequence number the client already has. Only lines
                at or after it are returned, so the client appends instead of
                repainting (and never silently loses lines that scrolled past a
                fixed-size tail between two polls).
        """
        running = self._install_task is not None and not self._install_task.done()
        with self._lock:
            base, total = self._log_base_seq, self._log_base_seq + len(self._log)
            start = max(0, min(int(since), total) - base)
            lines = self._log[start:]
            steps = [{k: v for k, v in self._steps[i].items() if k != "_t0"}
                     for i in self._step_order if i in self._steps]
            started, finished = self._started_at, self._finished_at
            exit_code = self._exit_code
        return {
            "installing": running,
            "error": self._error,
            # Absolute sequence of the first returned line, so a client that
            # fell behind the trim window can tell it lost some.
            "first_seq": base + start,
            "next_seq": total,
            "lines": lines,
            "steps": steps,
            "started_at": started,
            "finished_at": finished,
            "exit_code": exit_code,
            "elapsed": (finished or time.time()) - started if started else None,
            "log_path": str(self._log_path()),
            "ref": self._ref,
            "installed": self.installed(),
        }

    # ------------------------------------------------------------ run / stop
    def _read_pid(self) -> Optional[int]:
        try:
            data = json.loads(PID_PATH.read_text("utf-8"))
            return int(data.get("pid"))
        except (OSError, ValueError, TypeError, AttributeError, json.JSONDecodeError):
            return None

    def _write_pid(self, pid: int) -> None:
        try:
            MILOCO_ROOT.mkdir(parents=True, exist_ok=True)
            PID_PATH.write_text(json.dumps(
                {"pid": pid, "exe": str(_backend_exe()), "started_at": time.time()},
                ensure_ascii=False), "utf-8")
        except OSError as e:
            _LOGGER.warning("failed to write %s: %s", PID_PATH, e)

    def _clear_pid(self) -> None:
        try:
            PID_PATH.unlink()
        except OSError:
            pass

    def _close_log(self) -> None:
        if self._proc_log is not None:
            try:
                self._proc_log.close()
            except OSError:
                pass
            self._proc_log = None

    def _running_pid(self) -> Optional[int]:
        """The pid of a live Miloco backend, or None.

        Two sources, in order: the child we spawned, then the pidfile — the
        latter is what lets a relaunched desktop app re-adopt a Miloco that
        outlived it (the old Docker deployment got this from the named
        container surviving app restarts).
        """
        if self._proc is not None:
            code = self._proc.poll()
            if code is None:
                return self._proc.pid
            self._last_exit_code = code
            self._proc = None
            self._close_log()
            self._clear_pid()
        pid = self._read_pid()
        if pid is None:
            return None
        if _pid_is_miloco(pid):
            return pid
        self._clear_pid()
        return None

    async def start(self) -> Dict[str, Any]:
        if not self.installed():
            raise RuntimeError(f"尚未安装 Miloco（缺少 {_backend_exe()}），请先点击「安装 Miloco」。")

        # Refresh the compat layer and resolve the host timezone before spawning,
        # so an install made by an older build of this app gets both without the
        # user having to reinstall. Both are off the event loop: writing files
        # touches disk, and the timezone lookup runs the venv interpreter.
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, self._ensure_win_compat)
        await loop.run_in_executor(None, self._local_timezone)

        # Seed our Mi credentials before first boot so Miloco comes up bound.
        seeded = self._seed_credentials()

        # Only patch the agent webhook into an existing config. On a *first*
        # boot there is no config.json yet, and writing a partial one ({"agent":
        # …}, no "server" section) is what stopped Miloco from generating and
        # persisting its own web token — leaving server_token() empty forever
        # and the dashboard URL tokenless. First boot is handled after the
        # backend has written its own complete config (see below).
        first_boot = not self._config_path().exists()
        if not first_boot:
            self._seed_agent_webhook()

        if self._running_pid() is not None:
            return await self.status()

        self._start_error = None
        self._last_exit_code = None
        try:
            self._spawn()
        except OSError as e:
            self._start_error = str(e)
            raise RuntimeError(f"启动 Miloco 失败：{e}") from e

        # Popen returns as soon as the process is created — a crash a second
        # later (missing dependency, port already bound, bad config) would
        # otherwise surface as a silent "未运行". Give it a moment and report
        # the real reason from its own captured output.
        alive, code = await self._settle(timeout=4.0)
        if not alive:
            # Only the stdout we captured for *this* spawn: Miloco's own service
            # log may still be the previous run's file when it dies this early.
            tail = self._tail_file(BACKEND_LOG_PATH, 40).strip()
            self._start_error = tail or f"进程启动后立即退出（exit {code}）"
            raise RuntimeError(
                f"Miloco 启动后立即退出（exit {code}）。日志：\n{self._start_error}")

        if first_boot:
            # Let Miloco write its own config.json (which is what mints the web
            # token), then merge our agent section in and restart so it takes
            # effect. Doing it in this order is what guarantees a token exists.
            if await self._await_config(timeout=25.0):
                self._seed_agent_webhook()
                await self._stop_process()
                self._spawn()
                await self._settle(timeout=4.0)
            else:
                _LOGGER.warning(
                    "miloco did not write %s within the wait window; agent "
                    "webhook not seeded", self._config_path())

        status = await self.status()
        status["seeded_credentials"] = seeded
        return status

    def _spawn(self) -> None:
        """Launch miloco-backend as a child process with our env + log capture."""
        MILOCO_HOME.mkdir(parents=True, exist_ok=True)
        MILOCO_ROOT.mkdir(parents=True, exist_ok=True)
        # Keep one generation of the previous run's output for post-mortems.
        try:
            if BACKEND_LOG_PATH.exists():
                BACKEND_LOG_PATH.replace(BACKEND_LOG_PATH.parent / (BACKEND_LOG_PATH.name + ".1"))
        except OSError:
            pass
        env = {
            **os.environ,
            "MILOCO_HOME": str(MILOCO_HOME),
            # MILOCO_* env wins over config.json in miloco's own settings
            # (env_prefix="MILOCO_", env_nested_delimiter="__"), which is how
            # the old `docker run -e MILOCO_SERVER__PORT=…` pinned the port.
            "MILOCO_SERVER__PORT": str(MILOCO_PORT),
            # server.url is a *separate* setting the CLI/plugins dial, and it
            # defaults to :1810 — Miloco logs a "配置不一致" warning and its own
            # health checks break when only the port is overridden, so pin both.
            "MILOCO_SERVER__URL": MILOCO_URL,
            "VIRTUAL_ENV": str(VENV_DIR),
            "PATH": _child_path(),
            # Force UTF-8 so Chinese log lines don't die on a cp936 console.
            "PYTHONUTF8": "1",
            "PYTHONIOENCODING": "utf-8",
            "PYTHONUNBUFFERED": "1",
        }
        # Nothing of *our* interpreter may leak into Miloco's: PYTHONHOME /
        # PYTHONPATH would point it at this app's frozen stdlib, and the
        # bootloader's own markers make a child think it is the frozen app.
        env.pop("PYTHONHOME", None)
        env.pop("PYTHONPATH", None)
        for key in [k for k in env if k.startswith("_PYI") or k == "_MEIPASS2"]:
            env.pop(key, None)
        # Tell Miloco the real host timezone (see _local_timezone). Skipped when
        # config.json sets one explicitly — env wins over config there, and the
        # user's own choice must not be overridden by our detection.
        if not str(self._read_config().get("timezone") or "").strip():
            tz = self._tz_cache or ""
            if tz:
                env["MILOCO_TIMEZONE"] = tz
        self._close_log()
        try:
            self._proc_log = open(BACKEND_LOG_PATH, "w", encoding="utf-8", errors="replace")
        except OSError:
            self._proc_log = None
        flags = 0
        if IS_WINDOWS:
            flags = _no_window() | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        self._proc = subprocess.Popen(
            [str(_backend_exe())],
            cwd=str(MILOCO_HOME), env=env,
            stdout=self._proc_log or subprocess.DEVNULL,
            stderr=subprocess.STDOUT if self._proc_log else subprocess.DEVNULL,
            stdin=subprocess.DEVNULL, creationflags=flags)
        self._write_pid(self._proc.pid)
        _LOGGER.info("started miloco backend pid=%s log=%s", self._proc.pid, BACKEND_LOG_PATH)

    async def _settle(self, timeout: float = 4.0) -> Tuple[bool, Optional[int]]:
        """Poll liveness briefly after start; return (alive, exit code).

        Long enough to catch an immediate crash (missing dependency, port
        clash, bad config all fail within a second or two) without stalling the
        happy path — a later crash is caught by the periodic status poll.
        """
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self._running_pid() is None:
                return False, self._last_exit_code
            await asyncio.sleep(0.4)
        return self._running_pid() is not None, self._last_exit_code

    async def _await_config(self, timeout: float = 25.0) -> bool:
        """Wait for Miloco to create its config.json *and* put a usable token in
        it. The file can appear before it's fully populated, so we wait on the
        token rather than on mere existence."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self._token_info()[0]:
                return True
            await asyncio.sleep(0.5)
        return bool(self._config_path().exists())

    async def _stop_process(self) -> None:
        pid = self._running_pid()
        if pid is None:
            self._clear_pid()
            return
        await asyncio.get_event_loop().run_in_executor(None, _kill_tree, pid)
        if self._proc is not None:
            try:
                self._last_exit_code = self._proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
            self._proc = None
        self._close_log()
        self._clear_pid()

    async def stop(self) -> Dict[str, Any]:
        await self._stop_process()
        return await self.status()

    @staticmethod
    def _tail_file(path: Path, tail: int) -> str:
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                # Read only the tail: these files grow for as long as Miloco runs.
                try:
                    size = os.fstat(f.fileno()).st_size
                    f.seek(max(0, size - 256 * 1024))
                except OSError:
                    pass
                lines = f.read().splitlines()
        except OSError as e:
            return f"（读取失败：{e}）"
        return "\n".join(lines[-max(1, tail):])

    async def logs(self, tail: int = 200) -> str:
        """Miloco's own service log plus our captured stdout/stderr.

        Two sources, because neither alone is enough: Miloco routes its logging
        to ``$MILOCO_HOME/log/miloco-backend_<ts>.log`` (so on a healthy run our
        captured stdout is empty), while anything that kills it before logging
        is configured — a failed import, a port clash, a traceback — only ever
        reaches the stdout we capture.
        """
        def _read() -> str:
            parts: List[str] = []
            log_dir = MILOCO_HOME / "log"
            newest = None
            if log_dir.is_dir():
                candidates = sorted(log_dir.glob("miloco-backend_*.log"),
                                    key=lambda p: p.stat().st_mtime, reverse=True)
                newest = candidates[0] if candidates else None
            if newest is not None:
                parts.append(f"=== {newest} ===\n" + self._tail_file(newest, tail))
            if BACKEND_LOG_PATH.exists():
                captured = self._tail_file(BACKEND_LOG_PATH, tail)
                if captured.strip():
                    parts.append(f"=== {BACKEND_LOG_PATH} (进程输出) ===\n" + captured)
            if not parts:
                if not BACKEND_LOG_PATH.exists():
                    return f"尚无日志（{BACKEND_LOG_PATH} 不存在，Miloco 还没启动过）。"
                return "日志为空（Miloco 尚未输出任何内容）。"
            return "\n\n".join(parts)
        return await asyncio.get_event_loop().run_in_executor(None, _read)

    # ------------------------------------------------------------- health
    async def _health(self) -> bool:
        try:
            async with httpx.AsyncClient(timeout=3.0) as c:
                r = await c.get(f"{MILOCO_URL}/health")
                return r.status_code < 400
        except Exception:  # pylint: disable=broad-except
            return False

    async def _probe(self, url: str) -> Optional[int]:
        """HTTP status for `url`, or None if it couldn't be reached."""
        try:
            async with httpx.AsyncClient(timeout=4.0, follow_redirects=True) as c:
                r = await c.get(url)
                return r.status_code
        except Exception:  # pylint: disable=broad-except
            return None

    async def status(self) -> Dict[str, Any]:
        pid = self._running_pid()
        healthy = await self._health() if pid is not None else False
        token, token_source = self._token_info()
        # The dashboard route (miloco.main:spa_handler) ignores query-string
        # tokens entirely — it injects server.token server-side into the HTML
        # it serves at "/", and the frontend JS reads it from there to set an
        # Authorization header on subsequent /api/* calls. A "?token=..."
        # query param does nothing here (that fallback only exists for a
        # couple of non-SPA endpoints like snapshot <img> pulls), so the
        # preview URL is just the plain root — no token to encode into it.
        preview_url = f"{MILOCO_URL}/"
        # Probe it so a bad dashboard load is attributable instead of a
        # mystery: spa_handler returns 503 when server.token isn't configured
        # yet and 404 when the static build (index.html) is missing, so the
        # status code alone tells us which.
        preview_status = await self._probe(preview_url) if healthy else None
        info = self.install_info()
        return {
            "deploy_mode": "native",
            "platform": platform.system().lower(),
            "arch": platform.machine(),
            "env": self.probe_env(),
            "installed": self.installed(),
            "install_info": info,
            "web_built": bool(info.get("web_built")),
            "install_root": str(MILOCO_ROOT),
            "venv_dir": str(VENV_DIR),
            "src_dir": str(SRC_DIR),
            "backend_exe": str(_backend_exe()),
            "running": pid is not None,
            "pid": pid,
            "healthy": healthy,
            "exit_code": self._last_exit_code if pid is None else None,
            "start_error": self._start_error,
            "url": MILOCO_URL,
            "preview_url": preview_url,
            "has_server_token": bool(token),
            "token_source": token_source,
            "preview_status": preview_status,
            "config_path": str(self._config_path()),
            "config_exists": self._config_path().exists(),
            "config_sections": sorted(self._read_config().keys()),
            "has_credentials": self.has_credentials(),
            "miloco_home": str(MILOCO_HOME),
            "log_path": str(BACKEND_LOG_PATH),
        }
