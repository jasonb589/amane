import shutil
import tempfile
from collections.abc import Mapping, Sequence
from contextlib import suppress
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Literal, cast

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query
from PIL import Image
from pydantic import BaseModel, TypeAdapter
from sqlalchemy.exc import SQLAlchemyError

from ...aggregate import compute_merge_updates
from ...db.embeddings import EmbeddingKind
from ...db.models import Library, Metadata, MetadataSortField, SavedQueryEntity, SortOrder, TaskType
from ...db.repo_types import WriteMode
from ...db.repos.media import file_phase_of
from ...enums import ActorGender, MetadataField
from ...handlers import ScrapePayload
from ...media import manual_crop_image
from ...media.file_type import detect_media_type, is_image_mime
from ...media.nfo import read_nfo_number
from ...media.nfo_sync import sync_metadata_nfos
from ...media.provenance import resolve_image_provenance
from ...media.resource_store import RESOURCE_URL_PREFIX, internal_url_hash
from ...net.errors import RequestError
from ...organize import resolve_existing_poster_path
from ...organize.path_templates import POSTER_TEMPLATE_DEFAULT
from ...organize.template import PathEngine, TemplateContext
from ...parsing import FilePhaseSummary as ParsedFilePhase
from ...parsing import parse_file_info, summarize_file_phases
from ...semantic import SemanticUnavailable
from ...utils.collections import unique_ids
from ...utils.dates import normalize_calendar_date
from ...utils.model import to_resp
from ...utils.path import is_descendant, path_key
from ...utils.threads import in_thread
from ..deps import NetworkDep, RepoDep, RuntimeDep
from ..models import (
    CommentResponse,
    CropPosterRequest,
    FilePhaseSummary,
    MergeRequest,
    MetadataBatchDeleteResponse,
    MetadataBatchIdsRequest,
    MetadataBatchScrapeRequest,
    MetadataBatchScrapeResponse,
    MetadataDetailResponse,
    MetadataListItem,
    MetadataListResponse,
    MetadataLocksRequest,
    MetadataMergeEntriesRequest,
    MetadataResponse,
    MetadataUserTagsRequest,
    MetadataUserTagsResponse,
    PartialMetadata,
    PosterSyncResult,
    SelectPosterRequest,
    SemanticHitInfo,
    UserTagResponse,
    to_media_file_response,
)
from ..support.metadata_filters import MetadataFilters, parse_metadata_filters, require_known_definition
from .saved_queries import resolve_saved_query_id_subquery

if TYPE_CHECKING:
    from ...db.repo_types import MetadataFields

logger = structlog.get_logger()

router = APIRouter(prefix="/metadata", tags=["metadata"])


async def build_metadata_responses(repo: RepoDep, items: Sequence[Metadata]) -> list[MetadataListItem]:
    """列表与语义检索共用: 补 file_count / file_phase 聚合; 列表项不含 raw / field_sources."""
    summaries = await repo.summarize_media_by_metadata(items)
    responses: list[MetadataListItem] = []
    for m in items:
        summary = summaries.get(m.id) if m.id is not None else None
        response = to_resp(MetadataListItem, m)
        if summary is not None:
            response = response.model_copy(
                update={
                    "file_count": summary.file_count,
                    "file_phase": _file_phase_resp(summary.phase),
                }
            )
        responses.append(response)
    return responses


def _file_phase_resp(summary: ParsedFilePhase) -> FilePhaseSummary:
    return FilePhaseSummary(
        has_subtitle=summary.has_subtitle,
        censored=summary.censored,
        uncensored=summary.uncensored,
        mosaics=list(summary.mosaics),
        definition=summary.definition,
    )


@router.get("/schema")
async def get_metadata_schema() -> dict:
    return TypeAdapter(PartialMetadata).json_schema()


