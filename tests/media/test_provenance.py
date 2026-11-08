"""通知图片来源必须与最终候选及裁剪资源链一致."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, cast
from unittest.mock import AsyncMock

import pytest
from PIL import Image

from amane.config import HotSettings
from amane.db.models import Resource
from amane.media import ResourceStore, materialize_images
from amane.media.provenance import resolve_image_provenance
from amane.media.resource_store import RESOURCE_URL_PREFIX
from tests.media.test_pipeline import FakeClient

if TYPE_CHECKING:
    from amane.net.http import WebClient


@pytest.mark.asyncio
async def test_selected_larger_poster_defines_source(resource_store: ResourceStore, tmp_path: Path) -> None:
    amazon = "https://m.media-amazon.com/images/P/B00TEST001.jpg"
    tenhow = "https://tenhow.net/images/B00TEST001.jpg"
    raw = {"amazon": {"poster_urls": [amazon]}, "tenhow": {"poster_urls": [tenhow]}}
    client = cast("WebClient", FakeClient({amazon: (352, 500), tenhow: (1055, 1500)}))
    output = await materialize_images([amazon, tenhow], [], [], resource_store, client, HotSettings(), tmp_path)

    info = await resolve_image_provenance(output.poster_urls[0], resource_store, raw)

    assert info.source == "tenhow"
    assert info.source_url == tenhow
    assert info.label == "Tenhow 原生海报 · 1055×1500"


@pytest.mark.asyncio
@pytest.mark.parametrize("size", [(800, 538), (2184, 1477)])
async def test_dmm_crop_quality_uses_source_pixels(
    resource_store: ResourceStore, tmp_path: Path, size: tuple[int, int]
) -> None:
    thumb = "https://awsimgsrc.dmm.co.jp/pics_dig/mono/movie/118abf295/118abf295pl.jpg"
    output = await materialize_images(
        [], [thumb], [], resource_store, cast("WebClient", FakeClient({thumb: size})), HotSettings(), tmp_path
    )

    info = await resolve_image_provenance(output.poster_urls[0], resource_store, {"javbus": {"thumb_urls": [thumb]}})

    assert info.source == "dmm"
    assert info.source_url == thumb
    assert info.cropped and not info.upscaled
    assert (info.source_width, info.source_height) == size
    assert ("高清横图裁剪" in info.label) is (size[0] >= 1000)
    assert f"源图 {size[0]}×{size[1]}" in info.label


@pytest.mark.asyncio
async def test_nested_manual_crops_trace_original_after_source_cache_removed(resource_store: ResourceStore) -> None:
    source = "https://m.media-amazon.com/images/I/81TEST001.jpg"

    async def produce(dest: Path) -> bool:
        Image.new("RGB", (500, 700)).save(dest)
        return True

    first = await resource_store.acquire_derived(source, "crop", "box:10,10,600,900", produce)
    assert first is not None
    second = await resource_store.acquire_derived(first.url, "crop", "box:0,0,500,700", produce)
    assert second is not None
    internal = f"{RESOURCE_URL_PREFIX}/{resource_store.url_hash(second.url)}?token=private"

    info = await resolve_image_provenance(internal, resource_store)

    assert info.source_url == source
    assert info.source == "amazon"
    assert info.cropped
    assert info.label == "Amazon 图片裁剪 · 500×700"
    assert info.source_width is None
    assert "private" not in info.label


@pytest.mark.asyncio
async def test_upscaled_source_does_not_claim_native_high_resolution(resource_store: ResourceStore) -> None:
    source = "https://awsimgsrc.dmm.co.jp/pics_dig/digital/video/ssis00497/ssis00497pl.jpg"
    await resource_store.acquire(source, cast("WebClient", FakeClient({source: (800, 538)})))
    original = await resource_store.get_by_url(source)
    assert original is not None

    async def upscale(src: Path, out: Path) -> bool:
        Image.new("RGB", (3200, 2152)).save(out)
        return True

    await resource_store.upscale_in_place(original, {"tool": "test", "scale": 4}, upscale)

    async def crop(dest: Path) -> bool:
        Image.new("RGB", (1506, 2152)).save(dest)
        return True

    cropped = await resource_store.acquire_derived(source, "crop", "0.7", crop)
    assert cropped is not None
    info = await resolve_image_provenance(
        f"{RESOURCE_URL_PREFIX}/{resource_store.url_hash(cropped.url)}", resource_store
    )

    assert info.source == "dmm"
    assert info.cropped and info.upscaled
    assert info.source_width is None and info.source_height is None
    assert (info.width, info.height) == (1506, 2152)
    assert "高清" not in info.label
    assert "已超分" in info.label


@pytest.mark.asyncio
async def test_remote_photo_does_not_report_local_upscale(resource_store: ResourceStore) -> None:
    source = "https://pics.dmm.co.jp/digital/video/ssis00497/ssis00497ps.jpg?signature=private"
    await resource_store.acquire(source, cast("WebClient", FakeClient({source: (147, 200)})))
    original = await resource_store.get_by_url(source)
    assert original is not None

    async def upscale(src: Path, out: Path) -> bool:
        Image.new("RGB", (1470, 2000)).save(out)
        return True

    await resource_store.upscale_in_place(original, {"tool": "test", "scale": 10}, upscale)
    remote = await resolve_image_provenance(source, resource_store, local_pixels=False)
    local = await resolve_image_provenance(source, resource_store)
    internal = await resolve_image_provenance(
        f"{RESOURCE_URL_PREFIX}/{resource_store.url_hash(source)}", resource_store, local_pixels=False
    )

    assert remote.label == "DMM 原生海报"
    assert not remote.upscaled
    assert remote.width is None and remote.source_width is None
    assert local.label == internal.label == "DMM 原生海报 · 已超分 · 1470×2000"
    assert "private" not in local.label


@pytest.mark.asyncio
async def test_remote_photo_can_use_original_cached_dimensions(resource_store: ResourceStore) -> None:
    source = "https://awsimgsrc.dmm.co.jp/pics_dig/mono/movie/118abf295/118abf295ps.jpg"
    await resource_store.acquire(source, cast("WebClient", FakeClient({source: (1032, 1477)})))

    info = await resolve_image_provenance(source, resource_store, local_pixels=False)

    assert info.label == "DMM 高清原生海报 · 1032×1477"
    assert not info.upscaled


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("url", "raw", "expected"),
    [
        ("https://images-na.ssl-images-amazon.com/images/I/81TEST001.jpg", {}, "Amazon 原生海报"),
        ("https://www.tenhow.net/images/B00TEST001.jpg", {}, "Tenhow 原生海报"),
        ("https://pics.dmm.co.jp/mono/movie/adult/x/xps.jpg", {}, "DMM 原生海报"),
        (
            "https://cdn.example/cover.jpg",
            {"javarchive": {"poster_urls": ["https://cdn.example/cover.jpg"]}},
            "JavArchive 原生海报",
        ),
        (
            "https://cdn.example/cover.jpg",
            {"fc2ppv_db": {"poster_urls": ["https://cdn.example/cover.jpg"]}},
            "FC2PPV-DB 原生海报",
        ),
        (
            "https://cdn.example/cover.jpg",
            {"custom.provider:jp": {"poster_urls": ["https://cdn.example/cover.jpg"]}},
            "custom.provider 原生海报",
        ),
        (
            "https://cdn.example/cover.jpg",
            {"wrong": {"poster_urls": ["https://cdn.example/other.jpg"]}},
            "cdn.example 原生海报",
        ),
        ("https://awsimgsrc.dmm.co.jp.evil.example/cover.jpg", {}, "awsimgsrc.dmm.co.jp.evil.example 原生海报"),
        ("https://user:secret@pics.dmm.co.jp/image.jpg", {}, "来源未知"),
        ("https://[invalid/image.jpg", {}, "来源未知"),
        ("file:///movie/poster.jpg", {}, "来源未知"),
        ("/api/resources/deadbeef?token=private", {}, "来源未知"),
        ("", {}, "来源未知"),
    ],
)
async def test_url_attribution_without_local_resource(url: str, raw: dict[str, object], expected: str) -> None:
    info = await resolve_image_provenance(url, None, raw)
    assert info.label == expected
    assert info.width is None


@pytest.mark.asyncio
async def test_thumb_fallback_reports_actual_horizontal_image(resource_store: ResourceStore) -> None:
    thumb = "https://pics.dmm.co.jp/mono/movie/adult/x/xpl.jpg"
    await resource_store.acquire(thumb, cast("WebClient", FakeClient({thumb: (2184, 1477)})))
    info = await resolve_image_provenance(thumb, resource_store, kind="thumb")
    assert info.label == "DMM 高清横图 · 2184×1477"
    assert not info.cropped


@pytest.mark.asyncio
@pytest.mark.parametrize("parent", ["derived:cycle", None, 1])
async def test_malformed_or_cyclic_resource_chain_terminates(
    resource_store: ResourceStore, monkeypatch: pytest.MonkeyPatch, parent: object
) -> None:
    record = Resource(url="derived:cycle", file_path="missing.jpg", meta={"op": "crop", "src": parent})
    lookup = AsyncMock(return_value=record)
    monkeypatch.setattr(resource_store, "get_by_url", lookup)

    info = await resolve_image_provenance(record.url, resource_store)

    assert info.label == "来源未知"
    assert lookup.await_count == 1


@pytest.mark.asyncio
async def test_resource_chain_depth_is_bounded(resource_store: ResourceStore, monkeypatch: pytest.MonkeyPatch) -> None:
    async def next_resource(url: str) -> Resource:
        index = int(url.rsplit(":", 1)[1])
        return Resource(url=url, file_path="missing.jpg", meta={"op": "crop", "src": f"derived:{index + 1}"})

    lookup = AsyncMock(side_effect=next_resource)
    monkeypatch.setattr(resource_store, "get_by_url", lookup)
    info = await resolve_image_provenance("derived:0", resource_store)
    assert info.source_url == ""
    assert info.label == "来源未知"
    assert lookup.await_count <= 20
