"""FC2 特集在首份文件移动前冻结分集, 后续任务复用原始成员身份."""

from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING

from ..db.fc2_parts import Fc2PlannedMember
from ..library import MEDIA_EXTENSIONS, LibraryFileKind, LibraryScan
from ..parsing import ContentType, FileInfo, normalize_fc2_number, parse_file_info
from ..parsing.fc2_parts import Fc2Member, has_fc2_special_marker, parse_fc2_member, plan_fc2_parts
from ..utils.path import existing_disk_path, nfc_path, path_key
from ..utils.threads import in_thread

if TYPE_CHECKING:
    from ..config import HotSettings
    from ..db.models import Fc2PartPlan, Library, MediaFile, Metadata
    from ..db.repository import Repository


@dataclass(frozen=True)
class Fc2PartContext:
    plan_id: int
    library_id: int
    number: str
    member: Fc2PlannedMember
    source_member: Fc2Member | None
    """原始文件使用角色配字幕; 已输出的 CD 文件使用正常 CD 配对."""

    def output_info(self, original: FileInfo) -> FileInfo:
        return replace(original, cd=self.member.cd)


@dataclass(frozen=True)
class _Candidate:
    path: str
    size: int
    mtime_ns: int
    member: Fc2Member | None
    special: bool


@in_thread
def _discover(directory: Path, number: str, scan: LibraryScan) -> list[_Candidate]:
    """只读取当前目录的正片候选; 不注册文件或扩大本次整理范围."""
    candidates: list[_Candidate] = []
    disk_directory = existing_disk_path(directory)
    if disk_directory is None:
        raise FileNotFoundError(f"FC2 分集目录不存在: {directory}")
    for path in disk_directory.iterdir():
        if not path.is_file() or scan.classify(path) is not LibraryFileKind.MEDIA:
            continue
        info = parse_file_info(path)
        if info.number is None or normalize_fc2_number(info.number) != number:
            continue
        if info.content_type is not ContentType.FC2:
            continue
        stat = path.stat()
        candidates.append(
            _Candidate(
                path=nfc_path(str(path)),
                size=stat.st_size,
                mtime_ns=stat.st_mtime_ns,
                member=parse_fc2_member(path.name),
                special=has_fc2_special_marker(path.name),
            )
        )
    return sorted(candidates, key=lambda candidate: path_key(Path(candidate.path)))


@in_thread
def _verify_source(path: Path, member: Fc2PlannedMember, *, output: bool = False) -> None:
    disk_path = existing_disk_path(path)
    if disk_path is None:
        raise FileNotFoundError(f"FC2 分集文件不存在: {path}")
    stat = disk_path.stat()
    expected_mtime = member.target_mtime_ns if output else member.source_mtime_ns
    if (stat.st_size, stat.st_mtime_ns) != (member.source_size, expected_mtime):
        raise ValueError(f"FC2 分集源文件已变化，不能沿用原分集编号: {path.name}")


@in_thread
def _target_mtime(path: Path, member: Fc2PlannedMember) -> int:
    disk_path = existing_disk_path(path)
    if disk_path is None:
        raise FileNotFoundError(f"FC2 分集目标不存在: {path}")
    stat = disk_path.stat()
    if stat.st_size != member.source_size:
        raise ValueError(f"FC2 分集目标大小与源文件不一致: {path.name}")
    return stat.st_mtime_ns


async def prepare_fc2_part(repo: Repository, context: Fc2PartContext, source: Path, target: Path) -> None:
    """落盘前复核文件身份并预留目标; 重试不可修改已经分配的目标."""
    await verify_fc2_part(context, source)
    target_key = path_key(target)
    for plan in await repo.list_fc2_part_plans(context.library_id, context.number):
        if plan.id == context.plan_id:
            continue
        if any(
            target_key == path_key(member.source_path)
            or (member.target_path is not None and target_key == path_key(member.target_path))
            for member in plan.members
        ):
            raise ValueError("FC2 分集目标属于另一原始目录的分组，不能重复使用")
    await repo.prepare_fc2_part_target(context.plan_id, context.member.source_path, context.member.cd, target)


async def verify_fc2_part(context: Fc2PartContext, source: Path) -> None:
    """复制的原始文件与已完成目标均须保留身份; 目标缺失时不自动重建."""
    await _verify_source(source, context.member, output=context.source_member is None)
    if context.member.placed and context.source_member is not None:
        assert context.member.target_path is not None
        await _verify_source(Path(context.member.target_path), context.member, output=True)


async def complete_fc2_part(repo: Repository, context: Fc2PartContext, target: Path) -> None:
    """视频就位后立即记录身份, 不依赖 NFO 成功或 MediaFile 后续是否保留."""
    mtime = await _target_mtime(target, context.member)
    await repo.complete_fc2_part(context.plan_id, context.member.source_path, target, target_mtime_ns=mtime)


