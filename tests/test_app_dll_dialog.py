"""libmpv 不可用时致命错误对话框回归。

历史缺陷（0.1.29~0.1.32 用户实际踩中）：DLL 加载失败时用
qfluentwidgets ``MessageBox(..., None)`` 弹提示——MaskDialogBase 构造
即取 ``parent.width()``，parent=None 直接 AttributeError，错误提示本身
弹不出来，应用带着 PyInstaller 崩溃窗退出。修复后改用 Qt 原生无
parent QMessageBox（合法且与 updater 同模式）。

真实 Qt 对象按仓库惯例放子进程 + offscreen 验证。
"""
from __future__ import annotations

import os
import subprocess
import sys
import textwrap

_SCRIPT = textwrap.dedent(
    """
    from PySide6.QtWidgets import QApplication, QMessageBox

    app = QApplication([])

    # 修复后的路径：原生 QMessageBox 无 parent 构造成功
    box = QMessageBox(
        QMessageBox.Icon.Critical,
        "title",
        "body",
    )
    box.close()
    print("native-ok")

    # 历史缺陷根因存档：qfluentwidgets MessageBox 的 parent=None 必崩
    from qfluentwidgets import MessageBox

    try:
        MessageBox("t", "b", None)
    except AttributeError as e:
        assert "width" in str(e), str(e)
        print("qfluentwidgets-crash-reproduced")
    else:
        raise SystemExit("qfluentwidgets MessageBox(None) 竟然没有崩溃")
    """
)


def test_fatal_dll_dialog_uses_parentless_native_messagebox() -> None:
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
    assert "native-ok" in result.stdout
    assert "qfluentwidgets-crash-reproduced" in result.stdout
