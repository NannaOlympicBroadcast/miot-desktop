# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for the MIoT Desktop backend.

Produces a onedir bundle `dist-backend/backend/miot-backend.exe` that embeds the
Python runtime, the vendored `miot` package (with its data files) and all
runtime dependencies — so the shipped app needs no system Python.

Build:  pyinstaller backend/miot-backend.spec --noconfirm --distpath dist-backend
"""
import os
import sys
from PyInstaller.utils.hooks import collect_all

ROOT = os.path.abspath(os.getcwd())
VENDOR = os.path.join(ROOT, 'vendor', 'miot_kit')
# Make the vendored `miot` importable during analysis (for collect_all + imports).
sys.path.insert(0, VENDOR)

datas, binaries, hiddenimports = [], [], []
for pkg in [
    'miot', 'av', 'PIL',
    'pydantic', 'pydantic_core',
    'aiohttp', 'aiocache', 'aiofiles',
    'zeroconf', 'cryptography', 'yaml', 'psutil',
]:
    try:
        d, b, h = collect_all(pkg)
        datas += d
        binaries += b
        hiddenimports += h
    except Exception as e:  # pragma: no cover
        print('collect_all skip', pkg, e)

a = Analysis(
    [os.path.join(ROOT, 'backend', 'server.py')],
    pathex=[VENDOR, os.path.join(ROOT, 'backend')],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=['tkinter', 'fastmcp', 'mcp'],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='miot-backend',
    console=True,
    disable_windowed_traceback=False,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name='backend',
)
