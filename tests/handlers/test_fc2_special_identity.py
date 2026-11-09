"""FC2 特集固定身份在重试、外部修改和跨目录硬链接时保持独立."""

import os
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Literal

import pytest
from structlog.testing import capture_logs

import amane.handlers.file as file_handler
from amane.db.models import MediaFileStatus
from amane.enums import MoveMode
from tests.handlers.test_fc2_append import _add_member
from tests.handlers.test_fc2_specials import _make_group, _organize_one, _output

if TYPE_CHECKING:
    from amane.db.repository import Repository
    from amane.media import ResourceStore
    from amane.parsing import FileInfo
    from amane.parsing.fc2_parts import Fc2Member


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", [MoveMode.COPY, MoveMode.HARDLINK])
async def test_missing_completed_target_is_not_recreated_from_retained_source(
    repo: Repository, resource_store: ResourceStore, tmp_path: Path, mode: MoveMode
) -> None:
    group = await _make_group(repo, tmp_path, ("_4K", "_sp_4K"), move_mode=mode)
    source = group.files[1]
    original = source.read_bytes()
    first = await _organize_one(repo, resource_store, group, group.media_ids[1], tmp_path)
    assert (first.organized, first.failed) == (1, 0)
    assert group.library.id is not None
    before = await repo.get_fc2_part_plan(group.library.id, group.source_dir, group.metadata.number)
    assert before is not None and before.members[1].placed
    target = _output(group, 2)
    target.unlink()

    with capture_logs() as records:
        repeated = await _organize_one(repo, resource_store, group, group.media_ids[1], tmp_path)

    assert (repeated.organized, repeated.failed, repeated.conflicted) == (0, 1, 0)
    assert any("分集文件不存在" in str(record.get("error", "")) for record in records)
    assert not target.exists()
    assert source.read_bytes() == original
    retained = await repo.get_media_file(group.media_ids[1])
    assert retained is not None and retained.path == str(source)
    after = await repo.get_fc2_part_plan(group.library.id, group.source_dir, group.metadata.number)
    assert after is not None and after.members == before.members


@pytest.mark.asyncio
async def test_changed_source_after_subtitle_discovery_is_not_moved_or_marked_complete(
    repo: Repository, resource_store: ResourceStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    group = await _make_group(repo, tmp_path, ("_4K", "_sp_4K"), subtitle_template="{video_dir}/{video_name}.{ext}")
    source = group.files[1]
    original_stat = source.stat()
    replacement = b"Z" * original_stat.st_size
    subtitle = source.with_suffix(".chs.srt")
    subtitle.write_text("special subtitle", encoding="utf-8")
    discover = file_handler.discover_subtitles

    async def discover_then_replace(
        video_path: Path,
        extensions: Sequence[str],
        video: FileInfo,
        *,
        fc2_member: Fc2Member | None = None,
    ) -> list[Path]:
        found = await discover(video_path, extensions, video, fc2_member=fc2_member)
        assert found == [subtitle]
        source.write_bytes(replacement)
        os.utime(source, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns + 7_000_000_000))
        return found

    monkeypatch.setattr(file_handler, "discover_subtitles", discover_then_replace)
    with capture_logs() as records:
        outcome = await _organize_one(repo, resource_store, group, group.media_ids[1], tmp_path)

    assert (outcome.organized, outcome.failed, outcome.conflicted) == (0, 1, 0)
    assert any("源文件已变化" in str(record.get("error", "")) for record in records)
    assert source.read_bytes() == replacement
    assert subtitle.read_text(encoding="utf-8") == "special subtitle"
    assert not _output(group, 2).exists()
    assert not _output(group, 2).with_suffix(".srt").exists()
    retained = await repo.get_media_file(group.media_ids[1])
    assert retained is not None and retained.path == str(source)
    assert group.library.id is not None
    plan = await repo.get_fc2_part_plan(group.library.id, group.source_dir, group.metadata.number)
    assert plan is not None
    assert plan.members[1].source_mtime_ns == original_stat.st_mtime_ns
    assert not plan.members[1].placed and plan.members[1].target_mtime_ns is None


