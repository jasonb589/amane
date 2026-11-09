"""FC2 分段分配的冻结、条件更新、并发与删除边界."""

import asyncio
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from amane.db.fc2_parts import Fc2PlannedMember
from amane.db.models import Fc2PartPlan, TaskType
from amane.db.repository import Repository
from amane.utils.path import nfc_path


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
        ({"cd": 100}, "less than or equal to 99"),
        ({"part": 0}, "greater than 0"),
        ({"part": "1"}, "valid integer"),
        ({"part": 100}, "less than or equal to 99"),
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


@pytest.mark.asyncio
async def test_append_preserves_existing_placement_and_json_after_rebuild(repo: Repository, tmp_path: Path) -> None:
    plan = await _reserve(repo, tmp_path)
    assert plan.id is not None
    first, second = plan.members
    target = tmp_path / "output" / "CD1.mp4"
    await repo.prepare_fc2_part_target(plan.id, first.source_path, 1, target)
    await repo.complete_fc2_part(plan.id, first.source_path, target, target_mtime_ns=2001)
    saved = await repo.get_fc2_part_plan(plan.library_id, tmp_path, plan.number)
    assert saved is not None
    original_json = [dict(member) for member in saved.members_json]
    fixed = first.model_copy(
        update={"source_path": str(tmp_path / "fc4800334_fix1_4K.mp4"), "role": "fix", "part": 1, "cd": 3}
    )
    rebuilt = Repository(repo._engine, embeddings=repo.embeddings)
    extended = await rebuilt.append_fc2_part_members(plan.id, [fixed, fixed])
    assert extended.members_json[:2] == original_json
    assert [(member.role, member.cd) for member in extended.members] == [("main", 1), ("special", 2), ("fix", 3)]
    assert extended.members[0].placed and extended.members[0].target_mtime_ns == 2001
    second_target = tmp_path / "output" / "CD2.mp4"
    await rebuilt.prepare_fc2_part_target(plan.id, second.source_path, 2, second_target)
    await rebuilt.complete_fc2_part(plan.id, second.source_path, second_target, target_mtime_ns=2002)
    reused = await repo.append_fc2_part_members(plan.id, [fixed])
    assert reused.members[1].placed and reused.members[1].target_path == str(second_target)
    assert len(reused.members) == 3


@pytest.mark.asyncio
async def test_append_all_roles_in_order_without_reassigning_existing_cds(repo: Repository, tmp_path: Path) -> None:
    library = await repo.create_library(name="FC2", path=str(tmp_path))
    assert library.id is not None
    main = _members(tmp_path)[0]
    plan = await repo.reserve_fc2_part_plan(library.id, tmp_path, "FC2-4800334", [main])
    assert plan.id is not None
    special1 = _members(tmp_path)[1].model_copy(update={"part": 1})
    special2 = special1.model_copy(update={"source_path": str(tmp_path / "fc4800334_sp2.mp4"), "part": 2, "cd": 3})
    fix1 = main.model_copy(
        update={"source_path": str(tmp_path / "fc4800334_fix1.mp4"), "role": "fix", "part": 1, "cd": 4}
    )
    fix2 = fix1.model_copy(update={"source_path": str(tmp_path / "fc4800334_fix2.mp4"), "part": 2, "cd": 5})
    await repo.append_fc2_part_members(plan.id, [special1])
    await repo.append_fc2_part_members(plan.id, [fix1, special2])
    final = await repo.append_fc2_part_members(plan.id, [fix1, fix2])
    assert [(member.role, member.part, member.cd) for member in final.members] == [
        ("main", None, 1),
        ("special", 1, 2),
        ("special", 2, 3),
        ("fix", 1, 4),
        ("fix", 2, 5),
    ]
    assert final.members[0] == main and final.members[1] == special1


