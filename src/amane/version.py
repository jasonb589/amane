from __future__ import annotations

import os
from importlib.metadata import version

from ._custom_version import VERSION as _COMMITTED_VERSION

PACKAGE_NAME = "amane"

_BUILD_VERSION = ""
"""CI 尽力写入的版本标签; 构建缓存偶尔会吃掉, 只当兜底."""


def get_version() -> str:
    """版本优先级: ``AMANE_VERSION`` 环境变量 > 提交在代码里的 ``_custom_version.VERSION``
    > 构建时写入的 ``_BUILD_VERSION`` > 包元数据.

    版本号跟着补丁提交走, 所以镜像不依赖构建参数、也不受 Docker 构建缓存影响;
    ``AMANE_VERSION`` 仍可临时覆盖 (compose 里想固定显示时用).
    """
    for candidate in (os.environ.get("AMANE_VERSION", "").strip(), _COMMITTED_VERSION, _BUILD_VERSION):
        if candidate:
            return candidate
    return version(PACKAGE_NAME)
