"""编辑回写 NFO 的位置记忆、归属、安全边界与失败隔离."""

import asyncio
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import pytest

from amane.config import HotSettings
from amane.db.models import Metadata
from amane.db.repo_types import WriteMode
from amane.enums import MoveMode
from amane.handlers.file import apply_file_operations, commit_organized_media_file
from amane.media.nfo import write_nfo
from amane.media.nfo_sync import sync_metadata_nfos
from amane.organize import PlaceOutcome

if TYPE_CHECKING:
    from amane.db.repository import Repository
    from amane.media import ResourceStore


@pytest.mark.asyncio
async def test_outside_move_keeps_nfo_location_after_media_row_is_deleted(
    repo: Repository, resource_store: ResourceStore, tmp_path: Path
) -> None:
    incoming = tmp_path / "incoming"
    incoming.mkdir()
    video = incoming / "ABC-001.mp4"
    video.write_bytes(b"media")
    destination = tmp_path / "organized"
    library = await repo.create_library(
        "incoming",
        str(incoming),
        move_mode=MoveMode.MOVE,
        video_template=str(destination / "{title}" / "{number}.{ext}"),
        nfo_template="{video_dir}/{title}.nfo",
    )
    assert library.id is not None
    metadata = await repo.upsert_metadata("ABC-001", title="Original")
    assert metadata.id is not None
    media = await repo.create_media_file(library_id=library.id, path=str(video), metadata_id=metadata.id)
    assert media.id is not None
    placed = await apply_file_operations(repo, media.id, metadata, HotSettings(), resource_store, safe_dirs=[tmp_path])
    assert placed is not None and placed.outcome is PlaceOutcome.PLACED and placed.dest is not None
    await commit_organized_media_file(repo, media, placed.dest, incoming)
    assert await repo.get_media_file(media.id) is None
    updated = await repo.update_metadata(
        metadata.id, mode=WriteMode.MANUAL, title="Edited", actors=["Actor"], runtime=180
    )
    assert updated is not None
    result = await sync_metadata_nfos(repo, updated, metadata, [tmp_path])
    assert result.written == 1 and result.failed == result.skipped == 0
    nfo = destination / "Original" / "Original.nfo"
    root = ET.parse(nfo).getroot()
    assert root.findtext("title") == "ABC-001 Edited"
    assert root.findtext("actor/name") == "Actor"
    assert root.findtext("runtime") == "180"
    assert placed.dest.is_file() and not video.exists()
    assert not (destination / "Edited").exists()


@pytest.mark.asyncio
async def test_existing_media_nfo_uses_previous_metadata_path_and_remembers_it(
    repo: Repository, tmp_path: Path
) -> None:
    video = tmp_path / "ABC-001.mp4"
    video.write_bytes(b"media")
    library = await repo.create_library("library", str(tmp_path), nfo_template="{video_dir}/{title}.nfo")
    metadata = await repo.upsert_metadata("ABC-001", title="Original")
    assert library.id is not None and metadata.id is not None
    await repo.create_media_file(library_id=library.id, path=str(video), metadata_id=metadata.id)
    nfo = tmp_path / "Original.nfo"
    assert await write_nfo(metadata, nfo)
    updated = await repo.update_metadata(metadata.id, mode=WriteMode.MANUAL, title="Edited")
    assert updated is not None
    first = await sync_metadata_nfos(repo, updated, metadata, [tmp_path])
    assert first.written == 1
    again = await repo.update_metadata(metadata.id, mode=WriteMode.MANUAL, title="Again")
    assert again is not None
    second = await sync_metadata_nfos(repo, again, updated, [tmp_path])
    assert second.written == 1
    assert ET.parse(nfo).findtext("title") == "ABC-001 Again"
    assert not (tmp_path / "Again.nfo").exists()


