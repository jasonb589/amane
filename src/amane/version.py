from __future__ import annotations

import os
from importlib.metadata import version

PACKAGE_NAME = "amane"


def get_version() -> str:
    """已安装包的版本 (``pyproject.toml`` → dist-info); 设置了 ``AMANE_VERSION`` 时以其为准.

    自定义镜像在构建时把版本标签注入 ``AMANE_VERSION``, 面板因此显示自己的版本号.
    """
    override = os.environ.get("AMANE_VERSION", "").strip()
    if override:
        return override
    return version(PACKAGE_NAME)
