"""日志配置：同时输出到控制台和文件。

将 async_upnp_client（及其 traffic 子 logger）的日志也接入本体系，
这样手机投屏时的 SSDP / SOAP 收发包都能在日志里看到，便于诊断。
"""
from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler

from .constants import APP_NAME, LOG_PATH

_CONFIGURED: bool = False

# 第三方库的 logger 名 —— 接进来后用同一套 handler
_THIRD_PARTY_LOGGERS = (
    "async_upnp_client",
    "async_upnp_client.traffic.ssdp",
    "async_upnp_client.traffic.upnp",
    "aiohttp.access",
)


def setup_logging(level: int = logging.INFO) -> logging.Logger:
    """配置全局日志，返回应用 logger。多次调用安全。

    level 只作用于 轻投自身和通用第三方；SSDP/UPnP 流量日志单独设为 DEBUG，
    这样即使主日志是 INFO，也能看到设备发现的收发包细节（诊断投屏必备）。
    """
    global _CONFIGURED
    logger = logging.getLogger(APP_NAME)
    if _CONFIGURED:
        logger.setLevel(level)
        return logger

    logger.setLevel(level)
    formatter = logging.Formatter(
        "%(asctime)s [%(levelname)-7s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # 控制台
    console = logging.StreamHandler()
    console.setLevel(level)
    console.setFormatter(formatter)
    logger.addHandler(console)

    # 文件（轮转，单文件 1MB，保留 3 份）
    try:
        # 打包后日志目录（%APPDATA%\LightCast）首次运行可能不存在
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            LOG_PATH, maxBytes=1_000_000, backupCount=3, encoding="utf-8"
        )
        file_handler.setLevel(logging.DEBUG)  # 文件记全量
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)
    except OSError:
        logger.warning("无法写入日志文件 %s，仅使用控制台输出", LOG_PATH)

    # 把第三方库的 logger 挂到同一组 handler，但级别独立控制
    # 流量日志（收发包明细）默认 DEBUG，便于诊断
    for name in _THIRD_PARTY_LOGGERS:
        third = logging.getLogger(name)
        third.setLevel(logging.DEBUG if "traffic" in name else level)
        third.addHandler(console)
        try:
            third.addHandler(file_handler)
        except NameError:  # file_handler 在 OSError 分支未定义
            pass
        third.propagate = False  # 避免重复输出

    _CONFIGURED = True
    return logger


def get_logger(name: str | None = None) -> logging.Logger:
    """获取子 logger。name 为 None 时返回根应用 logger。"""
    if not name:
        return logging.getLogger(APP_NAME)
    if name.startswith(APP_NAME):
        return logging.getLogger(name)
    return logging.getLogger(f"{APP_NAME}.{name}")
