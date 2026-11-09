"""FC2 分集计划仅在尾部追加, 已完成的文件身份与编号持续保留."""

from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path
from typing import TYPE_CHECKING, Literal

import pytest
from structlog.testing import capture_logs

from amane.db.models import MediaFileStatus
from tests.handlers.test_fc2_specials import _make_group, _organize_one, _output

if TYPE_CHECKING:
    from amane.db.fc2_parts import Fc2PlannedMember
    from amane.db.repository import Repository
    from amane.media import ResourceStore
    from tests.handlers.test_fc2_specials import _Group


async def _add_member(repo: Repository, group: _Group, suffix: str) -> tuple[Path, int]:
    assert group.library.id is not None
    group.source_dir.mkdir(parents=True, exist_ok=True)
    path = group.source_dir / f"hhd800_com@fc4800334{suffix}.mp4"
    path.write_bytes(f"late-video:{suffix}".encode())
    media = await repo.create_media_file(
        group.library.id,
        path=str(path),
        number=group.metadata.number,
        metadata_id=group.metadata.id,
        status=MediaFileStatus.SCRAPED,
    )
    assert media.id is not None
    return path, media.id


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "timeline",
    [
        (("_4K", "main", None), ("_4K_sp", "special", None), ("_4K_fix", "fix", None)),
        (
            ("_4K", "main", None),
            ("_sp1_4K", "special", 1),
            ("_4K_sp2", "special", 2),
            ("_fix1_4K", "fix", 1),
            ("_4K_fix2", "fix", 2),
        ),
        (("_1_4K", "main", 1), ("_2_4K", "main", 2), ("_4K_sp", "special", None), ("_4K_fix", "fix", None)),
    ],
    ids=["main-special-fix", "numbered-specials-and-fixes", "second-main-before-special-and-fix"],
)
async def test_late_special_and_fix_append_after_main_source_and_index_are_removed(
    repo: Repository,
    resource_store: ResourceStore,
    tmp_path: Path,
    timeline: tuple[tuple[str, Literal["main", "special", "fix"], int | None], ...],
) -> None:
    group = await _make_group(repo, tmp_path, (timeline[0][0],), subtitle_template="{video_dir}/{video_name}.{ext}")
    assert group.library.id is not None and group.metadata.id is not None
    await repo.update_library(group.library.id, write_nfo=True)
    members_before: tuple[Fc2PlannedMember, ...] = ()
    frozen_plan_id: int | None = None
    expected_outputs: dict[str, bytes] = {}
    expected_roles: list[tuple[str, int | None, int]] = []

    for cd, (suffix, role, part) in enumerate(timeline, start=1):
        path, media_id = (group.files[0], group.media_ids[0]) if cd == 1 else await _add_member(repo, group, suffix)
        content = path.read_bytes()
        subtitle = path.with_suffix(".chs.srt")
        subtitle_content = f"{role}:{part}"
        subtitle.write_text(subtitle_content, encoding="utf-8")
        outcome = await _organize_one(repo, resource_store, group, media_id, tmp_path)
        assert (outcome.organized, outcome.failed, outcome.conflicted) == (1, 0, 0)
        target = _output(group, cd)
        assert target.read_bytes() == content
        assert target.with_suffix(".srt").read_text(encoding="utf-8") == subtitle_content
        assert not path.exists() and not subtitle.exists()
        assert await repo.get_media_file(media_id) is None
        assert not group.source_dir.exists() or not list(group.source_dir.iterdir())
        assert not (group.output_dir / f"{group.metadata.number}.mp4").exists()

        nfo_path = target.with_suffix(".nfo")
        nfo = ET.parse(nfo_path).getroot()
        assert nfo.findtext("num") == group.metadata.number
        assert nfo.findtext("title") == "FC2-4800334 Special group"
        nfo_output = await repo.get_nfo_output(nfo_path)
        assert nfo_output is not None
        assert (nfo_output.metadata_id, nfo_output.library_id) == (group.metadata.id, group.library.id)
        assert Path(nfo_output.video_path) == target

        for name, previous_content in expected_outputs.items():
            assert (group.output_dir / name).read_bytes() == previous_content
        expected_outputs.update(
            (file.name, file.read_bytes()) for file in (target, target.with_suffix(".srt"), nfo_path)
        )
        plan = await repo.get_fc2_part_plan(group.library.id, group.source_dir, group.metadata.number)
        assert plan is not None and plan.id is not None
        if frozen_plan_id is None:
            frozen_plan_id = plan.id
        assert plan.id == frozen_plan_id
        assert plan.members[: len(members_before)] == members_before
        expected_roles.append((role, part, cd))
        assert [(member.role, member.part, member.cd) for member in plan.members] == expected_roles
        assert all(member.placed and member.target_mtime_ns is not None for member in plan.members)
        members_before = plan.members

    assert len(await repo.list_nfo_outputs(group.metadata.id)) == len(timeline)
    assert {file.name: file.read_bytes() for file in group.output_dir.iterdir()} == expected_outputs


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("suffixes", "late_suffix", "reason"),
    [
        (("_4K", "_sp1", "_fix"), "_sp2_4K", "分集编号"),
        (("_4K", "_fix1"), "_sp_4K", "分集编号"),
        (("_4K", "_sp1"), "_sp3_4K", "连续"),
        (("_4K", "_sp"), "_sp2_4K", "未编号"),
        (("_4K", "_fix1"), "_fix3_4K", "连续"),
        (("_4K", "_sp1"), "_custom_release", "无法确定角色或分段"),
        (("_4K",), "_1080p", "未编号"),
    ],
    ids=[
        "special-before-placed-fix",
        "new-special-before-fix",
        "special-gap",
        "mixed-special",
        "fix-gap",
        "unknown-late-name",
        "duplicate-main-version",
    ],
)
async def test_late_members_that_reorder_or_leave_gaps_preserve_plan_and_existing_outputs(
    repo: Repository,
    resource_store: ResourceStore,
    tmp_path: Path,
    suffixes: tuple[str, ...],
    late_suffix: str,
    reason: str,
) -> None:
    group = await _make_group(repo, tmp_path, suffixes, subtitle_template="{video_dir}/{video_name}.{ext}")
    for media_id in reversed(group.media_ids):
        outcome = await _organize_one(repo, resource_store, group, media_id, tmp_path)
        assert (outcome.organized, outcome.failed, outcome.conflicted) == (1, 0, 0)
    assert group.library.id is not None
    before = await repo.get_fc2_part_plan(group.library.id, group.source_dir, group.metadata.number)
    assert before is not None
    outputs_before = {path.name: path.read_bytes() for path in group.output_dir.iterdir()}
    late, late_id = await _add_member(repo, group, late_suffix)
    late_content = late.read_bytes()
    subtitle = late.with_suffix(".chs.srt")
    subtitle.write_text("late subtitle", encoding="utf-8")

    with capture_logs() as records:
        rejected = await _organize_one(repo, resource_store, group, late_id, tmp_path)

    assert (rejected.organized, rejected.failed, rejected.conflicted) == (0, 1, 0)
    assert any(reason in str(record.get("error", "")) for record in records)
    after = await repo.get_fc2_part_plan(group.library.id, group.source_dir, group.metadata.number)
    assert after is not None and after.id == before.id and after.members == before.members
    assert late.read_bytes() == late_content
    assert subtitle.read_text(encoding="utf-8") == "late subtitle"
    retained = await repo.get_media_file(late_id)
    assert retained is not None and retained.path == str(late)
    assert {path.name: path.read_bytes() for path in group.output_dir.iterdir()} == outputs_before


