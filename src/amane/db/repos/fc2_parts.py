"""FC2 分段分配的条件写入; 事务期间不访问磁盘或网络."""

from collections.abc import Sequence
from pathlib import Path

from sqlmodel import col, select
from sqlmodel.ext.asyncio.session import AsyncSession

from ...parsing import normalize_fc2_number
from ...parsing.fc2_parts import Fc2Member, plan_fc2_parts
from ...utils.path import path_key
from ..fc2_parts import Fc2PlannedMember, normalize_fc2_plan_path, validate_fc2_part_members
from ..models import Fc2PartPlan, Library
from ..repo_types import _utcnow
from .base import RepositoryMixinBase


def _number(number: str) -> str:
    normalized = normalize_fc2_number(number)
    if normalized is None:
        raise ValueError("FC2 分段分配需要有效的 FC2 番号")
    return normalized


def _members_in_directory(members: Sequence[Fc2PlannedMember], directory_key: str) -> tuple[Fc2PlannedMember, ...]:
    validated = validate_fc2_part_members(members)
    if any(path_key(Path(member.source_path).parent) != directory_key for member in validated):
        raise ValueError("FC2 分段成员必须位于同一原始目录")
    return validated


def _membership(members: Sequence[Fc2PlannedMember]) -> list[tuple[str, str, int | None, int, int, int]]:
    return sorted(
        (
            path_key(member.source_path),
            member.role,
            member.part,
            member.cd,
            member.source_size,
            member.source_mtime_ns,
        )
        for member in members
    )


def _validate_order(members: Sequence[Fc2PlannedMember], number: str) -> None:
    assigned = plan_fc2_parts(
        [Fc2Member(number=number, role=member.role, part=member.part) for member in members],
        require_main_plan=True,
    )
    if any(member.cd != cd for member, cd in zip(members, assigned, strict=True)):
        raise ValueError("FC2 分段输出编号必须按正片、特集与 FIX 的顺序连续排列, 不能改变已固定编号")


def _validate_plan(plan: Fc2PartPlan) -> Fc2PartPlan:
    if plan.directory_key != path_key(normalize_fc2_plan_path(plan.original_directory)):
        raise ValueError("FC2 分段分配的原始目录不一致")
    if plan.number != _number(plan.number):
        raise ValueError("FC2 分段分配的番号不规范")
    validated = _members_in_directory(plan.members, plan.directory_key)
    _validate_order(validated, plan.number)
    return plan


async def _require_plan(session: AsyncSession, plan_id: int) -> Fc2PartPlan:
    plan = await session.get(Fc2PartPlan, plan_id)
    if plan is None:
        raise ValueError("FC2 分段分配不存在")
    return _validate_plan(plan)


def _member(plan: Fc2PartPlan, source_path: str) -> tuple[int, Fc2PlannedMember]:
    source_key = path_key(source_path)
    for index, member in enumerate(plan.members):
        if path_key(member.source_path) == source_key:
            return index, member
    raise ValueError("源文件不属于已固定的 FC2 分段文件组")


def _replace_member(plan: Fc2PartPlan, index: int, member: Fc2PlannedMember) -> None:
    updated = list(plan.members)
    updated[index] = member
    plan.members_json = [value.model_dump(mode="json") for value in validate_fc2_part_members(updated)]
    plan.updated_at = _utcnow()


async def _ensure_paths_available(
    session: AsyncSession, members: Sequence[Fc2PlannedMember], *, plan_id: int | None = None
) -> None:
    """所有媒体库共用真实路径; 其它计划已预留的目标同样不能重新作为源文件."""
    sources = {path_key(member.source_path) for member in members}
    targets = {path_key(member.target_path) for member in members if member.target_path is not None}
    stmt = select(Fc2PartPlan)
    if plan_id is not None:
        stmt = stmt.where(col(Fc2PartPlan.id) != plan_id)
    for other in (await session.exec(stmt)).all():
        for member in other.members:
            if member.target_path is not None and path_key(member.target_path) in sources:
                raise ValueError("FC2 分段源文件已被其他分组预留为输出文件")
            if path_key(member.source_path) in targets or (
                member.target_path is not None and path_key(member.target_path) in targets
            ):
                raise ValueError("FC2 分段目标已被其他分组占用")


