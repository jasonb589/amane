"""FC2 文件名分类与特集分集规划; 不读取目录、不执行文件操作."""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Sequence
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .file_info import Mosaic, normalize_fc2_number, parse_file_info, strip_filename_junk

_FC2_HEAD = re.compile(
    r"(?P<number>FC2(?:[-_.\s\u2010-\u2015\u2212]*PPV)?[-_.\s\u2010-\u2015\u2212]*[0-9]{5,}"
    r"|FC(?!2)[-_.\s\u2010-\u2015\u2212]*[0-9]{5,})(?=$|[-_.\s])"
)
_FC2_INTENT_HEAD = re.compile(r"(?<![A-Z0-9])" + _FC2_HEAD.pattern)
_SPECIAL = re.compile(r"SP(?P<part>0?[1-9]|[1-9][0-9])?(?=$|[-_.\s])")
_EXPLICIT_MAIN = re.compile(r"(?:CD|PART)\s*(?P<part>0?[1-9]|[1-9][0-9])(?=$|[-_.\s])")
_BARE_MAIN = re.compile(r"(?P<part>0?[1-9]|[1-9][0-9]|[AB])(?=$|[-_.\s])")
_SEPARATORS = re.compile(r"[-_.\s]+")
_DEFINITION = re.compile(r"(?:[48]K(?:S)?[0-9]*|(?:2160|1440|1080|720|480)P[0-9]*|HD|SD)")
_DOMAIN_PREFIX = re.compile(r"(?:[A-Z0-9](?:[A-Z0-9-]{0,61}[A-Z0-9])?\.)+[A-Z]{2,63}@")
_KNOWN_EXTENSIONS = frozenset(
    {
        ".mp4",
        ".mkv",
        ".avi",
        ".wmv",
        ".flv",
        ".mov",
        ".ts",
        ".iso",
        ".strm",
        ".m4v",
        ".m2ts",
        ".mts",
        ".mpg",
        ".mpeg",
        ".webm",
        ".srt",
        ".ass",
        ".ssa",
        ".vtt",
        ".sub",
    }
)
_PHASE_TAGS = frozenset(
    {
        "C",
        "U",
        "UC",
        "LTX",
        "CRACKED",
        "LEAKED",
        "UNCENSORED",
        "破解",
        "克破",
        "流出",
        "无码",
        "無碼",
        "字幕",
        "中文",
        "中文字幕",
        "LR",
        "VR",
        "DVD",
        "FULL",
        "HEVC",
        "H264",
        "H265",
        "X264",
        "X265",
        "AAC",
        "XXX",
        "PRT",
        "CHS",
        "CHT",
        "CHI",
        "ZHO",
        "ZH",
        "CN",
        "TW",
        "HK",
        "HANS",
        "HANT",
        "EN",
        "ENG",
        "JA",
        "JPN",
    }
)


