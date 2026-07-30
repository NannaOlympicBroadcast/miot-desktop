#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Apply the Windows compatibility layer to an already-installed Miloco venv.

MIoT Desktop writes this layer itself — on every install and every start — so
you only need this script when the app itself is still an older build and Miloco
dies at import with one of:

    ModuleNotFoundError: No module named 'fcntl'
    AttributeError: module 'os' has no attribute 'sysconf'

Both come from upstream Miloco using POSIX-only stdlib APIs at module import
time (it supports Linux/macOS only). See README「关于 Miloco（Windows 裸机部署）」
→「Windows 兼容层」for what each shim does and why.

Usage — any Python 3.8+ will do, including the venv's own:

    python scripts\\apply_win_compat.py
    python scripts\\apply_win_compat.py --venv "D:\\path\\to\\miloco_runtime\\venv"

The shims are read straight out of backend/miloco_controller.py, so this script
cannot drift from what the app writes. They are *parsed*, not imported —
importing that module would drag in httpx and the vendored miot package for no
reason.
"""
import argparse
import ast
import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
CONTROLLER = REPO / "backend" / "miloco_controller.py"

_MODULE_CONSTANTS = ("_FCNTL_SHIM", "_WIN_COMPAT_SRC", "_WIN_COMPAT_MODULE", "_WIN_COMPAT_PTH")
_CLASS_CONSTANTS = ("_VMMAP_NEEDLE", "_VMMAP_REPLACEMENT")


def _assigned_name(node):
    if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
        return node.targets[0].id
    if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
        return node.target.id
    return None


def read_constants():
    """Pull the shim sources out of the controller by parsing it."""
    if not CONTROLLER.is_file():
        sys.exit(f"找不到 {CONTROLLER}\n请在 miot-desktop 仓库里运行本脚本。")
    tree = ast.parse(CONTROLLER.read_text("utf-8"))
    found = {}
    for node in tree.body:
        name = _assigned_name(node)
        if name in _MODULE_CONSTANTS:
            found[name] = ast.literal_eval(node.value)
        elif isinstance(node, ast.ClassDef) and node.name == "MilocoController":
            for sub in node.body:
                sub_name = _assigned_name(sub)
                if sub_name in _CLASS_CONSTANTS:
                    found[sub_name] = ast.literal_eval(sub.value)
    missing = [n for n in _MODULE_CONSTANTS + _CLASS_CONSTANTS if n not in found]
    if missing:
        sys.exit(f"{CONTROLLER} 里找不到 {', '.join(missing)}——仓库版本太旧？请先 git pull。")
    return found


def default_venv():
    root = os.environ.get("MILOCO_ROOT_HOST")
    if root:
        return Path(root) / "venv"
    cache = os.environ.get("MIOT_CACHE_PATH") or os.path.join(os.path.expanduser("~"), ".miot_cache")
    return Path(cache) / "miloco_runtime" / "venv"


def site_packages(venv: Path) -> Path:
    candidates = [venv / "Lib" / "site-packages"]
    candidates += sorted((venv / "lib").glob("python3.*/site-packages"))
    for cand in candidates:
        if cand.is_dir():
            return cand
    sys.exit(f"在 {venv} 下找不到 site-packages —— Miloco 还没安装？\n"
             f"可用 --venv 指定虚拟环境路径。")


def venv_python(venv: Path) -> Path:
    for cand in (venv / "Scripts" / "python.exe", venv / "bin" / "python"):
        if cand.exists():
            return cand
    sys.exit(f"在 {venv} 下找不到 python —— Miloco 还没安装？")


def verify(python: Path):
    """Import what the backend imports; returns (ok, output)."""
    proc = subprocess.run(
        [str(python), "-c",
         "import fcntl, miloco.node_monitor.vmmap, miloco.main; print('compat ok')"],
        capture_output=True, text=True, timeout=600)
    return proc.returncode == 0, ((proc.stdout or "") + (proc.stderr or "")).strip()


def patch_vmmap(site: Path, needle: str, replacement: str):
    """Fallback for a venv that doesn't run .pth startup hooks: edit the one
    module-scope POSIX call directly. Leaves a changed upstream line alone."""
    target = site / "miloco" / "node_monitor" / "vmmap.py"
    if not target.is_file():
        return None
    text = target.read_text("utf-8")
    if replacement in text or needle not in text:
        return None
    target.write_text(text.replace(needle, replacement, 1), "utf-8")
    for pyc in (target.parent / "__pycache__").glob("vmmap.*.pyc"):
        try:
            pyc.unlink()
        except OSError:
            pass
    return target


def main() -> int:
    parser = argparse.ArgumentParser(description="给已安装的 Miloco 虚拟环境补上 Windows 兼容层")
    parser.add_argument("--venv", type=Path, default=None,
                        help="虚拟环境路径（默认 ~/.miot_cache/miloco_runtime/venv）")
    args = parser.parse_args()

    venv = args.venv or default_venv()
    consts = read_constants()
    site = site_packages(venv)
    python = venv_python(venv)
    print(f"[1/3] 虚拟环境: {venv}")

    files = [
        ("fcntl.py", consts["_FCNTL_SHIM"]),
        (f"{consts['_WIN_COMPAT_MODULE']}.py", consts["_WIN_COMPAT_SRC"]),
        (consts["_WIN_COMPAT_PTH"], f"import {consts['_WIN_COMPAT_MODULE']}\n"),
    ]
    for name, content in files:
        (site / name).write_text(content, "utf-8")
        print(f"       写入 {site / name}")
    print(f"[2/3] 已写入 {len(files)} 个补丁文件")

    ok, out = verify(python)
    if not ok:
        print("       .pth 启动钩子未生效，回退修补 vmmap.py …")
        patched = patch_vmmap(site, consts["_VMMAP_NEEDLE"], consts["_VMMAP_REPLACEMENT"])
        if patched:
            print(f"       已修补 {patched}")
        ok, out = verify(python)
    print(f"[3/3] 校验: {out.splitlines()[-1] if out else '(无输出)'}")
    if not ok:
        print()
        print(out)
        print("修复失败。请把上面的 traceback 反馈给我们。")
        return 1
    print()
    print("完成 —— 回到 MIoT Desktop 点击「启动 Miloco」即可。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
