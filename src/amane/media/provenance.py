"""按最终选中的图片追溯来源与处理过程, 不依据字段来源优先级推断."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import TYPE_CHECKING, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel

from .images import probe_oriented_size
from .resource_store import internal_url_hash

if TYPE_CHECKING:
    from ..db.models import Resource
    from .resource_store import ResourceStore


class ImageProvenance(BaseModel):
    source: str = ""
    source_url: str = ""
    kind: Literal["poster", "thumb"] = "poster"
    cropped: bool = False
    selected_still: bool = False
    upscaled: bool = False
    width: int | None = None
    height: int | None = None
    source_width: int | None = None
    source_height: int | None = None
    """未超分的根资源尺寸; 根资源已被就地超分或不存在时留空."""

    @property
    def label(self) -> str:
        if not self.source_url:
            return "来源未知"
        source = {
            "1pondo": "一本道",
            "amazon": "Amazon",
            "tenhow": "Tenhow",
            "dmm": "DMM",
            "javarchive": "JavArchive",
            "fc2ppv_db": "FC2PPV-DB",
            "avwikidb": "AVWikiDB",
        }.get(self.source, self.source)
        source = source or "来源未知"
        source_size = (
            (self.source_width, self.source_height)
            if self.source_width is not None and self.source_height is not None
            else None
        )
        # 高清描述只使用仍保留原始像素的根资源, 不能把 CDN 域名或超分后的尺寸当作证据.
        quality = "高清" if self.source == "dmm" and source_size and max(source_size) >= 1000 else ""
        if self.selected_still:
            description = f"{quality}剧照裁剪" if self.cropped else f"{quality}剧照"
        elif self.cropped:
            shape = "横图" if source_size and source_size[0] > source_size[1] else "海报" if source_size else "图片"
            description = f"{quality}{shape}裁剪"
        elif self.kind == "thumb" or (source_size and source_size[0] > source_size[1]):
            description = f"{quality}横图"
        else:
            description = f"{quality}原生海报"
        parts = [f"{source} {description}"]
        if self.upscaled:
            parts.append("已超分")
        if self.cropped and source_size:
            parts.append(f"源图 {source_size[0]}×{source_size[1]}")
        if self.width is not None and self.height is not None:
            parts.append(f"{self.width}×{self.height}")
        return " · ".join(parts)


def _http_host(url: str) -> str | None:
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    if parts.scheme not in {"http", "https"} or parts.username or parts.password:
        return None
    return parts.hostname


def _source(url: str, raw: Mapping[str, object] | None) -> str:
    host = _http_host(url)
    if not host:
        return ""
    if host in {"pics.dmm.co.jp", "awsimgsrc.dmm.co.jp"}:
        return "dmm"
    if host == "tenhow.net" or host.endswith(".tenhow.net"):
        return "tenhow"
    if host == "1pondo.tv" or host.endswith(".1pondo.tv"):
        return "1pondo"
    if host in {
        "m.media-amazon.com",
        "images-na.ssl-images-amazon.com",
        "images-eu.ssl-images-amazon.com",
        "images-fe.ssl-images-amazon.com",
        "ecx.images-amazon.com",
    }:
        return "amazon"
    for site, entry in (raw or {}).items():
        if not isinstance(entry, dict):
            continue
        for field in ("poster_urls", "thumb_urls", "extrafanart"):
            urls = entry.get(field)
            if isinstance(urls, list) and url in urls:
                return site.partition(":")[0]
    return host


async def resolve_image_provenance(
    url: str,
    store: ResourceStore | None,
    raw: Mapping[str, object] | None = None,
    *,
    kind: Literal["poster", "thumb"] = "poster",
    local_pixels: bool = True,
) -> ImageProvenance:
    """只读取已选图片及其资源链, 不下载图片、不回退到其它候选.

    裁剪链可跨多次手动操作; 清理已删除的源缓存仍允许按根 URL 识别站点,
    但不能再推断原图尺寸. 历史异常链按重复 locator 与深度上限终止.
    发送外部 URL 时设 local_pixels=False: 本地就地超分不改变远端图片, 不显示其效果与尺寸.
    内部 URL 始终采用本地文件及处理链.
    """
    result = ImageProvenance(kind=kind)
    current = url.strip()
    use_local_pixels = local_pixels or _http_host(current) is None
    visited: set[str] = set()
    for depth in range(16):
        if not current or current in visited:
            break
        visited.add(current)
        record: Resource | None = None
        if store is not None:
            if internal := internal_url_hash(current):
                resolved = await store.get_by_url_hash(internal)
                record = resolved[0] if resolved is not None else None
            else:
                record = await store.get_by_url(current)
        locator = record.url if record is not None else current
        root = _http_host(locator) is not None
        meta = record.meta or {} if record is not None else {}
        is_upscaled = isinstance(meta.get("sr"), dict)
        result.upscaled |= use_local_pixels and is_upscaled
        if record is not None and store is not None:
            if (depth == 0 and (use_local_pixels or not is_upscaled)) or (root and not is_upscaled):
                size = await asyncio.to_thread(probe_oriented_size, store.full_path(record))
                if size is not None:
                    if depth == 0:
                        result.width, result.height = size
                    if root and not is_upscaled:
                        result.source_width, result.source_height = size
            result.cropped |= meta.get("op") == "crop"
            if meta.get("op") == "select-poster":
                result.selected_still = True
                source_name = meta.get("args")
                if not result.source and isinstance(source_name, str):
                    result.source = source_name.partition(":")[0]
        if root:
            result.source_url = locator
            result.source = result.source or _source(locator, raw)
            break
        if record is None:
            break
        parent = meta.get("src")
        if not isinstance(parent, str):
            break
        current = parent
    return result
