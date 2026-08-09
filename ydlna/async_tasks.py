"""后台 asyncio 任务的持有、异常收口与退出清理。"""
from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from typing import Any, TypeVar

from .logger import get_logger

log = get_logger("async_tasks")

_T = TypeVar("_T")


class BackgroundTasks:
    """持有后台任务的强引用，并在所属组件退出时统一取消等待。"""

    def __init__(self) -> None:
        self._tasks: set[asyncio.Task[Any]] = set()

    def __len__(self) -> int:
        return len(self._tasks)

    def create(
        self,
        coro: Coroutine[Any, Any, _T],
        *,
        name: str,
    ) -> asyncio.Task[_T]:
        """创建并持有任务；完成后取出异常，避免无人 await 的告警。"""
        task = asyncio.create_task(coro, name=name)
        self._tasks.add(task)
        task.add_done_callback(self._on_done)
        return task

    def _on_done(self, task: asyncio.Task[Any]) -> None:
        self._tasks.discard(task)
        if task.cancelled():
            return
        try:
            exc = task.exception()
        except asyncio.CancelledError:
            return
        if exc is not None:
            log.error(
                "后台任务 %s 异常: %s",
                task.get_name(), exc,
                exc_info=(type(exc), exc, exc.__traceback__),
            )

    async def cancel_all(self) -> None:
        """取消并等待当前持有的全部任务；可重复调用。"""
        current = asyncio.current_task()
        tasks = tuple(
            task for task in self._tasks
            if task is not current and not task.done()
        )
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.difference_update(tasks)
