# -*- mode: python ; coding: utf-8 -*-
"""轻投（LightCast）PyInstaller 打包配置。

构建（在项目根目录）::

    pyinstaller LightCast.spec --noconfirm

产物: ``dist/LightCast/LightCast.exe``（onedir 模式）。
依赖的 libmpv-2.dll（约 110MB）会一并打入 ``_internal/bin/``，
运行时由 ``ydlna.constants.ensure_bin_in_path`` 注入 PATH 供 python-mpv 加载。
"""
from PyInstaller.utils.hooks import collect_all

datas = []
binaries = []
hiddenimports = []

# qfluentwidgets 运行时按 __file__ 加载 QSS/字体/图标等资源，
# 静态分析抓不全，必须整包收集（官方推荐做法）
qf_datas, qf_binaries, qf_hidden = collect_all("qfluentwidgets")
datas += qf_datas
binaries += qf_binaries
hiddenimports += qf_hidden

# 懒加载/动态导入的模块
hiddenimports += [
    "didl_lite",                      # async_upnp_client 的 DIDL-Lite 解析
    "async_upnp_client.traffic.ssdp",
    "async_upnp_client.traffic.upnp",
]

# 应用自身资源
datas += [
    ("i18n", "i18n"),
    ("assets/icon.ico", "assets"),
    ("assets/icon.png", "assets"),
]

# libmpv：放到 _internal/bin/（与 BIN_DIR 一致）
binaries += [("bin/libmpv-2.dll", "bin")]

a = Analysis(
    ["main.py"],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    # 环境里同时装有 PyQt6（qfluentwidgets 的兼容层会探测到），
    # 必须排除，否则 PyInstaller 拒绝同时打包两套 Qt 绑定
    excludes=[
        "PyQt5",
        "PyQt5.QtCore",
        "PyQt5.QtGui",
        "PyQt5.QtWidgets",
        "PyQt6",
        "PyQt6.QtCore",
        "PyQt6.QtGui",
        "PyQt6.QtWidgets",
        # ML 栈：qfluentwidgets 的 image_utils 运行时必需 numpy/scipy，
        # 但环境里装的 scipy 会条件导入 torch，torch 又拖进
        # transformers/pandas/cv2/matplotlib/sympy... 全部与本应用无关，
        # 排除掉（否则安装包会膨胀到 4.9GB）
        "torch",
        "torchvision",
        "torchaudio",
        "transformers",
        "huggingface_hub",
        "safetensors",
        "cv2",
        "pandas",
        "pyarrow",
        "sklearn",
        "scikit_learn",
        "matplotlib",
        "networkx",
        "sympy",
        "pytest",
        "joblib",
        "dill",
        "fsspec",
        "contourpy",
        "pygments",
        "rich",
        "google.protobuf",
    ],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="LightCast",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,             # UPX 压缩大 dll 易误报毒且可能破坏 libmpv
    console=False,         # 窗口程序，不弹控制台
    disable_windowed_traceback=False,
    icon="assets/icon.ico",
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="LightCast",
)
