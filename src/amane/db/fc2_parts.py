"""FC2 分段分配的持久化值; 路径归一仅处理字面形式, 不访问文件系统."""

import os
from collections.abc import Sequence
from pathlib import Path
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ..utils.path import nfc_path, path_key


def normalize_fc2_plan_path(path: str | Path) -> str:
    value = nfc_path(str(path))
    if not value.strip() or "\x00" in value or not Path(value).is_absolute():
        raise ValueError("FC2 分段路径必须是非空绝对路径")
    return os.path.normpath(value)


class Fc2PlannedMember(BaseModel):
    """原始成员身份与输出编号固定; 目标和落盘状态仅由 Repository 修改."""

    model_config = ConfigDict(frozen=True, extra="forbid", revalidate_instances="always")

    source_path: str
    role: Literal["main", "special"]
    part: int | None = Field(default=None, gt=0, strict=True)
    cd: int = Field(gt=0, strict=True)
    source_size: int = Field(ge=0, strict=True)
    source_mtime_ns: int = Field(ge=0, strict=True)
    target_path: str | None = None
    target_mtime_ns: int | None = Field(default=None, ge=0, strict=True)
    placed: bool = Field(default=False, strict=True)

    @field_validator("source_path", "target_path")
    @classmethod
    def _normalize_path(cls, value: str | None) -> str | None:
        return normalize_fc2_plan_path(value) if value is not None else None

    @model_validator(mode="after")
    def _placed_requires_target(self) -> Self:
        if self.placed and (self.target_path is None or self.target_mtime_ns is None):
            raise ValueError("FC2 已落盘成员必须具有目标路径与目标修改时间")
        if not self.placed and self.target_mtime_ns is not None:
            raise ValueError("FC2 尚未落盘成员不能具有目标修改时间")
        return self


def validate_fc2_part_members(members: Sequence[Fc2PlannedMember]) -> tuple[Fc2PlannedMember, ...]:
    validated = tuple(Fc2PlannedMember.model_validate(member) for member in members)
    if not validated:
        raise ValueError("FC2 分段文件组不能为空")
    source_keys: set[str] = set()
    cds: set[int] = set()
    target_keys: set[str] = set()
    for member in validated:
        source_key = path_key(member.source_path)
        if source_key in source_keys:
            raise ValueError("FC2 分段文件组包含重复源路径")
        if member.cd in cds:
            raise ValueError("FC2 分段文件组包含重复输出编号")
        source_keys.add(source_key)
        cds.add(member.cd)
        if member.target_path is not None:
            target_key = path_key(member.target_path)
            if target_key in target_keys:
                raise ValueError("FC2 分段文件组包含重复目标路径")
            target_keys.add(target_key)
    for member in validated:
        if (
            member.target_path is not None
            and path_key(member.target_path) != path_key(member.source_path)
            and path_key(member.target_path) in source_keys
        ):
            raise ValueError("FC2 分段目标不能占用同组其他源文件")
    return tuple(sorted(validated, key=lambda member: member.cd))
