"""设置页布局回归：长说明 / 长设备名不得把页面撑出视口。

历史缺陷（v0.1.29~v0.1.34 用户实机踩中）：`_SettingCard` 的说明
QLabel 不换行，「投屏需本机确认」等 v0.1.28+ 新增的超长说明把卡片
最小宽度撑到 ~1190px，设置页滚动内容比视口宽出一段，所有卡片的
右侧控件（开关/下拉框）被裁出屏幕，观感如同早期开发版。

修复：说明自动换行、卡片高度随内容增长、音频设备 ComboBox 固定
宽度（Fluent ComboBox 选中长文本会 adjustSize 自行扩宽）、横向滚动
禁用。本测试实例化**完整 SettingsInterface**，用超长音频设备名 +
受限视口宽度锁定整个溢出类别，而不是只测单个控件。

真实 QWidget 按仓库惯例放子进程 + offscreen 验证。
"""
from __future__ import annotations

import os
import subprocess
import sys
import textwrap

_SCRIPT = textwrap.dedent(
    """
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication

    app = QApplication([])

    # ---- 单卡片层：换行 + 最小宽度收敛 ----
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
    w = card.minimumSizeHint().width()
    assert w < 640, f"长说明卡片最小宽度 {w}px 未收敛（未换行时 ~1190px）"

    # ---- 完整设置页：超长设备名 + 受限宽度，整页不得横向溢出 ----
    LONG_DESC = "Super Long Audio Device Name " * 6


    class _FakeConfig:
        def get(self, key, default=None):
            # 让长名设备成为当前选中项：ComboBox 按文本扩宽的路径
            if key == "audio_device":
                return "long-device"
            if key == "friendly_name":
                return "轻投"
            return {"language": "zh", "theme": "dark"}.get(key, default)

        def set(self, key, value, persist=True):
            pass


    class _FakePlayer:
        def get_audio_devices(self):
            return [("long-device", LONG_DESC)]

        def set_audio_device(self, name):
            pass


    from ydlna.ui.settings_interface import SettingsInterface

    page = SettingsInterface(_FakeConfig(), _FakePlayer())
    page.resize(900, 800)
    page.show()
    app.processEvents()

    policy = page.scrollArea.horizontalScrollBarPolicy()
    assert policy == Qt.ScrollBarAlwaysOff, "横向滚动条必须禁用"
    viewport_w = page.scrollArea.viewport().width()
    content_w = page.scrollWidget.width()
    assert content_w <= viewport_w, (
        f"滚动内容 {content_w}px 超出视口 {viewport_w}px，右侧控件会被裁掉"
    )
    assert page.audioDeviceCombo.width() == 220, (
        f"音频设备下拉宽度 {page.audioDeviceCombo.width()}px 未固定，"
        "长设备名会把它撑宽"
    )
    # 长说明卡片必须折行增高（锁 setMinimumHeight 不得回退成 setFixedHeight）
    assert page.castConfirmCard.height() > 76, (
        f"长说明卡片高度 {page.castConfirmCard.height()}px 未增长，"
        "说明可能被固定高度截断"
    )
    print("settings-layout-ok")
    """
)


def test_settings_page_no_horizontal_overflow_with_long_texts() -> None:
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
    assert "settings-layout-ok" in result.stdout
