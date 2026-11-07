"""DMM 图片 CDN 的域名与路径规则, 由 DMM 详情页与 r18 dump 两个来源共用.

pics 的 mono 地址在 cid 前多一层分类目录, aws 省去该层; aws 副本的像素可能高于标准地址,
但并非每部作品都有高清副本. digital 路径可以双向换域名, mono 缺失的分类目录不能反向推断.
"""

from urllib.parse import urlsplit, urlunsplit

PICS_CDN = "https://pics.dmm.co.jp"
HIGH_RES_CDN = "https://awsimgsrc.dmm.co.jp/pics_dig"

_THUMB_SUFFIX = "pl.jpg"
_POSTER_SUFFIX = "ps.jpg"
# aws 有副本的路径前缀; mono 单独处理, 因为它需要改写路径.
_DIGITAL_HIGH_RES_PREFIXES = ("digital/video/", "digital/amateur/")
_MONO_MOVIE_PREFIX = "mono/movie/"


def _normal_url(url: str) -> str:
    return f"https:{url}" if url.startswith("//") else url


def _strip_transform(url: str) -> str:
    """去掉图床的缩放查询串; 非 DMM 图床的地址原样返回.

    高清图床按查询串返回缩放后的缩略图, 标准图床忽略查询串, 两处一律去掉.
    """
    url = _normal_url(url)
    try:
        parts = urlsplit(url)
    except ValueError:
        return url
    if (
        parts.scheme not in ("http", "https")
        or parts.netloc not in ("pics.dmm.co.jp", "awsimgsrc.dmm.co.jp")
        or (parts.netloc == "awsimgsrc.dmm.co.jp" and not parts.path.startswith("/pics_dig/"))
    ):
        return url
    return urlunsplit(("https", parts.netloc, parts.path, "", ""))


def _canonical_path(rel_path: str) -> str:
    """省去 mono 路径在 cid 前多出的分类目录."""
    segments = rel_path[len(_MONO_MOVIE_PREFIX) :].split("/")
    return f"{_MONO_MOVIE_PREFIX}{'/'.join(segments[-2:])}" if len(segments) > 2 else rel_path


def high_res_url(pics_url: str) -> str | None:
    """pics 绝对地址 → aws 绝对地址; 非 pics 域名或该分类无 aws 副本时返回 None."""
    pics_url = _strip_transform(pics_url)
    if not pics_url.startswith(f"{PICS_CDN}/"):
        return None
    rel_path = pics_url[len(PICS_CDN) + 1 :]
    if rel_path.startswith(_MONO_MOVIE_PREFIX):
        return f"{HIGH_RES_CDN}/{_canonical_path(rel_path)}"
    if rel_path.startswith(_DIGITAL_HIGH_RES_PREFIXES):
        return f"{HIGH_RES_CDN}/{rel_path}"
    return None


def image_candidates(image_url: str) -> list[str]:
    """图片地址 → 高清候选在前、标准地址兜底的候选列表.

    digital 路径在两个 CDN 上相同, 已是 aws 地址也补充 pics 副本. mono 地址缺失分类目录,
    不反向推导. 来源给出的缩放地址保留为最后候选, 下载后由管线比较实际像素.
    """
    if not image_url:
        return []
    url = _strip_transform(image_url)
    urls = [url]
    if url.startswith(f"{HIGH_RES_CDN}/"):
        rel_path = url[len(HIGH_RES_CDN) + 1 :]
        if rel_path.startswith(_DIGITAL_HIGH_RES_PREFIXES):
            urls.append(f"{PICS_CDN}/{rel_path}")
    elif high_res := high_res_url(url):
        urls.insert(0, high_res)
    urls.append(_normal_url(image_url))
    return list(dict.fromkeys(urls))


def thumb_candidates(image_url: str) -> list[str]:
    """已知 DMM 海报缩略图补充 pl 横版封面; 原始图片仍保留为兜底."""
    url = _strip_transform(image_url)
    if url.startswith((f"{PICS_CDN}/", f"{HIGH_RES_CDN}/")) and url.endswith(_POSTER_SUFFIX):
        cover = f"{url.removesuffix(_POSTER_SUFFIX)}{_THUMB_SUFFIX}"
        return list(dict.fromkeys([*image_candidates(cover), *image_candidates(image_url)]))
    return image_candidates(image_url)


def poster_candidates(cover_url: str) -> list[str]:
    """封面地址 (pl 形态) → 竖版海报候选; 不是 pl 形态时返回空."""
    url = _strip_transform(cover_url)
    if not url.startswith((f"{PICS_CDN}/", f"{HIGH_RES_CDN}/")) or not url.endswith(_THUMB_SUFFIX):
        return []
    return image_candidates(f"{url.removesuffix(_THUMB_SUFFIX)}{_POSTER_SUFFIX}")
