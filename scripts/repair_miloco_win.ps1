# Repair an existing bare-metal Miloco install on Windows, without updating or
# rebuilding MIoT Desktop.
#
# The desktop app writes this compatibility layer itself (backend/
# miloco_controller.py, on every install and every start), so you only need this
# script when you are still running an older build of the app and Miloco dies at
# import with one of:
#
#   ModuleNotFoundError: No module named 'fcntl'
#   AttributeError: module 'os' has no attribute 'sysconf'
#
# Both come from upstream Miloco using POSIX-only stdlib APIs at module import
# time; upstream supports Linux/macOS only. See README「关于 Miloco（Windows
# 裸机部署）」→「Windows 兼容层」for what each shim does and why.
#
# Usage (PowerShell):
#   .\scripts\repair_miloco_win.ps1
#   .\scripts\repair_miloco_win.ps1 -VenvDir "D:\somewhere\miloco_runtime\venv"

param(
    [string]$VenvDir = (Join-Path $env:USERPROFILE ".miot_cache\miloco_runtime\venv")
)

$ErrorActionPreference = "Stop"

$site = Join-Path $VenvDir "Lib\site-packages"
if (-not (Test-Path $site)) {
    Write-Error "site-packages not found: $site  (是否还没安装 Miloco？)"
}
Write-Host "[1/4] site-packages: $site"

# ---- 1. fcntl: a module Windows does not have at all ------------------------
# miloco.home_profile.store imports it at module scope for a cross-process file
# lock, and miloco.main imports that module. flock() maps onto msvcrt.locking()
# byte-range locks — a real lock, not a no-op.
$fcntl = @'
"""Windows `fcntl` stand-in — installed by MIoT Desktop, not by Miloco."""
import msvcrt
import os
import time

LOCK_SH = 1
LOCK_EX = 2
LOCK_NB = 4
LOCK_UN = 8

F_GETFD = 1
F_SETFD = 2
F_GETFL = 3
F_SETFL = 4
FD_CLOEXEC = 1

_RANGE = 1
_MAX_WAIT = 60.0


def flock(fd, operation):
    blocking = not operation & LOCK_NB
    op = operation & ~LOCK_NB
    pos = os.lseek(fd, 0, os.SEEK_CUR)
    os.lseek(fd, 0, os.SEEK_SET)
    try:
        if op == LOCK_UN:
            try:
                msvcrt.locking(fd, msvcrt.LK_UNLCK, _RANGE)
            except OSError:
                pass
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
                    return
    finally:
        os.lseek(fd, pos, os.SEEK_SET)


def lockf(fd, operation, length=0, start=0, whence=0):
    return flock(fd, operation)


def fcntl(fd, cmd, arg=0):
    raise OSError("fcntl.fcntl() is not available on Windows")


def ioctl(fd, request, arg=0, mutate_flag=True):
    raise OSError("fcntl.ioctl() is not available on Windows")
'@
Set-Content -Path (Join-Path $site "fcntl.py") -Value $fcntl -Encoding UTF8
Write-Host "[2/4] wrote fcntl.py"

# ---- 2. os.sysconf, applied at interpreter startup via a .pth ---------------
# miloco.node_monitor.vmmap evaluates os.sysconf("SC_PAGESIZE") at module scope
# and resource_monitor imports it unconditionally (macOS-only code, dispatched
# at call time). Only names with a truthful Windows answer are provided.
$compat = @'
"""Windows compatibility shims for Miloco — installed by MIoT Desktop."""
import os


def _install_sysconf():
    if hasattr(os, "sysconf"):
        return
    import mmap

    values = {
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
'@
Set-Content -Path (Join-Path $site "miot_desktop_win_compat.py") -Value $compat -Encoding UTF8
# site.py executes the `import` line of every .pth in site-packages at startup —
# before any miloco module is imported, which is the only workable timing.
Set-Content -Path (Join-Path $site "zz-miot-desktop-win-compat.pth") `
    -Value "import miot_desktop_win_compat" -Encoding UTF8
Write-Host "[3/4] wrote miot_desktop_win_compat.py + zz-miot-desktop-win-compat.pth"

# ---- 3. verify against the venv itself --------------------------------------
$py = Join-Path $VenvDir "Scripts\python.exe"
$probe = & $py -c "import fcntl, os; import miloco.main; print('OK page=' + str(os.sysconf('SC_PAGESIZE')))" 2>&1
if ($LASTEXITCODE -ne 0) {
    Write-Host $probe
    Write-Error "[4/4] 校验失败：miloco 仍无法导入（见上面的 traceback）"
}
Write-Host "[4/4] $probe"
Write-Host ""
Write-Host "Miloco 现在可以启动了 — 回到 MIoT Desktop 点击「启动 Miloco」。" -ForegroundColor Green