@router.get("")
async def list_metadata(
    repo: RepoDep,
    runtime: RuntimeDep,
    filters: Annotated[MetadataFilters, Depends(parse_metadata_filters)],
    search: Annotated[str | None, Query()] = None,
    offset: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=200)] = 20,
    sort_by: Annotated[MetadataSortField, Query(description="Sort field")] = MetadataSortField.UPDATED_AT,
    order: Annotated[SortOrder, Query(description="Sort order")] = SortOrder.DESC,
    semantic_query: Annotated[
        str | None, Query(min_length=1, max_length=200, description="按画面描述排序; 提供时忽略 sort_by / order")
    ] = None,
    semantic_kind: Annotated[Literal["all", "cover", "still"], Query(description="参与语义排序的图片来源")] = "all",
) -> MetadataListResponse:
    definition = require_known_definition(filters.definition)
    id_subquery_sql = None
    if filters.saved_query_id is not None:
        id_subquery_sql = await resolve_saved_query_id_subquery(repo, filters.saved_query_id, SavedQueryEntity.METADATA)
    if semantic_query is not None:
        return await _semantic_list(
            runtime=runtime,
            repo=repo,
            filters=filters,
            search=search,
            semantic_query=semantic_query,
            semantic_kind=semantic_kind,
            definition=definition,
            id_subquery_sql=id_subquery_sql,
            offset=offset,
            limit=limit,
        )
    items, total = await repo.list_metadata(
        keyword=search,
        offset=offset,
        limit=limit,
        sort_by=sort_by,
        order=order,
        actor_ids=filters.actor_id,
        director_ids=filters.director_id,
        tag_ids=filters.tag_id,
        studio_ids=filters.studio_id,
        publisher_ids=filters.publisher_id,
        series_ids=filters.series_id,
        user_tag_ids=filters.user_tag_id,
        library_ids=filters.library_id,
        has_files=filters.has_files,
        has_subtitle=filters.has_subtitle,
        vr=filters.vr,
        mosaic=filters.mosaic,
        uncensored=filters.uncensored,
        definition=definition,
        content_type=filters.content_type,
        id_subquery_sql=id_subquery_sql,
    )
    return MetadataListResponse(items=await build_metadata_responses(repo, items), total=total)


async def _semantic_list(
    *,
    runtime: RuntimeDep,
    repo: RepoDep,
    filters: MetadataFilters,
    search: str | None,
    semantic_query: str,
    semantic_kind: Literal["all", "cover", "still"],
    definition: str | None,
    id_subquery_sql: str | None,
    offset: int,
    limit: int,
) -> MetadataListResponse:
    """语义参数出现时接管排序: 候选集来自同一套筛选, 返回结构与普通列表一致."""
    service = runtime.semantic
    if service is None or not service.enabled:
        raise HTTPException(status_code=503, detail="语义检索未启用")
    allowed_ids = None
    if filters.active or search:
        allowed_ids = await repo.list_metadata_ids(
            keyword=search,
            actor_ids=filters.actor_id,
            director_ids=filters.director_id,
            tag_ids=filters.tag_id,
            studio_ids=filters.studio_id,
            publisher_ids=filters.publisher_id,
            series_ids=filters.series_id,
            user_tag_ids=filters.user_tag_id,
            library_ids=filters.library_id,
            has_files=filters.has_files,
            has_subtitle=filters.has_subtitle,
            vr=filters.vr,
            mosaic=filters.mosaic,
            uncensored=filters.uncensored,
            definition=definition,
            content_type=filters.content_type,
            id_subquery_sql=id_subquery_sql,
        )
    kind = None if semantic_kind == "all" else EmbeddingKind(semantic_kind)
    try:
        hits = await service.search(semantic_query, limit, kind, allowed_ids, offset)
        total = await service.count(kind, allowed_ids)
    except SemanticUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    if not hits:
        return MetadataListResponse(items=[], total=total)
    ids = [hit.metadata_id for hit in hits]
    items, _ = await repo.list_metadata(ids=ids, limit=len(ids))
    by_id = {item.id: item for item in items}
    ordered = [by_id[hit.metadata_id] for hit in hits if hit.metadata_id in by_id]
    responses = {response.id: response for response in await build_metadata_responses(repo, ordered)}
    return MetadataListResponse(
        items=[responses[hit.metadata_id] for hit in hits if hit.metadata_id in responses],
        total=total,
        semantic_hits=[
            SemanticHitInfo(
                id=hit.metadata_id,
                kind=hit.kind,
                resource_url=hit.resource_url,
                similarity=hit.score,
            )
            for hit in hits
            if hit.metadata_id in responses
        ],
    )


