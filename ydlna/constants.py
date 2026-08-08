"""轻投（LightCast）全局常量定义。"""
from __future__ import annotations

import os
import sys
from pathlib import Path

# --------------------------------------------------------------------------- #
# 路径
# --------------------------------------------------------------------------- #
# 项目根目录（ydlna/ 的上一级；打包后为 PyInstaller 的 _internal 目录）
APP_DIR: Path = Path(__file__).resolve().parent.parent
# 存放 libmpv (mpv-2.dll) 等二进制
BIN_DIR: Path = APP_DIR / "bin"
# 资源目录（图标等）
ASSETS_DIR: Path = APP_DIR / "assets"
# 国际化文案目录
I18N_DIR: Path = APP_DIR / "i18n"


def ensure_bin_in_path() -> None:
    """让 python-mpv 能在 ``import mpv`` 时找到 bin/ 里的 libmpv。

    实现说明（Windows）：
    - 若用 ``os.add_dll_directory(bin)``，会把 bin/ 持久加入 DLL 搜索路径，
      而其中体积巨大的 ``libmpv-2.dll`` 会干扰 PySide6 (Qt6) 的 dll 解析，
      导致 ``import PySide6.QtWidgets`` 报「DLL load failed」。
    - 因此这里 **不** 使用 ``add_dll_directory``，只把 bin/ 临时放到
      ``os.environ['PATH']`` 最前。``ctypes.util.find_library`` 在 Windows
      上会查 PATH，于是 ``import mpv`` 能定位到 libmpv-2.dll；
      而 Qt 加载自己的 dll 时不走 PATH（走 ``add_dll_directory`` 注册的目录），
      故不受影响。

    必须在 ``import mpv`` 之前调用。可在 ``import ydlna`` 时自动触发。
    """
    bin_str = str(BIN_DIR)
    cur = os.environ.get("PATH", "")
    if bin_str not in cur.split(os.pathsep):
        os.environ["PATH"] = bin_str + os.pathsep + cur


# --------------------------------------------------------------------------- #
# 应用元信息
# --------------------------------------------------------------------------- #
APP_NAME: str = "LightCast"
# 版本号跟随 ydlna.__version__（发布 workflow 会在打包时按 tag 同步，
# 保证「关于」页与 Release 版本一致；不要在这里硬编码）
from . import __version__ as _version  # noqa: E402

APP_VERSION: str = _version
APP_DISPLAY_NAME: str = "轻投"

# 配置文件/日志：开发模式放项目目录；打包（frozen）后放 %APPDATA%\LightCast，
# 避免写入程序安装目录（可能无写权限、升级时被覆盖清空）
if getattr(sys, "frozen", False):
    _DATA_DIR: Path = Path(os.environ.get("APPDATA", str(Path.home()))) / APP_NAME
else:
    _DATA_DIR = APP_DIR
CONFIG_PATH: Path = _DATA_DIR / "config.json"
LOG_PATH: Path = _DATA_DIR / "lightcast.log"

# UPnP / DLNA 协议常量
DEVICE_TYPE_MEDIA_RENDERER: str = ":urn:schemas-upnp-org:device:MediaRenderer:1"
SERVICE_TYPE_AVTRANSPORT: str = "urn:schemas-upnp-org:service:AVTransport:1"
SERVICE_TYPE_RENDERING_CONTROL: str = "urn:schemas-upnp-org:service:RenderingControl:1"
SERVICE_TYPE_CONNECTION_MANAGER: str = "urn:schemas-upnp-org:service:ConnectionManager:1"

# 默认网络配置
DEFAULT_SSDP_PORT: int = 1900
DEFAULT_HTTP_PORT: int = 0  # 0 = 让系统自动分配可用端口

# 默认 SinkProtocolInfo：声明本渲染器支持的协议（http-get:*:*:* 表示接受任意 HTTP 流）
DEFAULT_SINK_PROTOCOL_INFO: str = (
    "http-get:*:*:*,"  # 任意 HTTP 流
    "http-get:*:video/*:*,"
    "http-get:*:audio/*:*,"
    "http-get:*:image/*:*,"
)
