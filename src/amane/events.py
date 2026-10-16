"""EventBus 供 scheduler 与 api 共用 (避免 api↔scheduler 环依赖)."""

import json
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import TYPE_CHECKING

import structlog

if TYPE_CHECKING:
    from fastapi import WebSocket

logger = structlog.get_logger()


class EventType(StrEnum):
    TASK_STARTED = "task.started"
    TASK_PROGRESS = "task.progress"
    TASK_COMPLETED = "task.completed"
    TASK_FAILED = "task.failed"
    FILE_DISCOVERED = "file.discovered"
    FILE_REMOVED = "file.removed"
    LOG = "log"


@dataclass
class Event:
    type: EventType
    data: dict = field(default_factory=dict)
    timestamp: str = field(default_factory=lambda: datetime.now(UTC).isoformat())


class EventBus:
    """失效连接在发送失败时移除. 序列化失败不得据此踢掉全部客户端."""

    def __init__(self) -> None:
        self._connections: list[WebSocket] = []
        self._listeners: list[Callable[[Event], Awaitable[None]]] = []

    @property
    def connection_count(self) -> int:
        return len(self._connections)

    def add_listener(self, listener: Callable[[Event], Awaitable[None]]) -> None:
        """注册旁路监听器 (通知等). 监听器抛异常只记日志, 不影响任务链路."""
        self._listeners.append(listener)


    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()
        self._connections.append(ws)

    def disconnect(self, ws: WebSocket) -> None:
        if ws in self._connections:
            self._connections.remove(ws)

    async def close_all(self) -> None:
        """主动断开全部客户端, 避免 graceful shutdown 被常驻 WS 拖死."""
        connections = list(self._connections)
        self._connections.clear()
        for ws in connections:
            with suppress(Exception):
                await ws.close()

    async def broadcast(self, event: Event) -> None:
        """序列化失败不得判定连接已死: 否则一条坏 event 会静默踢掉全部客户端, 且 TCP 未断、无 onclose."""
        try:
            payload = asdict(event)
            # 失败则丢弃该 event, 保留所有连接
            json.dumps(payload)
        except TypeError, ValueError:
            logger.warning("dropping non-serializable event", event_type=str(event.type))
            return

        dead: list[WebSocket] = []
        for ws in self._connections:
            try:
                await ws.send_json(payload)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self._connections.remove(ws)

    async def emit(self, event_type: EventType, data: dict | None = None) -> None:
        event = Event(type=event_type, data=data or {})
        await self.broadcast(event)
        for listener in list(self._listeners):
            try:
                await listener(event)
            except Exception:
                # 旁路 (通知发送等) 失败绝不能冒泡回任务链路.
                logger.exception("event listener failed", event_type=str(event.type))
