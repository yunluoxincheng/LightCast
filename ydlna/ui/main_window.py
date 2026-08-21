"""主窗口：MSFluentWindow 框架，组合三个子页面。"""
from __future__ import annotations

from typing import TYPE_CHECKING

from PySide6.QtCore import QEvent, QObject, QRect, Qt, QTimer, Signal
from PySide6.QtGui import QCloseEvent, QIcon
from qfluentwidgets import (
    FluentIcon as FIF,
    MSFluentWindow,
    NavigationItemPosition,
)

from ..config import Config
from ..constants import APP_DISPLAY_NAME
from ..i18n import tr, Translator
from ..logger import get_logger
from .home_interface import HomeInterface
from ..player.player_interface import PlayerInterface
from .settings_interface import SettingsInterface

if TYPE_CHECKING:
    from ..dlna.server import DlnaServer
    from ..player.mpv_player import Player

log = get_logger("ui.main")

DEFAULT_WINDOW_SIZE = (1200, 800)
WINDOW_GEOMETRY_VERSION_KEY = "window_geometry_v8"

# 开机自启时窗口从未显示，Qt 首次 polish 曾把它压到 minimumSize；无边框
# 外框保存出的实际 geometry 约为 914×614。只迁移这一小段异常范围，避免
# 再次无条件清除用户正常拖拽得到的较大自定义尺寸。
_COLLAPSED_GEOMETRY_MAX = (930, 630)
# 无边框窗口必须保留可拖动的顶部区域；仅窗口内容/右下角可见无法移回屏幕。
_TITLE_BAR_HEIGHT = 48
_MIN_VISIBLE_TITLE_BAR_SIZE = (80, 24)


def _parse_geometry(value: object) -> tuple[int, int, int, int] | None:
    if not isinstance(value, list) or len(value) != 4:
        return None
    try:
        x, y, width, height = (int(item) for item in value)
    except (TypeError, ValueError):
        return None
    if width <= 0 or height <= 0:
        return None
    return x, y, width, height


def _is_geometry_visible(
    geometry: tuple[int, int, int, int],
    available_geometries: list[tuple[int, int, int, int]],
    title_bar_button_width: int,
) -> bool:
    """保存窗口的顶部拖动区域在任一屏幕工作区内仍可实际操作。"""
    x, y, width, _height = geometry
    draggable_width = max(0, width - max(0, title_bar_button_width))
    draggable_title_bar = QRect(x, y, draggable_width, _TITLE_BAR_HEIGHT)
    for screen_geometry in available_geometries:
        visible_title_bar = draggable_title_bar.intersected(QRect(*screen_geometry))
        if (
            visible_title_bar.width() >= _MIN_VISIBLE_TITLE_BAR_SIZE[0]
            and visible_title_bar.height() >= _MIN_VISIBLE_TITLE_BAR_SIZE[1]
        ):
            return True
    return False


def _geometry_to_restore(
    config: Config,
    available_geometries: list[tuple[int, int, int, int]] | None = None,
    title_bar_button_width: int = 0,
) -> tuple[int, int, int, int] | None:
    """返回可恢复几何，并清理已知污染或已离开所有屏幕的尺寸位置。"""
    geom = _parse_geometry(config.get("window_geometry"))
    if not config.get(WINDOW_GEOMETRY_VERSION_KEY, False):
        if (
            geom is not None
            and geom[2] <= _COLLAPSED_GEOMETRY_MAX[0]
            and geom[3] <= _COLLAPSED_GEOMETRY_MAX[1]
        ):
            config.set("window_geometry", None, persist=False)
            geom = None
        config.set(WINDOW_GEOMETRY_VERSION_KEY, True)
    if (
        geom is not None
        and available_geometries is not None
        and not _is_geometry_visible(
            geom,
            available_geometries,
            title_bar_button_width,
        )
    ):
        config.set("window_geometry", None)
        return None
    return geom


