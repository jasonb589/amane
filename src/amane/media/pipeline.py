"""刮削期按 ``download_resources`` 下载 / 裁剪 / 就地超分, 返回应写入 metadata 的 URL.

与整理到媒体库路径无关. 任一步失败不抛, 仅降级.
"""

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import structlog

from ..config import DownloadableResource
from ..parsing import ContentType
from ..sr import get_preset_meta, run_SR
from .images import (
    CropRotation,
    crop_box,
    crop_poster,
    format_crop_box_args,
    needs_upscale,
    probe_oriented_size,
    probe_size,
    resolve_crop_mode,
    rotated_size,
    should_crop_poster,
    validate_crop_box,
)
from .resource_store import RESOURCE_URL_PREFIX, internal_url_hash

if TYPE_CHECKING:
    from ..config import HotSettings, SrConfig
    from ..db.models import Resource
    from ..net.http import WebClient
    from .resource_store import ResourceStore

logger = structlog.get_logger()


@dataclass
class MaterializedImages:
    # fanart 不是 metadata 字段 (整理时按 JAV 约定取 thumb), 不在此返回.

    poster_urls: list[str]
    thumb_urls: list[str]
    trailer_urls: list[str]


def _internal_url(store: ResourceStore, locator_url: str) -> str:
    return f"{RESOURCE_URL_PREFIX}/{store.url_hash(locator_url)}"


#: 海报候选最长边小于它就算「明显偏小」: 即使比例判定够高, 也比一比 thumb 裁出来的那张.
_SMALL_POSTER_EDGE = 800
# 未选择封面下载时, 裁切选源只额外探测有限数量, 避免将海报任务扩展成全部封面下载.
_CROP_THUMB_LIMIT = 6


def _success_first(urls: list[str], succeeded: set[str]) -> list[str]:
    # 成功者保序前置, 失败者保序置于尾部. succeeded 为空时幂等返回原序.
    return [u for u in urls if u in succeeded] + [u for u in urls if u not in succeeded]


def _best_first(urls: list[str], best: str | None) -> list[str]:
    """把像素面积最大的那张挪到最前: 来源顺序只用来打破平局.

    有的来源按配置排在前面却只能给兜底小图 (如 amazon ``/images/P/<ASIN>.jpg`` 352x500),
    排在后面的大图源不该因此被丢掉.
    """
    if not best or best not in urls:
        return urls
    return [best, *(u for u in urls if u != best)]


async def _native_size(store: ResourceStore, url: str, size: tuple[int, int]) -> tuple[int, int] | None:
    """超分缓存按记录的输入像素排序; 历史标记缺少输入尺寸时不冒充原生高清."""
    if internal := internal_url_hash(url):
        found = await store.get_by_url_hash(internal)
        record = found[0] if found else None
    else:
        record = await store.get_by_url(url)
    sr = (record.meta or {}).get("sr") if record else None
    if isinstance(sr, dict):
        original = sr.get("source_size")
        if (
            isinstance(original, list)
            and len(original) == 2
            and all(type(edge) is int and edge > 0 for edge in original)
        ):
            return original[0], original[1]
        return None
    return size


def sr_args_dict(cfg: SrConfig) -> dict:
    pm = get_preset_meta(cfg.preset)
    return {"preset": cfg.preset, "tool": pm.tool, "model": pm.model, "scale": pm.scale}


async def _maybe_upscale(
    store: ResourceStore,
    resource: Resource,
    config: HotSettings,
    data_dir: Path,
) -> None:
    if not config.sr.enabled:
        return
    full = store.full_path(resource)
    size = probe_size(full)
    file_bytes = full.stat().st_size if full.exists() else 0
    if not needs_upscale(
        size,
        file_bytes,
        max_dim_threshold=config.sr.max_dim_threshold,
        max_bytes_threshold=config.sr.max_bytes_threshold,
    ):
        return

    async def producer(src: Path, out: Path) -> bool:
        result = await run_SR(src, out, config.sr, data_dir)
        return result.success

    await store.upscale_in_place(resource, sr_args_dict(config.sr), producer)


