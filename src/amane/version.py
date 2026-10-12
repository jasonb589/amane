from __future__ import annotations

import os
from importlib.metadata import version
from pathlib import Path

PACKAGE_NAME = "amane"

#: CI 构建镜像时写下的版本标签文件 (构建缓存把源码/环境变量吃掉的兜底).
_BUILD_VERSION_FILE = Path("/app/.amane-build-version")

_BUILD_VERSION = ""
"""自定义镜像构建时由 CI 写入的版本标签 (空 = 用包自身版本)."""


def _file_version() -> str:
    """读镜像内版本文件; 不存在或读不出时返回空串."""
    try:
        return _BUILD_VERSION_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def get_version() -> str:
    """版本优先级: ``AMANE_VERSION`` 环境变量 > 构建时写入的 ``_BUILD_VERSION`` > 镜像内版本文件 > 包元数据.

    前三级都来自构建时注入, 让镜像自带版本号; 环境变量 (compose) 仍可临时覆盖.
    """
    for candidate in (os.environ.get("AMANE_VERSION", "").strip(), _BUILD_VERSION, _file_version()):
        if candidate:
            return candidate
    return version(PACKAGE_NAME)
