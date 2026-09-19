"""投屏确认弹窗生产路径回归。

历史缺陷（0.1.28~0.1.33，用户实机踩中：点「允许」后投屏必然失败）：
确认框把 ``deleteLater`` 挂在 ``finished`` 信号上，用户点击后 C++ 对象
被销毁，协程在下一个事件循环轮次才恢复，``clickedButton()`` 抛
「Internal C++ object already deleted」，门控把异常按拒绝处理——确认
从未成功放行过。

修复后 ``ydlna.app._confirm_cast_dialog`` 在 ``buttonClicked`` 信号回调
里同步捕获点击结果。本测试直接调用该生产函数，验证三个结果路径。

真实 Qt 对象按仓库惯例放子进程 + offscreen 验证。
"""
from __future__ import annotations

import os
import subprocess
import sys
import textwrap

_SCRIPT = textwrap.dedent(
    """
    import asyncio

    from PySide6.QtCore import QCoreApplication, QEvent
    from PySide6.QtWidgets import QApplication, QMessageBox, QWidget

    import ydlna.app as app_module

    app = QApplication([])

    captured = {}

    class _SpyBox(QMessageBox):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            captured["box"] = self

    # 生产函数内部 `from PySide6.QtWidgets import QMessageBox`，替换模块
    # 属性即可让生产代码构造 _SpyBox
    import PySide6.QtWidgets
    PySide6.QtWidgets.QMessageBox = _SpyBox

    window = QWidget()
    window.show()


    def _click(role):
        box = captured["box"]
        for b in box.buttons():
            if box.buttonRole(b) == role:
                # click() 同步发射 buttonClicked/finished（直连），
                # 不依赖 Qt 事件循环
                b.click()
                return
        raise SystemExit(f"找不到 role={role} 的按钮")


    async def scenario(role=None, timeout=5.0):
        if role is not None:
            async def _driver():
                await asyncio.sleep(0.05)
                _click(role)
                # 复现生产环境时序：qasync 下 DeferredDelete 会在协程
                # 恢复前被事件循环处理（历史缺陷正是在此之后访问 C++
                # 对象崩溃）。纯 asyncio 循环不会自动处理，这里强制执行。
                QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
            task = asyncio.ensure_future(_driver())
        else:
            task = None
        allowed = await app_module._confirm_cast_dialog(
            window, "192.0.2.7", "https://example.com/a.m3u8", "某番剧",
            timeout=timeout,
        )
        if task is not None:
            await task
        return allowed


    assert asyncio.run(scenario(QMessageBox.ButtonRole.AcceptRole)) is True, "点允许必须放行"
    print("allow-ok")
    assert asyncio.run(scenario(QMessageBox.ButtonRole.RejectRole)) is False, "点取消必须拒绝"
    print("cancel-ok")
    assert asyncio.run(scenario(timeout=0.2)) is False, "超时必须自动拒绝"
    print("timeout-ok")
    """
)


def test_confirm_cast_dialog_production_paths() -> None:
    """允许 → True；取消 → False；超时 → False。

    历史缺陷下「允许」路径会在生产函数内抛
    「Internal C++ object already deleted」→ 本测试直接失败。
    """
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
    for marker in ("allow-ok", "cancel-ok", "timeout-ok"):
        assert marker in result.stdout
