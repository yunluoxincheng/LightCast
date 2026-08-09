"""应用配置：持久化到 config.json，单例 Config。

配置项包括：设备 UDN、友好名、主题、语言、音量、窗口几何、DLNA 开关等。
"""
from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any

from PySide6.QtCore import QObject, Signal

from .constants import CONFIG_PATH
from .logger import get_logger

log = get_logger("config")

# 配置默认值
DEFAULTS: dict[str, Any] = {
    # DLNA 设备标识。UDN 必须持久化，否则控制点会把它当成新设备
    "udn": f"uuid:{uuid.uuid4()}",
    "friendly_name": "轻投",
    "dlna_enabled": True,
    "http_port": 0,  # 0 = 自动分配
    # UI
    "language": "zh",  # "zh" | "en"
    "theme": "auto",  # "light" | "dark" | "auto"
    # 播放器
    "volume": 80,
    "muted": False,
    # 音频输出设备（mpv audio-device 名，"" = 默认）
    "audio_device": "",
    # 启动时自动检查 GitHub Release 更新
    "auto_update": True,
    # 更新下载使用加速镜像（智能选源：直连与镜像并行探测取最快）
    "update_mirror": True,
    # 窗口几何（x, y, w, h），None 表示用系统默认
    "window_geometry": None,
    # 窗口默认尺寸版本标记（每次调整默认尺寸时 +1，首次启动重置一次旧几何）
    "window_geometry_v6": False,
    "window_geometry_v7": False,
    # 是否已提示过「关闭即最小化到托盘」
    "minimize_hint_shown": False,
    # 投屏 URL 是否允许指向内网私有地址（手机、NAS 等）。
    # 默认开：DLNA 投屏本就是同局域网场景，默认挡会破坏正常功能。此产品边界也意味着
    # 任意可达的 RFC1918 / ULA 地址（含路由器、VPN、虚拟网卡网络）均可作为媒体源；
    # 关闭后进入拒绝私网的严格模式。loopback/link-local/云元数据等地址始终拦截。
    "allow_intranet_cast": True,
}


class Config(QObject):
    """带变更信号的配置单例。

    用法::

        cfg = Config.instance()
        cfg.set("volume", 50)        # 会自动写盘并发射 changed
        vol = cfg.get("volume")
    """

    # (key, value) — 任一配置变化时发射
    changed = Signal(str, object)

    _instance: "Config | None" = None

    def __init__(self, path: Path = CONFIG_PATH) -> None:
        super().__init__()
        self._path = path
        self._data: dict[str, Any] = dict(DEFAULTS)
        self._load()

    @classmethod
    def instance(cls) -> "Config":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    # ------------------------------------------------------------------ #
    # 读写
    # ------------------------------------------------------------------ #
    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            with self._path.open("r", encoding="utf-8") as f:
                stored = json.load(f)
            if isinstance(stored, dict):
                # 只接受已知 key，合并到默认值上
                for k, v in stored.items():
                    if k in DEFAULTS:
                        self._data[k] = v
        except (OSError, json.JSONDecodeError) as e:
            log.warning("读取配置失败，使用默认值: %s", e)

    def save(self) -> None:
        try:
            # 首次保存时目录可能不存在（如打包后 %APPDATA%\LightCast）
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with self._path.open("w", encoding="utf-8") as f:
                json.dump(self._data, f, ensure_ascii=False, indent=2)
        except OSError as e:
            log.warning("写入配置失败: %s", e)

    def get(self, key: str, default: Any = None) -> Any:
        return self._data.get(key, DEFAULTS.get(key, default))

    def set(self, key: str, value: Any, *, persist: bool = True) -> None:
        if self._data.get(key) == value:
            return
        self._data[key] = value
        log.debug("配置变更: %s = %r", key, value)
        self.changed.emit(key, value)
        if persist:
            self.save()

    def as_dict(self) -> dict[str, Any]:
        return dict(self._data)
