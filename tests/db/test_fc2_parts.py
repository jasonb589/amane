"""FC2 分段分配的冻结、条件更新、并发与删除边界."""

import asyncio
import json
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from pydantic import ValidationError

from amane.db.fc2_parts import Fc2PlannedMember
from amane.db.models import TaskType
from amane.utils.path import nfc_path

if TYPE_CHECKING:
    from amane.db.models import Fc2PartPlan
    from amane.db.repository import Repository


def _members(directory: Path) -> tuple[Fc2PlannedMember, ...]:
    return (
        Fc2PlannedMember(
            source_path=str(directory / "fc4800334_4K.mp4"),
            role="main",
            cd=1,
            source_size=100,
            source_mtime_ns=1001,
        ),
        Fc2PlannedMember(
            source_path=str(directory / "fc4800334_sp_4K.mp4"),
            role="special",
            cd=2,
            source_size=50,
            source_mtime_ns=1002,
        ),
    )


async def _reserve(repo: Repository, directory: Path) -> Fc2PartPlan:
    library = await repo.create_library(name="FC2", path=str(directory))
    assert library.id is not None
    return await repo.reserve_fc2_part_plan(library.id, directory, "FC2-4800334", _members(directory))


@pytest.mark.asyncio
async def test_concurrent_reserve_reuses_normalized_group_and_keeps_progress(repo: Repository, tmp_path: Path) -> None:
    directory = tmp_path / "cafe\u0301"
    library = await repo.create_library(name="FC2", path=str(directory))
    assert library.id is not None
    members = _members(directory)
    plans = await asyncio.gather(
        *(
            repo.reserve_fc2_part_plan(
                library.id,
                directory if index % 2 else Path(nfc_path(str(directory))),
                "fc2-ppv-4800334" if index % 2 else "FC2-4800334",
                members if index % 2 else tuple(reversed(members)),
            )
            for index in range(10)
        )
    )
    plan = plans[0]
    assert plan.id is not None
    assert {item.id for item in plans} == {plan.id}
    assert [member.cd for member in plan.members] == [1, 2]
    target = tmp_path / "output" / "FC2-4800334-CD2.mp4"
    await repo.prepare_fc2_part_target(plan.id, members[1].source_path, 2, target)
    await repo.complete_fc2_part(plan.id, members[1].source_path, target, target_mtime_ns=2002)

    reused = await repo.reserve_fc2_part_plan(library.id, directory, "fc4800334", members)
    assert reused.members[1].placed
    assert reused.members[1].target_path == str(target)
    assert reused.members[1].target_mtime_ns == 2002
    assert len(await repo.list_fc2_part_plans(library.id, "FC2-4800334")) == 1


@pytest.mark.asyncio
async def test_reserved_membership_rejects_late_files_and_changed_identity(repo: Repository, tmp_path: Path) -> None:
    plan = await _reserve(repo, tmp_path)
    assert plan.id is not None
    members = _members(tmp_path)
    target = tmp_path / "output" / "CD1.mp4"
    await repo.prepare_fc2_part_target(plan.id, members[0].source_path, 1, target)
    await repo.complete_fc2_part(plan.id, members[0].source_path, target, target_mtime_ns=2001)
    changes: list[dict[str, object]] = [
        {"source_path": str(tmp_path / "replacement.mp4")},
        {"source_size": 101},
        {"source_mtime_ns": 1003},
        {"role": "special"},
        {"part": 1},
        {"cd": 3},
    ]
    candidates: list[tuple[Fc2PlannedMember, ...]] = [
        (members[0].model_copy(update=change), members[1]) for change in changes
    ]
    candidates.extend(
        [
            (members[0],),
            (*members, members[1].model_copy(update={"source_path": str(tmp_path / "sp2.mp4"), "cd": 3})),
        ]
    )
    for candidate in candidates:
        with pytest.raises(ValueError, match=r"文件组已固定.*重新整理整组"):
            await repo.reserve_fc2_part_plan(plan.library_id, tmp_path, plan.number, candidate)
    saved = await repo.get_fc2_part_plan(plan.library_id, tmp_path, plan.number)
    assert saved is not None
    assert len(saved.members) == 2
    assert saved.members[0].placed
    assert saved.members[0].target_path == str(target)


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"source_path": "relative.mp4"}, "非空绝对路径"),
        ({"source_path": ""}, "非空绝对路径"),
        ({"source_path": "\x00"}, "非空绝对路径"),
        ({"source_size": -1}, "greater than or equal to 0"),
        ({"source_size": True}, "valid integer"),
        ({"source_mtime_ns": -1}, "greater than or equal to 0"),
        ({"source_mtime_ns": 1.5}, "valid integer"),
        ({"cd": 0}, "greater than 0"),
        ({"cd": True}, "valid integer"),
        ({"part": 0}, "greater than 0"),
        ({"part": "1"}, "valid integer"),
        ({"role": "unknown"}, "main.*special"),
        ({"placed": True}, "目标路径与目标修改时间"),
        ({"target_mtime_ns": 3001}, "尚未落盘"),
        ({"unexpected": 1}, "Extra inputs"),
    ],
)
def test_member_rejects_invalid_identity(tmp_path: Path, change: dict[str, object], message: str) -> None:
    value = {**_members(tmp_path)[0].model_dump(), **change}
    with pytest.raises(ValidationError, match=message):
        Fc2PlannedMember.model_validate(value)