@router.post("/batch/delete")
async def batch_delete_metadata(req: MetadataBatchIdsRequest, repo: RepoDep) -> MetadataBatchDeleteResponse:
    """级联行为与单条删除一致; 重复 id 去重后只处理一次."""
    ids = unique_ids(req.ids)
    deleted, missing = await repo.batch_delete_metadata(ids)
    skipped = len(req.ids) - len(ids)
    logger.info("metadata batch deleted", deleted=deleted, missing=missing, skipped=skipped)
    return MetadataBatchDeleteResponse(deleted=deleted, missing=missing, skipped=skipped)


@router.post("/batch/scrape", status_code=202)
async def batch_scrape_metadata(req: MetadataBatchScrapeRequest, repo: RepoDep) -> MetadataBatchScrapeResponse:
    """以各自 number 重新刮削; 重复 id 去重后只提交一次."""
    ids = unique_ids(req.ids)
    metadata_by_id = {meta.id: meta for meta in await repo.get_metadata_by_ids(ids) if meta.id is not None}
    missing = 0
    payloads: list[ScrapePayload] = []
    for metadata_id in ids:
        metadata = metadata_by_id.get(metadata_id)
        if metadata is None:
            missing += 1
            continue
        payloads.append(
            ScrapePayload(
                number=metadata.number,
                content_type=req.content_type if req.content_type is not None else metadata.content_type,
                use_cache=req.use_cache,
            )
        )
    # 批量提交动辄数百条, 逐条提交会重复付出事务开销
    tasks = await repo.create_tasks(TaskType.SCRAPE, payloads)
    task_ids = [task.id for task in tasks if task.id is not None]
    skipped = len(req.ids) - len(ids)
    logger.info("metadata batch scrape submitted", submitted=len(task_ids), missing=missing, skipped=skipped)
    return MetadataBatchScrapeResponse(submitted=len(task_ids), missing=missing, skipped=skipped, task_ids=task_ids)


@router.post("/batch/user-tags")
async def batch_metadata_user_tags(req: MetadataUserTagsRequest, repo: RepoDep) -> MetadataUserTagsResponse:
    """把一组用户标签应用到一组影片; 未知标签 id 返回 404, 不存在的影片 id 计入 missing."""
    ids = unique_ids(req.ids)
    try:
        result = await repo.apply_metadata_user_tags(ids, req.user_tag_ids, action=req.action)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    skipped = len(req.ids) - len(ids)
    logger.info(
        "metadata user tags applied",
        action=req.action,
        changed=result.changed,
        unchanged=result.unchanged,
        missing=result.missing,
        skipped=skipped,
    )
    return MetadataUserTagsResponse(
        changed=result.changed, unchanged=result.unchanged, missing=result.missing, skipped=skipped
    )


@router.post("/merge-entries")
async def merge_metadata_entries(req: MetadataMergeEntriesRequest, repo: RepoDep) -> MetadataResponse:
    """把来源条目的 raw 快照补入目标并删除来源: 目标字段不变, 关联记录迁入目标.

    ``rescrape`` 要求合并提交后再按目标番号入队 SCRAPE: 只有 raw 已落库, 复用快照的聚合
    才能按当前优先级重建字段与站点级字典.
    """
    try:
        metadata = await repo.merge_metadata_entries(req.target_id, req.source_ids)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    if metadata is None:
        raise HTTPException(status_code=404, detail="影片不存在")
    rescrape_task_id: int | None = None
    if req.rescrape:
        task = await repo.create_task(
            TaskType.SCRAPE,
            ScrapePayload(number=metadata.number, content_type=metadata.content_type),
        )
        rescrape_task_id = task.id
    logger.info(
        "metadata entries merged",
        target_id=req.target_id,
        source_ids=req.source_ids,
        rescrape_task_id=rescrape_task_id,
    )
    return to_resp(MetadataResponse, metadata)