class Fc2Member(BaseModel):
    """一份文件在同番号组内的角色与局部序号; 调用方持有文件路径与组范围."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    number: str
    role: Literal["main", "special"]
    part: int | None = Field(default=None, ge=1, le=99, strict=True)
    definition: str | None = None
    has_subtitle: bool = False
    mosaic: Mosaic | None = None

    @field_validator("number")
    @classmethod
    def _canonical_number(cls, value: str) -> str:
        number = normalize_fc2_number(value)
        if number is None:
            raise ValueError("FC2 成员必须使用完整商品番号")
        return number


def parse_fc2_member(filename: str) -> Fc2Member | None:
    """仅解析明确的 FC2 文件名; 未知尾标不推测角色或顺序, 返回 None."""
    normalized = _normalized_stem(filename)
    if normalized is None:
        return None
    head = _FC2_HEAD.match(normalized)
    if head is None:
        return None
    number = normalize_fc2_number(head["number"])
    if number is None:
        return None
    tail = normalized[head.end() :]
    role: Literal["main", "special"] = "main"
    part: int | None = None
    if tail:
        separator = _SEPARATORS.match(tail)
        if separator is None:
            return None
        body = tail[separator.end() :]
        if marker := _SPECIAL.match(body):
            role = "special"
            part = int(marker["part"]) if marker["part"] else None
            tail = body[marker.end() :]
        elif marker := _EXPLICIT_MAIN.match(body):
            part = int(marker["part"])
            tail = body[marker.end() :]
        elif separator.group() in {"-", "_"} and (marker := _BARE_MAIN.match(body)):
            value = marker["part"]
            part = ord(value) - ord("A") + 1 if value in {"A", "B"} else int(value)
            tail = body[marker.end() :]
    if not _known_phase_tail(tail):
        return None
    # 添加扩展名以保留正文中的点分隔标记; 垃圾串已经过滤, 不重复应用配置正则.
    info = parse_file_info(text=f"{normalized}.mp4", escape_strings=[])
    return Fc2Member(
        number=number,
        role=role,
        part=part,
        definition=info.definition,
        has_subtitle=info.has_subtitle_in_name,
        mosaic=info.mosaic,
    )


def has_fc2_special_marker(filename: str) -> bool:
    """商品号后任一独立 SP 尾标均触发核对; 未知前缀、错位标记与非法序号仍由严格解析拒绝."""
    normalized = _normalized_stem(filename)
    if normalized is None:
        return False
    return any(
        token.startswith("SP")
        for head in _FC2_INTENT_HEAD.finditer(normalized)
        for token in _SEPARATORS.split(normalized[head.end() :])
    )


def _normalized_stem(filename: str) -> str | None:
    normalized = unicodedata.normalize("NFKC", strip_filename_junk(filename)).strip().upper()
    if "/" in normalized or "\\" in normalized:
        return None
    # 发布站域名只作前缀过滤, 不参与画质标记检测; 任意文本加 @ 不属于域名.
    if domain := _DOMAIN_PREFIX.match(normalized):
        normalized = normalized[domain.end() :]
    suffix = Path(normalized).suffix
    return normalized[: -len(suffix)] if suffix.lower() in _KNOWN_EXTENSIONS else normalized


def _known_phase_tail(tail: str) -> bool:
    if not tail:
        return True
    if re.fullmatch(r"(?:[-_.\s]+[^-_.\s]+)+", tail) is None:
        return False
    return all(token in _PHASE_TAGS or _DEFINITION.fullmatch(token) for token in _SEPARATORS.split(tail)[1:])


def plan_fc2_parts(members: Sequence[Fc2Member]) -> list[int | None]:
    """返回与输入对应的 CD; 存在特集时必须能证明正片与特集各自的完整顺序."""
    if len({member.number for member in members}) > 1:
        raise ValueError("同一组包含不同 FC2 番号，无法规划分集")
    if not any(member.role == "special" for member in members):
        return [member.part for member in members]
    if not any(member.role == "main" for member in members):
        raise ValueError("特集缺少同组正片，无法确定分集起点")
    mains = _ordered_indices(members, "main")
    specials = _ordered_indices(members, "special")
    if len(members) > 99:
        raise ValueError("正片与特集合计超过 99 段，无法生成分集编号")
    result: list[int | None] = [None] * len(members)
    for cd, index in enumerate([*mains, *specials], start=1):
        result[index] = cd
    return result


def _ordered_indices(members: Sequence[Fc2Member], role: Literal["main", "special"]) -> list[int]:
    selected = [(index, member) for index, member in enumerate(members) if member.role == role]
    label = "正片" if role == "main" else "特集"
    unnumbered = [member for _, member in selected if member.part is None]
    if unnumbered:
        if len(selected) == 1:
            return [selected[0][0]]
        if len(unnumbered) != len(selected):
            raise ValueError(f"{label}同时包含未编号文件与已编号分段，无法确定顺序")
        if len({(member.definition, member.has_subtitle, member.mosaic) for member in unnumbered}) > 1:
            raise ValueError(f"{label}存在多个未编号的画质或字幕版本，无法确定分集顺序")
        raise ValueError(f"{label}存在多个未编号文件，无法确定分集顺序")
    numbered: dict[int, int] = {}
    for index, member in selected:
        assert member.part is not None
        if member.part in numbered:
            raise ValueError(f"{label}第 {member.part} 段存在重复文件或不同版本，无法确定分集顺序")
        numbered[member.part] = index
    if sorted(numbered) != list(range(1, len(numbered) + 1)):
        parts = "、".join(str(part) for part in sorted(numbered))
        raise ValueError(f"{label}编号须从 1 连续排列，当前编号为 {parts}")
    return [numbered[part] for part in sorted(numbered)]
