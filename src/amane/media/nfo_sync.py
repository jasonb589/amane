"""编辑后的 NFO 同步, 使用实际输出记录或可验证的既有文件位置."""

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING

import structlog
from pydantic import BaseModel
from sqlalchemy.exc import SQLAlchemyError

from ..library import MEDIA_EXTENSIONS
from ..organize.path_templates import resolve_existing_nfo_path, resolve_paths
from ..organize.template import PathEngine
from ..parsing import parse_file_info
from ..utils.path import is_descendant, path_key
from ..utils.threads import in_thread
from .nfo import write_nfo

if TYPE_CHECKING:
    from ..db.models import Library, MediaFile, Metadata
    from ..db.repository import Repository
    from ..enums import ActorGender

logger = structlog.get_logger()


class NfoSyncResult(BaseModel):
    written: int = 0
    skipped: int = 0
    failed: int = 0
    unlocated: bool = False


class _Target(BaseModel):
    library_id: int
    video: Path
    nfo: Path


def _validate_target(target: _Target, library: Library, safe_dirs: Sequence[Path] | None) -> bool:
    if not target.video.is_file() or not target.nfo.parent.is_dir():
        return False
    if not target.video.is_absolute() or not target.nfo.is_absolute() or target.nfo.suffix.lower() != ".nfo":
        raise ValueError("NFO 输出位置不合法")
    if target.nfo.is_symlink():
        raise ValueError("NFO 输出位置不允许符号链接")
    if safe_dirs is not None:
        roots = [Path(library.path), *safe_dirs]
        for path in (target.video, target.nfo):
            if not any(is_descendant(path, root) for root in roots):
                raise ValueError(f"NFO 同步路径超出安全目录: {path}")
    if target.nfo.exists() and target.nfo.samefile(target.video):
        raise ValueError("NFO 输出位置不能指向媒体文件")
    return True


@in_thread
def _write_target(metadata: Metadata, target: _Target, library: Library, safe_dirs: Sequence[Path] | None) -> str:
    try:
        if not _validate_target(target, library, safe_dirs):
            return "skipped"
        return (
            "written"
            if write_nfo.sync(metadata, target.nfo, create_parent=False, expected_number=metadata.number)
            else "failed"
        )
    except (OSError, ValueError) as exc:
        logger.warning("nfo sync rejected", path=str(target.nfo), error=str(exc))
        return "failed"


@in_thread
def _discover_targets(
    previous: Metadata,
    media_files: Sequence[MediaFile],
    libraries: Mapping[int, Library],
    safe_dirs: Sequence[Path] | None,
    actor_genders: Mapping[str, ActorGender],
) -> tuple[list[_Target], int]:
    targets: list[_Target] = []
    failed = 0
    for media in media_files:
        library = libraries.get(media.library_id)
        if library is None or not library.write_nfo:
            continue
        video = Path(media.path)
        try:
            if not video.is_file():
                continue
            nfo = resolve_existing_nfo_path(
                library,
                previous,
                video,
                file_info=parse_file_info(media.path, has_external_subtitle=media.has_external_subtitle),
                safe_dirs=safe_dirs,
                actor_genders=actor_genders,
            )
            if nfo.is_file():
                targets.append(_Target(library_id=media.library_id, video=video, nfo=nfo))
        except (OSError, ValueError) as exc:
            failed += 1
            logger.warning("nfo target discovery failed", path=media.path, error=str(exc))

    if media_files:
        return targets, failed
    # 旧版整理到库外没有输出记录. 仅尝试可完整还原的模板与已存在的 NFO, 不遍历目录.
    unavailable = ("raw_dir", "dir", "raw_name", "raw_relpath", "raw_srt_name")
    for library_id, library in libraries.items():
        if not library.write_nfo:
            continue
        templates = (library.video_template, library.link_template or "", library.nfo_template or "")
        if any(PathEngine(template).uses(name) for template in templates for name in unavailable):
            continue
        for ext in sorted(MEDIA_EXTENSIONS):
            try:
                paths = resolve_paths(
                    library, previous, ext=ext.lstrip("."), safe_dirs=safe_dirs, actor_genders=actor_genders
                )
                if paths.video.is_file() and paths.nfo.is_file():
                    targets.append(_Target(library_id=library_id, video=paths.video, nfo=paths.nfo))
            except OSError, ValueError:
                continue
    return targets, failed


async def sync_metadata_nfos(
    repo: Repository, metadata: Metadata, previous: Metadata, safe_dirs: Sequence[Path] | None
) -> NfoSyncResult:
    """DB 已提交后执行; 输出失效或写入失败仅记入统计, 不撤销元数据修改."""
    try:
        return await _sync_metadata_nfos(repo, metadata, previous, safe_dirs)
    except OSError, ValueError, SQLAlchemyError:
        logger.exception("metadata nfo sync failed", metadata_id=metadata.id)
        return NfoSyncResult(failed=1)


async def _sync_metadata_nfos(
    repo: Repository, metadata: Metadata, previous: Metadata, safe_dirs: Sequence[Path] | None
) -> NfoSyncResult:
    result = NfoSyncResult()
    if metadata.id is None:
        result.unlocated = True
        return result
    async with repo.nfo_sync_lock:
        # 并发编辑必须采用取得锁之后的最新值, 避免旧请求覆盖新 NFO.
        current = await repo.get_metadata(metadata.id)
        if current is None:
            return result
        libraries = {library.id: library for library in await repo.list_libraries() if library.id is not None}
        outputs = await repo.list_nfo_outputs(metadata.id)
        targets = [_Target(library_id=o.library_id, video=Path(o.video_path), nfo=Path(o.nfo_path)) for o in outputs]
        media_files = await repo.get_media_by_metadata_id(metadata.id)
        known_videos = {path_key(target.video) for target in targets}
        pending = [media for media in media_files if path_key(media.path) not in known_videos]
        if pending or not targets:
            genders = {actor.name: actor.gender for actor in await repo.get_actors_by_names(previous.actors)}
            found, failed = await _discover_targets(previous, pending, libraries, safe_dirs, genders)
            targets.extend(found)
            result.failed += failed
        seen: set[str] = set()
        for target in targets:
            key = path_key(target.nfo)
            if key in seen:
                continue
            seen.add(key)
            library = libraries.get(target.library_id)
            if library is None or not library.write_nfo:
                result.skipped += 1
                continue
            owner = await repo.get_nfo_output(target.nfo)
            if owner is not None and owner.metadata_id != metadata.id:
                result.failed += 1
                continue
            outcome = await _write_target(current, target, library, safe_dirs)
            if outcome == "written":
                await repo.remember_nfo_output(metadata.id, target.library_id, target.video, target.nfo)
                result.written += 1
            elif outcome == "skipped":
                result.skipped += 1
            else:
                result.failed += 1
        result.unlocated = not targets and not result.failed
    logger.info("metadata nfo sync completed", metadata_id=metadata.id, **result.model_dump())
    return result