async def materialize_images(
    poster_urls: list[str],
    thumb_urls: list[str],
    trailer_urls: list[str],
    store: ResourceStore,
    client: WebClient,
    config: HotSettings,
    data_dir: Path,
    content_type: ContentType | None = None,
    *,
    extrafanart_urls: dict[str, list[str]] | None = None,
) -> MaterializedImages:
    """仅下载集合内的类型; 未选中的保留聚合 URL.

    死 URL 保序置于尾部, 供来源恢复后重试.
    extrafanart 仅下载到 Resource, 站点分组结构不重排.
    """
    kinds = set(config.scraping.download_resources)

    # 下载 thumb.
    thumb_ok: set[str] = set()
    thumb_local: Path | None = None
    thumb_src: str | None = None
    thumb_size: tuple[int, int] | None = None
    thumb_native_size: tuple[int, int] | None = None
    thumb_area = -1
    if DownloadableResource.thumb in kinds:
        for url in dict.fromkeys(thumb_urls):
            local = await store.acquire(url, client)
            size = probe_oriented_size(local) if local else None
            if local and size:
                thumb_ok.add(url)
                native = await _native_size(store, url, size)
                area = native[0] * native[1] if native else 0
                # 同一组候选已全部下载, 按真实像素选源, 不固定使用首个成功的小图.
                if area > thumb_area:
                    thumb_local, thumb_src = local, url
                    thumb_size = size
                    thumb_native_size = native
                    thumb_area = area

    # 下载 poster; 候选偏矮则从 thumb 裁剪.
    poster_ok: set[str] = set()
    poster_candidate_local: Path | None = None
    poster_candidate_url: str | None = None
    poster_native_size: tuple[int, int] | None = None
    poster_candidate_area = -1
    result_poster_urls = list(poster_urls)
    if DownloadableResource.poster in kinds:
        for url in dict.fromkeys(poster_urls):
            local = await store.acquire(url, client)
            size = probe_oriented_size(local) if local else None
            if local and size:
                poster_ok.add(url)
                native = await _native_size(store, url, size)
                area = native[0] * native[1] if native else 0
                # 选像素面积最大的一张; 来源顺序只用于同尺寸时的先后.
                if area > poster_candidate_area:
                    poster_candidate_local, poster_candidate_url = local, url
                    poster_native_size = native
                    poster_candidate_area = area

        # 未选择封面下载时, 仅为缺失或低清海报尝试有限数量的裁切源.
        mode = resolve_crop_mode(config.scraping.poster_crop_mode, content_type)
        if (
            thumb_local is None
            and thumb_urls
            and config.scraping.crop_poster
            and mode != "none"
            and (poster_native_size is None or max(poster_native_size) < _SMALL_POSTER_EDGE)
        ):
            for url in list(dict.fromkeys(thumb_urls))[:_CROP_THUMB_LIMIT]:
                local = await store.acquire(url, client)
                size = probe_oriented_size(local) if local else None
                if local and size:
                    native = await _native_size(store, url, size)
                    area = native[0] * native[1] if native else 0
                    if area > thumb_area:
                        thumb_local, thumb_src = local, url
                        thumb_size = size
                        thumb_native_size = native
                        thumb_area = area

        cand_size = poster_native_size
        # 候选明显偏小 (最长边 < 800, 例如日亚只剩 352x500 兜底图) 时, 即使比例判定「够高不裁」
        # 也把 thumb 裁一份来比面积: 裁出来的更大就用裁的, 更小仍保留原候选.
        small_candidate = cand_size is not None and max(cand_size) < _SMALL_POSTER_EDGE
        cropped = False

        if (
            config.scraping.crop_poster
            and mode != "none"
            and thumb_local
            and (
                small_candidate
                or poster_candidate_local is None
                or should_crop_poster(thumb_native_size, cand_size, skip_ratio=config.scraping.poster_crop_skip_ratio)
            )
        ):
            ratio = config.scraping.poster_ratio
            # ``right`` 是旧版唯一行为: args 保持纯比例, 既有派生资源不失效; 其它模式才加前缀.
            args = f"{ratio:.4f}" if mode == "right" else f"{mode}:{ratio:.4f}"
            local_thumb = thumb_local

            async def crop_producer(dest: Path) -> bool:
                return crop_poster(
                    local_thumb,
                    dest,
                    mode=mode,
                    poster_ratio=ratio,
                    jpeg_quality=config.scraping.jpeg_quality,
                )

            crop_res = await store.acquire_derived(thumb_src or "", "crop", args, crop_producer)
            if crop_res:
                crop_size = probe_size(store.full_path(crop_res))
                crop_native_size = await _native_size(store, crop_res.url, crop_size) if crop_size else None
                crop_area = (
                    crop_native_size[0] * crop_native_size[1] * thumb_area / (thumb_size[0] * thumb_size[1])
                    if crop_native_size and thumb_size
                    else 0
                )
                # 由「候选偏小」触发的裁切要与原候选比面积: 裁出来更小就没必要换.
                if not small_candidate or (
                    crop_size is not None and cand_size is not None and crop_area > cand_size[0] * cand_size[1]
                ):
                    result_poster_urls = [_internal_url(store, crop_res.url)]
                    cropped = True
                    await _maybe_upscale(store, crop_res, config, data_dir)
                else:
                    result_poster_urls = _best_first(_success_first(poster_urls, poster_ok), poster_candidate_url)
            else:
                result_poster_urls = _best_first(_success_first(poster_urls, poster_ok), poster_candidate_url)
        else:
            result_poster_urls = _best_first(_success_first(poster_urls, poster_ok), poster_candidate_url)
        if not cropped and poster_candidate_url:
            res = await store.get_by_url(poster_candidate_url)
            if res:
                await _maybe_upscale(store, res, config, data_dir)

        # 海报最终来自哪张、多大: 只靠结果页看不出来, 记一条便于定位是哪一环掉了.
        logger.info(
            "poster chosen",
            chosen=result_poster_urls[0] if result_poster_urls else None,
            size=list(cand_size) if cand_size else None,
            thumb_size=list(thumb_size) if thumb_size else None,
            candidates=len(poster_urls),
            cropped=cropped,
        )

    # 超分不能参与原图质量排序, 否则首个低清来源放大后会压过真实高清来源.
    if DownloadableResource.thumb in kinds and thumb_src:
        res = await store.get_by_url(thumb_src)
        if res:
            await _maybe_upscale(store, res, config, data_dir)

    # 下载预告片.
    trailer_ok: set[str] = set()
    if DownloadableResource.trailer in kinds:
        for url in trailer_urls:
            local = await store.acquire(url, client)
            if local:
                trailer_ok.add(url)

    # 下载剧照: 按站点优先级, 有结果即停.
    if DownloadableResource.extrafanart in kinds and extrafanart_urls:
        priority = list(extrafanart_urls.keys())
        await store.acquire_extrafanart(extrafanart_urls, priority, client)

    out = MaterializedImages(
        poster_urls=result_poster_urls,
        thumb_urls=_best_first(_success_first(thumb_urls, thumb_ok), thumb_src)
        if DownloadableResource.thumb in kinds
        else list(thumb_urls),
        trailer_urls=_success_first(trailer_urls, trailer_ok),
    )
    logger.debug(
        "images materialized",
        kinds=sorted(kinds),
        poster=out.poster_urls[:1],
        thumb=out.thumb_urls[:1],
        trailer=out.trailer_urls[:1],
    )
    return out


