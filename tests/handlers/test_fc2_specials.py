"""FC2 正片与特集跨独立整理任务保留分集计划, 不依赖 NFO 或源索引."""

from __future__ import annotations

import os
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal

import pytest
from structlog.testing import capture_logs

from amane.config import HotSettings
from amane.db.models import MediaFileStatus
from amane.enums import MoveMode
from amane.handlers import OrganizeHandler, OrganizePayload
from amane.handlers.file import apply_file_operations
from amane.observability.models import OrganizeConflictReason
from amane.organize.file import PlaceOutcome
from amane.parsing import ContentType

if TYPE_CHECKING:
    from amane.db.models import Library, Metadata
    from amane.db.repository import Repository
    from amane.handlers.models import OrganizeResult
    from amane.media import ResourceStore


@dataclass(frozen=True)
class _Group:
    library: Library
    metadata: Metadata
    source_dir: Path
    output_dir: Path
    files: tuple[Path, ...]
    media_ids: tuple[int, ...]


async def _make_group(
    repo: Repository,
    root: Path,
    suffixes: tuple[str, ...],
    *,
    internal: bool = False,
    move_mode: MoveMode = MoveMode.MOVE,
    subtitle_template: str = "{video_dir}/{raw_srt_name}.{ext}",
) -> _Group:
    incoming = root / "incoming"
    source_dir = incoming / "source"
    source_dir.mkdir(parents=True)
    output_dir = (incoming if internal else root) / "organized"
    library = await repo.create_library(
        name="FC2 special integration",
        path=str(incoming),
        video_template=f"{output_dir.as_posix()}/{{number}}[-CD{{cd?}}].{{ext}}",
        subtitle_template=subtitle_template,
        subtitle_extensions=[".srt"],
        write_nfo=False,
        copy_resources=[],
        move_mode=move_mode,
    )
    assert library.id is not None
    metadata = await repo.upsert_metadata(number="FC2-4800334", title="Special group", content_type=ContentType.FC2)
    assert metadata.id is not None
    files: list[Path] = []
    media_ids: list[int] = []
    for index, suffix in enumerate(suffixes):
        path = source_dir / f"hhd800.com@fc4800334{suffix}.mp4"
        path.write_bytes(f"source-{index}:{suffix}".encode())
        media = await repo.create_media_file(
            library.id,
            path=str(path),
            number=metadata.number,
            metadata_id=metadata.id,
            status=MediaFileStatus.SCRAPED,
        )
        assert media.id is not None
        files.append(path)
        media_ids.append(media.id)
    return _Group(library, metadata, source_dir, output_dir, tuple(files), tuple(media_ids))


async def _organize_one(
    repo: Repository, resource_store: ResourceStore, group: _Group, media_id: int, safe_root: Path
) -> OrganizeResult:
    assert group.library.id is not None
    # 每次重新构造处理器, 确保编号来自数据库, 而非上一个处理器的内存状态.
    handler = OrganizeHandler(repo, HotSettings(), resource_store, safe_dirs=[safe_root])
    result = await handler.handle(OrganizePayload(library_id=group.library.id, media_file_ids=[media_id]))
    assert result.success and result.result is not None
    return result.result


