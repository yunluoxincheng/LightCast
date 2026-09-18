"""主页 InfoBar 提示回归（评审 M16）。

历史实现在 ``InfoBar.show(...)`` 上传 icon——但 qfluentwidgets 没有
``InfoBar.show`` 类方法（调用会落到 ``QWidget.show`` 并因多余参数抛
``TypeError``），且 icon 参数从未生效。修复后必须走公开入口
``InfoBar.new(icon=...)`` 并把 WARNING / INFORMATION 传进去。
"""
from __future__ import annotations

import os
import subprocess
import sys
import textwrap

from qfluentwidgets import InfoBarIcon

from ydlna.ui import home_interface
from ydlna.ui.home_interface import HomeInterface


class _RecordingInfoBar:
    """替换真实 InfoBar：捕获 new() 的关键字参数，且刻意不提供 show。"""

    last_kwargs: dict | None = None

    @classmethod
    def new(cls, **kwargs) -> None:
        cls.last_kwargs = kwargs


def test_show_info_passes_icon_and_uses_public_new_api(monkeypatch) -> None:
    monkeypatch.setattr(home_interface, "InfoBar", _RecordingInfoBar)

    HomeInterface.show_info(object(), "标题", "内容", is_warning=True)  # type: ignore[arg-type]
    kwargs = _RecordingInfoBar.last_kwargs
    assert kwargs is not None
    assert kwargs["icon"] == InfoBarIcon.WARNING
    assert kwargs["title"] == "标题"
    assert kwargs["content"] == "内容"

    HomeInterface.show_info(object(), "标题2", "内容2", is_warning=False)  # type: ignore[arg-type]
    kwargs = _RecordingInfoBar.last_kwargs
    assert kwargs is not None
    assert kwargs["icon"] == InfoBarIcon.INFORMATION


def test_real_infobar_new_accepts_icon_argument() -> None:
    """锁定库 API 假设：真实 qfluentwidgets 的 InfoBar.new 接受 icon 关键字。"""
    script = textwrap.dedent(
        """
        from PySide6.QtCore import Qt
        from PySide6.QtWidgets import QApplication
        from qfluentwidgets import InfoBar, InfoBarIcon, InfoBarPosition

        app = QApplication([])
        bar = InfoBar.new(
            icon=InfoBarIcon.WARNING,
            title="t",
            content="c",
            orient=Qt.Horizontal,
            isClosable=True,
            position=InfoBarPosition.TOP,
            duration=1,
            parent=None,
        )
        assert bar.icon == InfoBarIcon.WARNING
        assert bar.title == "t"
        assert bar.content == "c"
        """
    )
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