@pytest.mark.asyncio
async def test_reserve_rejects_invalid_group_without_writing(repo: Repository, tmp_path: Path) -> None:
    library = await repo.create_library(name="FC2", path=str(tmp_path))
    assert library.id is not None
    members = _members(tmp_path)
    cases: list[tuple[tuple[Fc2PlannedMember, ...], str]] = [
        ((), "不能为空"),
        ((members[0], members[0].model_copy(update={"cd": 2})), "重复源路径"),
        ((members[0], members[1].model_copy(update={"cd": 1})), "重复输出编号"),
        ((members[0].model_copy(update={"source_path": str(tmp_path.parent / "outside.mp4")}),), "同一原始目录"),
        ((members[0].model_copy(update={"target_path": str(tmp_path / "CD1.mp4")}),), "不能指定目标"),
    ]
    for candidate, message in cases:
        with pytest.raises(ValueError, match=message):
            await repo.reserve_fc2_part_plan(library.id, tmp_path, "FC2-4800334", candidate)
    for number in ("", "ABC-123", "FC2-1"):
        with pytest.raises(ValueError, match="有效的 FC2 番号"):
            await repo.reserve_fc2_part_plan(library.id, tmp_path, number, members)
    with pytest.raises(ValueError, match="媒体库不存在"):
        await repo.reserve_fc2_part_plan(999999, tmp_path, "FC2-4800334", members)
    assert await repo.list_fc2_part_plans(library.id, "FC2-4800334") == []


@pytest.mark.asyncio
async def test_concurrent_member_updates_do_not_lose_json_progress(repo: Repository, tmp_path: Path) -> None:
    plan = await _reserve(repo, tmp_path)
    assert plan.id is not None
    targets = [tmp_path / "output" / f"CD{member.cd}.mp4" for member in plan.members]
    await asyncio.gather(
        *(
            repo.prepare_fc2_part_target(plan.id, member.source_path, member.cd, target)
            for member, target in zip(plan.members, targets, strict=True)
        )
    )
    await asyncio.gather(
        *(
            repo.complete_fc2_part(plan.id, member.source_path, target, target_mtime_ns=2000 + member.cd)
            for member, target in zip(plan.members, targets, strict=True)
        ),
        repo.prepare_fc2_part_target(plan.id, plan.members[0].source_path, 1, targets[0]),
    )
    saved = await repo.get_fc2_part_plan(plan.library_id, tmp_path, plan.number)
    assert saved is not None
    assert [member.target_path for member in saved.members] == [str(path) for path in targets]
    assert [member.target_mtime_ns for member in saved.members] == [2001, 2002]
    assert all(member.placed for member in saved.members)
    await repo.complete_fc2_part(plan.id, plan.members[1].source_path, targets[1], target_mtime_ns=2002)
    with pytest.raises(ValueError, match="目标修改时间发生变化"):
        await repo.complete_fc2_part(plan.id, plan.members[1].source_path, targets[1], target_mtime_ns=3002)


@pytest.mark.asyncio
async def test_target_changes_and_wrong_completion_are_rejected(repo: Repository, tmp_path: Path) -> None:
    plan = await _reserve(repo, tmp_path)
    assert plan.id is not None
    first, second = plan.members
    target = tmp_path / "output" / "CD1.mp4"
    with pytest.raises(ValueError, match="目标未预留"):
        await repo.complete_fc2_part(plan.id, first.source_path, target, target_mtime_ns=2001)
    with pytest.raises(ValueError, match="不能占用同组其他源文件"):
        await repo.prepare_fc2_part_target(plan.id, first.source_path, 1, second.source_path)
    for cd in (0, True, 2):
        with pytest.raises(ValueError, match="输出编号"):
            await repo.prepare_fc2_part_target(plan.id, first.source_path, cd, target)
    await repo.prepare_fc2_part_target(plan.id, first.source_path, 1, target)
    with pytest.raises(ValueError, match="已分配给同组其他文件"):
        await repo.prepare_fc2_part_target(plan.id, second.source_path, 2, target)
    other_target = tmp_path / "output" / "changed.mp4"
    with pytest.raises(ValueError, match="目标已固定"):
        await repo.prepare_fc2_part_target(plan.id, first.source_path, 1, other_target)
    with pytest.raises(ValueError, match="与已固定目标不一致"):
        await repo.complete_fc2_part(plan.id, first.source_path, other_target, target_mtime_ns=2001)
    for timestamp in (-1, True):
        with pytest.raises(ValueError, match="非负整数"):
            await repo.complete_fc2_part(plan.id, first.source_path, target, target_mtime_ns=timestamp)
    for plan_id, source, message in (
        (999999, first.source_path, "分段分配不存在"),
        (plan.id, str(tmp_path / "unknown.mp4"), "不属于已固定"),
    ):
        with pytest.raises(ValueError, match=message):
            await repo.prepare_fc2_part_target(plan_id, source, 1, target)
        with pytest.raises(ValueError, match=message):
            await repo.complete_fc2_part(plan_id, source, target, target_mtime_ns=2001)
    saved = await repo.get_fc2_part_plan(plan.library_id, tmp_path, plan.number)
    assert saved is not None
    assert saved.members[0].target_path == str(target)
    assert not saved.members[0].placed
    assert saved.members[1].target_path is None