@router.get("/{metadata_id}")
async def get_metadata(metadata_id: int, repo: RepoDep) -> MetadataDetailResponse:
    metadata = await repo.get_metadata(metadata_id)
    if metadata is None:
        raise HTTPException(status_code=404, detail="影片不存在")
    # 关联文件 / 用户 tag / 评论 / 分类 id
    files = await repo.get_media_by_metadata_id(metadata_id)
    user_tags = await repo.list_metadata_user_tags(metadata_id)
    comments = await repo.list_comments(metadata_id)
    (
        actor_ids,
        actor_genders,
        director_ids,
        tag_ids,
        studio_id,
        publisher_id,
        series_id,
    ) = await repo.resolve_metadata_facet_ids(metadata)
    return MetadataDetailResponse(
        metadata=to_resp(MetadataResponse, metadata).model_copy(
            update={
                "file_count": len(files),
                "file_phase": _file_phase_resp(
                    summarize_file_phases((file_phase_of(f) for f in files), metadata.content_type)
                ),
            }
        ),
        files=[to_media_file_response(f, metadata.content_type) for f in files],
        user_tags=[to_resp(UserTagResponse, t) for t in user_tags],
        comments=[to_resp(CommentResponse, c) for c in comments],
        actor_ids=actor_ids,
        actor_genders=actor_genders,
        director_ids=director_ids,
        tag_ids=tag_ids,
        studio_id=studio_id,
        publisher_id=publisher_id,
        series_id=series_id,
    )


@router.patch("/{metadata_id}")
async def update_metadata(
    metadata_id: int, req: PartialMetadata, repo: RepoDep, runtime: RuntimeDep
) -> MetadataResponse:
    # exclude_unset 区分省略键与显式 null; 三态见 docs/dev/data-model.md.
    updates = cast("MetadataFields", req.model_dump(exclude_unset=True))
    if not updates:
        raise HTTPException(status_code=422, detail="没有需要修改的字段")
    if "release" in updates and updates["release"] is not None:
        raw_release = updates["release"]
        if isinstance(raw_release, str):
            normalized = normalize_calendar_date(raw_release)
            if normalized is None:
                raise HTTPException(status_code=422, detail="发行日期必须为 YYYY-MM-DD")
            updates["release"] = normalized
        else:
            raise HTTPException(status_code=422, detail="发行日期必须为 YYYY-MM-DD")
    previous = await repo.get_metadata(metadata_id)
    if previous is None:
        raise HTTPException(status_code=404, detail="影片不存在")
    metadata = await repo.update_metadata(metadata_id, mode=WriteMode.MANUAL, **updates)
    if metadata is None:
        raise HTTPException(status_code=404, detail="影片不存在")
    logger.info("metadata updated", metadata_id=metadata_id, fields=list(updates.keys()))
    synced = await sync_metadata_nfos(repo, metadata, previous, runtime.safe_dirs)
    return to_resp(MetadataResponse, metadata).model_copy(update={"nfo_sync": synced})


@router.put("/{metadata_id}/locks")
async def set_metadata_locks(metadata_id: int, req: MetadataLocksRequest, repo: RepoDep) -> MetadataResponse:
    """整体替换锁定字段集合."""
    metadata = await repo.set_metadata_locks(metadata_id, req.fields)
    if metadata is None:
        raise HTTPException(status_code=404, detail="影片不存在")
    logger.info("metadata locks updated", metadata_id=metadata_id, fields=[str(field) for field in req.fields])
    return to_resp(MetadataResponse, metadata)


@router.delete("/{metadata_id}", status_code=204)
async def delete_metadata(metadata_id: int, repo: RepoDep) -> None:
    deleted = await repo.delete_metadata(metadata_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="影片不存在")
    logger.info("metadata deleted", metadata_id=metadata_id)


class _PosterTarget(BaseModel):
    video: Path
    library: Library
    has_external_subtitle: bool = False
    remembered: bool = False
    nfo: Path | None = None


