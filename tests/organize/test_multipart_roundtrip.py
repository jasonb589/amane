"""分段模板的再次解析保留番号、分段与文件标记."""

from pathlib import Path

import pytest

from amane.db.models import Library, Metadata
from amane.organize import resolve_paths
from amane.parsing import mosaic_nature, parse_file_info


@pytest.mark.parametrize(
    ("filename", "number", "part"),
    [
        ("4K688.com@fc4982441_1.mp4", "FC2-4982441", 1),
        ("FC2-PPV-4982441_2-UC-4K.mp4", "FC2-4982441", 2),
        ("MIDV-123_10.mp4", "MIDV-123", 10),
        ("MIDV-123 Part 2.mp4", "MIDV-123", 2),
        ("BCD-1.mp4", "BCD-1", None),
        ("CD-1.mp4", "CD-1", None),
        ("BCD-1-CD2.mp4", "BCD-1", 2),
        ("010616_01.mp4", "010616_01", None),
        ("010616_01_2.mp4", "010616_01", 2),
        ("HEYDOUGA-4037-1.mp4", "HEYDOUGA-4037-1", None),
        ("MD0165-1.mp4", "MD0165-1", None),
        ("SSIS123-A.mp4", "SSIS-123", 1),
    ],
)
@pytest.mark.parametrize("part_template", ["[-CD{cd?}]", "[-Part {cd?}]"])
def test_multipart_templates_roundtrip_without_losing_catalog_identity(
    tmp_path: Path, filename: str, number: str, part: int | None, part_template: str
) -> None:
    library = Library(
        name="Multipart",
        path=str(tmp_path),
        video_template="{number}/{number}" + part_template + "[-{mosaic?}][-{def?}][-{sub?}].{ext}",
    )
    original = parse_file_info(tmp_path / filename)
    assert (original.number, original.cd) == (number, part)
    metadata = Metadata(number=number, content_type=original.content_type)
    first = resolve_paths(library, metadata, ext="mp4", file_info=original)
    parsed = parse_file_info(first.video)
    assert (parsed.number, parsed.content_type, parsed.cd, parsed.has_subtitle, parsed.definition) == (
        original.number,
        original.content_type,
        original.cd,
        original.has_subtitle,
        original.definition,
    )
    # 模板按条目类型补出无码标记, 再解析后仍须表达同一文件性质.
    assert mosaic_nature(parsed.mosaic, parsed.content_type) == mosaic_nature(original.mosaic, original.content_type)
    second = resolve_paths(library, metadata, ext="mp4", file_info=parsed)
    assert second.video == first.video
    assert second.nfo == first.nfo
