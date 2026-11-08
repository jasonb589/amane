"""异步资源的操作租约; 获取与释放必须在所属事件循环执行."""

import asyncio
from collections.abc import Awaitable, Callable, Generator
from contextlib import contextmanager


class AsyncLifetime:
    """退役后等待已有操作结束, 只执行一次关闭; 关闭等待者取消不传播给清理任务."""

    def __init__(self, close: Callable[[], Awaitable[None]]) -> None:
        self._close = close
        self._users = 0
        self._retired = False
        self._task: asyncio.Task[None] | None = None
        self._finished = asyncio.Event()
        self._error: BaseException | None = None

    @contextmanager
    def lease(self) -> Generator[None]:
        # 退役期间已有持有者仍可开始后续步骤; 零引用开始关闭后禁止重新使用.
        if self._task is not None:
            raise RuntimeError("资源已开始关闭")
        self._users += 1
        try:
            yield
        finally:
            self._users -= 1
            self._start_close()

    def retire(self) -> None:
        self._retired = True
        self._start_close()

    def _start_close(self) -> None:
        if self._retired and self._users == 0 and self._task is None:
            self._task = asyncio.create_task(self._finish())

    async def _finish(self) -> None:
        try:
            await self._close()
        except BaseException as exc:
            self._error = exc
        finally:
            self._finished.set()

    async def aclose(self) -> None:
        self.retire()
        await self._finished.wait()
        if self._error is not None:
            raise self._error
