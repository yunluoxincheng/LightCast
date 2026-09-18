"""播放器页 / 缓冲 overlay 空闲定时器启停回归（评审 M8）。

历史实现中 `_Spinner` 的 30ms 重绘定时器与页面 `_anchor_timer` 的
150ms 锚定定时器在控件不可见期间仍持续运行，笔记本省电场景下无谓
唤醒主线程。修复后：

- `_Spinner` 定时器随自身显隐启停（构造于隐藏的 overlay 内时绝不运行）；
- `_anchor_timer` 只在播放器页面可见时运行。

按仓库惯例，真实 QWidget 生命周期测试放子进程 + offscreen 运行。
"""
from __future__ import annotations

import os
import subprocess
import sys
import textwrap

_SPINNER_SCRIPT = textwrap.dedent(
    """
    from PySide6.QtWidgets import QApplication, QWidget

    from ydlna.player.player_interface import BufferingOverlay

    app = QApplication([])
    events = []

    # 构造即隐藏的 BufferingOverlay：spinner 定时器绝不能已在运行
    overlay = BufferingOverlay()
    assert not overlay.spinner._timer.isActive(), "隐藏 overlay 的 spinner 在转"

    # 父级隐藏期间 show()：不算真正上屏，也不启动（isVisible 门控）
    host = QWidget()
    overlay2 = BufferingOverlay(host)
    overlay2.show()
    app.processEvents()
    assert not overlay2.spinner._timer.isActive(), "隐藏宿主下的 spinner 在转"

    # 宿主真正显示 → 级联 show 事件 → 启动
    host.show()
    app.processEvents()
    assert host.isVisible() and overlay2.isVisible()
    assert overlay2.spinner._timer.isActive(), "上屏后 spinner 未启动"

    # 隐藏宿主 → 级联 hide 事件 → 停止
    host.hide()
    app.processEvents()
    assert not overlay2.spinner._timer.isActive(), "隐藏后 spinner 仍在转"

    # 顶层 overlay：直接 show/hide 路径
    overlay.show()
    app.processEvents()
    assert overlay.isVisible()
    assert overlay.spinner._timer.isActive(), "顶层 overlay 显示后 spinner 未启动"
    overlay.hide()
    app.processEvents()
    assert not overlay.spinner._timer.isActive(), "顶层 overlay 隐藏后 spinner 仍在转"
    """
)

_ANCHOR_SCRIPT = textwrap.dedent(
    """
    from PySide6.QtCore import QObject, Signal
    from PySide6.QtWidgets import QApplication, QWidget

    from ydlna.player.player_interface import PlayerInterface


    class _FakeSignals(QObject):
        positionChanged = Signal(object)
        durationChanged = Signal(object)
        stateChanged = Signal(str)
        mediaChanged = Signal(str, str)
        ended = Signal()
        volumeChanged = Signal(int)
        muteChanged = Signal(bool)
        errorOccurred = Signal(str)
        playbackFailed = Signal(str, str)
        bufferingChanged = Signal(bool)


    class _FakePlayer(QObject):
        # PlayerInterface/ControlBar 构造与 show 路径用到的最小表面
        available = False

        def __init__(self) -> None:
            super().__init__()
            self.signals = _FakeSignals()

        def get_volume(self) -> int:
            return 100

        def get_state(self) -> str:
            return "idle"

        def get_duration(self):
            return None

        def get_loading(self) -> bool:
            return False

        def get_buffering(self) -> bool:
            return False

        def is_muted(self) -> bool:
            return False

        def set_mute(self, muted) -> None:
            pass

        def set_volume(self, v) -> None:
            pass

        def set_speed(self, s) -> None:
            pass

        def play_pause(self) -> None:
            pass

        def seek_relative(self, d) -> None:
            pass

        def attach(self, wid) -> None:
            pass


    app = QApplication([])
    host = QWidget()
    page = PlayerInterface(_FakePlayer(), host)

    # 页面构造后处于隐藏状态：150ms 锚定定时器不得空转（M8）
    assert not page._anchor_timer.isActive(), "隐藏页面的锚定定时器在跑"

    # 隐藏宿主下 page.show()：showEvent 会触发（开机自启静默模式会切到
    # 播放器页但不显示主窗口），但页面实际不可见，定时器必须保持停止
    page.show()
    app.processEvents()
    assert not page.isVisible()
    assert not page._anchor_timer.isActive(), "隐藏宿主下的页面锚定定时器在跑"

    # 宿主真正显示 → 级联 show → 页面实际可见 → 启动
    host.show()
    app.processEvents()
    assert page.isVisible()
    assert page._anchor_timer.isActive(), "页面上屏后锚定定时器未启动"

    # 切走 → hideEvent → 停止
    host.hide()
    app.processEvents()
    assert not page._anchor_timer.isActive(), "页面切走后锚定定时器仍在跑"

    # 切回 → 再次启动
    host.show()
    app.processEvents()
    assert page._anchor_timer.isActive(), "页面切回后锚定定时器未恢复"
    """
)


def _run(script: str) -> None:
    env = os.environ.copy()
    env.setdefault("QT_QPA_PLATFORM", "offscreen")
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=os.getcwd(),
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_spinner_timer_runs_only_while_visible() -> None:
    _run(_SPINNER_SCRIPT)


def test_anchor_timer_runs_only_while_page_visible() -> None:
    _run(_ANCHOR_SCRIPT)
