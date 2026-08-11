#!/usr/bin/env python3
"""轻投（LightCast）入口。

导入顺序非常重要（Windows）：
1. 先完全加载 PySide6（Qt6 的 dll 必须先就位）
2. 再导入 qasync
3. 最后才导入 ydlna.player.mpv_player（它会在导入时加载 bin/libmpv-2.dll）

如果反过来，libmpv-2.dll 的依赖会污染进程 DLL 表，导致
``import PySide6.QtWidgets`` 报「DLL load failed」。

启动方式：必须用 ``qasync.run``（QEventLoop），不能用标准 ``asyncio.run``，
否则 mpv 事件线程 emit 的 Qt 信号无法投递到主线程（见 app.run）。
"""
from __future__ import annotations

import asyncio
import sys


def _bootstrap() -> int:
    # 1. PySide6 完全加载
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication(sys.argv)

    # 2. qasync
    import qasync
    from qasync import QEventLoop

    # 3. ydlna（含 libmpv 加载）
    from ydlna.app import run

    # 用 qasync 的 QEventLoop 运行主协程
    loop = QEventLoop(app)
    asyncio.set_event_loop(loop)

    with loop:
        try:
            result = loop.run_until_complete(run())
        except KeyboardInterrupt:
            result = 0
    return result


if __name__ == "__main__":
    sys.exit(_bootstrap())