async def _write_poster_into_libraries(repo: RepoDep, runtime: RuntimeDep, metadata_id: int) -> PosterSyncResult:
    """手动海报写回以当前元数据为准, 已整理文件优先沿用记存的实际视频位置."""
    try:
        async with repo.nfo_sync_lock:
            current = await repo.get_metadata(metadata_id)
            if current is None or current.poster_url is None:
                return PosterSyncResult(unlocated=True)
            hash_ = internal_url_hash(current.poster_url)
            found = await runtime.resource_store.get_by_url_hash(hash_) if hash_ is not None else None
            if found is None:
                return PosterSyncResult(failed=1)
            linked = await repo.get_media_by_metadata_id(metadata_id)
            if not linked and current.number:
                same = await repo.list_media_files(search=current.number, limit=None)
                linked = [f for f in same if f.number == current.number and f.metadata_id in {None, metadata_id}]
            outputs = await repo.list_nfo_outputs(metadata_id)
            library_ids = {f.library_id for f in linked} | {output.library_id for output in outputs}
            libraries = {library_id: await repo.get_library(library_id) for library_id in library_ids}
            targets = {
                path_key(media.path): _PosterTarget(
                    video=Path(media.path), library=library, has_external_subtitle=media.has_external_subtitle
                )
                for media in linked
                if (library := libraries.get(media.library_id)) is not None
            }
            for output in outputs:
                library = libraries.get(output.library_id)
                if library is not None:
                    key = path_key(output.video_path)
                    previous = targets.get(key)
                    targets[key] = _PosterTarget(
                        video=Path(output.video_path),
                        library=library,
                        has_external_subtitle=previous.has_external_subtitle if previous is not None else False,
                        remembered=True,
                        nfo=Path(output.nfo_path),
                    )
            if not targets:
                return PosterSyncResult(unlocated=True)
            genders = {actor.name: actor.gender for actor in await repo.get_actors_by_names(current.actors)}
            return await _copy_cropped_posters(found[1], current, list(targets.values()), runtime.safe_dirs, genders)
    except OSError, ValueError, SQLAlchemyError:
        logger.exception("poster write-back failed", metadata_id=metadata_id)
        return PosterSyncResult(failed=1)


@in_thread
def _copy_cropped_posters(
    local: Path,
    metadata: Metadata,
    targets: Sequence[_PosterTarget],
    safe_dirs: Sequence[Path] | None,
    actor_genders: Mapping[str, ActorGender],
) -> PosterSyncResult:
    written: set[str] = set()
    result = PosterSyncResult()
    for target in targets:
        source, library = target.video, target.library
        temporary: Path | None = None
        try:
            if not source.is_file():
                result.skipped += 1
                continue
            if target.nfo is not None:
                if target.nfo.is_symlink() or not target.nfo.is_file():
                    raise ValueError("已整理媒体的 NFO 身份无法核验")
                previous_number = read_nfo_number(target.nfo)
                if not previous_number or previous_number.casefold() != metadata.number.casefold():
                    raise ValueError("已整理媒体的现存 NFO 番号不匹配")
            file_info = parse_file_info(str(source), has_external_subtitle=target.has_external_subtitle)
            if target.remembered and not is_descendant(source, library.path):
                if library.link_template or (
                    safe_dirs is not None and not any(is_descendant(source, root) for root in safe_dirs)
                ):
                    raise ValueError("已整理媒体的海报位置无法安全还原")
                template = PathEngine(library.poster_template or POSTER_TEMPLATE_DEFAULT)
                if any(template.uses(name) for name in ("raw_dir", "dir", "raw_name", "raw_relpath", "raw_srt_name")):
                    raise ValueError("海报模板依赖已不可用的原始路径")
                context = TemplateContext.from_metadata(
                    metadata, ext=source.suffix.lstrip("."), file_info=file_info, actor_genders=actor_genders
                )
                context.apply_video(source, Path(library.path))
                context.apply_link(None)
                poster = template.resolve(context, Path(library.path), safe_dirs)
                if not poster.parent.is_dir():
                    result.skipped += 1
                    continue
            else:
                poster = resolve_existing_poster_path(
                    library, metadata, source, file_info=file_info, safe_dirs=safe_dirs, actor_genders=actor_genders
                )
            key = path_key(poster)
            if key in written:
                result.skipped += 1
                continue
            if poster.is_symlink():
                raise ValueError("海报输出位置不允许符号链接")
            if poster.suffix.lower() not in {
                ".jpg",
                ".jpeg",
                ".png",
                ".webp",
                ".gif",
                ".avif",
                ".bmp",
                ".tif",
                ".tiff",
            }:
                raise ValueError("海报模板必须使用图片扩展名")
            if poster.exists() and not is_image_mime(detect_media_type(poster)):
                raise ValueError("海报输出位置已有非图片文件")
            if poster.exists() and poster.samefile(source):
                raise ValueError("海报模板不能指向媒体文件")
            poster.parent.mkdir(parents=True, exist_ok=True)
            # 临时文件与目标同目录, 写入失败不会截断现存海报.
            with local.open("rb") as original, tempfile.NamedTemporaryFile(dir=poster.parent, delete=False) as output:
                temporary = Path(output.name)
                shutil.copyfileobj(original, output)
            temporary.replace(poster)
            written.add(key)
            result.written += 1
        except (OSError, ValueError) as exc:
            result.failed += 1
            logger.warning("poster write-back failed", path=str(source), error=str(exc))
        finally:
            if temporary is not None:
                with suppress(OSError):
                    temporary.unlink(missing_ok=True)
    return result


