"""libmpv 不可用时致命错误对话框回归（生产路径）。

历史缺陷（0.1.29~0.1.32 用户实际踩中）：DLL 加载失败时用
qfluentwidgets ``MessageBox(..., None)`` 弹提示——MaskDialogBase 构造
即取 ``parent.width()``，parent=None 直接 AttributeError，错误提示本身
弹不出来，应用带着 PyInstaller 崩溃窗退出。

生产函数 ``ydlna.app._show_libmpv_missing_dialog`` 必须用 Qt 原生无
parent QMessageBox。本测试直接调用该生产函数：若将来有人改回
qfluentwidgets MessageBox(None)，函数会在构造时抛 AttributeError 而使
测试失败（不锁定第三方库的行为，只锁定我们自己的生产代码）。

真实 Qt 对象按仓库惯例放子进程 + offscreen 验证。
"""
from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from pathlib import Path

_SCRIPT = textwrap.dedent(
    """
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication, QMessageBox

    import ydlna.app as app_module

    app = QApplication([])

    captured = {}

    class _SpyBox(QMessageBox):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            captured["box"] = self

    # 生产函数内部 `from PySide6.QtWidgets import QMessageBox`，替换
    # 模块属性即可让生产代码构造 _SpyBox
    import PySide6.QtWidgets
    PySide6.QtWidgets.QMessageBox = _SpyBox

    # exec() 是模态事件循环：定时关闭 activeModal 使其返回
    def _close():
        w = QApplication.activeModalWidget()
        if w is not None:
            w.close()

    QTimer.singleShot(150, _close)
    app_module._show_libmpv_missing_dialog()

    box = captured.get("box")
    assert box is not None, "生产函数未构造对话框"
    assert box.icon() == QMessageBox.Icon.Critical
    assert box.parent() is None, "致命对话框必须无 parent"
    assert box.standardButtons() & QMessageBox.StandardButton.Ok
    print("production-dialog-ok")
    """
)


def test_run_gate_calls_the_extracted_dialog_function() -> None:
    """run() 的 libmpv 门控必须调用抽出的生产函数（防止内联回潮）。"""
    src = Path("ydlna/app.py").read_text(encoding="utf-8")
    gate = src.index("if not is_available():")
    section = src[gate : gate + 300]
    assert "_show_libmpv_missing_dialog()" in section
    assert "MessageBox" not in section, "门控内不得内联对话框构造"


def test_show_libmpv_missing_dialog_production_path() -> None:
    """真实调用生产函数：原生 QMessageBox、Critical、无 parent、带 OK。"""
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
    assert "production-dialog-ok" in result.stdout