@pytest.mark.asyncio
async def test_concurrent_target_reservation_has_only_one_owner(repo: Repository, tmp_path: Path) -> None:
    plan = await _reserve(repo, tmp_path)
    assert plan.id is not None
    target = tmp_path / "output" / "shared.mp4"
    results = await asyncio.gather(
        *(repo.prepare_fc2_part_target(plan.id, member.source_path, member.cd, target) for member in plan.members),
        return_exceptions=True,
    )
    assert sum(result is None for result in results) == 1
    failures = [result for result in results if isinstance(result, BaseException)]
    assert len(failures) == 1
    assert isinstance(failures[0], ValueError)
    assert "已分配给同组其他文件" in str(failures[0])


@pytest.mark.asyncio
async def test_plan_survives_media_metadata_and_task_deletion_but_not_library(repo: Repository, tmp_path: Path) -> None:
    plan = await _reserve(repo, tmp_path)
    metadata = await repo.upsert_metadata(plan.number)
    assert metadata.id is not None
    media = await repo.create_media_file(
        plan.library_id, path=plan.members[0].source_path, number=plan.number, metadata_id=metadata.id
    )
    task = await repo.create_task(TaskType.SCRAPE, payload={"number": plan.number})
    assert media.id is not None and task.id is not None
    assert await repo.delete_media_file(media.id)
    assert await repo.delete_metadata(metadata.id)
    assert await repo.delete_task(task.id)
    retained = await repo.get_fc2_part_plan(plan.library_id, tmp_path, plan.number)
    assert retained is not None and retained.id == plan.id
    assert retained.members == plan.members

    other = await _reserve(repo, tmp_path / "other-library")
    await repo.delete_library(plan.library_id)
    assert await repo.list_fc2_part_plans(plan.library_id, plan.number) == []
    assert [item.id for item in await repo.list_fc2_part_plans(other.library_id, other.number)] == [other.id]


@pytest.mark.asyncio
async def test_plan_listing_is_scoped_by_library_and_number(repo: Repository, tmp_path: Path) -> None:
    plan = await _reserve(repo, tmp_path / "first")
    second_dir = tmp_path / "second"
    second = await repo.reserve_fc2_part_plan(plan.library_id, second_dir, plan.number, _members(second_dir))
    await repo.reserve_fc2_part_plan(plan.library_id, second_dir, "FC2-9999999", _members(second_dir))
    other = await _reserve(repo, tmp_path / "other")
    selected = await repo.list_fc2_part_plans(plan.library_id, "fc4800334")
    assert {item.id for item in selected} == {plan.id, second.id}
    assert other.id not in {item.id for item in selected}
    assert await repo.get_fc2_part_plan(plan.library_id, tmp_path / "missing", plan.number) is None


@pytest.mark.asyncio
async def test_invalid_stored_members_fail_before_conditional_update(repo: Repository, tmp_path: Path) -> None:
    plan = await _reserve(repo, tmp_path)
    assert plan.id is not None
    corrupted = [member.model_dump(mode="json") for member in plan.members]
    corrupted[1]["cd"] = 1
    async with repo._session() as session:
        connection = await session.connection()
        await connection.exec_driver_sql(
            "UPDATE fc2_part_plans SET members=? WHERE id=?", (json.dumps(corrupted), plan.id)
        )
        await session.commit()
    with pytest.raises(ValueError, match="重复输出编号"):
        await repo.get_fc2_part_plan(plan.library_id, tmp_path, plan.number)
    with pytest.raises(ValueError, match="重复输出编号"):
        await repo.prepare_fc2_part_target(plan.id, plan.members[0].source_path, 1, tmp_path / "CD1.mp4")
