"""任务身份无需日志记录器; 共享处理器的进度回调按执行任务隔离."""

import asyncio
from typing import TYPE_CHECKING

import pytest

from amane.db.models import TaskStatus, TaskType
from amane.events import Event, EventBus, EventType
from amane.handlers.protocol import TaskHandler, TaskResult
from amane.observability import get_task_id
from amane.scheduler.worker import CANCEL_ERROR, AsyncWorker
from tests.helpers import LOAD_TOLERANT_TIMEOUT

if TYPE_CHECKING:
    from amane.db.repository import Repository


class _IdentityHandler(TaskHandler[dict[str, int], dict[str, int]]):
    def __init__(self) -> None:
        super().__init__(payload_t=dict, result_t=dict)
        self.identities: list[tuple[int, int | None]] = []

    async def handle(self, payload: dict[str, int]) -> TaskResult[dict[str, int]]:
        return TaskResult(success=True, result={"value": payload["value"] + 1})

    async def handle_task(self, payload: dict[str, int], task_id: int) -> TaskResult[dict[str, int]]:
        self.identities.append((task_id, get_task_id()))
        result = await super().handle_task(payload, task_id)
        assert result.result is not None
        result.result["task_id"] = task_id
        return result


class _ProgressHandler(TaskHandler[dict[str, int], dict[str, int]]):
    payload_type = dict

    def __init__(self, *, initialize_base: bool) -> None:
        if initialize_base:
            super().__init__(payload_t=dict, result_t=dict)
        self.started = [asyncio.Event(), asyncio.Event()]
        self.release = [asyncio.Event(), asyncio.Event()]

    async def handle(self, payload: dict[str, int]) -> TaskResult[dict[str, int]]:
        index = payload["index"]
        self.started[index].set()
        await self.release[index].wait()
        await self.report_progress(index + 1, 2, f"member-{index}")
        return TaskResult(success=True, result={"completed": index})


async def _stop(worker: AsyncWorker) -> None:
    worker.retire()
    await worker.wait_stopped()
    await worker.shutdown_active()


@pytest.mark.asyncio
async def test_worker_supplies_task_identity_without_log_recorder(repo: Repository) -> None:
    handler = _IdentityHandler()
    task = await repo.create_task(TaskType.REFRESH, payload={"value": 8})
    assert task.id is not None
    worker = AsyncWorker(repo, {TaskType.REFRESH: handler}, poll_interval=0.01)
    worker.start()
    try:
        async with asyncio.timeout(LOAD_TOLERANT_TIMEOUT):
            assert await worker._done_queue.get() == task.id
        saved = await repo.get_task(task.id)
        assert saved is not None and saved.status is TaskStatus.DONE
        assert saved.result == {"value": 9, "task_id": task.id}
        assert handler.identities == [(task.id, None)]
    finally:
        await _stop(worker)


@pytest.mark.asyncio
@pytest.mark.parametrize("initialize_base", [True, False], ids=["normal", "legacy-no-super"])
@pytest.mark.parametrize("cancel_second", [False, True], ids=["first-completes", "second-cancelled"])
async def test_shared_handler_progress_stays_with_task_after_another_task_finishes(
    repo: Repository, initialize_base: bool, cancel_second: bool
) -> None:
    handler = _ProgressHandler(initialize_base=initialize_base)
    events: list[Event] = []
    bus = EventBus()

    async def collect(event: Event) -> None:
        if event.type is EventType.TASK_PROGRESS:
            events.append(event)

    bus.add_listener(collect)
    tasks = [await repo.create_task(TaskType.REFRESH, payload={"index": index}) for index in range(2)]
    first_id, second_id = (task.id for task in tasks)
    assert first_id is not None and second_id is not None
    worker = AsyncWorker(repo, {TaskType.REFRESH: handler}, concurrency=2, poll_interval=0.01, event_bus=bus)
    worker.start()
    try:
        async with asyncio.timeout(LOAD_TOLERANT_TIMEOUT):
            await asyncio.gather(*(event.wait() for event in handler.started))
            if cancel_second:
                assert await worker.cancel_task(second_id)
                assert await worker._done_queue.get() == second_id
            handler.release[0].set()
            assert await worker._done_queue.get() == first_id
            if not cancel_second:
                handler.release[1].set()
                assert await worker._done_queue.get() == second_id

        expected = [{"task_id": first_id, "current": 1, "total": 2, "message": "member-0"}]
        if not cancel_second:
            expected.append({"task_id": second_id, "current": 2, "total": 2, "message": "member-1"})
        assert [event.data for event in events] == expected
        first = await repo.get_task(first_id)
        second = await repo.get_task(second_id)
        assert first is not None and first.status is TaskStatus.DONE and first.result == {"completed": 0}
        assert second is not None
        if cancel_second:
            assert second.status is TaskStatus.FAILED and second.error == CANCEL_ERROR
        else:
            assert second.status is TaskStatus.DONE and second.result == {"completed": 1}
        # 测试调用上下文没有安装回调, 不得继承任何已结束 worker 任务的回调.
        await handler.report_progress(2, 2, "outside-worker")
        assert [event.data for event in events] == expected
    finally:
        await _stop(worker)


@pytest.mark.asyncio
async def test_handler_instances_keep_independent_callbacks_in_same_execution_context() -> None:
    first = _IdentityHandler()
    second = _IdentityHandler()
    reported: list[tuple[str, int, int, str]] = []

    async def report_first(current: int, total: int, message: str) -> None:
        reported.append(("first", current, total, message))

    async def report_second(current: int, total: int, message: str) -> None:
        reported.append(("second", current, total, message))

    await first.report_progress(0, 1, "unbound")
    first.set_progress_callback(report_first)
    second.set_progress_callback(report_second)
    await first.report_progress(1, 3, "first-progress")
    first.set_progress_callback(None)
    await first.report_progress(2, 3, "cleared")
    await second.report_progress(3, 3, "second-progress")

    assert reported == [("first", 1, 3, "first-progress"), ("second", 3, 3, "second-progress")]
