"""批量接口保留逐文件任务, 共享刮削组仅为服务端调度状态."""

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import pytest

from amane.config import HotSettings
from amane.crawlers.base import Crawler
from amane.crawlers.factory import CrawlerFactory
from amane.crawlers.models import MediaMetadata
from amane.db.models import MediaFileStatus, TaskStatus, TaskType
from amane.handlers import ScrapeHandler
from amane.parsing import ContentType
from amane.scheduler.worker import AsyncWorker

if TYPE_CHECKING:
    from pathlib import Path

    from fastapi import FastAPI
    from httpx2 import AsyncClient

    from amane.app.runtime import AppRuntime
    from amane.db.repository import Repository


@pytest.mark.asyncio
async def test_batch_keeps_each_file_task_and_organize_without_exposing_shared_group(
    app: FastAPI,
    client: AsyncClient,
    repo: Repository,
    safe_path: Path,
    stop_worker: None,
) -> None:
    runtime: AppRuntime = app.state.runtime
    incoming = safe_path / "incoming"
    incoming.mkdir()
    library = await repo.create_library(name="Shared scrape", path=str(incoming), auto_organize=True)
    assert library.id is not None
    media_ids: list[int] = []
    for part in (1, 2):
        video = incoming / f"MIDV-123-CD{part}.mp4"
        video.write_bytes(f"part-{part}".encode())
        media = await repo.create_media_file(library.id, path=str(video))
        assert media.id is not None
        media_ids.append(media.id)

    submitted = await client.post(
        "tasks/batch/submit",
        json={"submissions": [{"type": "scrape", "media_id": media_id} for media_id in media_ids]},
    )
    assert submitted.status_code == 202
    body = submitted.json()
    assert (body["submitted"], body["missing"], body["skipped"]) == (2, 0, 0)
    task_ids = body["task_ids"]
    tasks = await repo.find_tasks(task_ids=task_ids)
    assert len(task_ids) == len(tasks) == 2
    groups = {task.scrape_group_id for task in tasks}
    assert len(groups) == 1 and None not in groups
    assert all(set(task.payload["use_cache"]) == {"metadata", "trans"} for task in tasks)

    config = HotSettings()
    config.actor_scraping.auto_scrape = False
    config.scraping.content_routes[ContentType.CENSORED] = ["javdb"]
    crawler = AsyncMock(spec=Crawler)
    crawler.fetch.return_value = MediaMetadata(number="MIDV-123", title="Shared title")
    factory = AsyncMock(spec=CrawlerFactory)
    factory.get_crawlers.return_value = {"javdb": crawler}
    handler = ScrapeHandler(repo, factory, runtime.resource_store, config)
    worker = AsyncWorker(repo, {TaskType.SCRAPE: handler}, concurrency=1)
    for _ in task_ids:
        claimed = await repo.claim_next_task()
        assert claimed is not None and claimed.type is TaskType.SCRAPE
        await worker._execute(claimed)

    metadata = await repo.get_metadata_by_number("MIDV-123")
    assert metadata is not None and metadata.id is not None
    assert crawler.fetch.await_count == factory.get_crawlers.await_count == 1
    for media_id, task_id in zip(media_ids, task_ids, strict=True):
        media = await repo.get_media_file(media_id)
        assert media is not None and media.metadata_id == metadata.id and media.status is MediaFileStatus.SCRAPED
        detail = await client.get(f"tasks/{task_id}")
        assert detail.status_code == 200
        response = detail.json()
        assert response["status"] == TaskStatus.DONE
        assert response["result"]["metadata_id"] == metadata.id
        assert "scrape_group_id" not in response and "scrape_group_id" not in response["payload"]
        children = await client.get(f"tasks/{task_id}/children")
        assert children.status_code == 200
        assert children.json()["total"] == 1
        child = children.json()["items"][0]
        assert child["type"] == TaskType.ORGANIZE and child["status"] == TaskStatus.QUEUED
        organize = await repo.get_task(child["id"])
        assert organize is not None and organize.payload["media_file_ids"] == [media_id]

    schema_response = await client.get("docs/openapi.json")
    assert schema_response.status_code == 200
    schemas = schema_response.json()["components"]["schemas"]
    assert "ScrapeGroup" not in schemas
    for model in ("TaskResponse", "TaskListItem", "TaskChildItem"):
        assert "scrape_group_id" not in schemas[model]["properties"]
