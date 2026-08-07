"""轻投（LightCast）—— DLNA 投屏接收软件包。

注意：本包**不**在导入时修改 PATH/DLL 搜索路径，以免干扰 PySide6。
libmpv 的加载由 ``ydlna.player.mpv_player`` 模块在 import mpv 时临时处理。
"""
from __future__ import annotations

__version__ = "0.1.0"
