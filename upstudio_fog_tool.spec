# -*- mode: python ; coding: utf-8 -*-

import os
import sys

from PyInstaller.utils.hooks import collect_all, collect_dynamic_libs, collect_submodules


datas = []
binaries = []
hiddenimports = []

for package in ("PyQt6", "pyqtgraph", "cv2", "pyrealsense2"):
    pkg_datas, pkg_binaries, pkg_hiddenimports = collect_all(package)
    datas += pkg_datas
    binaries += pkg_binaries
    hiddenimports += pkg_hiddenimports

hiddenimports += collect_submodules("PyQt6")
binaries += collect_dynamic_libs("cv2")
binaries += collect_dynamic_libs("pyrealsense2")

# Conda keeps several Python runtime dependencies in Library/bin, outside the
# locations PyInstaller scans reliably. Without these, the packaged app can
# fail before Qt starts (for example while importing ctypes/pyqtgraph).
conda_runtime_dir = os.path.join(sys.prefix, "Library", "bin")
for runtime_dll in ("ffi.dll", "libcrypto-3-x64.dll", "libssl-3-x64.dll", "libexpat.dll"):
    runtime_path = os.path.join(conda_runtime_dir, runtime_dll)
    if os.path.isfile(runtime_path):
        binaries.append((runtime_path, "."))


a = Analysis(
    ["main.py"],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="UpStudioFOGTool",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="UpStudioFOGTool",
)
