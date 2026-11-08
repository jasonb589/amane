"""测试刮削期资源管线 materialize_images (选源/裁剪/就地超分)."""

import asyncio
import contextlib
from typing import TYPE_CHECKING, cast
from unittest.mock import AsyncMock

import pytest
import sqlalchemy.exc
from PIL import Image

from amane.config import HotSettings, SrConfig
from amane.enums import DownloadableResource
from amane.media import CropRotation, ResourceStore, manual_crop_image, materialize_images
from amane.media import pipeline as pipeline_mod
from tests.helpers import LOAD_TOLERANT_TIMEOUT, await_for

if TYPE_CHECKING:
    from pathlib import Path

    from amane.net.http import WebClient


class FakeClient:
    """伪 WebClient: download 按 url 写出预设尺寸的图片 (或视频占位); fail 集合内 URL 下载失败."""

    def __init__(
        self,
        sizes: dict[str, tuple[int, int]],
        fail: set[str] | None = None,
        orientations: dict[str, int] | None = None,
    ):
        self._sizes = sizes
        self.fail = fail or set()
        self.orientations = orientations or {}
        self.downloaded: list[str] = []

    async def download(self, url: str, dest: Path, **kwargs) -> bool:
        self.downloaded.append(url)
        if url in self.fail:
            return False
        dest.parent.mkdir(parents=True, exist_ok=True)
        size = self._sizes.get(url)
        if size is None:
            dest.write_bytes(b"video-bytes")  # trailer 等非图
            return True
        img = Image.new("RGB", size, "blue")
        orientation = self.orientations.get(url)
        if orientation is None:
            img.save(dest)
        else:
            exif = img.getexif()
            exif[274] = orientation
            img.save(dest, exif=exif)
        return True

    async def get_filesize(self, url: str, **kwargs) -> int | None:
        return None

    async def resolve_final_url(self, url: str, **kwargs) -> str:
        return url


@pytest.mark.asyncio
async def test_poster_cropped_when_candidate_small(resource_store: ResourceStore, tmp_path: Path):
    """poster 候选偏矮 → 从 thumb 按配置 poster_ratio 靠右裁, 不用候选长宽比."""
    client = FakeClient(
        {
            "https://s/t.jpg": (800, 538),
            "https://s/p.jpg": (300, 420),  # b/h=0.78 < 0.9 → 裁; 候选比 ≈0.714 ≠ 配置 0.7
        }
    )
    cfg = HotSettings()  # sr.enabled 默认 False; poster_ratio 默认 0.7
    out = await materialize_images(
        ["https://s/p.jpg"], ["https://s/t.jpg"], [], resource_store, cast("WebClient", client), cfg, tmp_path
    )
    assert out.thumb_urls == ["https://s/t.jpg"]  # 外部列表原样
    assert len(out.poster_urls) == 1
    assert out.poster_urls[0].startswith(pipeline_mod.RESOURCE_URL_PREFIX)  # 内部派生
    # 派生 args 为配置比例, 非候选 300/420
    url_hash = out.poster_urls[0].rsplit("/", 1)[-1]
    got = await resource_store.get_by_url_hash(url_hash)
    assert got is not None
    crop_res, crop_path = got
    assert crop_res.meta is not None
    assert crop_res.meta.get("args") == "0.7000"
    poster = Image.open(crop_path)
    assert poster.size == (376, 538)  # int(538 * 0.7), 非 int(538 * 300/420)=384


@pytest.mark.asyncio
async def test_poster_external_when_candidate_tall(resource_store: ResourceStore, tmp_path: Path):
    """poster 候选够高 → 不裁剪, poster 记外部 URL."""
    client = FakeClient(
        {
            "https://s/t.jpg": (800, 538),
            "https://s/p.jpg": (379, 538),  # b/h=1.0 ≥ 0.9 → 不裁
        }
    )
    out = await materialize_images(
        ["https://s/p.jpg"], ["https://s/t.jpg"], [], resource_store, cast("WebClient", client), HotSettings(), tmp_path
    )
    assert out.poster_urls == ["https://s/p.jpg"]  # 外部列表原样