def _output(group: _Group, cd: int) -> Path:
    return group.output_dir / f"{group.metadata.number}-CD{cd}.mp4"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "suffixes",
    [
        ("_4K", "_sp_4K"),
        ("_1_4K", "_2_4K", "_sp_4K"),
        ("_1_4K", "_2_4K", "_sp1_4K", "_sp2_4K"),
    ],
)
async def test_reverse_single_file_tasks_keep_cd_after_external_move_and_handler_rebuild(
    repo: Repository, resource_store: ResourceStore, tmp_path: Path, suffixes: tuple[str, ...]
) -> None:
    group = await _make_group(repo, tmp_path, suffixes)
    originals = [path.read_bytes() for path in group.files]
    for index in reversed(range(len(group.files))):
        outcome = await _organize_one(repo, resource_store, group, group.media_ids[index], tmp_path)
        assert (outcome.organized, outcome.failed, outcome.conflicted) == (1, 0, 0)
        assert _output(group, index + 1).read_bytes() == originals[index]
        assert not group.files[index].exists()
        assert await repo.get_media_file(group.media_ids[index]) is None

    assert group.library.id is not None
    plan = await repo.get_fc2_part_plan(group.library.id, group.source_dir, group.metadata.number)
    assert plan is not None and len(plan.members) == len(suffixes)
    assert all(member.placed and member.target_mtime_ns is not None for member in plan.members)
    assert not list(group.output_dir.glob("*.nfo"))
    assert not (group.output_dir / f"{group.metadata.number}.mp4").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("filenames", "expected_members"),
    [
        (
            ("FC2-3049697-4K.mp4", "hhd800_com@fc3049697_4K_sp.mp4"),
            (("main", None), ("special", None)),
        ),
        (
            (
                "FC2-3049697_1_4K.mp4",
                "FC2-3049697_2_4K.mp4",
                "hhd800_com@fc3049697_4K_sp1.mp4",
                "hhd800.com@fc3049697_4K_sp2.mp4",
            ),
            (("main", 1), ("main", 2), ("special", 1), ("special", 2)),
        ),
    ],
    ids=["main-and-post-quality-special", "two-main-and-two-post-quality-specials"],
)
async def test_post_quality_specials_keep_video_subtitle_and_nfo_cd_after_separate_moves(
    repo: Repository,
    resource_store: ResourceStore,
    tmp_path: Path,
    filenames: tuple[str, ...],
    expected_members: tuple[tuple[Literal["main", "special"], int | None], ...],
) -> None:
    incoming = tmp_path / "incoming"
    source_dir = incoming / "source"
    source_dir.mkdir(parents=True)
    output_dir = tmp_path / "organized"
    library = await repo.create_library(
        name="FC2 real filename integration",
        path=str(incoming),
        video_template=f"{output_dir.as_posix()}/{{number}}[-CD{{cd?}}].{{ext}}",
        subtitle_template="{video_dir}/{video_name}.{ext}",
        subtitle_extensions=[".srt"],
        write_nfo=True,
        copy_resources=[],
        move_mode=MoveMode.MOVE,
    )
    assert library.id is not None
    metadata = await repo.upsert_metadata(number="FC2-3049697", title="Special group", content_type=ContentType.FC2)
    assert metadata.id is not None
    files = tuple(source_dir / filename for filename in filenames)
    originals = tuple(f"video-{index}:{filename}".encode() for index, filename in enumerate(filenames))
    subtitles = tuple(path.with_suffix(".chs.srt") for path in files)
    subtitle_contents = tuple(f"subtitle-{index}:{filename}" for index, filename in enumerate(filenames))
    media_ids: list[int] = []
    for path, content, subtitle, subtitle_content in zip(files, originals, subtitles, subtitle_contents, strict=True):
        path.write_bytes(content)
        subtitle.write_text(subtitle_content, encoding="utf-8")
        media = await repo.create_media_file(
            library.id,
            path=str(path),
            number=metadata.number,
            metadata_id=metadata.id,
            status=MediaFileStatus.SCRAPED,
        )
        assert media.id is not None
        media_ids.append(media.id)
    group = _Group(library, metadata, source_dir, output_dir, files, tuple(media_ids))
    expected_assignments = {
        str(path): (role, part, cd)
        for cd, (path, (role, part)) in enumerate(zip(files, expected_members, strict=True), start=1)
    }
    frozen_plan_id: int | None = None

    # 先移走最后一份特集, 每次使用独立处理器, 后续正片仍沿用第一次保存的编号.
    for index in reversed(range(len(files))):
        outcome = await _organize_one(repo, resource_store, group, media_ids[index], tmp_path)
        assert (outcome.organized, outcome.failed, outcome.conflicted) == (1, 0, 0)
        target = _output(group, index + 1)
        assert target.read_bytes() == originals[index]
        assert target.with_suffix(".srt").read_text(encoding="utf-8") == subtitle_contents[index]
        assert not files[index].exists() and not subtitles[index].exists()
        assert await repo.get_media_file(media_ids[index]) is None
        for remaining in range(index):
            assert files[remaining].read_bytes() == originals[remaining]
            assert subtitles[remaining].read_text(encoding="utf-8") == subtitle_contents[remaining]

        nfo_path = target.with_suffix(".nfo")
        nfo = ET.parse(nfo_path).getroot()
        assert nfo.findtext("num") == metadata.number
        assert nfo.findtext("title") == "FC2-3049697 Special group"
        nfo_output = await repo.get_nfo_output(nfo_path)
        assert nfo_output is not None
        assert (nfo_output.library_id, nfo_output.metadata_id) == (library.id, metadata.id)
        assert Path(nfo_output.video_path) == target

        plan = await repo.get_fc2_part_plan(library.id, source_dir, metadata.number)
        assert plan is not None and plan.id is not None
        if frozen_plan_id is None:
            frozen_plan_id = plan.id
        assert plan.id == frozen_plan_id
        assert {
            member.source_path: (member.role, member.part, member.cd) for member in plan.members
        } == expected_assignments
        assert {member.source_path for member in plan.members if member.placed} == {str(path) for path in files[index:]}
        assert all(member.target_mtime_ns is not None for member in plan.members if member.placed)

    assert len(list(output_dir.glob("*.mp4"))) == len(files)
    assert len(list(output_dir.glob("*.srt"))) == len(files)
    assert len(list(output_dir.glob("*.nfo"))) == len(files)
    assert len(await repo.list_nfo_outputs(metadata.id)) == len(files)


