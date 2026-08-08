"""播放器页面（内嵌 mpv 渲染区 + 悬浮控制栏）。

架构
----
- MpvWidget（原生窗口）嵌入本页面，占主体
- ControlBar（独立浮层窗口）悬浮在页面底部内侧——不占布局（画面比例不变）、
  独立窗口盖在原生渲染区之上（可点击）
- 页面切换处理：本页 hide 时强制 mpvWidget.hide()（防止原生窗口刺穿到其它
  导航页造成 UI 残留）；show 时恢复

投屏到达时由 app.py 切换到本页。
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import (
    QFrame,
    QGraphicsDropShadowEffect,
    QHBoxLayout,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from qfluentwidgets import (
    BodyLabel,
    CardWidget,
    FluentIcon as FIF,
    IconWidget,
    InfoBar,
    InfoBarPosition,
    StrongBodyLabel,
    SubtitleLabel,
    TitleLabel,
)

from ..i18n import tr, Translator
from ..logger import get_logger
from .control_bar import ControlBar
from .mpv_player import Player
from .mpv_widget import MpvWidget

if TYPE_CHECKING:
    pass

log = get_logger("ui.player")


class _Spinner(QWidget):
    """旋转缓冲指示器：QPainter 画一段圆弧，定时器驱动旋转。"""

    _STEP = 12  # 每帧旋转角度（30ms 一帧 → 1 圈 0.9s）

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setFixedSize(36, 36)
        self._angle = 0
        self._timer = QTimer(self)
        self._timer.setInterval(30)
        self._timer.timeout.connect(self._advance)
        self._timer.start()

    def _advance(self) -> None:
        self._angle = (self._angle + self._STEP) % 360
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802, ANN001
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        pen = QPen(QColor("#0078d4"), 3)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        p.setPen(pen)
        # 270° 圆弧随角度旋转（加载转圈效果）
        p.drawArc(self.rect().adjusted(5, 5, -5, -5), -self._angle * 16, 270 * 16)
        p.end()


class BufferingOverlay(QWidget):
    """缓冲提示卡片：spinner + 「正在缓冲…」，悬浮在渲染区中央。

    投屏到达立即显示（解码在后台进行），解码完成或失败后隐藏。
    """

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setFixedSize(200, 110)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet(
            "BufferingOverlay { background: rgba(18, 18, 18, 0.88);"
            " border: 1px solid #2a2a2a; border-radius: 12px; }"
            "BufferingOverlay QLabel { background: transparent; color: #e0e0e0; }"
        )
        lay = QVBoxLayout(self)
        lay.setContentsMargins(16, 14, 16, 14)
        lay.setSpacing(8)
        self.spinner = _Spinner(self)
        self.label = BodyLabel("", self)
        self.label.setAlignment(Qt.AlignCenter)
        lay.addWidget(self.spinner, 0, Qt.AlignCenter)
        lay.addWidget(self.label)
        self.hide()

    def set_text(self, text: str) -> None:
        self.label.setText(text)

# 播放失败技术细节 → 友好原因映射（按关键词，顺序敏感：403 优先于通用网络）
_HINT_RULES: tuple[tuple[tuple[str, ...], str], ...] = (
    (("403", "410", "forbidden"), "player.error.hint.forbidden"),
    (("404", "not found"), "player.error.hint.not_found"),
    (("decrypt", "cipher", "aes-128", "16 bytes", "key uri", "key file"),
     "player.error.hint.key"),
    (("invalid data", "failed to recognize", "could not find codec",
      "unknown format", "unrecognized"), "player.error.hint.invalid"),
    (("timed out", "timeout", "connection"), "player.error.hint.network"),
)


def _friendly_hint(detail: str) -> str:
    """把 mpv 错误细节映射成可读原因；不匹配返回空串。"""
    if not detail:
        return ""
    d = detail.lower()
    for needles, key in _HINT_RULES:
        if any(n in d for n in needles):
            return tr(key)
    return ""


class PlayerInterface(QWidget):
    """播放器页面（内嵌渲染区 + 悬浮控制栏）。"""

    stateChanged = Signal(str)

    def __init__(self, player: Player, parent=None) -> None:  # noqa: ANN001
        super().__init__(parent)
        self.setObjectName("player-interface")
        self._player = player
        self._build_ui()
        self._connect()
        self._retranslate()
        Translator.instance().languageChanged.connect(self._retranslate)

    # ------------------------------------------------------------------ #
    # UI
    # ------------------------------------------------------------------ #
    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # 顶部信息条（媒体标题 / 空状态提示）
        self.header = QFrame(self)
        self.header.setObjectName("playerHeader")
        self.header.setStyleSheet(
            "#playerHeader { background: #1a1a1a; border-bottom: 1px solid #2a2a2a; }"
        )
        header_lay = QHBoxLayout(self.header)
        header_lay.setContentsMargins(20, 10, 20, 10)

        self.titleLabel = TitleLabel(tr("player.empty"))
        self.titleLabel.setStyleSheet("color: #e0e0e0; font-size: 16px;")
        header_lay.addWidget(self.titleLabel, 1)
        root.addWidget(self.header)

        # 中部：mpv 渲染区（占满）
        self.mpvWidget = MpvWidget(self._player, self)
        self.mpvWidget.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        root.addWidget(self.mpvWidget, 1)

        # 嵌入式控制栏（非全屏使用）：在布局内随窗口自然移动缩放，
        # 彻底消除悬浮窗口跟随抖动；媒体门控（投屏前不显示）
        self.embeddedBar = ControlBar(self._player, self, floating=False)
        root.addWidget(self.embeddedBar)
        self.embeddedBar.hide()

        # 空状态覆盖层（悬浮在渲染区之上，投屏后隐藏）
        self.emptyWidget = self._build_empty(self)
        self.emptyWidget.setGeometry(0, 0, 1, 1)  # 初始小几何，由 resizeEvent 校正
        self.emptyWidget.hide()

        # 缓冲动画浮层（投屏到达立即显示，解码完成/失败后隐藏）
        self.bufferOverlay = BufferingOverlay(self)
        self.bufferOverlay.setGeometry(0, 0, 1, 1)
        self.bufferOverlay.hide()
        # 投屏待缓冲标志：从「投屏到达」到「解码完成」期间强制保持浮层，
        # 挡住 paused-for-cache 观察器注册时的初始 False 异步回调（时序不定）
        self._buffering_override = False

        # 悬浮控制栏（全屏使用）：独立顶层窗口 + 锚定跟随
        # 关键：parent 必须在构造时传入——Qt.Tool | FramelessWindowHint 在构造时
        # 指定才保持"顶层工具窗口"身份；若先建后 setParent()，setParent 会剥离
        # Window 标志把它降级成普通子控件，几何会被父窗口裁剪（曾踩过这个坑）
        top = self.window()
        self.floatingBar = ControlBar(
            self._player, top if top is not self else None, floating=True
        )
        self.floatingBar.attach_to(self)
        for bar in (self.embeddedBar, self.floatingBar):
            bar.fullscreenRequested.connect(self._on_fullscreen_requested)
            bar.activity.connect(self._show_controls)
        self.mpvWidget.mouseActivity.connect(self._show_controls)
        self.mpvWidget.mouseDoubleClicked.connect(self._on_fullscreen_requested)
        # 单击画面 = 播放/暂停（双击全屏不受影响）
        self.mpvWidget.singleClicked.connect(self._player.play_pause)

        # 自动隐藏定时器
        self._hide_timer = QTimer(self)
        self._hide_timer.setSingleShot(True)
        self._hide_timer.setInterval(3000)
        self._hide_timer.timeout.connect(self._hide_controls)
        self._hide_timer.start()

        # 持续锚定定时器：事件驱动定位有盲区（窗口移动、页面切换时的瞬态
        # 布局都不会触发重新定位，控制栏会停留在旧位置甚至窗口外），
        # 这里周期刷新，保证控制栏始终贴住页面底部
        self._anchor_timer = QTimer(self)
        self._anchor_timer.setInterval(150)
        self._anchor_timer.timeout.connect(self._reanchor)
        self._anchor_timer.start()

    def _build_empty(self, parent) -> QWidget:  # noqa: ANN001
        w = QWidget(parent)
        w.setStyleSheet("background: #141414;")
        lay = QVBoxLayout(w)
        lay.setAlignment(Qt.AlignCenter)
        lay.setSpacing(12)

        icon = IconWidget(FIF.VIDEO, w)
        icon.setFixedSize(64, 64)
        self.emptyTitle = SubtitleLabel(tr("player.empty"))
        self.emptyTitle.setAlignment(Qt.AlignCenter)
        self.emptyHint = BodyLabel(tr("player.empty.hint"))
        self.emptyHint.setEnabled(False)
        self.emptyHint.setWordWrap(True)
        self.emptyHint.setAlignment(Qt.AlignCenter)

        lay.addWidget(icon, 0, Qt.AlignCenter)
        lay.addWidget(self.emptyTitle, 0, Qt.AlignCenter)
        lay.addWidget(self.emptyHint, 0, Qt.AlignCenter)
        return w

    def _connect(self) -> None:
        s = self._player.signals
        s.mediaChanged.connect(self._on_media_changed)
        s.stateChanged.connect(self._on_state_changed)
        s.playbackFailed.connect(self._on_playback_failed)
        s.bufferingChanged.connect(self._on_buffering_changed)

    # ------------------------------------------------------------------ #
    # 页面切换（关键：原生窗口的 hide/show 管理）
    # ------------------------------------------------------------------ #
    def showEvent(self, event) -> None:  # noqa: N802, ANN001
        super().showEvent(event)
        # 延迟到布局完成后恢复渲染区 + attach
        QTimer.singleShot(0, self._on_page_shown)

    def hideEvent(self, event) -> None:  # noqa: N802, ANN001
        super().hideEvent(event)
        # 关键：切到其它导航页时强制隐藏原生窗口，防止 z-order 刺穿残留
        self.mpvWidget.hide()
        self.floatingBar.hide()
        self.embeddedBar.hide()
        self._set_cursor_visible(True)

    def _on_page_shown(self) -> None:
        # 页面隐藏时直接返回：showEvent 的延迟回调可能在切回其它页后才执行
        # （启动时 _ensure_mpv_ready 先切播放器页再切回主页），此时不能
        # show 渲染区/控制栏，否则会残留到主页上
        if not self.isVisible():
            return
        self.mpvWidget.show()
        self.mpvWidget.attach_player()
        # 有媒体时显示播放画面，否则显示空状态；装载/网络缓冲中显示缓冲动画
        if self._player.get_loading() or self._player.get_buffering():
            self._hide_empty()
            self.show_buffering()
        elif self._player.get_duration() is None:
            self._show_empty()
        else:
            self._hide_empty()
        self._position_overlays()
        # 非全屏常驻显示控制栏；全屏且播放中才启动自动隐藏
        self._show_controls()
        # 让页面获得焦点以接收键盘快捷键
        self.setFocus()

    # ------------------------------------------------------------------ #
    # 覆盖层定位（空状态 + 控制栏）
    # ------------------------------------------------------------------ #
    def resizeEvent(self, event) -> None:  # noqa: N802, ANN001
        super().resizeEvent(event)
        self._position_overlays()

    def _position_overlays(self) -> None:
        """把空状态覆盖层铺满渲染区；控制栏按页面底部定位。"""
        # 空状态覆盖层 = 渲染区几何（含 header 下方）
        r = self.mpvWidget.geometry()
        self.emptyWidget.setGeometry(r)
        # 缓冲动画卡片居中悬浮在渲染区中央
        w, h = self.bufferOverlay.width(), self.bufferOverlay.height()
        self.bufferOverlay.setGeometry(
            r.x() + (r.width() - w) // 2,
            r.y() + (r.height() - h) // 2,
            w, h,
        )
        self.floatingBar.update_position()

    # ------------------------------------------------------------------ #
    # 控制栏显示/隐藏（双 bar 状态机 + 全屏光标隐藏）
    # ------------------------------------------------------------------ #
    def _has_media(self) -> bool:
        """是否已有可播放的媒体（成功投屏后才有）。"""
        return (
            self._player.get_duration() is not None
            or self._player.get_state() in ("playing", "paused")
        )

    def _sync_control_bars(self) -> None:
        """按当前模式（全屏/窗口）+ 媒体门控统一显隐两个控制栏（单一入口）。"""
        fs = self._is_fullscreen()
        media = self._has_media()
        if fs:
            self.embeddedBar.hide()
            if media:
                if not self.floatingBar.isVisible():
                    self.floatingBar.show()
                    self.floatingBar.update_position()
            else:
                self.floatingBar.hide()
        else:
            self.floatingBar.hide()
            if media:
                if not self.embeddedBar.isVisible():
                    self.embeddedBar.show()
            else:
                self.embeddedBar.hide()

    def _set_cursor_visible(self, visible: bool) -> None:
        """全屏时隐藏鼠标（mpvWidget 继承页面光标）。"""
        if visible:
            self.unsetCursor()
        else:
            self.setCursor(Qt.BlankCursor)

    def _show_controls(self) -> None:
        # 页面不可见（切到其它页的残留调用）或无媒体（尚未投屏）时不显示
        if not self.isVisible() or not self._has_media():
            return
        self._sync_control_bars()
        self._set_cursor_visible(True)
        # 非全屏时控制栏常驻（不启动隐藏计时）；全屏时才自动隐藏
        if self._is_fullscreen():
            self._hide_timer.start()

    def _hide_controls(self) -> None:
        # 仅全屏时自动隐藏：隐藏悬浮条 + 隐藏鼠标（动一下即恢复）
        if self._is_fullscreen() and self._player.get_state() == "playing":
            self.floatingBar.hide()
            self._set_cursor_visible(False)

    def _reanchor(self) -> None:
        """周期跟随：页面可见且悬浮条可见时刷新其位置。"""
        if self.isVisible() and self.floatingBar.isVisible():
            self.floatingBar.update_position()

    def _is_fullscreen(self) -> bool:
        win = self.window()
        return win is not None and win.isFullScreen()

    def on_fullscreen_changed(self, is_fullscreen: bool) -> None:
        """全屏状态变化时同步控制栏（由 MainWindow 调用）。

        进全屏：_show_controls 完成「嵌入式条 → 悬浮条」切换并启动隐藏计时；
        退全屏：停止计时、恢复鼠标、切回嵌入式条。
        """
        if is_fullscreen:
            # 内部会同步双 bar 并在全屏时启动自动隐藏计时
            self._show_controls()
        else:
            self._hide_timer.stop()
            self._set_cursor_visible(True)
            self._show_controls()

    def _show_empty(self) -> None:
        self.emptyWidget.show()
        self.emptyWidget.raise_()

    def _hide_empty(self) -> None:
        self.emptyWidget.hide()

    # ------------------------------------------------------------------ #
    # 缓冲动画（投屏到达 / mpv 装载中 / 网络缓冲中显示）
    # ------------------------------------------------------------------ #
    def show_buffering(self, *, override: bool = False) -> None:
        """显示缓冲动画（投屏到达后立即调用，不等解码）。

        override=True：投屏待缓冲，强制保持浮层直到解码完成/失败
        （挡住 paused-for-cache 初始 False 的异步回调时序问题）。
        """
        if override:
            self._buffering_override = True
        self._hide_empty()
        if not self.bufferOverlay.isVisible():
            self.bufferOverlay.show()
            self.bufferOverlay.raise_()
            self._position_overlays()

    def hide_buffering(self) -> None:
        self._buffering_override = False
        if self.bufferOverlay.isVisible():
            self.bufferOverlay.hide()

    def _on_buffering_changed(self, buffering: bool) -> None:
        if buffering:
            self.show_buffering()
        elif not self._player.get_loading() and not self._buffering_override:
            # 装载中或投屏待缓冲时不隐藏，避免缓冲动画闪没
            self.hide_buffering()

    # ------------------------------------------------------------------ #
    # 全屏请求（由 MainWindow/app 处理：主窗口全屏 + 隐藏导航）
    # ------------------------------------------------------------------ #
    def _on_fullscreen_requested(self) -> None:
        self.toggleFullscreenRequested.emit()

    toggleFullscreenRequested = Signal()

    # ------------------------------------------------------------------ #
    # 键盘/鼠标快捷键
    # ------------------------------------------------------------------ #
    def keyPressEvent(self, event) -> None:  # noqa: N802, ANN001
        from PySide6.QtGui import QKeyEvent
        if not isinstance(event, QKeyEvent):
            return super().keyPressEvent(event)
        self._show_controls()
        key = event.key()
        if key == Qt.Key_Escape:
            self.toggleFullscreenRequested.emit()  # MainWindow 会判断是否退出全屏
        elif key == Qt.Key_F:
            self.toggleFullscreenRequested.emit()
        elif key == Qt.Key_Space:
            self._player.play_pause()
        elif key == Qt.Key_Right:
            self._player.seek_relative(10)
        elif key == Qt.Key_Left:
            self._player.seek_relative(-10)
        elif key == Qt.Key_Up:
            self._player.set_volume(min(100, self._player.get_volume() + 5))
        elif key == Qt.Key_Down:
            self._player.set_volume(max(0, self._player.get_volume() - 5))
        else:
            super().keyPressEvent(event)

    def mousePressEvent(self, event) -> None:  # noqa: N802, ANN001
        # 点击页面获得键盘焦点（快捷键才能生效）
        self.setFocus()
        self._show_controls()
        super().mousePressEvent(event)

    # ------------------------------------------------------------------ #
    # 槽
    # ------------------------------------------------------------------ #
    def _on_media_changed(self, title: str, url: str) -> None:
        self.titleLabel.setText(title or tr("player.unknown_title"))
        self._hide_empty()
        self.hide_buffering()  # 解码完成，开始播放
        self._show_controls()

    def _on_state_changed(self, state: str) -> None:
        self.stateChanged.emit(state)
        if state == "playing":
            self._hide_empty()
            self.hide_buffering()
            # 投屏开始播放 → 控制栏出现（媒体门控在 _show_controls 内）
            self._show_controls()
            self._hide_timer.start()
        elif state == "idle" and self._player.get_duration() is None:
            self._show_empty()
            self.hide_buffering()
            # 没有媒体了 → 控制栏也隐藏
            self.embeddedBar.hide()
            self.floatingBar.hide()

    def _on_playback_failed(self, title: str, detail: str) -> None:
        """播放/加载失败 → 顶部信息条 + InfoBar 友好提示（技术细节见日志）。

        优先把 mpv 技术错误映射成可读原因（403 防盗链/404 链接失效/密钥
        拦截/内容异常/网络超时），匹配不到再退回通用提示。
        """
        name = title or tr("player.error.play_failed")
        self.titleLabel.setText(name)
        self.hide_buffering()
        hint = _friendly_hint(detail) or tr("player.error.play_failed.hint")
        content = hint
        if detail:
            content = f"{content}\n{detail}"
        InfoBar.error(
            title=tr("player.error.play_failed"),
            content=content,
            orient=Qt.Horizontal,
            isClosable=True,
            duration=8000,
            parent=self,
            position=InfoBarPosition.TOP,
        )
        log.warning("播放失败: %s (%s)", name, detail)

    # ------------------------------------------------------------------ #
    # 国际化
    # ------------------------------------------------------------------ #
    def _retranslate(self, *_args) -> None:
        self.emptyTitle.setText(tr("player.empty"))
        self.emptyHint.setText(tr("player.empty.hint"))
        self.titleLabel.setText(tr("player.empty"))
        self.bufferOverlay.set_text(tr("player.buffering"))

    def retranslate_ui(self) -> None:
        self._retranslate()