async def _save_manual_poster(
    repo: RepoDep, runtime: RuntimeDep, previous: Metadata, poster_url: str, *, source: str | None = None
) -> MetadataResponse:
    assert previous.id is not None
    provenance = await resolve_image_provenance(poster_url, runtime.resource_store, previous.raw)
    sources = {**previous.field_sources, MetadataField.POSTER_URLS: source or provenance.source or "manual"}
    updated = await repo.update_metadata(
        previous.id, mode=WriteMode.MANUAL, poster_urls=[poster_url], field_sources=sources
    )
    if updated is None:
        raise HTTPException(status_code=404, detail="影片不存在")
    poster_sync = await _write_poster_into_libraries(repo, runtime, previous.id)
    nfo_sync = await sync_metadata_nfos(repo, updated, previous, runtime.safe_dirs)
    return to_resp(MetadataResponse, updated).model_copy(update={"poster_sync": poster_sync, "nfo_sync": nfo_sync})


def _still_source(metadata: Metadata, url: str) -> str | None:
    return next((site for site, urls in metadata.extrafanart_urls.items() if url in urls), None)


@in_thread
def _verify_selected_image(path: Path) -> None:
    try:
        with Image.open(path) as image:
            image.verify()
        with Image.open(path) as image:
            image.load()
    except Exception as exc:
        raise ValueError("选定剧照不是可读取的图片") from exc


@in_thread
def _copy_selected_image(source: Path, destination: Path) -> bool:
    shutil.copyfile(source, destination)
    return True


@router.post("/{metadata_id}/select-poster")
async def select_poster_from_extrafanart(
    metadata_id: int, req: SelectPosterRequest, repo: RepoDep, runtime: RuntimeDep, network: NetworkDep
) -> MetadataResponse:
    """将本影片已有剧照原图设为海报, 保留来源并锁定海报字段; 不自动裁切."""
    metadata = await repo.get_metadata(metadata_id)
    if metadata is None:
        raise HTTPException(status_code=404, detail="影片不存在")
    site = _still_source(metadata, req.url)
    if site is None:
        raise HTTPException(status_code=400, detail="选图必须是当前影片已有的剧照")
    try:
        source = await runtime.resource_store.resolve_source(req.url, network.web_client)
        if source is None:
            raise ValueError("无法获取选定剧照")
        await _verify_selected_image(source.path)
        record = await runtime.resource_store.get_by_url(source.locator)
        extension = Path(record.file_path).suffix if record is not None else source.path.suffix

        async def producer(destination: Path) -> bool:
            return await _copy_selected_image(source.path, destination)

        selected = await runtime.resource_store.acquire_derived(
            source.locator, "select-poster", site, producer, ext=extension or ".bin"
        )
        if selected is None:
            raise ValueError("无法保存选定剧照")
    except (OSError, ValueError, RequestError) as exc:
        raise HTTPException(status_code=400, detail="选定剧照无法获取或不是有效图片") from exc
    current = await repo.get_metadata(metadata_id)
    if current is None:
        raise HTTPException(status_code=404, detail="影片不存在")
    if _still_source(current, req.url) != site:
        raise HTTPException(status_code=409, detail="影片剧照已发生变化，请刷新后重新选择")
    poster_url = f"{RESOURCE_URL_PREFIX}/{runtime.resource_store.url_hash(selected.url)}"
    response = await _save_manual_poster(repo, runtime, current, poster_url, source=site)
    logger.info("poster selected from still", metadata_id=metadata_id, poster_url=poster_url, source=site)
    return response