def _available_screen_geometries() -> list[tuple[int, int, int, int]]:
    """读取全部屏幕的可用工作区（排除任务栏等系统保留区域）。"""
    from PySide6.QtWidgets import QApplication

    return [screen.availableGeometry().getRect() for screen in QApplication.screens()]


def _visible_title_bar_button_width(title_bar: QWidget) -> int:
    """返回窗口显示后会占用右侧拖动区的标题栏按钮总宽度。"""
    from qframelesswindow import TitleBarButton

    # MainWindow 构造期间父窗口尚未 show()，此时 isVisible() 恒为 False；
    # isHidden() 能区分按钮自身是否被明确隐藏，与 TitleBarBase 显示后的语义一致。
    return sum(
        button.width()
        for button in title_bar.findChildren(TitleBarButton)
        if not button.isHidden()
    )


class _StartupGeometryGuard(QObject):
    """首次 Show 完成 Qt polish 后，重新应用启动窗口几何。"""

    def __init__(
        self,
        window: QWidget,
        geometry: tuple[int, int, int, int] | None,
    ) -> None:
        super().__init__(window)
        self._window = window
        self._geometry = geometry
        self._pending = True
        window.installEventFilter(self)

    @property
    def can_save(self) -> bool:
        """首次显示校正完成后，窗口几何才可持久化。"""
        return not self._pending

    def eventFilter(self, watched, event) -> bool:  # noqa: ANN001, N802
        if (
            watched is self._window
            and event.type() == QEvent.Type.Show
            and self._pending
        ):
            QTimer.singleShot(0, self._apply)
        return False

    def _apply(self) -> None:
        if not self._pending:
            return
        if self._geometry is None:
            self._window.resize(*DEFAULT_WINDOW_SIZE)
        else:
            self._window.setGeometry(*self._geometry)
        self._pending = False