@pytest.mark.asyncio
async def test_append_rejects_ambiguous_or_changed_members_atomically(repo: Repository, tmp_path: Path) -> None:
    plan = await _reserve(repo, tmp_path)
    assert plan.id is not None
    first, special = plan.members
    fixed = first.model_copy(update={"source_path": str(tmp_path / "fc4800334_fix.mp4"), "role": "fix", "cd": 3})
    completed = await repo.append_fc2_part_members(plan.id, [fixed])
    baseline = completed.members_json
    candidates: list[tuple[list[Fc2PlannedMember], str]] = [
        ([], "不能为空"),
        ([first.model_copy(update={"source_size": 101})], "已固定成员"),
        ([first.model_copy(update={"source_mtime_ns": 1002})], "已固定成员"),
        ([first.model_copy(update={"part": 1})], "已固定成员"),
        ([first.model_copy(update={"cd": 4})], "已固定成员"),
        ([fixed.model_copy(update={"source_path": str(tmp_path.parent / "outside.mp4"), "cd": 4})], "同一原始目录"),
        ([fixed.model_copy(update={"target_path": str(tmp_path / "CD3.mp4")})], "不能指定目标"),
        ([fixed, fixed.model_copy(update={"source_size": 101})], "身份不一致"),
        ([fixed.model_copy(update={"source_path": str(tmp_path / "fix-other.mp4"), "cd": 4})], "多个未编号"),
        (
            [fixed.model_copy(update={"source_path": str(tmp_path / "fix1.mp4"), "part": 1, "cd": 4})],
            "未编号文件与已编号",
        ),
        ([special.model_copy(update={"source_path": str(tmp_path / "sp-other.mp4"), "cd": 4})], "多个未编号"),
        ([first.model_copy(update={"source_path": str(tmp_path / "main-other.mp4"), "cd": 4})], "多个未编号"),
        ([fixed.model_copy(update={"source_path": str(tmp_path / "fix-other.mp4"), "cd": 2})], "重复输出编号"),
    ]
    for members, message in candidates:
        with pytest.raises(ValueError, match=message):
            await repo.append_fc2_part_members(plan.id, members)
        saved = await repo.get_fc2_part_plan(plan.library_id, tmp_path, plan.number)
        assert saved is not None and saved.members_json == baseline
    with pytest.raises(ValueError, match="分段分配不存在"):
        await repo.append_fc2_part_members(999999, [fixed])


@pytest.mark.asyncio
async def test_append_rejects_insertion_before_assigned_roles_and_part_gaps(repo: Repository, tmp_path: Path) -> None:
    library = await repo.create_library(name="FC2", path=str(tmp_path))
    assert library.id is not None
    first = _members(tmp_path)[0]
    fixed = first.model_copy(update={"source_path": str(tmp_path / "fix1.mp4"), "role": "fix", "part": 1, "cd": 2})
    plan = await repo.reserve_fc2_part_plan(library.id, tmp_path, "FC2-4800334", [first, fixed])
    assert plan.id is not None
    candidates = [
        (_members(tmp_path)[1].model_copy(update={"cd": 3}), "不能改变已固定编号"),
        (fixed.model_copy(update={"source_path": str(tmp_path / "fix3.mp4"), "part": 3, "cd": 3}), "从 1 连续排列"),
        (fixed.model_copy(update={"source_path": str(tmp_path / "fix2.mp4"), "part": 2, "cd": 4}), "连续排列"),
    ]
    for candidate, message in candidates:
        with pytest.raises(ValueError, match=message):
            await repo.append_fc2_part_members(plan.id, [candidate])
        saved = await repo.get_fc2_part_plan(library.id, tmp_path, plan.number)
        assert saved is not None and saved.members_json == plan.members_json