@router.post("/{metadata_id}/crop-poster")
async def crop_poster_from_thumb(
    metadata_id: int,
    req: CropPosterRequest,
    repo: RepoDep,
    runtime: RuntimeDep,
    network: NetworkDep,
) -> MetadataResponse:
    """从封面 (thumb) 按像素框裁切海报, 写入派生 Resource 并更新 poster_urls.

    先按 EXIF 转正、再顺时针旋转 ``req.rotation``, 框相对旋转后的图像.
    裁切结果按媒体库海报模板写回现存媒体文件对应的位置.
    """
    metadata = await repo.get_metadata(metadata_id)
    if metadata is None:
        raise HTTPException(status_code=404, detail="影片不存在")
    if req.source_url is not None and req.source_url not in [*metadata.poster_urls, *metadata.thumb_urls]:
        raise HTTPException(status_code=400, detail="裁切源必须是当前影片的海报或封面候选")
    if req.source_url is None and not metadata.thumb_urls:
        raise HTTPException(status_code=400, detail="无封面图可裁切")

    # 按像素框裁切封面, 写入派生 Resource
    thumb_url = req.source_url if req.source_url is not None else metadata.thumb_urls[0]
    box = (req.left, req.top, req.right, req.bottom)
    try:
        poster_url = await manual_crop_image(
            thumb_url,
            box,
            runtime.resource_store,
            network.web_client,
            network.hot,
            runtime.config.cold.data_dir,
            subject="封面图",
            rotation=req.rotation,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e

    current = await repo.get_metadata(metadata_id)
    if current is None:
        raise HTTPException(status_code=404, detail="影片不存在")
    if thumb_url not in [*current.poster_urls, *current.thumb_urls]:
        raise HTTPException(status_code=409, detail="影片图片已发生变化，请刷新后重新裁切")
    response = await _save_manual_poster(repo, runtime, current, poster_url)
    logger.info(
        "poster cropped",
        metadata_id=metadata_id,
        box=box,
        rotation=req.rotation,
        poster_url=poster_url,
        written_dirs=response.poster_sync.written if response.poster_sync is not None else 0,
    )
    return response


@router.post("/{metadata_id}/merge")
async def merge_metadata(metadata_id: int, req: MergeRequest, repo: RepoDep, runtime: RuntimeDep) -> MetadataResponse:
    if not req.selections:
        raise HTTPException(status_code=400, detail="未提供选择项")

    metadata = await repo.get_metadata(metadata_id)
    if metadata is None:
        raise HTTPException(status_code=404, detail="影片不存在")

    # 按 selections 从 raw 合并字段
    try:
        updates = compute_merge_updates(metadata.raw, metadata.field_sources, req.selections)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e

    if not updates:
        raise HTTPException(status_code=400, detail="没有有效的选择项")

    # 写回元数据
    updated = await repo.update_metadata(metadata_id, mode=WriteMode.MANUAL, **cast("MetadataFields", updates))
    assert updated is not None
    logger.info("metadata merged", metadata_id=metadata_id, selections=req.selections)
    synced = await sync_metadata_nfos(repo, updated, metadata, runtime.safe_dirs)
    return to_resp(MetadataResponse, updated).model_copy(update={"nfo_sync": synced})