@pytest.mark.asyncio
async def test_failed_nfo_after_move_still_places_subtitles(
    repo: Repository, resource_store: ResourceStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    incoming = tmp_path / "incoming"
    incoming.mkdir()
    video = incoming / "ABC-001.mp4"
    video.write_bytes(b"media")
    subtitle = video.with_suffix(".srt")
    subtitle.write_text("subtitle", encoding="utf-8")
    destination = tmp_path / "organized"
    library = await repo.create_library(
        "incoming",
        str(incoming),
        move_mode=MoveMode.MOVE,
        video_template=str(destination / "{number}.{ext}"),
        subtitle_template="{video_dir}/{video_name}.{ext}",
    )
    metadata = await repo.upsert_metadata("ABC-001", title="Original")
    assert library.id is not None and metadata.id is not None
    media = await repo.create_media_file(library_id=library.id, path=str(video), metadata_id=metadata.id)
    assert media.id is not None
    monkeypatch.setattr("amane.handlers.file.write_nfo_file", AsyncMock(return_value=False))
    placed = await apply_file_operations(repo, media.id, metadata, HotSettings(), resource_store, safe_dirs=[tmp_path])
    assert placed is not None and placed.outcome is PlaceOutcome.FAILED and placed.dest is not None
    assert placed.error == "NFO 写入失败"
    await commit_organized_media_file(repo, media, placed.dest, incoming)
    assert await repo.get_media_file(media.id) is None
    assert placed.dest.read_bytes() == b"media"
    assert not video.exists() and not subtitle.exists()
    assert placed.dest.with_suffix(".srt").read_text(encoding="utf-8") == "subtitle"
    assert len(await repo.list_nfo_outputs(metadata.id)) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["missing_video", "missing_directory", "wrong_number", "outside", "disabled"])
async def test_stale_or_unsafe_output_is_not_overwritten(repo: Repository, tmp_path: Path, case: str) -> None:
    library_root = tmp_path / "library"
    library_root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    library = await repo.create_library("library", str(library_root), write_nfo=case != "disabled")
    metadata = await repo.upsert_metadata("ABC-001", title="Original")
    assert library.id is not None and metadata.id is not None
    directory = outside if case == "outside" else library_root
    video = directory / "ABC-001.mp4"
    nfo = directory / "ABC-001.nfo"
    if case not in {"missing_video", "missing_directory"}:
        video.write_bytes(b"media")
    if case == "missing_directory":
        nfo = directory / "absent" / "ABC-001.nfo"
    else:
        nfo.write_text(
            "<movie><num>ABC-999</num><title>Keep</title></movie>" if case == "wrong_number" else "original",
            encoding="utf-8",
        )
    assert await repo.remember_nfo_output(metadata.id, library.id, video, nfo)
    updated = await repo.update_metadata(metadata.id, mode=WriteMode.MANUAL, title="Edited")
    assert updated is not None
    result = await sync_metadata_nfos(repo, updated, metadata, [])
    assert result.written == 0
    assert result.failed == (1 if case in {"wrong_number", "outside"} else 0)
    assert result.skipped == (0 if case in {"wrong_number", "outside"} else 1)
    assert (await repo.get_metadata(metadata.id)).title == "Edited"  # type: ignore[union-attr]
    assert not (library_root / "absent").exists()
    if case != "missing_directory":
        assert "Edited" not in nfo.read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_legacy_outside_output_is_recovered_only_by_exact_existing_template(
    repo: Repository, tmp_path: Path
) -> None:
    incoming = tmp_path / "incoming"
    incoming.mkdir()
    destination = tmp_path / "organized" / "ABC-001"
    destination.mkdir(parents=True)
    video = destination / "ABC-001.mp4"
    video.write_bytes(b"media")
    await repo.create_library("incoming", str(incoming), video_template=str(destination / "{number}.{ext}"))
    metadata = await repo.upsert_metadata("ABC-001", title="Original")
    assert metadata.id is not None
    nfo = video.with_suffix(".nfo")
    assert await write_nfo(metadata, nfo)
    updated = await repo.update_metadata(metadata.id, mode=WriteMode.MANUAL, title="Edited")
    assert updated is not None
    result = await sync_metadata_nfos(repo, updated, metadata, [tmp_path])
    assert result.written == 1 and not result.unlocated
    assert ET.parse(nfo).findtext("title") == "ABC-001 Edited"
    assert len(await repo.list_nfo_outputs(metadata.id)) == 1


