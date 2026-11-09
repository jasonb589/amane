"""同轮分集只复用影片结果; 每个文件独立关联元数据与派生整理任务."""

from typing import TYPE_CHECKING

from ..db.models import MediaFileStatus, TaskStatus
from ..observability import current
from ..observability.models import SiteOutcomeKind, SiteOutcomeRecord
from .models import CacheKind, ScrapeResult
from .protocol import TaskResult

if TYPE_CHECKING:
    from ..db.models import MediaFile, Metadata, ScrapeGroup
    from ..db.repository import Repository
    from .models import ScrapePayload


def _record_reused_outcomes(result: ScrapeResult, producer_task_id: int | None) -> ScrapeResult:
    outcomes = [
        record.model_copy(update={"outcome": SiteOutcomeKind.CACHE_HIT})
        if record.outcome is SiteOutcomeKind.OK
        else record.model_copy(deep=True)
        for record in result.outcomes
    ]
    # 未启用记录器时仍使成功站点在任务结果中具有明确的缓存命中语义.
    recorded_sites = {record.site for record in outcomes}
    outcomes.extend(
        SiteOutcomeRecord(site=site, outcome=SiteOutcomeKind.CACHE_HIT)
        for site in dict.fromkeys(result.field_sources.values())
        if site not in recorded_sites
    )
    rec = current()
    rec.update_summary(eligible_sites=[record.site for record in outcomes], sites_queried=[r.site for r in outcomes])
    for record in outcomes:
        rec.record_site_outcome(
            site=record.site,
            outcome=record.outcome,
            reason=record.reason,
            http_status=record.http_status,
            detail=record.detail,
        )
    rec.info("same number scrape reused", source_task_id=producer_task_id, metadata_id=result.metadata_id)
    return result.model_copy(update={"outcomes": outcomes}, deep=True)


def existing_file_scrape_result(
    payload: ScrapePayload, file: MediaFile | None, metadata: Metadata | None
) -> ScrapeResult | None:
    """默认缓存的新文件可关联同号既有影片; 显式刷新、已关联文件与手动 URL 仍执行刮削."""
    if (
        file is None
        or file.metadata_id is not None
        or file.status is not MediaFileStatus.PENDING
        or payload.url is not None
        or payload.use_cache != {CacheKind.metadata, CacheKind.trans}
        or metadata is None
        or metadata.id is None
        or metadata.content_type is not payload.content_type
        or not metadata.raw
    ):
        return None
    result = ScrapeResult(
        metadata_id=metadata.id,
        field_sources=metadata.field_sources,
        failed_sites=[],
        outcomes=[SiteOutcomeRecord(site=site, outcome=SiteOutcomeKind.CACHE_HIT) for site in metadata.raw],
    )
    current().info("existing metadata reused for new file", metadata_id=metadata.id, media_file_id=file.id)
    return _record_reused_outcomes(result, None)


async def reused_scrape_result(
    repo: Repository, group: ScrapeGroup, payload: ScrapePayload
) -> TaskResult[ScrapeResult]:
    """不重新联网、不改写 Metadata; 旧结果失效时明确失败, 由用户重试开启新一轮."""
    result: ScrapeResult | None = None
    if group.result is not None:
        try:
            result = ScrapeResult.model_validate(group.result)
        except ValueError:
            return TaskResult(success=False, error="同番号刮削结果无法读取，请重新提交刮削")
    if group.status is TaskStatus.FAILED:
        return TaskResult(
            success=False,
            error=f"同番号刮削失败: {group.error or '来源未返回可用结果'}",
            result=_record_reused_outcomes(result, group.producer_task_id) if result is not None else None,
        )
    if result is None or result.metadata_id is None:
        return TaskResult(success=False, error="同番号刮削结果缺少影片信息，请重新提交刮削")
    metadata = await repo.get_metadata_by_number(payload.number)
    if metadata is None or metadata.id != result.metadata_id or metadata.content_type is not payload.content_type:
        return TaskResult(success=False, error="同番号刮削结果对应的影片已删除或变更，请重新提交刮削")
    return TaskResult(success=True, result=_record_reused_outcomes(result, group.producer_task_id))
