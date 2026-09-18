"""托盘 tooltip 状态复位回归（评审 M15）。

历史上 tooltip 只在 mediaChanged 时写成「正在播放：xxx」，播放结束后
从不复位，只有切换语言才恢复 idle 文案。修复后 stateChanged 到 idle
必须把 tooltip 复位回 idle 文案，且暂停时保持媒体信息。

真实 QSystemTrayIcon 依赖 QApplication，按仓库惯例放在子进程中运行，
避免与其他用例共享 Qt 进程状态。
"""
from __future__ import annotations

import os
import subprocess
import sys
import textwrap

_SCRIPT = textwrap.dedent(
    """
    from PySide6.QtCore import QObject, Signal
    from PySide6.QtWidgets import QApplication

    from ydlna.i18n import tr
    from ydlna.ui.tray import TrayIcon


    class _FakeSignals(QObject):
        stateChanged = Signal(str)
        mediaChanged = Signal(str, str)


    class _FakePlayer(QObject):
        # TrayIcon 只依赖 signals 与 get_state()，用最小桩替代真实 Player。
        def __init__(self) -> None:
            super().__init__()
            self.signals = _FakeSignals()

        def get_state(self) -> str:
            return "idle"


    app = QApplication([])
    player = _FakePlayer()
    tray = TrayIcon(player)

    assert tray.toolTip() == tr("tray.tooltip.idle")

    player.signals.mediaChanged.emit("某番剧", "https://example.com/a.m3u8")
    assert "某番剧" in tray.toolTip()

    # 暂停时媒体仍在，tooltip 不应被误清
    player.signals.stateChanged.emit("paused")
    assert "某番剧" in tray.toolTip()

    player.signals.stateChanged.emit("idle")
    assert tray.toolTip() == tr("tray.tooltip.idle")

    # 新媒体开始播放后 tooltip 恢复「正在播放」文案
    player.signals.mediaChanged.emit("另一番剧", "https://example.com/b.m3u8")
    assert "另一番剧" in tray.toolTip()

    tray.hide()
    """
)


def test_tray_tooltip_resets_to_idle_when_playback_ends() -> None:
    """真实 QSystemTrayIcon 上验证 tooltip 的设置与 idle 复位时序。"""
    env = os.environ.copy()
    env.setdefault("QT_QPA_PLATFORM", "offscreen")
    result = subprocess.run(
        [sys.executable, "-c", _SCRIPT],
        cwd=os.getcwd(),
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )

    assert result.returncode == 0, result.stderr
