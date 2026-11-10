"""手动剧照选图的候选限制、来源、裁切衔接与独立文件写回结果."""

import asyncio
import xml.etree.ElementTree as ET
from io import BytesIO
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import pytest
from PIL import Image

from amane.media.nfo import write_nfo
from amane.media.provenance import resolve_image_provenance
from amane.media.resource_store import internal_url_hash
from tests.helpers import LOAD_TOLERANT_TIMEOUT

if TYPE_CHECKING:
    from fastapi import FastAPI
    from httpx2 import AsyncClient

    from amane.db.repository import Repository


def _image_download(app: FastAPI, monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    async def download(url: str, destination: Path, **kwargs: object) -> bool:
        if "failed" in url:
            return False
        if "invalid" in url:
            destination.write_text("<html>challenge</html>", encoding="utf-8")
            return True
        if "truncated" in url:
            image = BytesIO()
            Image.new("RGB", (800, 538), "blue").save(image, format="JPEG")
            destination.write_bytes(image.getvalue()[:-100])
            return True
        Image.new("RGB", (800, 538), "blue").save(destination, format="PNG")
        return True

    downloaded = AsyncMock(side_effect=download)
    web = app.state.runtime.web_client
    monkeypatch.setattr(web, "resolve_final_url", AsyncMock(side_effect=lambda url, **kwargs: url))
    monkeypatch.setattr(web, "download", downloaded)
    return downloaded


@pytest.mark.asyncio
async def test_selection_without_media_keeps_source_and_locks_then_crops_current_poster(
    app: FastAPI, client: AsyncClient, repo: Repository, monkeypatch: pytest.MonkeyPatch, stop_worker: None
) -> None:
    selected = "https://images.example/still.png"
    old_thumb = "https://images.example/cover.jpg"
    metadata = await repo.upsert_metadata(
        "SELECT-001",
        poster_urls=["https://images.example/old.jpg"],
        thumb_urls=[old_thumb],
        extrafanart_urls={"javarchive": [selected]},
        field_sources={"title": "javdb", "poster_urls": "amazon"},
    )
    assert metadata.id is not None
    downloaded = _image_download(app, monkeypatch)
    endpoint = f"metadata/{metadata.id}/select-poster"
    response = await client.post(endpoint, json={"url": selected})
    assert response.status_code == 200, response.text
    body = response.json()
    poster = body["poster_url"]
    assert body["poster_urls"] == [poster] and poster.startswith("/api/resources/")
    assert body["thumb_urls"] == [old_thumb] and body["extrafanart_urls"] == {"javarchive": [selected]}
    assert body["locked_fields"] == ["poster_urls"]
    assert body["field_sources"] == {"title": "javdb", "poster_urls": "javarchive"}
    assert (
        body["poster_sync"]
        == body["nfo_sync"]
        == {
            "written": 0,
            "skipped": 0,
            "failed": 0,
            "unlocated": True,
        }
    )
    source = await resolve_image_provenance(poster, app.state.runtime.resource_store)
    assert source.source == "javarchive" and source.source_url == selected
    assert source.selected_still and not source.cropped
    assert (source.width, source.height) == (800, 538)
    assert "剧照" in source.label and "原生海报" not in source.label
    repeated = await client.post(endpoint, json={"url": selected})
    assert repeated.status_code == 200 and repeated.json()["poster_url"] == poster
    assert downloaded.await_count == 1

    auto = await repo.upsert_metadata(metadata.number, poster_urls=["https://changed.example/new.jpg"])
    assert auto.poster_url == poster
    cropped = await client.post(
        f"metadata/{metadata.id}/crop-poster",
        json={"source_url": poster, "left": 100, "top": 0, "right": 479, "bottom": 538},
    )
    assert cropped.status_code == 200, cropped.text
    crop_body = cropped.json()
    assert crop_body["poster_url"] != poster and crop_body["thumb_urls"] == [old_thumb]
    assert crop_body["poster_sync"]["unlocated"] and crop_body["nfo_sync"]["unlocated"]
    provenance = await resolve_image_provenance(crop_body["poster_url"], app.state.runtime.resource_store)
    assert provenance.source == "javarchive" and provenance.source_url == selected
    assert provenance.cropped and provenance.selected_still and "剧照裁剪" in provenance.label
    assert (provenance.width, provenance.height) == (379, 538)
    assert downloaded.await_count == 1


@pytest.mark.asyncio
async def test_selection_rejects_outside_candidates_invalid_images_and_changed_candidate(
    app: FastAPI, client: AsyncClient, repo: Repository, monkeypatch: pytest.MonkeyPatch, stop_worker: None
) -> None:
    valid = "https://images.example/still.png"
    failed = "https://images.example/failed.png"
    invalid = "https://images.example/invalid.jpg"
    truncated = "https://images.example/truncated.jpg"
    old_poster = "https://images.example/old.jpg"
    metadata = await repo.upsert_metadata(
        "SELECT-002", poster_urls=[old_poster], extrafanart_urls={"dmm": [valid, failed, invalid, truncated]}
    )
    assert metadata.id is not None
    endpoint = f"metadata/{metadata.id}/select-poster"
    downloaded = _image_download(app, monkeypatch)
    for url in ("file:///private.png", "http://127.0.0.1/secret.png", "/api/resources/foreign", valid + "?other=1"):
        response = await client.post(endpoint, json={"url": url})
        assert response.status_code == 400 and "已有的剧照" in response.json()["detail"]
    assert downloaded.await_count == 0
    assert (await client.post(endpoint, json={"url": valid, "source": "forged"})).status_code == 422
    assert (await client.post(endpoint, json={"url": ""})).status_code == 422
    assert (await client.post("metadata/999999/select-poster", json={"url": valid})).status_code == 404
    for url in (failed, invalid, truncated):
        response = await client.post(endpoint, json={"url": url})
        assert response.status_code == 400 and "有效图片" in response.json()["detail"]
        saved = await repo.get_metadata(metadata.id)
        assert saved is not None and saved.poster_urls == [old_poster] and saved.locked_fields == []
    response = await client.post(
        f"metadata/{metadata.id}/crop-poster",
        json={"source_url": valid, "left": 0, "top": 0, "right": 50, "bottom": 50},
    )
    assert response.status_code == 400 and "裁切源必须" in response.json()["detail"]

    entered, released = asyncio.Event(), asyncio.Event()
    original = app.state.runtime.web_client.download

    async def paused_download(url: str, destination: Path, **kwargs: object) -> bool:
        entered.set()
        await released.wait()
        return await original(url, destination, **kwargs)

    monkeypatch.setattr(app.state.runtime.web_client, "download", paused_download)
    request = asyncio.create_task(client.post(endpoint, json={"url": valid}))
    try:
        async with asyncio.timeout(LOAD_TOLERANT_TIMEOUT):
            await entered.wait()
            await repo.update_metadata(metadata.id, extrafanart_urls={})
            released.set()
            response = await request
        assert response.status_code == 409 and "剧照已发生变化" in response.json()["detail"]
        saved = await repo.get_metadata(metadata.id)
        assert saved is not None and saved.poster_urls == [old_poster] and saved.locked_fields == []
    finally:
        released.set()
        if not request.done():
            request.cancel()
        await asyncio.gather(request, return_exceptions=True)


@pytest.mark.asyncio
async def test_selection_writes_linked_and_moved_outputs_and_reports_independent_failures(
    app: FastAPI,
    client: AsyncClient,
    repo: Repository,
    monkeypatch: pytest.MonkeyPatch,
    safe_path: Path,
    stop_worker: None,
) -> None:
    root = safe_path / "library"
    root.mkdir()
    library = await repo.create_library("posters", str(root), poster_template="{video_dir}/{video_name}-poster.jpg")
    metadata = await repo.upsert_metadata("SELECT-003", extrafanart_urls={"dmm": ["https://images.example/still.png"]})
    assert library.id is not None and metadata.id is not None
    video = root / "SELECT-003-CD1.mp4"
    video.write_bytes(b"video-one")
    linked = await repo.create_media_file(library.id, path=str(video), metadata_id=metadata.id)
    nfo = video.with_suffix(".nfo")
    assert await write_nfo(metadata, nfo)
    assert await repo.remember_nfo_output(metadata.id, library.id, video, nfo)
    moved_directory = safe_path / "moved"
    moved_directory.mkdir()
    moved = moved_directory / "SELECT-003-CD2.mp4"
    moved.write_bytes(b"video-two")
    moved_nfo = moved.with_suffix(".nfo")
    assert await write_nfo(metadata, moved_nfo)
    assert await repo.remember_nfo_output(metadata.id, library.id, moved, moved_nfo)
    _image_download(app, monkeypatch)
    endpoint = f"metadata/{metadata.id}/select-poster"
    response = await client.post(endpoint, json={"url": metadata.extrafanart[0]})
    assert response.status_code == 200, response.text
    body = response.json()
    assert (
        body["poster_sync"]
        == body["nfo_sync"]
        == {
            "written": 2,
            "skipped": 0,
            "failed": 0,
            "unlocated": False,
        }
    )
    for media, output_nfo in ((video, nfo), (moved, moved_nfo)):
        poster = media.with_name(f"{media.stem}-poster.jpg")
        with Image.open(poster) as image:
            assert image.size == (800, 538)
        assert ET.parse(output_nfo).findtext("poster") == body["poster_url"]
    assert linked.id is not None
    linked_poster = video.with_name(f"{video.stem}-poster.jpg")
    original_nfo = nfo.read_bytes()
    Image.new("RGB", (60, 60), "red").save(linked_poster)
    nfo.write_text("<movie><num>OTHER-998</num></movie>", encoding="utf-8")
    mismatched = await client.post(endpoint, json={"url": metadata.extrafanart[0]})
    assert mismatched.status_code == 200
    assert mismatched.json()["poster_sync"]["failed"] == 1 and mismatched.json()["poster_sync"]["written"] == 1
    with Image.open(linked_poster) as image:
        assert image.size == (60, 60)
    nfo.write_bytes(original_nfo)
    await repo.delete_media_file(linked.id)
    assert await repo.get_media_by_metadata_id(metadata.id) == []
    updated = await client.post(endpoint, json={"url": metadata.extrafanart[0]})
    assert updated.status_code == 200 and updated.json()["poster_sync"]["written"] == 2

    moved_nfo.write_text("<movie><num>OTHER-999</num></movie>", encoding="utf-8")
    moved_poster = moved.with_name(f"{moved.stem}-poster.jpg")
    Image.new("RGB", (50, 50), "red").save(moved_poster)
    blocked = await client.post(endpoint, json={"url": metadata.extrafanart[0]})
    assert blocked.status_code == 200
    assert blocked.json()["poster_sync"]["failed"] == 1 and blocked.json()["poster_sync"]["written"] == 1
    with Image.open(moved_poster) as image:
        assert image.size == (50, 50)
    other = root / "OTHER-999.mp4"
    other.write_bytes(b"another-video")
    for template in ("../outside.jpg", "{video_dir}/{video_name}.mp4", "{video_dir}/{video_name}.nfo", str(other)):
        await repo.update_library(library.id, poster_template=template)
        response = await client.post(endpoint, json={"url": metadata.extrafanart[0]})
        assert response.status_code == 200
        assert response.json()["poster_sync"]["failed"] == 2
        assert response.json()["nfo_sync"]["failed"] == 1
        assert response.json()["poster_url"] == body["poster_url"]
        assert video.read_bytes() == b"video-one" and moved.read_bytes() == b"video-two"
        assert ET.parse(moved_nfo).findtext("num") == "OTHER-999"
        assert not (safe_path / "outside.jpg").exists()
        assert other.read_bytes() == b"another-video"
    hash_ = internal_url_hash(body["poster_url"])
    assert hash_ is not None
    cached = await app.state.runtime.resource_store.get_by_url_hash(hash_)
    assert cached is not None and cached[0].meta["op"] == "select-poster"

    plain_root = safe_path / "no-nfo"
    plain_root.mkdir()
    plain_library = await repo.create_library("no-nfo", str(plain_root), write_nfo=False)
    plain = await repo.upsert_metadata("SELECT-004", extrafanart_urls=metadata.extrafanart_urls)
    assert plain_library.id is not None and plain.id is not None
    plain_video = plain_root / "SELECT-004.mp4"
    plain_video.write_bytes(b"plain-video")
    await repo.create_media_file(plain_library.id, path=str(plain_video), metadata_id=plain.id)
    no_nfo = await client.post(f"metadata/{plain.id}/select-poster", json={"url": metadata.extrafanart[0]})
    assert no_nfo.status_code == 200 and no_nfo.json()["poster_sync"]["written"] == 1
    assert (plain_root / "poster.jpg").is_file() and not plain_video.with_suffix(".nfo").exists()
    text_target = plain_root / "protected.jpg"
    text_target.write_bytes(b"unrelated-non-image")
    await repo.update_library(plain_library.id, poster_template=str(text_target))
    rejected = await client.post(f"metadata/{plain.id}/select-poster", json={"url": metadata.extrafanart[0]})
    assert rejected.status_code == 200 and rejected.json()["poster_sync"]["failed"] == 1
    assert text_target.read_bytes() == b"unrelated-non-image"
