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
from typing import Any

from PySide6.QtCore import QObject, QTimer, Signal
from qfluentwidgets import Theme, setTheme, setThemeColor

from .async_tasks import BackgroundTasks
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


async def _change_service_state(
    server: DlnaServer,
    window: Any,
    config: Config,
    start: bool,
) -> None:
    """启停 DLNA 服务，并确保失败/取消时资源与 UI 状态一致。"""
    try:
        if start:
            await server.async_start()
        else:
            await server.async_stop()
    except asyncio.CancelledError:
        await server.async_stop()
        raise
    except Exception as e:  # noqa: BLE001
        log.exception("%s DLNA 服务失败: %s", "启动" if start else "停止", e)
        try:
            await server.async_stop()
        except Exception as cleanup_error:  # noqa: BLE001
            log.warning("清理 DLNA 服务失败: %s", cleanup_error)
    finally:
        window.homeInterface.set_service_running(server.running)
        if server.running:
            window.refresh_device_info(
                config.get("friendly_name", "轻投"),
                get_local_ip(),
            )


async def _start_configured_service(
    server: DlnaServer,
    window: Any,
    config: Config,
) -> None:
    """按配置自动启动服务，并复用手动启停的失败清理路径。"""
    if not config.get("dlna_enabled", True):
        return
    log.info("自动启动 DLNA 服务")
    await _change_service_state(server, window, config, True)


def _connect_shutdown_requests(
    tray: Any,
    window: Any,
    stop_event: asyncio.Event,
) -> None:
    """把退出入口转换为异步停止请求，不提前终止 qasync 事件循环。"""
    tray.actQuit.triggered.connect(lambda *_args: stop_event.set())
    window.quitRequested.connect(stop_event.set)
    window.settingsInterface.applicationQuitRequested.connect(stop_event.set)


async def run() -> int:
    """主协程。在 qasync 的 QEventLoop 中运行。"""
    from PySide6.QtWidgets import QApplication
    from .ui.main_window import MainWindow
    from .ui.tray import TrayIcon

    app = QApplication.instance() or QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setQuitOnLastWindowClosed(False)  # 关闭主窗口不退出（最小化到托盘）

    # 开机自启（--autostart）：静默启动到托盘，不弹主窗口
    start_hidden = "--autostart" in sys.argv
    if start_hidden:
        log.info("开机自启启动（静默托盘模式）")

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
    background_tasks = BackgroundTasks()
    stop_event = asyncio.Event()

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
    service_task: asyncio.Task[None] | None = None

    def _toggle_service_wrapper(start: bool) -> None:
        nonlocal service_task
        if service_task is not None and not service_task.done():
            log.debug("DLNA 服务正在切换状态，忽略重复请求")
            return
        service_task = background_tasks.create(
            _change_service_state(server, window, config, start),
            name="toggle-dlna-service",
        )

    window.homeInterface.toggleServiceRequested.connect(_toggle_service_wrapper)

    # 托盘菜单动作
    tray.actShow.triggered.connect(lambda: (window.show(), window.raise_()))
    tray.actPlayPause.triggered.connect(player.play_pause)
    tray.actStop.triggered.connect(player.stop)
    _connect_shutdown_requests(tray, window, stop_event)

    # 播放器页的全屏请求 → 主窗口全屏（隐藏导航栏）
    window.playerInterface.toggleFullscreenRequested.connect(window.toggle_fullscreen)

    # 显示窗口 + 托盘（自启静默模式只出托盘，主窗口等投屏到达/点托盘再弹）
    if not start_hidden:
        window.show()
    tray.show()

    # 关键：先切到播放器页让 MpvWidget 显示并拿到 HWND，再 attach mpv。
    def _ensure_mpv_ready():
        # 确保渲染区已拿到原生窗口句柄；切回初始页（默认主页）
        window.switch_to_player()
        page = window.playerInterface
        if page.isVisible():
            page._on_page_shown()
        else:
            # 自启静默模式：窗口隐藏，强制创建 HWND 并 attach，
            # 投屏到达时窗口弹出即可直接出画面
            page.mpvWidget.winId()
            ok = page.mpvWidget.attach_player()
            if ok:
                log.info("mpv 已就绪（隐藏窗口，投屏可立即播放）")
                # 切回主页，等投屏到达再自动切到播放器页
                try:
                    window.switchTo(window.homeInterface)
                except Exception:  # noqa: BLE001
                    pass
                return
            QTimer.singleShot(300, _ensure_mpv_ready)
            return
        ok = page.mpvWidget.attach_player()
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

    # 自动更新检查（默认开启，可在设置中关闭；失败静默不打扰）
    def _startup_update_check() -> None:
        if not config.get("auto_update", True):
            return
        from .updater import check_for_update, run_update_flow

        async def _do() -> None:
            try:
                info = await check_for_update()
            except Exception as e:  # noqa: BLE001
                log.warning("自动检查更新失败: %s", e)
                return
            if info is not None:
                log.info("发现新版本 v%s", info.version)
                # 静默托盘模式下提示框不能挂在隐藏窗口上
                parent = window if window.isVisible() else None
                installer_started = await run_update_flow(
                    parent, info,
                    use_mirror=bool(config.get("update_mirror", True)),
                )
                if installer_started:
                    stop_event.set()

        background_tasks.create(_do(), name="startup-update-check")

    QTimer.singleShot(4000, _startup_update_check)

    # 初始设备信息
    window.refresh_device_info(
        config.get("friendly_name", "轻投"),
        get_local_ip(),
    )

    # 开机自动启动服务
    await _start_configured_service(server, window, config)

    # 等待退出
    await stop_event.wait()

    # 清理
    log.info("应用退出，清理资源")
    await background_tasks.cancel_all()
    await window.settingsInterface.shutdown()
    try:
        await server.async_stop()
    except Exception as e:  # noqa: BLE001
        log.warning("停止服务异常: %s", e)
    await bridge.shutdown_all()
    player.shutdown()

    return 0
