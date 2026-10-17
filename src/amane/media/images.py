"""海报裁剪与封面角标. 判定函数只依赖尺寸与阈值, 无 I/O."""

import os
from collections.abc import Mapping
from pathlib import Path

import structlog
from PIL import Image

from ..enums import WatermarkCorner, WatermarkKind
from ..parsing import ContentType, FileInfo, Mosaic, shows_uncensored
from .watermarks import load_stamp

logger = structlog.get_logger()

# 标准 DVD 封面约 800x538, 海报为右侧约 379x538 (≈0.704); 默认取 0.7.
_DEFAULT_POSTER_RATIO = 0.7
_DEFAULT_JPEG_QUALITY = 95

# 与自动右侧比 `0.7000` 共存于 op=crop.
CROP_BOX_ARGS_PREFIX = "box:"


def _save_jpeg(img: Image.Image, path: Path, quality: int) -> None:
    """落盘为 JPEG, 格式由本函数决定而非路径后缀.

    带 alpha 的源图不能直接存 JPEG: Pillow 按 ``.jpg`` 后缀选中 JPEG 编码器后拒绝
    RGBA. 源图是下载来的, 调用方无法预判它是否为 RGBA, 故转码不交给调用方.
    """
    img.convert("RGB").save(path, format="JPEG", quality=quality)


def format_crop_box_args(left: int, top: int, right: int, bottom: int) -> str:
    return f"{CROP_BOX_ARGS_PREFIX}{left},{top},{right},{bottom}"


def validate_crop_box(box: tuple[int, int, int, int], image_size: tuple[int, int]) -> bool:
    left, top, right, bottom = box
    w, h = image_size
    if w <= 0 or h <= 0:
        return False
    return left >= 0 and top >= 0 and right <= w and bottom <= h and left < right and top < bottom


def probe_size(path: Path) -> tuple[int, int] | None:
    # 损坏 / 非图像返回 None, 不抛异常.
    try:
        with Image.open(path) as img:
            return img.size
    except Exception as e:
        logger.debug("probe_size failed", path=str(path), error=str(e))
        return None


def should_crop_poster(
    thumb_size: tuple[int, int] | None, candidate_size: tuple[int, int] | None, *, skip_ratio: float = 0.9
) -> bool:
    """无 thumb 尺寸则 False. 无 poster 候选则 True.

    候选已足够高 (b/h ≥ skip_ratio) 则不裁剪: 裁剪有错位风险.
    """
    if thumb_size is None:
        return False
    if candidate_size is None:
        return True
    h = thumb_size[1]
    b = candidate_size[1]
    if h <= 0 or b <= 0:
        return False
    return (b / h) < skip_ratio


def needs_upscale(
    size: tuple[int, int] | None,
    file_bytes: int,
    *,
    max_dim_threshold: int,
    max_bytes_threshold: int,
) -> bool:
    """最长边 < max_dim_threshold 且 文件大小 ≤ max_bytes_threshold. 无法读取尺寸时不超分."""
    if size is None:
        return False
    if file_bytes > max_bytes_threshold:
        return False
    return max(size) < max_dim_threshold


def resolve_crop_mode(modes: Mapping[object, object] | None, content_type: object | None) -> str:
    """从 ``scraping.poster_crop_mode`` 取该内容类型的裁剪方式; 缺省 ``right`` (与旧行为一致)."""
    if modes and content_type is not None:
        value = modes.get(content_type)
        if value is None:
            value = modes.get(str(content_type))
        if value is not None:
            return str(value)
    return "right"


# 人脸检测模型: 构建镜像时下载到 /app/models; 也可用 AMANE_FACE_MODEL 指向别处.
# 取不到模型时退回 OpenCV 自带的 Haar 级联 (不需要模型文件); 连 opencv 都没有则返回 None,
# 由调用方回退到右侧裁剪 —— 任何环节缺失都不影响其它裁剪方式.
_FACE_MODEL_ENV = "AMANE_FACE_MODEL"
_DEFAULT_FACE_MODEL = Path("/app/models/face_detection_yunet.onnx")