@pytest.mark.asyncio
async def test_poster_picks_largest_candidate_over_priority(resource_store: ResourceStore, tmp_path: Path):
    """第一个候选只有小图时, 后面的大图要顶到最前 (amazon 兜底图 352x500 的教训)."""
    client = FakeClient(
        {
            "https://amazon/P.jpg": (352, 500),  # 排在 field_priority 第一位, 但只有小图
            "https://dmm/ps.jpg": (1032, 1468),
            "https://s/t.jpg": (800, 538),
        }
    )
    out = await materialize_images(
        ["https://amazon/P.jpg", "https://dmm/ps.jpg"],
        ["https://s/t.jpg"],
        [],
        resource_store,
        cast("WebClient", client),
        HotSettings(),
        tmp_path,
    )
    assert out.poster_urls[0] == "https://dmm/ps.jpg"
    assert out.poster_urls[1] == "https://amazon/P.jpg"


@pytest.mark.asyncio
async def test_poster_keeps_priority_order_when_sizes_equal(resource_store: ResourceStore, tmp_path: Path):
    """同尺寸时仍按来源顺序, 不改既有优先级语义."""
    client = FakeClient({"https://a/p.jpg": (1032, 1468), "https://b/p.jpg": (1032, 1468)})
    out = await materialize_images(
        ["https://a/p.jpg", "https://b/p.jpg"],
        [],
        [],
        resource_store,
        cast("WebClient", client),
        HotSettings(),
        tmp_path,
    )
    assert out.poster_urls == ["https://a/p.jpg", "https://b/p.jpg"]


@pytest.mark.asyncio
async def test_small_candidate_falls_back_to_cropped_thumb(resource_store: ResourceStore, tmp_path: Path):
    """候选最长边 < 800 时, 拿 thumb 裁一份比面积: 裁出来的更大就换掉那张小图."""
    client = FakeClient({"https://amazon/P.jpg": (352, 500), "https://s/t.jpg": (800, 538)})
    out = await materialize_images(
        ["https://amazon/P.jpg"],
        ["https://s/t.jpg"],
        [],
        resource_store,
        cast("WebClient", client),
        HotSettings(),
        tmp_path,
    )
    # 800x538 裁 0.7 → 376x538 (202288) > 352x500 (176000) → 用裁的
    assert out.poster_urls[0].startswith(pipeline_mod.RESOURCE_URL_PREFIX)
    url_hash = out.poster_urls[0].rsplit("/", 1)[-1]
    got = await resource_store.get_by_url_hash(url_hash)
    assert got is not None
    _, crop_path = got
    assert Image.open(crop_path).size == (376, 538)


@pytest.mark.asyncio
@pytest.mark.parametrize("download_thumb", [True, False])
async def test_crop_selects_high_resolution_cover_after_small_first_candidate(
    resource_store: ResourceStore, tmp_path: Path, download_thumb: bool
) -> None:
    client = FakeClient(
        {"https://s/small.jpg": (800, 538), "https://s/large.jpg": (2400, 1614), "https://s/poster.jpg": (352, 500)}
    )
    config = HotSettings()
    if not download_thumb:
        config.scraping.download_resources = [DownloadableResource.poster]
    covers = ["https://s/small.jpg", "https://s/large.jpg"]
    out = await materialize_images(
        ["https://s/poster.jpg"], covers, [], resource_store, cast("WebClient", client), config, tmp_path
    )
    found = await resource_store.get_by_url_hash(out.poster_urls[0].rsplit("/", 1)[-1])
    assert found is not None
    with Image.open(found[1]) as image:
        assert image.size == (1129, 1614)
    assert found[0].meta["src"] == "https://s/large.jpg"
    assert out.thumb_urls == (list(reversed(covers)) if download_thumb else covers)


@pytest.mark.asyncio
async def test_cover_quality_selection_ignores_invalid_images_and_preserves_equal_priority(
    resource_store: ResourceStore, tmp_path: Path
) -> None:
    urls = ["https://s/invalid.jpg", "https://s/first.jpg", "https://s/equal.jpg"]
    client = FakeClient({urls[1]: (1600, 1076), urls[2]: (1600, 1076)})
    config = HotSettings()
    config.scraping.download_resources = [DownloadableResource.thumb]
    out = await materialize_images([], urls, [], resource_store, cast("WebClient", client), config, tmp_path)
    assert out.thumb_urls == [urls[1], urls[2], urls[0]]


