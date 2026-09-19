"""设置卡片布局回归：长说明不得撑爆卡片最小宽度。

历史缺陷（v0.1.29~v0.1.34 用户实机踩中）：`_SettingCard` 的说明
QLabel 不换行，「投屏需本机确认」等 v0.1.28+ 新增的超长说明把卡片
最小宽度撑到 ~1190px，设置页滚动内容比视口宽出一段，所有卡片的
右侧控件（开关/下拉框）被裁出屏幕，页面横向滚动，观感如同早期
开发版。修复后说明自动换行，最小宽度收敛。

真实 QWidget 按仓库惯例放子进程 + offscreen 验证。
"""
from __future__ import annotations

import os
import subprocess
import sys
import textwrap

_SCRIPT = textwrap.dedent(
    """
    from PySide6.QtWidgets import QApplication

    app = QApplication([])
    from ydlna.ui.settings_interface import _SettingCard

    long_hint = (
        "首次来自新设备的投屏会弹窗询问（按控制点地址识别），确认后该设备的"
        "投屏与播放控制在本运行内均被放行；30 秒未确认自动拒绝。关闭后同一"
        "网络的任何设备都可直接投屏。"
    )
    card = _SettingCard("投屏需本机确认", long_hint)
    card.show()
    app.processEvents()

    assert card.descLabel.wordWrap() is True, "说明必须自动换行"
    width = card.minimumSizeHint().width()
    assert width < 640, f"长说明卡片最小宽度 {width}px 未收敛（未换行时 ~1190px）"
    print(f"layout-ok min-width={width}")
    """
)


def test_setting_card_long_hint_wraps_and_keeps_min_width_small() -> None:
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
    assert "layout-ok" in result.stdout
