"""六位日期号归一化: 文件名里的 122524_01 / 122524-1 统一成站内写法 122524-001."""

import pytest

from amane.enums import ContentType
from amane.parsing.file_info import _normalize_six_digit, parse_file_info


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("122524_01", "122524-001"),
        ("122524-001", "122524-001"),
        ("122524_1", "122524-001"),
        ("010115_002", "010115-002"),
        ("122524-0001", "122524-0001"),
    ],
)
def test_normalize_six_digit(raw: str, expected: str) -> None:
    assert _normalize_six_digit(raw) == expected


def test_parse_file_info_yields_canonical_number() -> None:
    parsed = parse_file_info("/media/cloud/122524_01-1080p.mp4")
    assert parsed.number == "122524-001"
    assert parsed.content_type is ContentType.UNCENSORED