@pytest.mark.asyncio
async def test_unindexed_same_directory_members_determine_cd_without_expanding_selected_scope(
    repo: Repository, resource_store: ResourceStore, tmp_path: Path
) -> None:
    group = await _make_group(repo, tmp_path, ("_sp_4K",))
    main = group.source_dir / "fc4800334_4K.mp4"
    main.write_bytes(b"unindexed-main")
    other_dir = group.source_dir.parent / "other"
    other_dir.mkdir()
    other_special = other_dir / "fc4800334_sp1.mp4"
    other_special.write_bytes(b"other-directory-special")
    other_number = group.source_dir / "fc4800335_sp.mp4"
    other_number.write_bytes(b"other-number-special")

    outcome = await _organize_one(repo, resource_store, group, group.media_ids[0], tmp_path)
    assert (outcome.organized, outcome.failed) == (1, 0)
    assert _output(group, 2).read_bytes() == b"source-0:_sp_4K"
    assert main.read_bytes() == b"unindexed-main"
    assert other_special.read_bytes() == b"other-directory-special"
    assert other_number.read_bytes() == b"other-number-special"
    assert await repo.get_media_file_by_path(str(main)) is None
    assert group.library.id is not None
    plan = await repo.get_fc2_part_plan(group.library.id, group.source_dir, group.metadata.number)
    assert plan is not None
    assert {Path(member.source_path) for member in plan.members} == {main, group.files[0]}
    assert not _output(group, 1).exists()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("suffixes", "reason"),
    [
        (("_1_4K", "_1_1080p", "_sp"), "重复文件或不同版本"),
        (("_4K", "_spX_4K"), "无法确定角色或分段"),
        (("_sp",), "缺少同组正片"),
        (("_1", "_3", "_sp"), "编号须从 1 连续排列"),
        (("_4K", "_sp", "_sp1"), "同时包含未编号文件与已编号分段"),
    ],
)
async def test_ambiguous_groups_fail_before_moving_any_file(
    repo: Repository,
    resource_store: ResourceStore,
    tmp_path: Path,
    suffixes: tuple[str, ...],
    reason: str,
) -> None:
    group = await _make_group(repo, tmp_path, suffixes)
    originals = [path.read_bytes() for path in group.files]
    with capture_logs() as records:
        outcome = await _organize_one(repo, resource_store, group, group.media_ids[-1], tmp_path)
    assert (outcome.organized, outcome.failed, outcome.conflicted) == (0, 1, 0)
    assert any(reason in str(record.get("error", "")) for record in records)
    assert [path.read_bytes() for path in group.files] == originals
    assert not group.output_dir.exists()
    assert group.library.id is not None
    assert await repo.get_fc2_part_plan(group.library.id, group.source_dir, group.metadata.number) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["late", "size", "mtime"])