@pytest.mark.asyncio
async def test_concurrent_append_preserves_other_member_updates_and_has_one_identity_owner(
    repo: Repository, tmp_path: Path
) -> None:
    plan = await _reserve(repo, tmp_path)
    assert plan.id is not None
    first = plan.members[0]
    fixed = first.model_copy(update={"source_path": str(tmp_path / "fix.mp4"), "role": "fix", "cd": 3})
    target = tmp_path / "output" / "CD1.mp4"
    await repo.prepare_fc2_part_target(plan.id, first.source_path, 1, target)
    rebuilt = Repository(repo._engine, embeddings=repo.embeddings)
    results = await asyncio.gather(
        *(owner.append_fc2_part_members(plan.id, [fixed]) for owner in [repo, rebuilt] * 5),
        rebuilt.complete_fc2_part(plan.id, first.source_path, target, target_mtime_ns=2001),
    )
    assert {item.id for item in results if isinstance(item, Fc2PartPlan)} == {plan.id}
    final = await rebuilt.get_fc2_part_plan(plan.library_id, tmp_path, plan.number)
    assert final is not None and len(final.members) == 3
    assert final.members[0].placed and final.members[0].target_mtime_ns == 2001

    other_plan = await _reserve(repo, tmp_path / "other")
    assert other_plan.id is not None
    candidates = [
        fixed.model_copy(update={"source_path": str(tmp_path / "other" / name)}) for name in ("fix-a.mp4", "fix-b.mp4")
    ]
    conflicts = await asyncio.gather(
        *(
            owner.append_fc2_part_members(other_plan.id, [candidate])
            for owner, candidate in zip([repo, rebuilt], candidates, strict=True)
        ),
        return_exceptions=True,
    )
    assert sum(isinstance(item, Fc2PartPlan) for item in conflicts) == 1
    assert sum(isinstance(item, ValueError) for item in conflicts) == 1
    retained = await repo.get_fc2_part_plan(other_plan.library_id, tmp_path / "other", other_plan.number)
    assert retained is not None and len(retained.members) == 3


@pytest.mark.asyncio
async def test_append_enforces_ninety_nine_part_limit(repo: Repository, tmp_path: Path) -> None:
    library = await repo.create_library(name="FC2", path=str(tmp_path))
    assert library.id is not None
    first = _members(tmp_path)[0]
    mains = [
        first.model_copy(update={"source_path": str(tmp_path / f"main{part}.mp4"), "part": part, "cd": part})
        for part in range(1, 99)
    ]
    plan = await repo.reserve_fc2_part_plan(library.id, tmp_path, "FC2-4800334", mains)
    assert plan.id is not None
    special = _members(tmp_path)[1].model_copy(update={"cd": 99})
    full = await repo.append_fc2_part_members(plan.id, [special])
    fixed = first.model_copy(update={"source_path": str(tmp_path / "fix.mp4"), "role": "fix", "cd": 100})
    with pytest.raises(ValueError, match="less than or equal to 99"):
        await repo.append_fc2_part_members(plan.id, [fixed])
    saved = await repo.get_fc2_part_plan(library.id, tmp_path, plan.number)
    assert saved is not None and saved.members_json == full.members_json and len(saved.members) == 99


@pytest.mark.asyncio
@pytest.mark.parametrize("placed", [False, True])
async def test_cross_plan_output_paths_cannot_be_reserved_as_sources_or_targets(
    repo: Repository, tmp_path: Path, placed: bool
) -> None:
    first_plan = await _reserve(repo, tmp_path / "first")
    second_directory = tmp_path / "second"
    second_plan = await _reserve(repo, second_directory)
    assert first_plan.id is not None and second_plan.id is not None
    target = second_directory / "already-output.mp4"
    first = first_plan.members[0]
    await repo.prepare_fc2_part_target(first_plan.id, first.source_path, 1, target)
    if placed:
        await repo.complete_fc2_part(first_plan.id, first.source_path, target, target_mtime_ns=2001)
    occupied_source = first.model_copy(update={"source_path": str(target), "role": "fix", "cd": 3})
    with pytest.raises(ValueError, match="源文件已被其他分组"):
        await repo.append_fc2_part_members(second_plan.id, [occupied_source])
    with pytest.raises(ValueError, match="源文件已被其他分组"):
        await repo.reserve_fc2_part_plan(
            second_plan.library_id,
            second_directory,
            "FC2-9999999",
            [occupied_source.model_copy(update={"role": "main", "cd": 1})],
        )
    for other_target in (target, Path(first.source_path)):
        with pytest.raises(ValueError, match="目标已被其他分组"):
            await repo.prepare_fc2_part_target(second_plan.id, second_plan.members[0].source_path, 1, other_target)
    saved = await repo.get_fc2_part_plan(second_plan.library_id, second_directory, second_plan.number)
    assert saved is not None and saved.members_json == second_plan.members_json


