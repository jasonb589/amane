"""测试 NFO 附属文件生成"""

import errno
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from amane.db.models import Metadata
from amane.media import write_nfo


@pytest.fixture
def metadata() -> Metadata:
    return Metadata(
        number="MIDV-123",
        title="Test Title",
        actors=["Actor A", "Actor B"],
        studio="Studio X",
        release="2026-01-15",
        runtime=120,
        tags=["Drama", "Romance"],
        series="Series Y",
        scores={"javdb": 85.0},
        directors=["Director Z"],
    )


@pytest.mark.asyncio(loop_scope="function")
async def test_write_nfo_has_required_fields(tmp_path: Path, metadata: Metadata):
    nfo_path = tmp_path / "MIDV-123.nfo"
    ok = await write_nfo(metadata, nfo_path)
    assert ok is True
    assert nfo_path.exists()
    content = nfo_path.read_text(encoding="utf-8")
    assert "<title>MIDV-123 Test Title</title>" in content
    assert "<num>MIDV-123</num>" in content
    assert "<actor>" in content
    assert "<name>Actor A</name>" in content
    assert "<studio>Studio X</studio>" in content
    assert "<genre>Drama</genre>" in content
    assert "<director>Director Z</director>" in content
    assert "<rating>85.0</rating>" in content
    # 系列使用规范嵌套: <set><name>…</name></set>
    assert "<series>Series Y</series>" in content
    assert "<set>" in content
    assert "<name>Series Y</name>" in content


@pytest.mark.asyncio(loop_scope="function")
async def test_write_nfo_plot_is_plain_text(tmp_path: Path):
    """长文本按纯文本写出: 无 CDATA, HTML 转义, 非法字符剥离, 换行原样保留."""
    plot = "结尾 ]]> 之后 & <标签>\n\n第二段\x0b"
    nfo_path = tmp_path / "PLOT-1.nfo"

    assert await write_nfo(Metadata(number="PLOT-1", title="T", plot=plot), nfo_path) is True

    content = nfo_path.read_text(encoding="utf-8")
    assert "<![CDATA[" not in content
    assert "]]>" not in content
    assert "\x0b" not in content

    root = ET.fromstring(content)
    assert root.findtext("plot") == "结尾 ]]> 之后 & <标签>\n\n第二段"
    assert root.findtext("outline") == root.findtext("plot")


@pytest.mark.asyncio
async def test_language_qualified_source_ids_keep_nfo_parseable(tmp_path: Path) -> None:
    metadata = Metadata(number="ABC-001", external_ids={"r18dev:jp": "abc00001", "dmm": "123"})
    nfo = tmp_path / "ABC-001.nfo"
    assert await write_nfo(metadata, nfo)
    root = ET.parse(nfo).getroot()
    assert root.findtext("dmmid") == "123"
    assert root.find("uniqueid[@type='r18dev:jp']").text == "abc00001"  # type: ignore[union-attr]
    assert await write_nfo(metadata, nfo, expected_number="ABC-001")
    nfo.write_text("<movie><num>ABC-001</num><r18dev:jpid>old</r18dev:jpid></movie>", encoding="utf-8")
    assert await write_nfo(metadata, nfo, expected_number="ABC-001")
    assert ET.parse(nfo).findtext("num") == "ABC-001"


@pytest.mark.asyncio
@pytest.mark.parametrize("existing", [False, True])
async def test_nfo_write_on_mount_that_rejects_chmod(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, existing: bool
) -> None:
    nfo = tmp_path / "ABC-001.nfo"
    if existing:
        nfo.write_text("<movie><num>ABC-001</num><title>Original</title></movie>", encoding="utf-8")

    def deny_chmod(path: Path, mode: int) -> None:
        raise PermissionError(errno.EPERM, "Operation not permitted", str(path))

    monkeypatch.setattr(Path, "chmod", deny_chmod)
    assert await write_nfo(Metadata(number="ABC-001", title="Edited"), nfo, expected_number="ABC-001")
    assert ET.parse(nfo).findtext("title") == "ABC-001 Edited"
    assert list(tmp_path.iterdir()) == [nfo]