def _source_context(plan: Fc2PartPlan, member: Fc2PlannedMember, current: Path) -> Fc2PartContext:
    assert plan.id is not None
    source_member = None
    if path_key(current) == path_key(Path(member.source_path)):
        source_member = Fc2Member(number=plan.number, role=member.role, part=member.part)
    return Fc2PartContext(
        plan_id=plan.id,
        library_id=plan.library_id,
        number=plan.number,
        member=member,
        source_member=source_member,
    )


async def resolve_fc2_part(
    repo: Repository,
    media: MediaFile,
    metadata: Metadata,
    library: Library,
    config: HotSettings,
) -> Fc2PartContext | None:
    """在库整理锁内调用; 先固定整组编号, 只返回当前选中文件的分配."""
    if metadata.content_type is not ContentType.FC2:
        return None
    number = normalize_fc2_number(metadata.number)
    current = Path(media.path)
    if number is None:
        return None

    # 输出文件可能已经改名或移至另一目录; 持久化身份优先于重新解释 CD 文件名.
    plans = await repo.list_fc2_part_plans(media.library_id, number)
    key = path_key(current)
    output_matches = [
        (plan, member)
        for plan in plans
        for member in plan.members
        if member.target_path is not None
        and path_key(Path(member.target_path)) == key
        and path_key(Path(member.source_path)) != key
    ]
    if len(output_matches) > 1:
        raise ValueError("FC2 输出路径属于多个分集计划，需先核对原分组")
    if output_matches:
        plan, member = output_matches[0]
        if not member.placed:
            raise ValueError("FC2 输出文件尚未确认整理完成，需先核对原任务")
        await _verify_source(current, member, output=True)
        return _source_context(plan, member, current)

    info = parse_file_info(current)
    if info.number is None or normalize_fc2_number(info.number) != number or info.content_type is not ContentType.FC2:
        if has_fc2_special_marker(current.name):
            raise ValueError("FC2 特集文件名与关联影片番号不一致，未分配分集")
        return None
    directory = current.parent
    plan = next((item for item in plans if path_key(Path(item.original_directory)) == path_key(directory)), None)
    scan = LibraryScan(
        trailer_pattern=library.trailer_pattern,
        blacklist_patterns=library.blacklist_patterns,
        min_file_size=library.min_file_size,
        media_extensions=frozenset(config.watcher.media_extensions) or MEDIA_EXTENSIONS,
    )
    candidates = await _discover(directory, number, scan)
    has_special = has_fc2_special_marker(current.name) or any(candidate.special for candidate in candidates)
    if plan is None and not has_special:
        return None

    if plan is None:
        if not any(path_key(candidate.path) == key for candidate in candidates):
            raise ValueError("当前 FC2 文件不符合分集候选规则，未固定编号或移动文件")
        if any(candidate.member is None for candidate in candidates):
            raise ValueError("FC2 特集组包含无法确定角色或分段的文件名，未移动文件")
        parsed = [candidate.member for candidate in candidates if candidate.member is not None]
        cds = plan_fc2_parts(parsed)
        members = [
            Fc2PlannedMember(
                source_path=candidate.path,
                role=parsed_member.role,
                part=parsed_member.part,
                cd=cd,
                source_size=candidate.size,
                source_mtime_ns=candidate.mtime_ns,
            )
            for candidate, parsed_member, cd in zip(candidates, parsed, cds, strict=True)
            if cd is not None
        ]
        # 只读同组索引用于核对已有人工关联, 不将未选文件绑定或提交整理.
        for member in members:
            indexed = await repo.get_media_file_by_path(member.source_path)
            if indexed is None:
                continue
            if indexed.library_id != media.library_id:
                raise ValueError("FC2 特集组内文件属于其他媒体库，未分配分集")
            if indexed.metadata_id is not None and indexed.metadata_id != metadata.id:
                raise ValueError("FC2 特集组内文件已关联其他影片，未分配分集")
        plan = await repo.reserve_fc2_part_plan(media.library_id, directory, number, members)

    original = {path_key(Path(member.source_path)): member for member in plan.members}
    output_keys = {path_key(Path(member.target_path)) for member in plan.members if member.target_path is not None}
    for candidate in candidates:
        candidate_key = path_key(Path(candidate.path))
        if candidate_key in original:
            member = original[candidate_key]
            if (candidate.size, candidate.mtime_ns) != (member.source_size, member.source_mtime_ns):
                raise ValueError(f"FC2 分集源文件已变化，需重新核对: {Path(candidate.path).name}")
        elif candidate_key not in output_keys:
            raise ValueError("FC2 分集编号已经固定，同组新增文件需重新规划，未自动重排")
    member = original.get(key)
    if member is None:
        raise ValueError("当前 FC2 文件不在已固定的分集计划中，需重新规划")
    await _verify_source(current, member)
    return _source_context(plan, member, current)