@pytest.mark.asyncio
async def test_single_unknown_main_name_keeps_ordinary_organize_behavior(
    repo: Repository, resource_store: ResourceStore, tmp_path: Path
) -> None:
    group = await _make_group(repo, tmp_path, ("_custom_release",))
    assert group.library.id is not None
    await repo.update_library(
        group.library.id,
        video_template=f"{group.output_dir.as_posix()}/{{raw_name}}.{{ext}}",
    )
    original = group.files[0].read_bytes()
    outcome = await _organize_one(repo, resource_store, group, group.media_ids[0], tmp_path)
    assert (outcome.organized, outcome.failed, outcome.conflicted) == (1, 0, 0)
    assert (group.output_dir / group.files[0].name).read_bytes() == original
    assert not group.files[0].exists()
    assert await repo.get_media_file(group.media_ids[0]) is None
    assert await repo.get_fc2_part_plan(group.library.id, group.source_dir, group.metadata.number) is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("suffixes", "reason"),
    [
        (("_4K", "_1080p"), "多个未编号"),
        (("_1_4K", "_1_1080p"), "重复文件或不同版本"),
        (("_4K", "_custom_release"), "无法确定角色或分段"),
    ],
    ids=["different-quality", "same-part-different-quality", "known-and-unknown-name"],
)
async def test_ambiguous_main_group_stays_complete_before_and_after_special_arrives(
    repo: Repository,
    resource_store: ResourceStore,
    tmp_path: Path,
    suffixes: tuple[str, ...],
    reason: str,
) -> None:
    group = await _make_group(repo, tmp_path, suffixes)
    assert group.library.id is not None
    await repo.update_library(
        group.library.id,
        video_template=f"{group.output_dir.as_posix()}/{{raw_name}}.{{ext}}",
    )
    originals = tuple(path.read_bytes() for path in group.files)
    for media_id in group.media_ids:
        with capture_logs() as records:
            outcome = await _organize_one(repo, resource_store, group, media_id, tmp_path)
        assert (outcome.organized, outcome.failed, outcome.conflicted) == (0, 1, 0)
        assert any(reason in str(record.get("error", "")) for record in records)
        assert tuple(path.read_bytes() for path in group.files) == originals
        assert not group.output_dir.exists()
        assert await repo.get_fc2_part_plan(group.library.id, group.source_dir, group.metadata.number) is None
        for path, retained_id in zip(group.files, group.media_ids, strict=True):
            retained = await repo.get_media_file(retained_id)
            assert retained is not None and retained.path == str(path)

    special, special_id = await _add_member(repo, group, "_4K_sp")
    special_content = special.read_bytes()
    with capture_logs() as records:
        outcome = await _organize_one(repo, resource_store, group, special_id, tmp_path)
    assert (outcome.organized, outcome.failed, outcome.conflicted) == (0, 1, 0)
    assert any(reason in str(record.get("error", "")) for record in records)
    assert tuple(path.read_bytes() for path in group.files) == originals
    assert special.read_bytes() == special_content
    retained = await repo.get_media_file(special_id)
    assert retained is not None and retained.path == str(special)
    assert not group.output_dir.exists()
    assert await repo.get_fc2_part_plan(group.library.id, group.source_dir, group.metadata.number) is None
