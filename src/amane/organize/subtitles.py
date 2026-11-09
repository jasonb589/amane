import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import structlog

from ..enums import ActorGender, MoveMode
from ..parsing import FileInfo, parse_file_info
from ..parsing.fc2_parts import parse_fc2_member
from ..utils.threads import in_thread
from .file import PlaceOutcome, execute_organize
from .path_templates import resolve_subtitle_path

if TYPE_CHECKING:
    from ..db.models import Library, Metadata
    from ..parsing.fc2_parts import Fc2Member

logger = structlog.get_logger()

_LANGUAGE_ONLY = re.compile(
    r"(?:chs|cht|chi|zho|zh(?:[-_.\s]+(?:cn|tw|hk|hans|hant))?|en|eng|ja|jpn)",
    re.IGNORECASE,
)


@in_thread
def discover_subtitles(
    video_path: Path,
    extensions: Sequence[str],
    video: FileInfo,
    *,
    fc2_member: Fc2Member | None = None,
) -> list[Path]:
    """只检查直接父目录, 不递归; 扩展名大小写不敏感. 空扩展名列表不发现. 多个字幕全部返回, 不挑主字幕.

    字幕只解析文件名 (`parse_file_info(text=...)`, 不依据目录、不使用路径回退).
    解析出番号时必须与当前视频番号相同 (忽略大小写), 再按分集配对;
    解析不出番号时回退独立目录规则: 有分集标记的跟当前视频同号, 解析不出的跟无分集或 CD1.
    传入 FC2 成员时依据源文件的正片/特集/FIX 角色与局部序号配对, 不使用整理目标 CD;
    无番号时仅允许明确语言名匹配未编号正片或正片第一段.
    """
    exts = {e.lower() for e in extensions}
    if not exts:
        return []
    parent = video_path.parent
    if not parent.is_dir():
        return []

    found: list[Path] = []
    for child in parent.iterdir():
        if not child.is_file() or child == video_path:
            continue
        if child.suffix.lower() not in exts:
            continue
        belongs = (
            _fc2_belongs(fc2_member, child.name)
            if fc2_member is not None
            else _belongs(video, parse_file_info(text=child.name))
        )
        if belongs:
            found.append(child)
    found.sort(key=lambda p: p.name.casefold())
    return found


@dataclass(frozen=True, slots=True)
class SubtitleConflict:
    """一条未落盘的字幕: 源路径 + 被占用的目标路径."""

    source: Path
    target: Path


@in_thread
def place_subtitles(
    sources: Sequence[Path],
    video_source: Path,
    video_dest: Path,
    library: Library,
    metadata: Metadata,
    file_info: FileInfo,
    mode: MoveMode,
    safe_dirs: Sequence[Path] | None = (),
    link_dir: Path | None = None,
    link_name: str | None = None,
    actor_genders: Mapping[str, ActorGender] | None = None,
) -> list[SubtitleConflict]:
    """失败与冲突只记日志, 不抛异常; 返回被占用而跳过的字幕源与其目标."""
    conflicted: list[SubtitleConflict] = []
    for sub in sources:
        dest = resolve_subtitle_path(
            library,
            metadata,
            sub,
            video_dir=video_dest.parent,
            link_dir=link_dir,
            video_name=video_dest.stem,
            video_dest=video_dest,
            link_name=link_name,
            source_path=video_source,
            file_info=file_info,
            safe_dirs=safe_dirs,
            actor_genders=actor_genders,
        )
        result = execute_organize.sync(
            source=sub,
            target_dir=dest.parent,
            target_stem=dest.stem,
            mode=mode,
            suffix=dest.suffix,
        )
        match result.outcome:
            case PlaceOutcome.PLACED:
                pass
            case PlaceOutcome.CONFLICT:
                logger.warning("subtitle target occupied", source=str(sub), dest=str(dest))
                conflicted.append(SubtitleConflict(source=sub, target=dest))
            case PlaceOutcome.FAILED:
                logger.warning("subtitle organize failed", source=str(sub), dest=str(dest), error=result.error)
    return conflicted


def _belongs(video: FileInfo, sub: FileInfo) -> bool:
    if sub.number is not None and (video.number is None or sub.number.casefold() != video.number.casefold()):
        return False
    return _cd_belongs(video.cd, sub.cd)


def _fc2_belongs(video: Fc2Member, subtitle_name: str) -> bool:
    if sub := parse_fc2_member(subtitle_name):
        if sub.number != video.number or sub.role != video.role:
            return False
        if video.role != "main":
            return sub.part == video.part
        return _cd_belongs(video.part, sub.part)
    return (
        video.role == "main"
        and video.part in {None, 1}
        and _LANGUAGE_ONLY.fullmatch(Path(subtitle_name).stem) is not None
    )


def _cd_belongs(video_cd: int | None, sub_cd: int | None) -> bool:
    if sub_cd == video_cd:
        return True
    return video_cd == 1 and sub_cd is None
