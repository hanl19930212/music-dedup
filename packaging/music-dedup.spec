# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置：将 music-dedup 打包为单文件 exe，并附带 ffmpeg / fpcalc。

用法（在项目根目录执行）：
    pyinstaller packaging/music-dedup.spec

前置：
    将 ffmpeg.exe 与 fpcalc.exe 放入项目根的 bin/ 目录
    （fpcalc 从 https://acoustid.org/chromaprint 下载，ffmpeg 从 https://www.gyan.dev/ffmpeg/builds/ 下载）

产物：
    dist/music-dedup.exe
"""

import os

# SPECPATH 是 PyInstaller 在执行 spec 时提供的变量（即 spec 文件所在目录）
PROJECT_ROOT = os.path.abspath(os.path.join(SPECPATH, ".."))

BIN_DIR = os.path.join(PROJECT_ROOT, "bin")
SRC_DIR = os.path.join(PROJECT_ROOT, "src")

# 收集外部二进制，打包到 exe 内的 bin/ 目录
binaries = []
for tool in ("ffmpeg.exe", "fpcalc.exe"):
    tool_path = os.path.join(BIN_DIR, tool)
    if os.path.exists(tool_path):
        binaries.append((tool_path, "bin"))
    else:
        print(f"[warn] 未找到 {tool_path}，打包后该工具将缺失，运行时会报错")

a = Analysis(
    [os.path.join(SRC_DIR, "music_dedup", "cli.py")],
    pathex=[SRC_DIR],
    binaries=binaries,
    datas=[],
    hiddenimports=[
        "mutagen.mp3",
        "mutagen.flac",
        "mutagen.wave",
        "mutagen.dsf",
        "mutagen.id3",
        "mutagen.easyid3",
        "send2trash",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["tkinter", "matplotlib", "numpy", "PIL"],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="music-dedup",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,          # 关闭 UPX，避免部分杀软误报
    console=True,       # 控制台程序
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
