"""Windows 开机自启：HKCU 注册表 Run 项。

开机时由系统执行注册表里的命令（不需要管理员权限）：
- 打包版：LightCast.exe --autostart（窗口程序，无控制台）
- 开发版：pythonw.exe main.py --autostart（pythonw 无控制台，静默后台）

命令统一带 ``--autostart`` 参数：应用识别后**静默启动到系统托盘**
（不弹主窗口），投屏到达时再弹出窗口。

提供 enable / disable / is_enabled 三个接口，由设置页开关调用。
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional

from .logger import get_logger

log = get_logger("autostart")

_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


def _command() -> str:
    """当前运行方式对应的启动命令（静默托盘模式）。"""
    from .constants import APP_DIR
    if getattr(sys, "frozen", False):
        # 打包版：exe 本身是窗口程序（无控制台）
        return f'"{sys.executable}" --autostart'
    # 开发版：用 pythonw.exe（无控制台的解释器），避免开机弹出命令行窗口；
    # 找不到 pythonw 时退回 python.exe（会弹控制台，仅作兜底）
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    interpreter = pythonw if pythonw.exists() else Path(sys.executable)
    return f'"{interpreter}" "{APP_DIR / "main.py"}" --autostart'


def _open_run_key(mode: int):
    import winreg
    return winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY, 0, mode)


def is_enabled() -> bool:
    """是否已注册开机自启。"""
    if sys.platform != "win32":
        return False
    try:
        import winreg
        with _open_run_key(winreg.KEY_READ) as key:
            value, _ = winreg.QueryValueEx(key, "LightCast")
            return bool(value)
    except OSError:
        return False


def enable() -> bool:
    """注册开机自启。成功返回 True。"""
    if sys.platform != "win32":
        return False
    try:
        import winreg
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, _RUN_KEY) as key:
            winreg.SetValueEx(key, "LightCast", 0, winreg.REG_SZ, _command())
        log.info("已注册开机自启: %s", _command())
        return True
    except OSError as e:
        log.warning("注册开机自启失败: %s", e)
        return False


def disable() -> bool:
    """取消开机自启。成功返回 True。"""
    if sys.platform != "win32":
        return False
    try:
        import winreg
        with _open_run_key(winreg.KEY_SET_VALUE) as key:
            winreg.DeleteValue(key, "LightCast")
        log.info("已取消开机自启")
        return True
    except FileNotFoundError:
        return True  # 本来就没有
    except OSError as e:
        log.warning("取消开机自启失败: %s", e)
        return False
