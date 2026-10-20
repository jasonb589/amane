"""自定义构建号的版本比较 (面板更新提示)."""

from amane.release import is_newer


def test_custom_build_version_is_compared_by_base() -> None:
    # ``0.22.1-custom.3`` 不是合法 PEP440: 没有宽松解析时 is_newer 恒为 False, 面板永远不提示更新.
    assert is_newer("v99.0.0", "0.22.1-custom.3") is True
    assert is_newer("v0.1.0", "0.22.1-custom.3") is False


def test_plain_versions_still_compare() -> None:
    assert is_newer("v0.23.0", "0.22.1") is True
    assert is_newer("v0.22.1", "0.22.1") is False


def test_non_version_tags_never_count_as_update() -> None:
    assert is_newer("app-1.0.0", "0.22.1-custom.3") is False
    assert is_newer("v99.0.0", "") is False
