"""影片编辑与 NFO 同步结果的 HTTP 接线."""

import xml.etree.ElementTree as ET
from typing import TYPE_CHECKING

import pytest

from amane.media.nfo import write_nfo

if TYPE_CHECKING:
    from pathlib import Path

    from httpx2 import AsyncClient

    from amane.db.repository import Repository


@pytest.mark.asyncio
async def test_patch_syncs_remembered_output_and_reports_unavailable_targets(
    client: AsyncClient, repo: Repository, safe_path: Path
) -> None:
    library = await repo.create_library("source", str(safe_path / "source"))
    output = safe_path / "moved"
    output.mkdir()
    video = output / "ABC-001.mp4"
    video.write_bytes(b"media")
    nfo = video.with_suffix(".nfo")
    metadata = await repo.upsert_metadata("ABC-001", title="Original")
    assert metadata.id is not None and library.id is not None
    assert await write_nfo(metadata, nfo)
    assert await repo.remember_nfo_output(metadata.id, library.id, video, nfo)

    response = await client.patch(f"metadata/{metadata.id}", json={"title": "Edited", "actors": ["Actor"]})
    assert response.status_code == 200
    assert response.json()["nfo_sync"] == {"written": 1, "skipped": 0, "failed": 0, "unlocated": False}
    assert ET.parse(nfo).findtext("title") == "ABC-001 Edited"

    nfo.write_text("<movie><num>ABC-999</num></movie>", encoding="utf-8")
    response = await client.patch(f"metadata/{metadata.id}", json={"title": "Saved despite mismatch"})
    assert response.status_code == 200
    assert response.json()["nfo_sync"]["failed"] == 1
    assert response.json()["title"] == "Saved despite mismatch"
    assert ET.parse(nfo).findtext("num") == "ABC-999"

    detached = await repo.upsert_metadata("ABC-002", title="No output")
    assert detached.id is not None
    response = await client.patch(f"metadata/{detached.id}", json={"title": "Saved"})
    assert response.status_code == 200 and response.json()["nfo_sync"]["unlocated"] is True