async def test_frozen_plan_rejects_late_members_and_changed_sources(
    repo: Repository,
    resource_store: ResourceStore,
    tmp_path: Path,
    change: Literal["late", "size", "mtime"],
) -> None:
    group = await _make_group(repo, tmp_path, ("_1", "_2", "_sp"))
    first = await _organize_one(repo, resource_store, group, group.media_ids[-1], tmp_path)
    assert first.organized == 1 and first.failed == 0
    special = _output(group, 3).read_bytes()
    if change == "late":
        (group.source_dir / "fc4800334_sp1.mp4").write_bytes(b"late-special")
        reason = "同组新增文件"
    elif change == "size":
        group.files[0].write_bytes(b"replacement-main-with-a-different-size")
        reason = "源文件已变化"
    else:
        stat = group.files[0].stat()
        os.utime(group.files[0], ns=(stat.st_atime_ns, stat.st_mtime_ns + 2_000_000_000))
        reason = "源文件已变化"

    with capture_logs() as records:
        outcome = await _organize_one(repo, resource_store, group, group.media_ids[0], tmp_path)
    assert (outcome.organized, outcome.failed, outcome.conflicted) == (0, 1, 0)
    assert any(reason in str(record.get("error", "")) for record in records)
    assert group.files[0].exists() and group.files[1].exists()
    assert not _output(group, 1).exists() and not _output(group, 2).exists()
    assert _output(group, 3).read_bytes() == special


@pytest.mark.asyncio
async def test_target_conflict_can_retry_without_reassigning_special_cd(
    repo: Repository, resource_store: ResourceStore, tmp_path: Path
) -> None:
    group = await _make_group(repo, tmp_path, ("_4K", "_sp_4K"))
    target = _output(group, 2)
    target.parent.mkdir(parents=True)
    target.write_bytes(b"existing-unrelated-video")
    first = await _organize_one(repo, resource_store, group, group.media_ids[1], tmp_path)
    assert (first.organized, first.conflicted, first.failed) == (0, 1, 0)
    assert first.conflicts[0].reason is OrganizeConflictReason.target_exists
    assert target.read_bytes() == b"existing-unrelated-video"
    assert all(path.exists() for path in group.files)
    assert group.library.id is not None
    before = await repo.get_fc2_part_plan(group.library.id, group.source_dir, group.metadata.number)
    assert before is not None

    target.unlink()
    main = await _organize_one(repo, resource_store, group, group.media_ids[0], tmp_path)
    assert (main.organized, main.failed) == (1, 0)
    retried = await _organize_one(repo, resource_store, group, group.media_ids[1], tmp_path)
    assert (retried.organized, retried.failed, retried.conflicted) == (1, 0, 0)
    after = await repo.get_fc2_part_plan(group.library.id, group.source_dir, group.metadata.number)
    assert after is not None and after.id == before.id
    assert [(member.role, member.cd) for member in after.members] == [("main", 1), ("special", 2)]
    assert target.read_bytes() == b"source-1:_sp_4K"