@pytest.mark.asyncio
async def test_hardlinked_source_in_another_group_cannot_claim_existing_output(
    repo: Repository, resource_store: ResourceStore, tmp_path: Path
) -> None:
    group = await _make_group(repo, tmp_path, ("_4K", "_sp_4K"), internal=True)
    second_directory = group.source_dir.parent / "second"
    second_directory.mkdir()
    second_main = second_directory / "fc4800334_4K.mp4"
    second_main.write_bytes(b"second group main")
    second_special = second_directory / "fc4800334_sp_4K.mp4"
    second_special.hardlink_to(group.files[1])
    assert group.library.id is not None
    second_media = await repo.create_media_file(
        group.library.id,
        path=str(second_special),
        number=group.metadata.number,
        metadata_id=group.metadata.id,
        status=MediaFileStatus.SCRAPED,
    )
    assert second_media.id is not None
    first = await _organize_one(repo, resource_store, group, group.media_ids[1], tmp_path)
    assert (first.organized, first.failed) == (1, 0)
    target = _output(group, 2)
    original = target.read_bytes()
    assert target.samefile(second_special)

    with capture_logs() as records:
        duplicate = await _organize_one(repo, resource_store, group, second_media.id, tmp_path)

    assert (duplicate.organized, duplicate.failed, duplicate.conflicted) == (0, 1, 0)
    assert any("另一原始目录的分组" in str(record.get("error", "")) for record in records)
    assert target.read_bytes() == second_special.read_bytes() == original
    assert second_main.read_bytes() == b"second group main"
    retained = await repo.get_media_file(second_media.id)
    assert retained is not None and retained.path == str(second_special)
    plans = await repo.list_fc2_part_plans(group.library.id, group.metadata.number)
    owners = [member for plan in plans for member in plan.members if member.target_path == str(target)]
    assert len(owners) == 1 and owners[0].placed
    second_plan = await repo.get_fc2_part_plan(group.library.id, second_directory, group.metadata.number)
    assert second_plan is not None
    assert all(member.target_path is None and not member.placed for member in second_plan.members)
    repeated = await _organize_one(repo, resource_store, group, group.media_ids[1], tmp_path)
    assert (repeated.organized, repeated.failed, repeated.conflicted) == (1, 0, 0)


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["missing", "size", "mtime"])
async def test_late_fix_rejects_missing_or_replaced_completed_target_before_extending_plan(
    repo: Repository,
    resource_store: ResourceStore,
    tmp_path: Path,
    change: Literal["missing", "size", "mtime"],
) -> None:
    group = await _make_group(repo, tmp_path, ("_4K", "_sp1"))
    for media_id in group.media_ids:
        outcome = await _organize_one(repo, resource_store, group, media_id, tmp_path)
        assert (outcome.organized, outcome.failed, outcome.conflicted) == (1, 0, 0)
        assert await repo.get_media_file(media_id) is None
    assert not any(path.exists() for path in group.files)
    assert group.library.id is not None
    before = await repo.get_fc2_part_plan(group.library.id, group.source_dir, group.metadata.number)
    assert before is not None and all(member.placed for member in before.members)
    target = _output(group, 1)
    original_stat = target.stat()
    if change == "missing":
        target.unlink()
        reason = "不存在"
    elif change == "size":
        target.write_bytes(b"replacement-video-with-a-different-file-size")
        reason = "已变化"
    else:
        target.write_bytes(b"X" * original_stat.st_size)
        os.utime(target, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns + 7_000_000_000))
        reason = "已变化"
    outputs_before = {path.name: path.read_bytes() for path in group.output_dir.iterdir()}
    late, late_id = await _add_member(repo, group, "_4K_fix")
    content = late.read_bytes()

    with capture_logs() as records:
        rejected = await _organize_one(repo, resource_store, group, late_id, tmp_path)

    assert (rejected.organized, rejected.failed, rejected.conflicted) == (0, 1, 0)
    assert any(reason in str(record.get("error", "")) for record in records)
    assert late.read_bytes() == content
    retained = await repo.get_media_file(late_id)
    assert retained is not None and retained.path == str(late)
    after = await repo.get_fc2_part_plan(group.library.id, group.source_dir, group.metadata.number)
    assert after is not None and after.id == before.id and after.members == before.members
    assert {path.name: path.read_bytes() for path in group.output_dir.iterdir()} == outputs_before
    assert not _output(group, 3).exists()