def _initial_members(members: Sequence[Fc2PlannedMember], directory_key: str) -> tuple[Fc2PlannedMember, ...]:
    validated = _members_in_directory(members, directory_key)
    if any(member.target_path is not None or member.placed for member in validated):
        raise ValueError("固定或追加 FC2 分段分配时不能指定目标或落盘状态")
    return validated


class Fc2PartsRepoMixin(RepositoryMixinBase):
    async def get_fc2_part_plan(self, library_id: int, directory: Path, number: str) -> Fc2PartPlan | None:
        directory_key = path_key(normalize_fc2_plan_path(directory))
        normalized_number = _number(number)
        async with self._session() as session:
            stmt = select(Fc2PartPlan).where(
                Fc2PartPlan.library_id == library_id,
                Fc2PartPlan.directory_key == directory_key,
                Fc2PartPlan.number == normalized_number,
            )
            plan = (await session.exec(stmt)).first()
            return _validate_plan(plan) if plan is not None else None

    async def list_fc2_part_plans(self, library_id: int, number: str) -> list[Fc2PartPlan]:
        normalized_number = _number(number)
        async with self._session() as session:
            stmt = (
                select(Fc2PartPlan)
                .where(Fc2PartPlan.library_id == library_id, Fc2PartPlan.number == normalized_number)
                .order_by(Fc2PartPlan.directory_key, col(Fc2PartPlan.id))
            )
            return [_validate_plan(plan) for plan in (await session.exec(stmt)).all()]

    async def reserve_fc2_part_plan(
        self,
        library_id: int,
        directory: Path,
        number: str,
        members: Sequence[Fc2PlannedMember],
    ) -> Fc2PartPlan:
        """固定整组分配; 已存在时只复用同一成员身份与编号, 不接受迟到成员或重排."""
        original_directory = normalize_fc2_plan_path(directory)
        directory_key = path_key(original_directory)
        normalized_number = _number(number)
        validated = _initial_members(members, directory_key)
        async with self._session() as session:
            connection = await session.connection()
            await connection.exec_driver_sql("BEGIN IMMEDIATE")
            if await session.get(Library, library_id) is None:
                raise ValueError("FC2 分段分配所属媒体库不存在")
            stmt = select(Fc2PartPlan).where(
                Fc2PartPlan.library_id == library_id,
                Fc2PartPlan.directory_key == directory_key,
                Fc2PartPlan.number == normalized_number,
            )
            existing = (await session.exec(stmt)).first()
            if existing is not None:
                _validate_plan(existing)
                if _membership(existing.members) != _membership(validated):
                    raise ValueError("FC2 分段文件组已固定, 成员或文件信息发生变化, 请重新整理整组")
                await session.commit()
                return existing
            _validate_order(validated, normalized_number)
            await _ensure_paths_available(session, validated)
            plan = Fc2PartPlan(
                library_id=library_id,
                original_directory=original_directory,
                directory_key=directory_key,
                number=normalized_number,
                members_json=[member.model_dump(mode="json") for member in validated],
            )
            session.add(plan)
            await session.commit()
            await session.refresh(plan)
            return plan

    async def append_fc2_part_members(self, plan_id: int, members: Sequence[Fc2PlannedMember]) -> Fc2PartPlan:
        """仅追加可证明顺序的末尾成员; 重复提交相同身份保持幂等, 既有成员与落盘进度不变."""
        async with self._session() as session:
            connection = await session.connection()
            await connection.exec_driver_sql("BEGIN IMMEDIATE")
            plan = await _require_plan(session, plan_id)
            unique: dict[str, Fc2PlannedMember] = {}
            for value in members:
                member = Fc2PlannedMember.model_validate(value)
                key = path_key(member.source_path)
                if key in unique and member != unique[key]:
                    raise ValueError("FC2 追加成员包含身份不一致的重复源文件")
                unique[key] = member
            validated = _initial_members(tuple(unique.values()), plan.directory_key)
            existing = {path_key(member.source_path): member for member in plan.members}
            added: list[Fc2PlannedMember] = []
            for member in validated:
                previous = existing.get(path_key(member.source_path))
                if previous is None:
                    added.append(member)
                elif _membership([previous]) != _membership([member]):
                    raise ValueError("FC2 已固定成员的身份、源文件信息或输出编号不能修改")
            if not added:
                await session.commit()
                return plan
            combined = validate_fc2_part_members([*plan.members, *added])
            _validate_order(combined, plan.number)
            await _ensure_paths_available(session, added, plan_id=plan.id)
            plan.members_json = [*plan.members_json, *(member.model_dump(mode="json") for member in added)]
            plan.updated_at = _utcnow()
            session.add(plan)
            await session.commit()
            await session.refresh(plan)
            return plan

    async def prepare_fc2_part_target(
        self, plan_id: int, source_path: str | Path, cd: int, target_path: str | Path
    ) -> None:
        """移动前固定目标; 重试只能使用相同编号与目标, 同组目标不可重复."""
        source = normalize_fc2_plan_path(source_path)
        target = normalize_fc2_plan_path(target_path)
        if isinstance(cd, bool) or not isinstance(cd, int) or not 1 <= cd <= 99:
            raise ValueError("FC2 输出编号必须是 1 至 99 的整数")
        async with self._session() as session:
            connection = await session.connection()
            await connection.exec_driver_sql("BEGIN IMMEDIATE")
            plan = await _require_plan(session, plan_id)
            index, member = _member(plan, source)
            if member.cd != cd:
                raise ValueError("FC2 输出编号与已固定分配不一致")
            if any(
                path_key(other.source_path) != path_key(member.source_path)
                and path_key(other.source_path) == path_key(target)
                for other in plan.members
            ):
                raise ValueError("FC2 分段目标不能占用同组其他源文件")
            if member.target_path is not None:
                if path_key(member.target_path) != path_key(target):
                    raise ValueError("FC2 分段目标已固定, 不能修改")
            else:
                if any(
                    other.target_path is not None and path_key(other.target_path) == path_key(target)
                    for other in plan.members
                ):
                    raise ValueError("FC2 分段目标已分配给同组其他文件")
                updated = member.model_copy(update={"target_path": target})
                await _ensure_paths_available(session, [updated], plan_id=plan.id)
                _replace_member(plan, index, updated)
                session.add(plan)
            await session.commit()

    async def complete_fc2_part(
        self,
        plan_id: int,
        source_path: str | Path,
        target_path: str | Path,
        *,
        target_mtime_ns: int,
    ) -> None:
        """仅将已预留且目标一致的成员标记为落盘; 重复完成保持幂等."""
        source = normalize_fc2_plan_path(source_path)
        target = normalize_fc2_plan_path(target_path)
        if isinstance(target_mtime_ns, bool) or not isinstance(target_mtime_ns, int) or target_mtime_ns < 0:
            raise ValueError("FC2 目标修改时间必须是非负整数")
        async with self._session() as session:
            connection = await session.connection()
            await connection.exec_driver_sql("BEGIN IMMEDIATE")
            plan = await _require_plan(session, plan_id)
            index, member = _member(plan, source)
            if member.target_path is None or path_key(member.target_path) != path_key(target):
                raise ValueError("FC2 分段目标未预留或与已固定目标不一致")
            if member.placed:
                if member.target_mtime_ns != target_mtime_ns:
                    raise ValueError("FC2 已落盘文件的目标修改时间发生变化")
            else:
                _replace_member(
                    plan, index, member.model_copy(update={"placed": True, "target_mtime_ns": target_mtime_ns})
                )
                session.add(plan)
            await session.commit()