def _face_model_path() -> Path | None:
    override = os.environ.get(_FACE_MODEL_ENV)
    if override:
        candidate = Path(override)
        return candidate if candidate.is_file() else None
    return _DEFAULT_FACE_MODEL if _DEFAULT_FACE_MODEL.is_file() else None


def detect_face_box(image_path: Path) -> tuple[int, int, int, int] | None:
    """最大人脸的 (left, top, right, bottom); 缺 opencv / 没检出 / 出错一律 None."""
    try:
        import cv2
        import numpy as np
    except Exception:  # 镜像未装 opencv: 该功能自动退化
        return None
    try:
        with Image.open(image_path) as img:
            arr = np.asarray(img.convert("RGB"))[:, :, ::-1]  # PIL RGB -> OpenCV BGR
        height, width = int(arr.shape[0]), int(arr.shape[1])
        if width <= 0 or height <= 0:
            return None

        boxes: list[tuple[int, int, int, int]] = []

        model = _face_model_path()
        factory = getattr(cv2, "FaceDetectorYN_create", None) or getattr(
            getattr(cv2, "FaceDetectorYN", None), "create", None
        )
        if model is not None and factory is not None:
            try:
                detector = factory(str(model), "", (width, height), 0.6, 0.3, 5000)
                detector.setInputSize((width, height))
                _, faces = detector.detect(arr)
                for face in faces if faces is not None else []:
                    x, y, face_w, face_h = (int(v) for v in face[:4])
                    boxes.append((x, y, x + face_w, y + face_h))
            except Exception as e:
                logger.debug("yunet detect failed", path=str(image_path), error=str(e))

        if not boxes:
            cascade_dir = getattr(getattr(cv2, "data", None), "haarcascades", "") or ""
            cascade = cv2.CascadeClassifier(str(cascade_dir) + "haarcascade_frontalface_default.xml")
            if not cascade.empty():
                gray = cv2.cvtColor(arr, cv2.COLOR_BGR2GRAY)
                min_side = max(24, min(width, height) // 10)
                for x, y, face_w, face_h in cascade.detectMultiScale(gray, 1.1, 6, minSize=(min_side, min_side)):
                    boxes.append((int(x), int(y), int(x + face_w), int(y + face_h)))

        if not boxes:
            return None
        return max(boxes, key=lambda box: (box[2] - box[0]) * (box[3] - box[1]))
    except Exception as e:
        logger.debug("face detect failed", path=str(image_path), error=str(e))
        return None


def _face_centered_box(
    face: tuple[int, int, int, int], size: tuple[int, int], *, poster_ratio: float
) -> tuple[int, int, int, int]:
    """以人脸为中心、按 ``poster_ratio`` 给出尽量大的裁剪框, 且不越界."""
    width, height = size
    left, top, right, bottom = face
    face_h = max(1, bottom - top)
    center_x = (left + right) // 2
    center_y = (top + bottom) // 2

    target_h = min(height, max(int(face_h * 3.4), int(height * 0.5)))
    target_w = int(target_h * poster_ratio)
    if target_w > width:
        target_w = width
        target_h = min(height, int(target_w / poster_ratio))
    if target_w <= 0 or target_h <= 0:
        return (0, 0, width, height)

    crop_left = max(0, min(center_x - target_w // 2, width - target_w))
    # 人脸略高于画面中线: 人像构图头顶留白少, 下方留肩
    crop_top = max(0, min(center_y - int(target_h * 0.42), height - target_h))
    return (crop_left, crop_top, crop_left + target_w, crop_top + target_h)


def crop_poster(
    thumb_path: Path,
    poster_path: Path,
    *,
    mode: str = "right",
    poster_ratio: float = _DEFAULT_POSTER_RATIO,
    jpeg_quality: float = _DEFAULT_JPEG_QUALITY,
) -> bool:
    """按 ``mode`` 从封面生成海报.

    right 取封面右侧 (默认, 光碟封面海报在右); center 居中裁剪; face 人脸识别裁剪
    (检不到人脸时回退 right); none 不裁剪, 整图作为海报. 裁剪宽度 = height × ``poster_ratio``.
    """
    try:
        with Image.open(thumb_path) as img:
            width, height = img.size
            target_w = int(height * poster_ratio)

            box: tuple[int, int, int, int] | None = None
            if mode == "none":
                box = None
            elif mode == "center":
                if target_w < width:
                    left = (width - target_w) // 2
                    box = (left, 0, left + target_w, height)
            elif mode == "face":
                face = detect_face_box(thumb_path)
                if face is not None:
                    box = _face_centered_box(face, (width, height), poster_ratio=poster_ratio)
                elif target_w < width:
                    box = (width - target_w, 0, width, height)
            elif target_w < width:
                box = (width - target_w, 0, width, height)

            poster_path.parent.mkdir(parents=True, exist_ok=True)
            if box is None:
                # 不裁剪 / 已足够窄: 整图作为海报.
                _save_jpeg(img, poster_path, jpeg_quality)
                return True
            _save_jpeg(img.crop(box), poster_path, jpeg_quality)
            return True
    except Exception as e:
        logger.warning("poster crop failed", path=str(thumb_path), error=str(e))
        return False


def crop_box(
    src_path: Path,
    dest_path: Path,
    box: tuple[int, int, int, int],
    *,
    jpeg_quality: int = _DEFAULT_JPEG_QUALITY,
) -> bool:
    """框须落在图像范围内且面积为正 (见 ``validate_crop_box``)."""
    try:
        img = Image.open(src_path)
        if not validate_crop_box(box, img.size):
            logger.warning(
                "crop box invalid",
                path=str(src_path),
                box=box,
                size=img.size,
            )
            return False
        cropped = img.crop(box)
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        _save_jpeg(cropped, dest_path, jpeg_quality)
        return True
    except Exception as e:
        logger.warning("box crop failed", path=str(src_path), error=str(e))
        return False


# 按图高缩放, 海报与封面同高时一样大.
_DEFAULT_SCALE = 0.08
_SCALE_MIN = 0.03
_SCALE_MAX = 0.25
_STAMP_MIN_HEIGHT = 16

_FIXED_KINDS: frozenset[str] = frozenset(
    (WatermarkKind.SUBTITLE, WatermarkKind.UNCENSORED, WatermarkKind.CRACKED, WatermarkKind.LEAKED)
)
_RIGHT_CORNERS = frozenset((WatermarkCorner.TOP_RIGHT, WatermarkCorner.BOTTOM_RIGHT))
_BOTTOM_CORNERS = frozenset((WatermarkCorner.BOTTOM_LEFT, WatermarkCorner.BOTTOM_RIGHT))


def _stamp_stems(
    *,
    has_subtitle: bool,
    uncensored: bool,
    mosaic: Mosaic | None,
    definition: str | None,
) -> list[str]:
    """相位 → PNG 主干, 顺序: 中字 / 无码 / 破解 / 流出 / 分辨率."""
    stems: list[str] = []
    if has_subtitle:
        stems.append("subtitle")
    if uncensored:
        stems.append("uncensored")
    if mosaic is Mosaic.CRACKED:
        stems.append("cracked")
    elif mosaic is Mosaic.LEAKED:
        stems.append("leaked")
    if definition:
        stems.append(definition.casefold())
    return stems


def _stamp_kind(stem: str) -> WatermarkKind:
    if stem in _FIXED_KINDS:
        return WatermarkKind(stem)
    return WatermarkKind.DEFINITION


def _corner_for(stem: str, corners: Mapping[WatermarkKind, WatermarkCorner] | None) -> WatermarkCorner:
    kind = _stamp_kind(stem)
    if corners is None:
        return WatermarkCorner.TOP_LEFT
    return corners.get(kind, WatermarkCorner.TOP_LEFT)


def _fit_stamp(stamp: Image.Image, target_h: int) -> Image.Image | None:
    """裁掉透明边再按高度缩放. 无可见像素则跳过."""
    rgba = stamp.convert("RGBA")
    bbox = rgba.getbbox()
    if bbox is None:
        return None
    cropped = rgba.crop(bbox)
    width, height = cropped.size
    if height <= 0 or width <= 0:
        return None
    new_h = target_h
    new_w = max(1, round(width * new_h / height))
    return cropped.resize((new_w, new_h), Image.Resampling.LANCZOS)


def _paste_stamps(
    img: Image.Image,
    stamps_by_corner: dict[WatermarkCorner, list[Image.Image]],
    *,
    pad: int,
    gap: int,
) -> None:
    """同角按列表顺序向内叠: 上角往下, 下角往上; 右角右对齐."""
    width, height = img.size
    for corner, stamps in stamps_by_corner.items():
        if not stamps:
            continue
        right = corner in _RIGHT_CORNERS
        if corner in _BOTTOM_CORNERS:
            y = height - pad
            for stamp in stamps:
                stamp_w, stamp_h = stamp.size
                y -= stamp_h
                x = width - pad - stamp_w if right else pad
                img.paste(stamp, (x, y), stamp)
                y -= gap
        else:
            y = pad
            for stamp in stamps:
                stamp_w, stamp_h = stamp.size
                x = width - pad - stamp_w if right else pad
                img.paste(stamp, (x, y), stamp)
                y += stamp_h + gap


def apply_cover_watermarks(
    path: Path,
    *,
    has_subtitle: bool,
    uncensored: bool,
    mosaic: Mosaic | None,
    definition: str | None,
    jpeg_quality: int = _DEFAULT_JPEG_QUALITY,
    watermark_dir: Path | None = None,
    scale: float = _DEFAULT_SCALE,
    corners: Mapping[WatermarkKind, WatermarkCorner] | None = None,
) -> bool:
    """叠到库路径封面/海报. 无标记或无图则不动. 不修改 Resource 原图."""
    stems = _stamp_stems(has_subtitle=has_subtitle, uncensored=uncensored, mosaic=mosaic, definition=definition)
    if not stems:
        return False
    try:
        with Image.open(path) as src:
            img = src.convert("RGBA")
        width, height = img.size
        if width <= 0 or height <= 0:
            return False
        ratio = min(_SCALE_MAX, max(_SCALE_MIN, scale))
        target_h = max(_STAMP_MIN_HEIGHT, round(height * ratio))
        pad = max(4, target_h // 8)
        gap = max(4, target_h // 10)
        grouped: dict[WatermarkCorner, list[Image.Image]] = {corner: [] for corner in WatermarkCorner}
        for stem in stems:
            raw = load_stamp(stem, watermark_dir)
            if raw is None:
                continue
            fitted = _fit_stamp(raw, target_h)
            if fitted is None:
                continue
            grouped[_corner_for(stem, corners)].append(fitted)
        if not any(grouped.values()):
            return False
        _paste_stamps(img, grouped, pad=pad, gap=gap)
        _save_jpeg(img, path, jpeg_quality)
        return True
    except Exception as e:
        logger.warning("cover watermark failed", path=str(path), error=str(e))
        return False


def apply_cover_watermarks_from_info(
    path: Path,
    info: FileInfo,
    content_type: ContentType,
    *,
    jpeg_quality: int = _DEFAULT_JPEG_QUALITY,
    watermark_dir: Path | None = None,
    scale: float = _DEFAULT_SCALE,
    corners: Mapping[WatermarkKind, WatermarkCorner] | None = None,
) -> bool:
    """``content_type`` 是条目类型: 无码章与列表角标同用 ``parsing/file_info.py::shows_uncensored`` 的口径."""
    return apply_cover_watermarks(
        path,
        has_subtitle=info.has_subtitle,
        uncensored=shows_uncensored(info.mosaic, content_type),
        mosaic=info.mosaic,
        definition=info.definition,
        jpeg_quality=jpeg_quality,
        watermark_dir=watermark_dir,
        scale=scale,
        corners=corners,
    )