@pytest.mark.asyncio
async def test_cover_quality_selection_precedes_upscaling(
    resource_store: ResourceStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    upscaler = AsyncMock()
    monkeypatch.setattr(pipeline_mod, "run_SR", upscaler)
    urls = ["https://s/small.jpg", "https://s/large.jpg"]
    client = FakeClient({urls[0]: (800, 538), urls[1]: (2400, 1614)})
    config = HotSettings()
    config.scraping.download_resources = [DownloadableResource.thumb]
    config.sr.enabled = True
    out = await materialize_images([], urls, [], resource_store, cast("WebClient", client), config, tmp_path)
    assert out.thumb_urls == list(reversed(urls))
    upscaler.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", [DownloadableResource.poster, DownloadableResource.thumb])
async def test_native_image_precedes_previously_upscaled_cache(
    resource_store: ResourceStore, tmp_path: Path, kind: DownloadableResource
) -> None:
    low, native = "https://s/low.jpg", "https://s/native.jpg"
    client = FakeClient({low: (400, 600), native: (1000, 1500)})
    await resource_store.acquire(low, cast("WebClient", client))
    record = await resource_store.get_by_url(low)
    assert record is not None

    async def upscale(src: Path, output: Path) -> bool:
        Image.new("RGB", (1600, 2400)).save(output)
        return True

    await resource_store.upscale_in_place(record, {"scale": 4}, upscale)
    saved = await resource_store.get_by_url(low)
    assert saved is not None and saved.meta["sr"]["source_size"] == [400, 600]
    config = HotSettings()
    config.scraping.download_resources = [kind]
    urls = [low, native]
    out = await materialize_images(
        urls if kind is DownloadableResource.poster else [],
        urls if kind is DownloadableResource.thumb else [],
        [],
        resource_store,
        cast("WebClient", client),
        config,
        tmp_path,
    )
    assert (out.poster_urls if kind is DownloadableResource.poster else out.thumb_urls) == [native, low]


@pytest.mark.asyncio
@pytest.mark.parametrize("candidate_size", [None, (400, 600)])
async def test_cached_upscale_does_not_suppress_native_cover_crop(
    resource_store: ResourceStore, tmp_path: Path, candidate_size: tuple[int, int] | None
) -> None:
    low, cover = "https://s/low.jpg", "https://s/cover.jpg"
    client = FakeClient({low: (400, 600), cover: (2200, 1500)})
    await resource_store.acquire(low, cast("WebClient", client))
    record = await resource_store.get_by_url(low)
    assert record is not None

    async def upscale(src: Path, output: Path) -> bool:
        Image.new("RGB", (1600, 2400)).save(output)
        return True

    await resource_store.upscale_in_place(record, {"scale": 4}, upscale)
    if candidate_size is None:
        async with resource_store._session() as session:
            legacy = await session.get(type(record), record.id)
            assert legacy is not None
            legacy.meta = {"sr": {"scale": 4}}
            session.add(legacy)
            await session.commit()
    config = HotSettings()
    config.scraping.download_resources = [DownloadableResource.poster]
    out = await materialize_images([low], [cover], [], resource_store, cast("WebClient", client), config, tmp_path)
    chosen = await resource_store.get_by_url_hash(out.poster_urls[0].rsplit("/", 1)[-1])
    assert chosen is not None and chosen[0].meta["src"] == cover
    with Image.open(chosen[1]) as image:
        assert image.size == (1050, 1500)


@pytest.mark.asyncio
@pytest.mark.parametrize("legacy_sr", [False, True])
async def test_upscaled_cover_crop_does_not_replace_better_native_poster(
    resource_store: ResourceStore, tmp_path: Path, legacy_sr: bool
) -> None:
    cover, poster = "https://s/cover.jpg", "https://s/poster.jpg"
    client = FakeClient({cover: (600, 400), poster: (400, 600)})
    await resource_store.acquire(cover, cast("WebClient", client))
    record = await resource_store.get_by_url(cover)
    assert record is not None

    async def upscale(src: Path, output: Path) -> bool:
        Image.new("RGB", (2400, 1600)).save(output)
        return True

    await resource_store.upscale_in_place(record, {"scale": 4}, upscale)
    if legacy_sr:
        async with resource_store._session() as session:
            legacy = await session.get(type(record), record.id)
            assert legacy is not None
            legacy.meta = {"sr": {"scale": 4}}
            session.add(legacy)
            await session.commit()
    out = await materialize_images(
        [poster], [cover], [], resource_store, cast("WebClient", client), HotSettings(), tmp_path
    )
    assert out.poster_urls == [poster]


@pytest.mark.asyncio
@pytest.mark.parametrize("legacy_sr", [False, True])
async def test_cached_upscaled_crop_does_not_replace_better_native_poster(
    resource_store: ResourceStore, tmp_path: Path, legacy_sr: bool
) -> None:
    cover, poster = "https://s/cover.jpg", "https://s/poster.jpg"
    client = FakeClient({cover: (600, 400), poster: (400, 600)})
    original = await materialize_images(
        [], [cover], [], resource_store, cast("WebClient", client), HotSettings(), tmp_path
    )
    cropped = await resource_store.get_by_url_hash(original.poster_urls[0].rsplit("/", 1)[-1])
    assert cropped is not None

    async def upscale(src: Path, output: Path) -> bool:
        Image.new("RGB", (1120, 1600)).save(output)
        return True

    await resource_store.upscale_in_place(cropped[0], {"scale": 4}, upscale)
    if legacy_sr:
        async with resource_store._session() as session:
            legacy = await session.get(type(cropped[0]), cropped[0].id)
            assert legacy is not None
            legacy.meta = {**legacy.meta, "sr": {"scale": 4}}
            session.add(legacy)
            await session.commit()
    out = await materialize_images(
        [poster], [cover], [], resource_store, cast("WebClient", client), HotSettings(), tmp_path
    )
    assert out.poster_urls == [poster]


@pytest.mark.asyncio
async def test_auto_crop_decision_uses_oriented_poster_dimensions(
    resource_store: ResourceStore, tmp_path: Path
) -> None:
    cover, poster = "https://s/cover.jpg", "https://s/poster.jpg"
    client = FakeClient({cover: (1200, 800), poster: (800, 560)}, orientations={poster: 6})
    out = await materialize_images(
        [poster], [cover], [], resource_store, cast("WebClient", client), HotSettings(), tmp_path
    )
    assert out.poster_urls == [poster]


@pytest.mark.asyncio
async def test_poster_only_quality_search_is_bounded(resource_store: ResourceStore, tmp_path: Path) -> None:
    urls = [f"https://s/{index}.jpg" for index in range(20)]
    client = FakeClient(dict.fromkeys(urls, (800, 538)))
    config = HotSettings()
    config.scraping.download_resources = [DownloadableResource.poster]
    out = await materialize_images([], urls, [], resource_store, cast("WebClient", client), config, tmp_path)
    assert out.poster_urls[0].startswith(pipeline_mod.RESOURCE_URL_PREFIX)
    assert len(client.downloaded) == 6
    assert out.thumb_urls == urls


@pytest.mark.asyncio
async def test_large_native_poster_does_not_trigger_unselected_cover_download(
    resource_store: ResourceStore, tmp_path: Path
) -> None:
    client = FakeClient({"https://s/poster.jpg": (1055, 1500), "https://s/cover.jpg": (2400, 1614)})
    config = HotSettings()
    config.scraping.download_resources = [DownloadableResource.poster]
    out = await materialize_images(
        ["https://s/poster.jpg"],
        ["https://s/cover.jpg"],
        [],
        resource_store,
        cast("WebClient", client),
        config,
        tmp_path,
    )
    assert out.poster_urls == ["https://s/poster.jpg"]
    assert client.downloaded == ["https://s/poster.jpg"]


@pytest.mark.asyncio
async def test_no_poster_source_crops_from_thumb(resource_store: ResourceStore, tmp_path: Path):
    """无 poster 候选 → 从 thumb 裁剪 (内部 URL)."""
    client = FakeClient({"https://s/t.jpg": (800, 538)})
    out = await materialize_images(
        [], ["https://s/t.jpg"], [], resource_store, cast("WebClient", client), HotSettings(), tmp_path
    )
    assert len(out.poster_urls) == 1
    assert out.poster_urls[0].startswith(pipeline_mod.RESOURCE_URL_PREFIX)


@pytest.mark.asyncio
async def test_trailer_downloaded_external_url(resource_store: ResourceStore, tmp_path: Path):
    client = FakeClient({"https://s/t.jpg": (800, 538)})
    settings = HotSettings()
    settings.scraping.download_resources.append(DownloadableResource.trailer)
    out = await materialize_images(
        [],
        ["https://s/t.jpg"],
        ["https://s/trailer.mp4"],
        resource_store,
        cast("WebClient", client),
        settings,
        tmp_path,
    )
    assert out.trailer_urls == ["https://s/trailer.mp4"]
    assert "https://s/trailer.mp4" in client.downloaded  # 已下载到本地


@pytest.mark.asyncio
async def test_empty_sources(resource_store: ResourceStore, tmp_path: Path):
    out = await materialize_images(
        [], [], [], resource_store, cast("WebClient", FakeClient({})), HotSettings(), tmp_path
    )
    assert out.poster_urls == []
    assert out.thumb_urls == []
    assert out.trailer_urls == []


@pytest.mark.asyncio
async def test_eager_sr_overwrites(resource_store: ResourceStore, tmp_path: Path, monkeypatch):
    """sr.enabled 时, 低质图被就地超分 (meta 打 sr 标记)."""
    from amane.sr.run import SrResult

    async def fake_run_sr(inp, out, cfg, data_dir, **kwargs):
        Image.new("RGB", (1600, 1076), "green").save(out)
        return SrResult(success=True, output=out)

    monkeypatch.setattr(pipeline_mod, "run_SR", fake_run_sr)

    client = FakeClient({"https://s/t.jpg": (800, 538)})  # max=800 < 1200 → 超分
    cfg = HotSettings()
    cfg.sr.enabled = True
    out = await materialize_images(
        [], ["https://s/t.jpg"], [], resource_store, cast("WebClient", client), cfg, tmp_path
    )

    thumb_res = await resource_store.get_by_url("https://s/t.jpg")
    assert thumb_res is not None
    assert thumb_res.meta is not None and "sr" in thumb_res.meta
    assert out.thumb_urls == ["https://s/t.jpg"]  # URL 不变 (就地覆盖)


class _GatedClient:
    """伪 WebClient: 下载开始时登记并停住, 由调用方放行 — 钉住「判缓存 → 落盘」的窗口.

    只有首次下载过闸: 后续下载直接落盘, 于是「谁在什么时候覆盖了同一路径」可被断言."""

    def __init__(self, sizes: dict[str, tuple[int, int]], gate: asyncio.Future[None]):
        self._sizes = sizes
        self._gate = gate
        self.downloaded: list[str] = []

    async def download(self, url: str, dest: Path, **kwargs: object) -> bool:
        first = not self.downloaded
        self.downloaded.append(url)
        if first:
            await self._gate  # 停在这里时缓存里还没有这条记录
        dest.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", self._sizes[url], "blue").save(dest)
        return True

    async def get_filesize(self, url: str, **kwargs: object) -> int | None:
        return None

    async def resolve_final_url(self, url: str, **kwargs: object) -> str:
        return url


@pytest.mark.asyncio
async def test_concurrent_materialize_keeps_sr_result(
    resource_store: ResourceStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """同一 URL 被两个影片任务并发物化: 后到的调用不得覆盖先落盘的超分结果.

    第一个任务的下载停在闸门上 (缓存里还没有这条记录), 第二个任务同时越过缓存判据. 放行后第一个
    任务走完下载 / 落库 / 超分, 第二个任务若仍去下载, 就把超分结果覆盖回原图, 其落库被 url UNIQUE
    拒绝 — 磁盘退回原图而记录仍记 sr 标记.
    """
    from amane.config import ScrapingConfig
    from amane.sr.run import SrResult

    calls = 0

    async def fake_run_sr(inp, out, cfg, data_dir, **kwargs):
        nonlocal calls
        calls += 1
        Image.new("RGB", (1600, 1076), "green").save(out)
        return SrResult(success=True, output=out)

    monkeypatch.setattr(pipeline_mod, "run_SR", fake_run_sr)

    gate = asyncio.get_running_loop().create_future()
    client = _GatedClient({"https://s/t.jpg": (800, 538)}, gate)
    cfg = HotSettings(
        sr=SrConfig(enabled=True, max_bytes_threshold=64 * 1024),
        scraping=ScrapingConfig(download_resources=[DownloadableResource.thumb], field_priority={}),
    )

    async def run() -> None:
        # 重复下载的落库被 url UNIQUE 拒绝: 文件已覆盖, 记录没写成 — 正是本用例要暴露的路径.
        with contextlib.suppress(sqlalchemy.exc.IntegrityError):
            await materialize_images(
                [], ["https://s/t.jpg"], [], resource_store, cast("WebClient", client), cfg, tmp_path
            )

    async def first_download_started() -> bool:
        return bool(client.downloaded)

    async def sr_done() -> bool:
        return calls == 1

    first = asyncio.create_task(run())
    second = asyncio.create_task(run())
    try:
        await await_for(first_download_started, timeout=LOAD_TOLERANT_TIMEOUT)
        gate.set_result(None)
        # 等下载方完成超分再收尾: 覆盖点若存在, 就落在「磁盘已是超分结果」之后.
        await await_for(sr_done, timeout=LOAD_TOLERANT_TIMEOUT)
        await asyncio.gather(first, second)
    finally:
        if not gate.done():
            gate.set_result(None)
        await asyncio.gather(first, second, return_exceptions=True)

    record = await resource_store.get_by_url("https://s/t.jpg")
    assert record is not None
    disk = resource_store.full_path(record)
    assert record.meta is not None and "sr" in record.meta
    assert resource_store._hash_file(disk) == record.content_hash
    assert Image.open(disk).size == (1600, 1076)  # 磁盘是超分结果, 不是后写的原图
    assert client.downloaded == ["https://s/t.jpg"]  # 记录写完即命中, 不重复下载
    assert calls == 1


@pytest.mark.asyncio
async def test_download_resources_gates_acquire(resource_store: ResourceStore, tmp_path: Path):
    """未选中的类型不写入 Resource."""
    from amane.config import DownloadableResource, ScrapingConfig

    client = FakeClient(
        {
            "https://s/t.jpg": (800, 538),
            "https://s/p.jpg": (400, 600),
            "https://s/trailer.mp4": (1, 1),
        }
    )
    cfg = HotSettings(scraping=ScrapingConfig(download_resources=[DownloadableResource.thumb], field_priority={}))
    await materialize_images(
        ["https://s/p.jpg"],
        ["https://s/t.jpg"],
        ["https://s/trailer.mp4"],
        resource_store,
        cast("WebClient", client),
        cfg,
        tmp_path,
        extrafanart_urls={"javdb": ["https://s/e1.jpg"]},
    )
    assert await resource_store.get_by_url("https://s/t.jpg") is not None
    assert await resource_store.get_by_url("https://s/p.jpg") is None
    assert await resource_store.get_by_url("https://s/trailer.mp4") is None
    assert "https://s/t.jpg" in client.downloaded
    assert "https://s/p.jpg" not in client.downloaded
    assert "https://s/trailer.mp4" not in client.downloaded


def test_success_first_partition():
    """稳定分区: 成功保序前置, 失败保序排到末尾; 空成功集幂等."""
    cases = [
        (["d1", "l1", "d2", "l2"], {"l1", "l2"}, ["l1", "l2", "d1", "d2"]),
        (["a", "b"], {"a", "b"}, ["a", "b"]),  # 全成功
        (["a", "b"], set(), ["a", "b"]),  # 全失败
        (["a", "b", "c"], {"c"}, ["c", "a", "b"]),  # 尾部成功提至首位
        (["a", "b", "c"], {"b"}, ["b", "a", "c"]),  # 中部成功前置
        ([], set(), []),
    ]
    for urls, ok, expected in cases:
        assert pipeline_mod._success_first(urls, ok) == expected


@pytest.mark.asyncio
async def test_poster_reordered_when_first_url_dead(resource_store: ResourceStore, tmp_path: Path):
    """poster 首位 URL 下载失败 → 成功者前置 (无 thumb 不触发裁剪)."""
    client = FakeClient(sizes={"https://s/live.jpg": (400, 600)}, fail={"https://s/dead.jpg"})
    out = await materialize_images(
        ["https://s/dead.jpg", "https://s/live.jpg"],
        [],
        [],
        resource_store,
        cast("WebClient", client),
        HotSettings(),
        tmp_path,
    )
    assert out.poster_urls == ["https://s/live.jpg", "https://s/dead.jpg"]
    assert "https://s/dead.jpg" in client.downloaded  # 死 URL 仍尝试下载


@pytest.mark.asyncio
async def test_thumb_trailer_reordered_when_first_dead(resource_store: ResourceStore, tmp_path: Path):
    """thumb/trailer 首位 URL 下载失败 → 成功者前置."""
    client = FakeClient(sizes={"https://s/live.jpg": (800, 538)}, fail={"https://s/dead.jpg", "https://s/dead.mp4"})
    settings = HotSettings()
    settings.scraping.download_resources.append(DownloadableResource.trailer)
    out = await materialize_images(
        [],
        ["https://s/dead.jpg", "https://s/live.jpg"],
        ["https://s/dead.mp4", "https://s/trailer.mp4"],
        resource_store,
        cast("WebClient", client),
        settings,
        tmp_path,
    )
    assert out.thumb_urls == ["https://s/live.jpg", "https://s/dead.jpg"]
    assert out.trailer_urls == ["https://s/trailer.mp4", "https://s/dead.mp4"]


@pytest.mark.asyncio
async def test_manual_crop_image_external_source(resource_store: ResourceStore, tmp_path: Path):
    client = FakeClient({"https://s/a.jpg": (800, 538)})

    url = await manual_crop_image(
        "https://s/a.jpg",
        (0, 0, 400, 538),
        resource_store,
        cast("WebClient", client),
        HotSettings(),
        tmp_path,
        subject="头像",
    )

    assert url.startswith(pipeline_mod.RESOURCE_URL_PREFIX)
    got = await resource_store.get_by_url_hash(url.rsplit("/", 1)[-1])
    assert got is not None
    record, path = got
    assert record.meta == {"op": "crop", "src": "https://s/a.jpg", "args": "box:0,0,400,538"}
    assert Image.open(path).size == (400, 538)


@pytest.mark.asyncio
async def test_manual_crop_image_rotation_records_args_and_dims(resource_store: ResourceStore, tmp_path: Path):
    """旋转后按互换的宽高校验并裁切; 派生键带角度, 与不转的结果分开."""
    client = FakeClient({"https://s/a.jpg": (800, 538)})
    common = (resource_store, cast("WebClient", client), HotSettings(), tmp_path)

    rotated = await manual_crop_image("https://s/a.jpg", (0, 0, 538, 538), *common, subject="封面图", rotation=90)
    got = await resource_store.get_by_url_hash(rotated.rsplit("/", 1)[-1])
    assert got is not None
    record, path = got
    assert record.meta == {"op": "crop", "src": "https://s/a.jpg", "args": "box:0,0,538,538:r90"}
    assert Image.open(path).size == (538, 538)

    plain = await manual_crop_image("https://s/a.jpg", (0, 0, 538, 538), *common, subject="封面图")
    assert plain != rotated
    again = await manual_crop_image("https://s/a.jpg", (0, 0, 538, 538), *common, subject="封面图", rotation=90)
    assert again == rotated  # 同角度同框命中缓存


@pytest.mark.asyncio
async def test_manual_crop_image_exif_source_uses_oriented_size(resource_store: ResourceStore, tmp_path: Path):
    """方向 6 的源图 (400x300) 按转正后 300x400 校验与裁切."""
    client = FakeClient({"https://s/o6.jpg": (400, 300)}, orientations={"https://s/o6.jpg": 6})
    args = (resource_store, cast("WebClient", client), HotSettings(), tmp_path)

    url = await manual_crop_image("https://s/o6.jpg", (0, 0, 300, 400), *args, subject="头像")
    got = await resource_store.get_by_url_hash(url.rsplit("/", 1)[-1])
    assert got is not None
    record, path = got
    assert record.meta == {"op": "crop", "src": "https://s/o6.jpg", "args": "box:0,0,300,400"}
    assert Image.open(path).size == (300, 400)

    with pytest.raises(ValueError, match="裁切区域无效"):
        await manual_crop_image("https://s/o6.jpg", (0, 0, 400, 300), *args, subject="头像")


@pytest.mark.asyncio
async def test_manual_crop_image_internal_source_records_locator(resource_store: ResourceStore, tmp_path: Path):
    async def producer(dest: Path) -> bool:
        Image.new("RGB", (400, 538), "blue").save(dest)
        return True

    seed = await resource_store.acquire_derived("https://s/orig.jpg", "crop", "box:0,0,400,538", producer)
    assert seed is not None
    internal = f"{pipeline_mod.RESOURCE_URL_PREFIX}/{ResourceStore.url_hash(seed.url)}"

    class _ExplodingClient:
        async def download(self, url: str, dest: Path, **kwargs: object) -> bool:
            raise AssertionError("内部源不应触网")

    url = await manual_crop_image(
        internal,
        (0, 0, 200, 269),
        resource_store,
        cast("WebClient", _ExplodingClient()),
        HotSettings(),
        tmp_path,
        subject="头像",
    )

    got = await resource_store.get_by_url_hash(url.rsplit("/", 1)[-1])
    assert got is not None
    record, path = got
    assert record.meta is not None
    assert record.meta["src"] == seed.url  # 底层 locator, 而非内部 URL
    assert Image.open(path).size == (200, 269)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("source_url", "box", "rotation", "message"),
    [
        ("https://s/missing.jpg", (0, 0, 10, 10), 0, "无法获取头像"),
        ("data:image/png;base64,AAAA", (0, 0, 10, 10), 0, "无法获取头像"),
        ("https://s/video.mp4", (0, 0, 10, 10), 0, "头像无法读取"),
        ("https://s/a.jpg", (10, 0, 5, 10), 0, "裁切区域无效"),
        ("https://s/a.jpg", (0, 0, 800, 538), 90, "裁切区域无效"),
    ],
)
async def test_manual_crop_image_invalid_inputs(
    resource_store: ResourceStore,
    tmp_path: Path,
    source_url: str,
    box: tuple[int, int, int, int],
    rotation: CropRotation,
    message: str,
):
    client = FakeClient({"https://s/a.jpg": (800, 538)}, fail={"https://s/missing.jpg"})
    with pytest.raises(ValueError, match=message):
        await manual_crop_image(
            source_url,
            box,
            resource_store,
            cast("WebClient", client),
            HotSettings(),
            tmp_path,
            subject="头像",
            rotation=rotation,
        )


@pytest.mark.asyncio
async def test_all_urls_fail_keeps_order(resource_store: ResourceStore, tmp_path: Path):
    """全部 URL 下载失败 → 列表保持原序 (死 URL 留尾供来源恢复后重试)."""
    client = FakeClient(sizes={}, fail={"https://s/a.jpg", "https://s/b.jpg"})
    out = await materialize_images(
        ["https://s/a.jpg", "https://s/b.jpg"],
        [],
        [],
        resource_store,
        cast("WebClient", client),
        HotSettings(),
        tmp_path,
    )
    assert out.poster_urls == ["https://s/a.jpg", "https://s/b.jpg"]


@pytest.mark.asyncio
async def test_unselected_kind_not_reordered(resource_store: ResourceStore, tmp_path: Path):
    """未选中下载的类型不重排 (无逐 URL 成败信息)."""
    from amane.config import DownloadableResource, ScrapingConfig

    cfg = HotSettings(scraping=ScrapingConfig(download_resources=[DownloadableResource.thumb], field_priority={}))
    client = FakeClient(sizes={"https://s/t.jpg": (800, 538)})
    out = await materialize_images(
        ["https://s/p1.jpg", "https://s/p2.jpg"],
        ["https://s/t.jpg"],
        [],
        resource_store,
        cast("WebClient", client),
        cfg,
        tmp_path,
    )
    assert out.poster_urls == ["https://s/p1.jpg", "https://s/p2.jpg"]


@pytest.mark.asyncio
async def test_cache_hit_counts_as_success(resource_store: ResourceStore, tmp_path: Path):
    """Resource 缓存命中计成功: 已缓存 URL 前置, 且不重复下载."""
    seed = FakeClient(sizes={"https://s/live.jpg": (800, 538)})
    assert await resource_store.acquire("https://s/live.jpg", cast("WebClient", seed)) is not None

    client = FakeClient(sizes={}, fail={"https://s/dead.jpg"})
    out = await materialize_images(
        ["https://s/dead.jpg", "https://s/live.jpg"],
        [],
        [],
        resource_store,
        cast("WebClient", client),
        HotSettings(),
        tmp_path,
    )
    assert out.poster_urls == ["https://s/live.jpg", "https://s/dead.jpg"]
    assert "https://s/live.jpg" not in client.downloaded  # 缓存命中, 无网络请求
    assert "https://s/dead.jpg" in client.downloaded