async def manual_crop_image(
    source_url: str,
    box: tuple[int, int, int, int],
    store: ResourceStore,
    client: WebClient,
    config: HotSettings,
    data_dir: Path,
    *,
    subject: str,
    rotation: CropRotation = 0,
) -> str:
    """手动裁切源图, 返回内部 URL. 源可为外部 http(s) 或 ``/api/resources/{hash}``.

    先按 EXIF 转正、再顺时针旋转 ``rotation``, 之后按 ``box`` 裁切; 失败抛 ``ValueError``
    (消息可直接作 API detail); ``subject`` 组装提示文案 (如「封面图」「头像」).
    """
    source = await store.resolve_source(source_url, client)
    if source is None:
        raise ValueError(f"无法获取{subject}")

    size = probe_oriented_size(source.path)
    if size is None:
        raise ValueError(f"{subject}无法读取")
    if not validate_crop_box(box, rotated_size(size, rotation)):
        raise ValueError("裁切区域无效")

    args = format_crop_box_args(*box, rotation=rotation)
    jpeg_quality = config.scraping.jpeg_quality

    async def producer(dest: Path) -> bool:
        return crop_box(source.path, dest, box, rotation=rotation, jpeg_quality=jpeg_quality)

    crop_res = await store.acquire_derived(source.locator, "crop", args, producer)
    if crop_res is None:
        raise ValueError("裁切失败")

    await _maybe_upscale(store, crop_res, config, data_dir)
    return _internal_url(store, crop_res.url)