class MainWindow(MSFluentWindow):
    """主窗口。"""

    # 用户关闭窗口时（用于决定是否最小化到托盘）
    closing = Signal()
    # 托盘退出或系统关闭必须先让 app.run() 完成异步清理，不能直接停 Qt loop。
    quitRequested = Signal()

    def __init__(
        self,
        player: "Player",
        server: "DlnaServer",
        config: Config,
        parent=None,
    ) -> None:  # noqa: ANN001
        super().__init__(parent)
        # 禁用 Mica 背景特效：全屏/还原切换时 DWM 重建表面，Mica 失效会
        # 回退到系统浅色背景（表现为"退出全屏瞬间整体闪浅色"）。
        # 纯 QSS 背景在两种状态下表现一致，无闪烁
        try:
            self.setMicaEffectEnabled(False)
        except Exception:  # noqa: BLE001
            pass
        # 窗口不透明绘制：resize（全屏切换）时不做系统清屏，消除白闪帧
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent, True)
        # 窗口就绪后把 DWM 帧强制为沉浸式深色（不随系统浅色主题变白）
        QTimer.singleShot(100, self._apply_dark_dwm_frame)
        self._player = player
        self._server = server
        self._config = config
        self._minimize_hint_shown = bool(config.get("minimize_hint_shown", False))

        # 三个子页面
        self.homeInterface = HomeInterface(player, server, self)
        self.playerInterface = PlayerInterface(player, self)
        self.settingsInterface = SettingsInterface(config, player, self)

        self.addSubInterface(self.homeInterface, FIF.HOME, tr("nav.home"))
        self.addSubInterface(self.playerInterface, FIF.VIDEO, tr("nav.player"))
        self.addSubInterface(
            self.settingsInterface, FIF.SETTING, tr("nav.settings"),
            position=NavigationItemPosition.BOTTOM,
        )

        self.setWindowTitle(APP_DISPLAY_NAME)
        # Qt 的窗口几何使用逻辑像素；直接指定固定尺寸，不能再除以 DPR。
        # 旧实现以 1500×1000 除缩放率，150% 下会变成 1000×666。
        self.resize(*DEFAULT_WINDOW_SIZE)
        self.setMinimumSize(900, 600)
        geom = _geometry_to_restore(
            config,
            _available_screen_geometries(),
            _visible_title_bar_button_width(self.titleBar),
        )
        if geom is not None:
            self.setGeometry(*geom)
        self._startup_geometry_guard = _StartupGeometryGuard(self, geom)

        # 设置图标（若有）
        icon = self._load_icon()
        if not icon.isNull():
            self.setWindowIcon(icon)

        # 页面切换的原生窗口 hide/show 由 PlayerInterface 的
        # showEvent/hideEvent 自行处理（QStackedWidget 切换会触发这两个事件）

        Translator.instance().languageChanged.connect(self._on_language_changed)

    def _load_icon(self) -> QIcon:
        from ..constants import ASSETS_DIR
        for name in ("icon.ico", "icon.png", "logo.png"):
            p = ASSETS_DIR / name
            if p.exists():
                return QIcon(str(p))
        return QIcon()

    def _apply_dark_dwm_frame(self) -> None:
        """DWM 沉浸式深色模式：窗口边框/扩展帧不随系统浅色主题变白。

        qframelesswindow 的无边框窗口保留 DWM 绘制的缩放边框，颜色跟随
        Windows 主题——系统为浅色时全屏切换会闪白边框。此调用在窗口
        句柄上强制深色帧（属性持久，全屏切换不失效）。
        """
        try:
            import ctypes
            from ctypes import wintypes
            hwnd = wintypes.HWND(int(self.winId()))
            DWMWA_USE_IMMERSIVE_DARK_MODE = 20
            value = ctypes.c_int(1)
            ctypes.windll.dwmapi.DwmSetWindowAttribute(
                hwnd, DWMWA_USE_IMMERSIVE_DARK_MODE,
                ctypes.byref(value), ctypes.sizeof(value),
            )
            log.info("已应用 DWM 沉浸式深色帧")
        except Exception as e:  # noqa: BLE001
            log.debug("设置 DWM 深色帧失败: %s", e)

    # ------------------------------------------------------------------ #
    def switch_to_player(self) -> None:
        """切到播放器页面（投屏开始时调用）。"""
        try:
            # MSFluentWindow 通过 navigationInterface 切换
            self.switchTo(self.playerInterface)
        except Exception:  # noqa: BLE001
            self.playerInterface.show()

    # ------------------------------------------------------------------ #
    # 全屏（隐藏导航栏 + 标题栏 + 播放器页 header，播放器页占满）
    # ------------------------------------------------------------------ #
    def toggle_fullscreen(self) -> None:
        if self.isFullScreen():
            self.showNormal()
            self.navigationInterface.show()
            if hasattr(self, "titleBar"):
                self.titleBar.show()
            # 恢复播放器页 header（标题栏）
            self.playerInterface.header.show()
            # 恢复 48px 顶部边距（MSFluentWindow 给悬浮标题栏留的位置）
            self.hBoxLayout.setContentsMargins(0, 48, 0, 0)
            self.playerInterface.on_fullscreen_changed(False)
        else:
            # 先切到播放器页
            self.switch_to_player()
            self.navigationInterface.hide()
            if hasattr(self, "titleBar"):
                self.titleBar.hide()
            # 隐藏播放器页 header（避免全屏时露出标题/按钮）
            self.playerInterface.header.hide()
            # 标题栏是浮层（不在布局里），布局靠固定 48px 上边距让位；
            # 全屏隐藏标题栏后必须清掉这个边距，否则顶部残留 48px 空条
            self.hBoxLayout.setContentsMargins(0, 0, 0, 0)
            self.showFullScreen()
            self.playerInterface.on_fullscreen_changed(True)
        self.playerInterface._position_overlays()

    def refresh_device_info(self, name: str, ip: str) -> None:
        self.homeInterface.update_device_info(name, ip)

    # ------------------------------------------------------------------ #
    def _set_nav_text(self, widget: QWidget, text: str) -> None:  # noqa: ANN001
        """设置左侧导航项的文本（按 widget 的 objectName 查找）。"""
        try:
            btn = self.navigationInterface.items.get(widget.objectName())
            if btn is not None and hasattr(btn, "setText"):
                btn.setText(text)
                btn.update()
        except Exception:  # noqa: BLE001
            pass

    def _on_language_changed(self, _code: str) -> None:
        # 刷新导航项文本
        self._set_nav_text(self.homeInterface, tr("nav.home"))
        self._set_nav_text(self.playerInterface, tr("nav.player"))
        self._set_nav_text(self.settingsInterface, tr("nav.settings"))
        # 刷新各子页面文案
        self.retranslate_ui()

    def retranslate_ui(self) -> None:
        for child in (self.homeInterface, self.playerInterface, self.settingsInterface):
            if hasattr(child, "retranslate_ui"):
                child.retranslate_ui()

    # ------------------------------------------------------------------ #
    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802
        """关闭窗口时最小化到托盘（首次弹提示，可勾选「不再提示」）。

        系统触发的关闭（Windows 关机/注销、安装器 Restart Manager 的
        关闭请求）不走托盘逻辑：保存几何后直接退出进程——否则应用会
        赖在托盘不退出，安装器无法覆盖升级 exe（只能等强杀）。
        """
        # 开机自启后从未显示过的窗口可能仍处于 Qt 隐藏布局的临时最小尺寸；
        # 只有首次显示校正完成后，当前 geometry 才代表用户真正看到的窗口。
        if self._startup_geometry_guard.can_save:
            g = self.geometry()
            self._config.set("window_geometry", [g.x(), g.y(), g.width(), g.height()])

        if event.spontaneous():
            # 系统/安装器触发的关闭：优雅退出（不弹最小化提示，不残留进程）
            log.info("系统触发的关闭（关机/注销/安装器关闭请求），直接退出")
            self.quitRequested.emit()
            event.accept()
            return

        if not self._minimize_hint_shown:
            try:
                self._ask_minimize_to_tray()
            except Exception as e:  # noqa: BLE001
                # 弹窗失败绝不能把应用卡死：保持窗口可见并记录
                log.warning("最小化提示弹窗异常: %s", e)
            event.ignore()
        else:
            # 已经提示过（或用户勾选「不再提示」），直接最小化
            self.hide()
            self.closing.emit()
            event.ignore()

    def _ask_minimize_to_tray(self) -> None:
        """弹「最小化到托盘」提示（带「不再提示」复选框）。

        使用 Qt 原生 QMessageBox + 非阻塞 open()：不嵌套事件循环，
        彻底避免模态对话框把应用卡死（此前 qfluentwidgets MessageBox
        的 exec() 在打包版中点击按钮会冻结）。
        """
        from PySide6.QtWidgets import QCheckBox, QMessageBox
        box = QMessageBox(self)
        box.setWindowTitle(tr("dialog.minimize_to_tray.title"))
        box.setIcon(QMessageBox.Icon.Information)
        box.setText(tr("dialog.minimize_to_tray.body"))
        dont_ask = QCheckBox(tr("dialog.minimize_to_tray.dont_ask"), box)
        box.setCheckBox(dont_ask)
        ok_btn = box.addButton(tr("common.ok"), QMessageBox.ButtonRole.AcceptRole)
        box.addButton(tr("common.cancel"), QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(ok_btn)
        box.finished.connect(
            lambda _result: self._on_minimize_answered(box, dont_ask, ok_btn)
        )
        # 关闭后销毁弹窗对象（否则每次点 X 都会累积一个隐藏的 QMessageBox）
        box.finished.connect(box.deleteLater)
        box.open()

    def _on_minimize_answered(self, box, dont_ask, ok_btn) -> None:  # noqa: ANN001
        """弹窗关闭后的回调：按勾选状态决定「记住」与「是否最小化」。"""
        if dont_ask.isChecked():
            self._config.set("minimize_hint_shown", True)
            self._minimize_hint_shown = True
            log.info("用户选择不再提示「最小化到托盘」")
        if box.clickedButton() is ok_btn:
            self.hide()
            self.closing.emit()
