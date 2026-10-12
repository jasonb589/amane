from __future__ import annotations

import os
from importlib.metadata import version

PACKAGE_NAME = "amane"

_BUILD_VERSION = ""
"""自定义镜像构建时由 CI 写入的版本标签 (空 = 用包自身版本)."""


def get_version() -> str:
    """版本优先级: ``AMANE_VERSION`` 环境变量 > 构建时写入的 ``_BUILD_VERSION`` > 包元数据.

    构建时写入让镜像自带版本号 (不受 Docker 构建缓存影响); 环境变量仍可临时覆盖.
    """
    override = os.environ.get("AMANE_VERSION", "").strip()
    if override:
        return override
    if _BUILD_VERSION:
        return _BUILD_VERSION
    return version(PACKAGE_NAME)
