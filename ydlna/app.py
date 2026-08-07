"""应用编排：组装 Player + DlnaServer + MainWindow + Tray。

由 main.py 调用 run()，负责：
- 应用全局设置（语言、主题、日志）
- 实例化各组件并连接
- 启动 DLNA 服务
- 把 DLNA 投屏事件桥接到 UI（切到播放器页等）
- 应用退出时的清理
"""
from __future__ import annotations

import asyncio
import sys
from typing import TYPE_CHECKING

from PySide6.QtCore import QObject, QTimer, Signal
from qfluentwidgets import Theme, setTheme, setThemeColor

from .config import Config
from .constants import APP_NAME
from .dlna.renderer_bridge import RendererBridge
from .dlna.server import DlnaServer, get_local_ip
from .i18n import Translator, set_language
from .logger import get_logger, setup_logging
from .player.mpv_player import Player, is_available

log = get_logger("app")


def _apply_theme(config: Config) -> None:
    """根据 config 应用主题。"""
    theme = config.get("theme", "auto")
    theme_map = {"light": Theme.LIGHT, "dark": Theme.DARK, "auto": Theme.AUTO}
    setTheme(theme_map.get(theme, Theme.AUTO))
    setThemeColor("#0078d4")  # Win11 蓝


def _apply_language(config: Config) -> None:
    code = config.get("language", "zh")
    set_language(code)


class _AppSignals(QObject):
    """跨模块的 Qt 信号总线（用于 DLNA 投屏 → UI 切换）。"""

    # 收到新的媒体投屏：(title, url)
    castReceived = Signal(str, str)


async def run() -> int:
    """主协程。在 qasync 的 QEventLoop 中运行。"""
    from PySide6.QtWidgets import QApplication
    from .ui.main_window import MainWindow
    from .ui.tray import TrayIcon

    app = QApplication.instance() or QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setQuitOnLastWindowClosed(False)  # 关闭主窗口不退出（最小化到托盘）

    config = Config.instance()
    setup_logging()
    _apply_language(config)
    _apply_theme(config)

    # libmpv 缺失检测
    if not is_available():
        log.error("libmpv 不可用")
        from qfluentwidgets import MessageBox
        from .i18n import tr
        box = MessageBox(
            tr("dialog.dll_missing.title"),
            tr("dialog.dll_missing.body") + "\n\n" + tr("dialog.dll_missing.detail"),
            None,
        )
        box.yesButton.setText(tr("common.ok"))
        box.cancelButton.hide()
        box.exec()
        return 1

    # 核心组件
    player = Player()
    # 应用保存的音量
    player.set_volume(int(config.get("volume", 80)))
    player.set_mute(bool(config.get("muted", False)))
    # 音频输出设备（attach 时应用）
    player.set_audio_device(config.get("audio_device", ""))

    bridge = RendererBridge(player)
    server = DlnaServer(bridge, config)

    # UI
    window = MainWindow(player, server, config)
    tray = TrayIcon(player, window)

    # 状态总线
    signals = _AppSignals()

    # ---- 连接 ----
    # 投屏到达（SetAVTransportURI）→ 立即进播放器页 + 缓冲动画，不等解码。
    # 后续 mediaChanged（解码完成）会再触发一次 on_cast 保证窗口置顶/控制栏
    def on_cast_started() -> None:
        log.info("投屏到达，立即进入播放器页并开始缓冲")
        window.show()
        window.raise_()
        window.activateWindow()
        window.switch_to_player()
        window.playerInterface.show_buffering(override=True)

    bridge.on_cast_started = on_cast_started

    # DLNA 投屏到达 → 切到播放器页（内嵌渲染区）
    def on_cast(title: str, url: str) -> None:
        log.info("投屏到达: %s", url)
        window.show()
        window.raise_()
        window.activateWindow()
        window.switch_to_player()
        window.playerInterface._show_controls()
        signals.castReceived.emit(title, url)

    # player 的 mediaChanged 同时通知投屏到达
    player.signals.mediaChanged.connect(on_cast)

    # 保存音量/静音
    player.signals.volumeChanged.connect(lambda v: config.set("volume", v))
    player.signals.muteChanged.connect(lambda m: config.set("muted", m))

    # 主页启停按钮
    async def toggle_service(start: bool) -> None:  # noqa: ANN202
        if start:
            await server.async_start()
        else:
            await server.async_stop()
        window.homeInterface.set_service_running(server.running)
        if server.running:
            window.refresh_device_info(
                config.get("friendly_name", "轻投"),
                get_local_ip(),
            )

    def _toggle_service_wrapper(start: bool) -> None:
        asyncio.create_task(toggle_service(start))

    window.homeInterface.toggleServiceRequested.connect(_toggle_service_wrapper)

    # 托盘菜单动作
    tray.actShow.triggered.connect(lambda: (window.show(), window.raise_()))
    tray.actPlayPause.triggered.connect(player.play_pause)
    tray.actStop.triggered.connect(player.stop)
    tray.actQuit.triggered.connect(app.quit)

    # 播放器页的全屏请求 → 主窗口全屏（隐藏导航栏）
    window.playerInterface.toggleFullscreenRequested.connect(window.toggle_fullscreen)

    # 显示窗口 + 托盘
    window.show()
    tray.show()

    # 关键：先切到播放器页让 MpvWidget 显示并拿到 HWND，再 attach mpv。
    def _ensure_mpv_ready():
        # 确保渲染区已显示（winId 才有效）；切回初始页（默认主页）
        window.switch_to_player()
        window.playerInterface._on_page_shown()
        ok = window.playerInterface.mpvWidget.attach_player()
        if ok:
            log.info("mpv 已就绪（投屏可立即播放）")
            # 切回主页，等投屏到达再自动切到播放器页
            try:
                window.switchTo(window.homeInterface)
            except Exception:  # noqa: BLE001
                pass
        else:
            QTimer.singleShot(300, _ensure_mpv_ready)

    QTimer.singleShot(100, _ensure_mpv_ready)

    # 初始设备信息
    window.refresh_device_info(
        config.get("friendly_name", "轻投"),
        get_local_ip(),
    )

    # 开机自动启动服务
    if config.get("dlna_enabled", True):
        log.info("自动启动 DLNA 服务")
        await server.async_start()
        window.homeInterface.set_service_running(server.running)

    # 等待退出
    stop_event = asyncio.Event()
    app.aboutToQuit.connect(stop_event.set)
    await stop_event.wait()

    # 清理
    log.info("应用退出，清理资源")
    try:
        await server.async_stop()
    except Exception as e:  # noqa: BLE001
        log.warning("停止服务异常: %s", e)
    player.shutdown()

    return 0