@pytest.mark.asyncio
async def test_special_subtitle_uses_output_cd_after_two_main_segments(
    repo: Repository, resource_store: ResourceStore, tmp_path: Path
) -> None:
    group = await _make_group(
        repo, tmp_path, ("_1_4K", "_2_4K", "_sp_4K"), subtitle_template="{video_dir}/{video_name}.{ext}"
    )
    subtitles = []
    for path, content in zip(group.files, ("main-one", "main-two", "special"), strict=True):
        subtitle = path.with_suffix(".chs.srt")
        subtitle.write_text(content, encoding="utf-8")
        subtitles.append(subtitle)
    special = await _organize_one(repo, resource_store, group, group.media_ids[2], tmp_path)
    assert (special.organized, special.failed, special.conflicted) == (1, 0, 0)
    assert _output(group, 3).with_suffix(".srt").read_text(encoding="utf-8") == "special"
    assert not subtitles[2].exists()
    assert subtitles[0].read_text(encoding="utf-8") == "main-one"
    assert subtitles[1].read_text(encoding="utf-8") == "main-two"
    for index in (1, 0):
        outcome = await _organize_one(repo, resource_store, group, group.media_ids[index], tmp_path)
        assert (outcome.organized, outcome.failed, outcome.conflicted) == (1, 0, 0)
        assert (
            _output(group, index + 1).with_suffix(".srt").read_text(encoding="utf-8") == ("main-one", "main-two")[index]
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", [MoveMode.MOVE, MoveMode.COPY])
async def test_internal_output_reorganizes_with_saved_role_and_target_identity(
    repo: Repository,
    resource_store: ResourceStore,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: MoveMode,
) -> None:
    group = await _make_group(repo, tmp_path, ("_4K", "_sp_4K"), internal=True, move_mode=mode)
    if mode is MoveMode.COPY:
        original_copy = Path.copy

        def copy_with_new_mtime(source: Path, target: Path) -> Path:
            copied = original_copy(source, target)
            source_stat = source.stat()
            os.utime(copied, ns=(source_stat.st_atime_ns, source_stat.st_mtime_ns + 2_000_000_000))
            return copied

        monkeypatch.setattr(Path, "copy", copy_with_new_mtime)

    for media_id in reversed(group.media_ids):
        outcome = await _organize_one(repo, resource_store, group, media_id, tmp_path)
        assert (outcome.organized, outcome.failed, outcome.conflicted) == (1, 0, 0)
    snapshot = {path.name: path.read_bytes() for path in group.output_dir.iterdir()}
    for index, media_id in enumerate(group.media_ids, start=1):
        media = await repo.get_media_file(media_id)
        assert media is not None and media.path == str(_output(group, index))
        repeated = await _organize_one(repo, resource_store, group, media_id, tmp_path)
        assert (repeated.organized, repeated.failed, repeated.conflicted) == (1, 0, 0)
    assert {path.name: path.read_bytes() for path in group.output_dir.iterdir()} == snapshot
    assert group.library.id is not None
    plan = await repo.get_fc2_part_plan(group.library.id, group.source_dir, group.metadata.number)
    assert plan is not None
    assert [(member.role, member.cd) for member in plan.members] == [("main", 1), ("special", 2)]
    if mode is MoveMode.COPY:
        assert all(member.target_mtime_ns != member.source_mtime_ns for member in plan.members)


@pytest.mark.asyncio
async def test_scan_glob_does_not_exclude_special_from_organize_plan(
    repo: Repository, resource_store: ResourceStore, tmp_path: Path
) -> None:
    group = await _make_group(repo, tmp_path, ("_1", "_sp"))
    assert group.library.id is not None
    await repo.update_library(group.library.id, patterns=["*_1.mp4"])
    main = await _organize_one(repo, resource_store, group, group.media_ids[0], tmp_path)
    assert (main.organized, main.failed) == (1, 0)
    plan = await repo.get_fc2_part_plan(group.library.id, group.source_dir, group.metadata.number)
    assert plan is not None
    assert [(member.role, member.cd) for member in plan.members] == [("main", 1), ("special", 2)]
    special = await _organize_one(repo, resource_store, group, group.media_ids[1], tmp_path)
    assert (special.organized, special.failed) == (1, 0)
    assert _output(group, 2).read_bytes() == b"source-1:_sp"


@pytest.mark.asyncio
@pytest.mark.parametrize("special_name", ["fc4800334_4K_sp0.mp4", "release-copy@fc4800334_sp.mp4"])
async def test_unparsed_special_intent_blocks_even_when_selected_file_is_main(
    repo: Repository, resource_store: ResourceStore, tmp_path: Path, special_name: str
) -> None:
    group = await _make_group(repo, tmp_path, ("_4K",))
    special = group.source_dir / special_name
    special.write_bytes(b"unparsed-special")
    with capture_logs() as records:
        outcome = await _organize_one(repo, resource_store, group, group.media_ids[0], tmp_path)
    assert (outcome.organized, outcome.failed) == (0, 1)
    assert any("无法确定角色或分段" in str(record.get("error", "")) for record in records)
    assert special.read_bytes() == b"unparsed-special" and group.files[0].exists()
    assert not group.output_dir.exists()
    assert group.library.id is not None
    assert await repo.get_fc2_part_plan(group.library.id, group.source_dir, group.metadata.number) is None


@pytest.mark.asyncio
async def test_nfo_write_failure_keeps_placed_special_and_remaining_cd_assignment(
    repo: Repository, resource_store: ResourceStore, tmp_path: Path
) -> None:
    group = await _make_group(repo, tmp_path, ("_4K", "_sp_4K"))
    assert group.library.id is not None
    await repo.update_library(group.library.id, write_nfo=True)
    blocked_nfo = _output(group, 2).with_suffix(".nfo")
    blocked_nfo.mkdir(parents=True)

    with capture_logs() as records:
        special = await _organize_one(repo, resource_store, group, group.media_ids[1], tmp_path)
    assert (special.organized, special.failed) == (0, 1)
    assert any("NFO 写入失败" in str(record.get("error", "")) for record in records)
    assert _output(group, 2).read_bytes() == b"source-1:_sp_4K"
    assert await repo.get_media_file(group.media_ids[1]) is None
    plan = await repo.get_fc2_part_plan(group.library.id, group.source_dir, group.metadata.number)
    assert plan is not None
    placed_special = next(member for member in plan.members if member.role == "special")
    assert placed_special.placed and placed_special.cd == 2

    main = await _organize_one(repo, resource_store, group, group.media_ids[0], tmp_path)
    assert (main.organized, main.failed) == (1, 0)
    assert _output(group, 1).read_bytes() == b"source-0:_4K"
    assert _output(group, 1).with_suffix(".nfo").is_file()


@pytest.mark.asyncio
async def test_direct_file_operations_do_not_reserve_plan_without_filtered_current_special(
    repo: Repository, resource_store: ResourceStore, tmp_path: Path
) -> None:
    group = await _make_group(repo, tmp_path, ("_4K", "_sp_4K"))
    group.files[0].write_bytes(b"main" * 32)
    group.files[1].write_bytes(b"sp")
    assert group.library.id is not None
    await repo.update_library(group.library.id, min_file_size=32)

    result = await apply_file_operations(
        repo,
        group.media_ids[1],
        group.metadata,
        HotSettings(),
        resource_store,
        write_nfo=False,
        copy_resources=[],
        safe_dirs=[tmp_path],
    )
    assert result is not None and result.outcome is PlaceOutcome.FAILED
    assert result.error is not None and "不符合分集候选规则" in result.error
    assert await repo.get_fc2_part_plan(group.library.id, group.source_dir, group.metadata.number) is None
    assert all(path.exists() for path in group.files) and not group.output_dir.exists()


@pytest.mark.asyncio
async def test_special_bound_to_another_fc2_number_fails_without_moving(
    repo: Repository, resource_store: ResourceStore, tmp_path: Path
) -> None:
    group = await _make_group(repo, tmp_path, ("_4K", "_sp_4K"))
    other = await repo.upsert_metadata(number="FC2-4800335", content_type=ContentType.FC2)
    assert other.id is not None
    await repo.update_media_file(group.media_ids[1], metadata_id=other.id, number=other.number)

    with capture_logs() as records:
        outcome = await _organize_one(repo, resource_store, group, group.media_ids[1], tmp_path)
    assert (outcome.organized, outcome.failed) == (0, 1)
    assert any("文件名与关联影片番号不一致" in str(record.get("error", "")) for record in records)
    assert all(path.exists() for path in group.files) and not group.output_dir.exists()
    assert group.library.id is not None
    assert await repo.list_fc2_part_plans(group.library.id, other.number) == []


@pytest.mark.asyncio
async def test_same_directory_candidate_owned_by_another_library_rejects_group(
    repo: Repository, resource_store: ResourceStore, tmp_path: Path
) -> None:
    group = await _make_group(repo, tmp_path, ("_sp_4K",))
    other_library = await repo.create_library(name="Overlapping index", path=str(group.source_dir))
    assert other_library.id is not None
    main = group.source_dir / "fc4800334_4K.mp4"
    main.write_bytes(b"main-owned-by-other-library")
    indexed = await repo.create_media_file(
        other_library.id,
        path=str(main),
        number=group.metadata.number,
        metadata_id=group.metadata.id,
        status=MediaFileStatus.SCRAPED,
    )
    assert indexed.id is not None

    with capture_logs() as records:
        outcome = await _organize_one(repo, resource_store, group, group.media_ids[0], tmp_path)
    assert (outcome.organized, outcome.failed) == (0, 1)
    assert any("属于其他媒体库" in str(record.get("error", "")) for record in records)
    assert main.read_bytes() == b"main-owned-by-other-library" and group.files[0].exists()
    assert not group.output_dir.exists()
    assert group.library.id is not None
    assert await repo.get_fc2_part_plan(group.library.id, group.source_dir, group.metadata.number) is None
    retained = await repo.get_media_file(indexed.id)
    assert retained is not None and retained.library_id == other_library.id
