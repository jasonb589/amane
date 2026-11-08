"""FC 长编号、文件路径与显式类型通过任务接口进入同一刮削路由."""

from typing import TYPE_CHECKING

import pytest

from amane.api.models.tasks import ScrapeRequest
from amane.db.models import Feed
from amane.handlers.models import build_feed_scrape_payload
from amane.parsing import ContentType

if TYPE_CHECKING:
    from httpx2 import AsyncClient

    from amane.db.repository import Repository


@pytest.mark.asyncio
async def test_fc2_alias_submission_file_inference_and_batch_deduplication(
    client: AsyncClient, seed_library: Repository, stop_worker: None
) -> None:
    first = await seed_library.create_media_file(
        library_id=1, path="/media/gx/正常/4K688.com@fc4982441_1.mp4", number="FC-4982441"
    )
    second = await seed_library.create_media_file(library_id=1, path="/media/fc4982441_2.mp4")
    assert first.id is not None and second.id is not None

    # 文件记录中的旧编号不参与重新识别, URL 与缓存选项仍然保留.
    detail = "https://fc2ppv-db.com/ja/videos/4982441"
    response = await client.post("tasks", json={"type": "scrape", "media_id": first.id, "url": detail, "use_cache": []})
    assert response.status_code == 202
    payload = response.json()["payload"]
    assert (payload["number"], payload["content_type"], payload["media_file_id"]) == ("FC2-4982441", "fc2", first.id)
    assert payload["url"] == detail and payload["use_cache"] == []

    forced = await client.post("tasks", json={"type": "scrape", "number": "FC-4982441", "content_type": "censored"})
    assert forced.status_code == 202
    assert forced.json()["payload"]["number"] == "FC2-4982441"
    assert forced.json()["payload"]["content_type"] == "censored"

    batch = await client.post(
        "tasks/batch/submit",
        json={
            "submissions": [
                {"type": "scrape", "number": "fc4982441", "content_type": None},
                {"type": "scrape", "number": "FC2-4982441"},
                {"type": "scrape", "media_id": first.id},
                {"type": "scrape", "media_id": second.id},
                {"type": "scrape", "media_id": first.id, "number": "MIDV-123"},
            ]
        },
    )
    assert batch.status_code == 202
    body = batch.json()
    assert (body["submitted"], body["skipped"], body["missing"]) == (4, 1, 0)
    tasks = [await seed_library.get_task(task_id) for task_id in body["task_ids"]]
    assert [(task.payload["number"], task.payload["content_type"]) for task in tasks if task] == [
        ("FC2-4982441", "fc2"),
        ("FC2-4982441", "fc2"),
        ("FC2-4982441", "fc2"),
        ("MIDV-123", "censored"),
    ]
    assert [task.payload["media_file_id"] for task in tasks if task] == [None, first.id, second.id, first.id]


@pytest.mark.parametrize(
    ("number", "expected_number", "expected_type"),
    [
        ("FC-4982441", "FC2-4982441", ContentType.FC2),
        ("FC4982441", "FC2-4982441", ContentType.FC2),
        ("ＦＣ２－４９８２４４１", "FC2-4982441", ContentType.FC2),
        ("FC2-PPV-4982441", "FC2-PPV-4982441", ContentType.FC2),
        ("FC2.PPV.4982441", "FC2.PPV.4982441", ContentType.FC2),
        ("FC2－4982441", "FC2－4982441", ContentType.FC2),
        ("FC-123", "FC-123", ContentType.CENSORED),
        ("AFC-4982441", "AFC-4982441", ContentType.CENSORED),
        ("MIDV-123", "MIDV-123", ContentType.CENSORED),
    ],
)
def test_fc2_submission_and_feed_share_identity_without_overwriting_explicit_type(
    number: str, expected_number: str, expected_type: ContentType
) -> None:
    request = ScrapeRequest(number=number)
    payload = request.build_payload(None)
    assert payload is not None
    assert (payload.number, payload.content_type) == (expected_number, expected_type)
    for forced in (None, ContentType.WESTERN):
        feed = Feed(name="source", url="https://feed.example/rss", content_type=forced, use_cache=["trans"])
        result = build_feed_scrape_payload(feed, number)
        assert (result.number, result.content_type) == (expected_number, forced or expected_type)
        assert result.use_cache == {"trans"}