@pytest.mark.asyncio
async def test_append_cannot_reclassify_its_existing_output_as_a_new_source(repo: Repository, tmp_path: Path) -> None:
    plan = await _reserve(repo, tmp_path)
    assert plan.id is not None
    target = tmp_path / "fc4800334_fix.mp4"
    first = plan.members[0]
    await repo.prepare_fc2_part_target(plan.id, first.source_path, 1, target)
    await repo.complete_fc2_part(plan.id, first.source_path, target, target_mtime_ns=2001)
    fixed = first.model_copy(update={"source_path": str(target), "role": "fix", "cd": 3})
    with pytest.raises(ValueError, match="不能占用同组其他源文件"):
        await repo.append_fc2_part_members(plan.id, [fixed])
    saved = await repo.get_fc2_part_plan(plan.library_id, tmp_path, plan.number)
    assert saved is not None and len(saved.members) == 2
    assert saved.members[0].placed and saved.members[0].target_path == str(target)


@pytest.mark.asyncio
async def test_concurrent_appended_source_and_other_plan_target_cannot_both_claim_path(
    repo: Repository, tmp_path: Path
) -> None:
    first = await _reserve(repo, tmp_path / "first")
    second_directory = tmp_path / "second"
    second = await _reserve(repo, second_directory)
    assert first.id is not None and second.id is not None
    shared = second_directory / "fix.mp4"
    fixed = second.members[0].model_copy(update={"source_path": str(shared), "role": "fix", "cd": 3})
    rebuilt = Repository(repo._engine, embeddings=repo.embeddings)
    results = await asyncio.gather(
        repo.prepare_fc2_part_target(first.id, first.members[0].source_path, 1, shared),
        rebuilt.append_fc2_part_members(second.id, [fixed]),
        return_exceptions=True,
    )
    failures = [result for result in results if isinstance(result, ValueError)]
    assert len(failures) == 1 and "其他分组" in str(failures[0])
    first_saved = await repo.get_fc2_part_plan(first.library_id, tmp_path / "first", first.number)
    second_saved = await rebuilt.get_fc2_part_plan(second.library_id, second_directory, second.number)
    assert first_saved is not None and second_saved is not None
    assert (first_saved.members[0].target_path == str(shared)) != (len(second_saved.members) == 3)


@pytest.mark.asyncio
async def test_concurrent_cross_plan_target_reservation_has_one_owner(repo: Repository, tmp_path: Path) -> None:
    first = await _reserve(repo, tmp_path / "first")
    second = await _reserve(repo, tmp_path / "second")
    assert first.id is not None and second.id is not None
    target = tmp_path / "output" / "CD1.mp4"
    rebuilt = Repository(repo._engine, embeddings=repo.embeddings)
    results = await asyncio.gather(
        repo.prepare_fc2_part_target(first.id, first.members[0].source_path, 1, target),
        rebuilt.prepare_fc2_part_target(second.id, second.members[0].source_path, 1, target),
        return_exceptions=True,
    )
    assert sum(result is None for result in results) == 1
    failures = [item for item in results if isinstance(item, ValueError)]
    assert len(failures) == 1 and "目标已被其他分组" in str(failures[0])
