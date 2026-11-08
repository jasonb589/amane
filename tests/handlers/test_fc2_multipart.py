"""FC2 下划线分段经过独立刮削与并发整理, 不覆盖另一段视频或字幕."""

import asyncio
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock
from xml.etree import ElementTree

import pytest

from amane.config import HotSettings
from amane.crawlers.base import Crawler
from amane.crawlers.factory import CrawlerFactory
from amane.crawlers.models import MediaMetadata
from amane.db.models import MediaFileStatus, TaskType
from amane.handlers import OrganizeHandler, OrganizePayload, ScrapeHandler, ScrapePayload
from amane.parsing import ContentType

if TYPE_CHECKING:
    from pathlib import Path

    from amane.db.repository import Repository
    from amane.media import ResourceStore


@pytest.mark.asyncio
@pytest.mark.parametrize("occupied_part", [None, 1])
async def test_fc2_parts_keep_distinct_external_outputs_after_concurrent_organize(
    repo: Repository, resource_store: ResourceStore, tmp_path: Path, occupied_part: int | None
) -> None:
    incoming = tmp_path / "incoming"
    source_dir = incoming / "正常"
    source_dir.mkdir(parents=True)
    output = tmp_path / "organized"
    number = "FC2-4982441"
    library = await repo.create_library(
        name="FC2 incoming",
        path=str(incoming),
        auto_organize=True,
        video_template=f"{output.as_posix()}/{{studio}}/{{number}}/{{number}}[-CD{{cd?}}].{{ext}}",
        subtitle_template="{video_dir}/{video_name}.{ext}",
        subtitle_extensions=[".srt"],
        copy_resources=[],
    )
    assert library.id is not None
    config = HotSettings()
    config.scraping.content_routes[ContentType.FC2] = ["javarchive"]
    config.actor_scraping.auto_scrape = False
    source = AsyncMock(spec=Crawler)
    source.fetch.return_value = MediaMetadata(number=number, title="Multipart title", studio="Studio")
    factory = AsyncMock(spec=CrawlerFactory)
    factory.get_crawlers.return_value = {"javarchive": source}
    scrape = ScrapeHandler(repo, factory, resource_store, config)

    files: dict[int, tuple[Path, Path, int]] = {}
    for part in (1, 2):
        video = source_dir / f"4K688.com@fc4982441_{part}.mp4"
        subtitle = source_dir / f"4K688.com@fc4982441_{part}.srt"
        video.write_bytes(f"video-part-{part}".encode())
        subtitle.write_text(f"subtitle-part-{part}", encoding="utf-8")
        media = await repo.create_media_file(library.id, path=str(video))
        assert media.id is not None
        files[part] = (video, subtitle, media.id)

    scraped = await asyncio.gather(
        *(
            scrape.handle(
                ScrapePayload(
                    number=number,
                    content_type=ContentType.FC2,
                    media_file_id=media_id,
                    path=str(video),
                )
            )
            for video, _, media_id in files.values()
        )
    )
    metadata = await repo.get_metadata_by_number(number)
    assert metadata is not None and metadata.id is not None
    organize_payloads: list[OrganizePayload] = []
    for result, (video, subtitle, media_id) in zip(scraped, files.values(), strict=True):
        assert result.success
        (followup,) = [item for item in result.followups if item.task_type is TaskType.ORGANIZE]
        payload = OrganizePayload.model_validate(followup.payload)
        assert payload.media_file_ids == [media_id]
        organize_payloads.append(payload)
        linked = await repo.get_media_file(media_id)
        assert linked is not None and linked.metadata_id == metadata.id
        assert linked.status is MediaFileStatus.SCRAPED
        assert video.exists() and subtitle.exists()

    target_dir = output / "Studio" / number
    if occupied_part is not None:
        target_dir.mkdir(parents=True)
        (target_dir / f"{number}-CD{occupied_part}.mp4").write_bytes(b"unrelated-existing-video")
        (target_dir / f"{number}-CD{occupied_part}.nfo").write_text("existing-nfo", encoding="utf-8")
        (target_dir / f"{number}-CD{occupied_part}.srt").write_text("existing-subtitle", encoding="utf-8")

    # 同一个 handler 的库锁串行化落盘; 提交顺序反转以覆盖第二段先执行的实际故障场景.
    organize = OrganizeHandler(repo, config, resource_store, safe_dirs=[tmp_path])
    first = await asyncio.gather(*(organize.handle(payload) for payload in reversed(organize_payloads)))
    assert all(result.success and result.result is not None for result in first)
    outcomes = [result.result for result in first if result.result is not None]
    assert sum(result.organized for result in outcomes) == (2 if occupied_part is None else 1)
    assert sum(result.conflicted for result in outcomes) == (0 if occupied_part is None else 1)
    assert sum(result.failed for result in outcomes) == 0

    for part, (video, subtitle, media_id) in files.items():
        target = target_dir / f"{number}-CD{part}.mp4"
        nfo = target.with_suffix(".nfo")
        sub = target.with_suffix(".srt")
        if part == occupied_part:
            assert target.read_bytes() == b"unrelated-existing-video"
            assert nfo.read_text(encoding="utf-8") == "existing-nfo"
            assert sub.read_text(encoding="utf-8") == "existing-subtitle"
            assert video.read_bytes() == f"video-part-{part}".encode()
            assert subtitle.read_text(encoding="utf-8") == f"subtitle-part-{part}"
            current = await repo.get_media_file(media_id)
            assert current is not None and current.path == str(video)
        else:
            assert target.read_bytes() == f"video-part-{part}".encode()
            assert sub.read_text(encoding="utf-8") == f"subtitle-part-{part}"
            assert ElementTree.parse(nfo).getroot().findtext("title") == f"{number} Multipart title"
            recorded = await repo.get_nfo_output(nfo)
            assert recorded is not None and recorded.metadata_id == metadata.id
            assert not video.exists() and not subtitle.exists()
            assert await repo.get_media_file(media_id) is None

    assert not (target_dir / f"{number}.mp4").exists()
    snapshot = {path.name: path.read_bytes() for path in target_dir.iterdir() if path.is_file()}
    again = await asyncio.gather(*(organize.handle(payload) for payload in organize_payloads))
    repeated = [result.result for result in again if result.result is not None]
    assert len(repeated) == 2
    assert sum(result.organized for result in repeated) == 0
    assert sum(result.failed for result in repeated) == 0
    assert sum(result.conflicted for result in repeated) == (0 if occupied_part is None else 1)
    assert sum(result.skipped for result in repeated) == (2 if occupied_part is None else 1)
    assert {path.name: path.read_bytes() for path in target_dir.iterdir() if path.is_file()} == snapshot