@pytest.mark.asyncio
async def test_nfo_ownership_and_lifecycle(repo: Repository, tmp_path: Path) -> None:
    library = await repo.create_library("library", str(tmp_path))
    first = await repo.upsert_metadata("ABC-001")
    second = await repo.upsert_metadata("ABC-002")
    assert library.id is not None and first.id is not None and second.id is not None
    nfo = tmp_path / "movie.nfo"
    video = tmp_path / "first.mp4"
    video.write_bytes(b"media")
    assert await write_nfo(first, nfo)
    assert await repo.remember_nfo_output(first.id, library.id, video, nfo)
    assert not await repo.remember_nfo_output(second.id, library.id, tmp_path / "second.mp4", nfo)
    await repo.merge_metadata_entries(second.id, [first.id])
    assert not await repo.list_nfo_outputs(first.id)
    assert len(await repo.list_nfo_outputs(second.id)) == 1
    result = await sync_metadata_nfos(repo, second, second, [tmp_path])
    assert result.failed == 1 and result.written == 0
    assert ET.parse(nfo).findtext("num") == first.number
    await repo.delete_library(library.id)
    assert not await repo.list_nfo_outputs(second.id)


@pytest.mark.asyncio
async def test_atomic_nfo_failure_preserves_existing_bytes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    nfo = tmp_path / "ABC-001.nfo"
    nfo.write_text("original", encoding="utf-8")

    def reject_replace(self: Path, target: Path) -> Path:
        raise PermissionError("read-only destination")

    monkeypatch.setattr(Path, "replace", reject_replace)
    assert not await write_nfo(Metadata(number="ABC-001", title="Edited"), nfo)
    assert nfo.read_text(encoding="utf-8") == "original"
    assert list(tmp_path.iterdir()) == [nfo]


@pytest.mark.asyncio
async def test_nfo_sync_rejects_link_without_overwriting_target(
    repo: Repository, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    library = await repo.create_library("library", str(tmp_path))
    metadata = await repo.upsert_metadata("ABC-001", title="Original")
    assert library.id is not None and metadata.id is not None
    video = tmp_path / "ABC-001.mp4"
    video.write_bytes(b"media")
    nfo = video.with_suffix(".nfo")
    assert await write_nfo(metadata, nfo)
    original = nfo.read_bytes()
    assert await repo.remember_nfo_output(metadata.id, library.id, video, nfo)
    real_is_symlink = Path.is_symlink
    monkeypatch.setattr(Path, "is_symlink", lambda path: path == nfo or real_is_symlink(path))
    result = await sync_metadata_nfos(repo, metadata, metadata, [tmp_path])
    assert result.failed == 1 and result.written == 0
    assert nfo.read_bytes() == original


@pytest.mark.asyncio
async def test_queued_older_sync_uses_latest_saved_metadata(repo: Repository, tmp_path: Path) -> None:
    library = await repo.create_library("library", str(tmp_path))
    old = await repo.upsert_metadata("ABC-001", title="Original")
    assert library.id is not None and old.id is not None
    video = tmp_path / "ABC-001.mp4"
    video.write_bytes(b"media")
    nfo = video.with_suffix(".nfo")
    assert await write_nfo(old, nfo)
    assert await repo.remember_nfo_output(old.id, library.id, video, nfo)
    await repo.nfo_sync_lock.acquire()
    first = asyncio.create_task(sync_metadata_nfos(repo, old, old, [tmp_path]))
    newer = await repo.update_metadata(old.id, mode=WriteMode.MANUAL, title="Latest")
    assert newer is not None
    repo.nfo_sync_lock.release()
    assert (await first).written == 1
    assert ET.parse(nfo).findtext("title") == "ABC-001 Latest"
