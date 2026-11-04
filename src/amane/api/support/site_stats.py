"""刮削来源成功率汇总: 直接读最近若干条已完成刮削任务的逐站点结果, 不落额外的统计表.

任务结果里的 ``outcomes`` 就是每个来源一次尝试的结论 (ok / failed / cache_hit) 与失败原因,
所以「成功率」用样本内的比例表达, 不需要另存时间序列.
"""

from __future__ import annotations

from collections import Counter
from typing import TYPE_CHECKING

from ...db.models import TaskStatus, TaskType
from ..models.network import SiteStatItem, SiteStatReason, SiteStatsResponse

if TYPE_CHECKING:
    from datetime import datetime

    from ...db.repository import Repository

#: 没记录原因时单独归一类, 免得和某个真实原因混在一起.
_UNKNOWN_REASON = ""


async def collect_site_stats(repo: Repository, *, limit: int) -> SiteStatsResponse:
    """最近 ``limit`` 条已完成刮削任务 → 逐来源的 ok / failed / cache_hit 与失败原因分布."""
    tasks = await repo.list_tasks(statuses=[TaskStatus.DONE], task_types=[TaskType.SCRAPE], limit=limit)
    counts: dict[str, Counter[str]] = {}
    reasons: dict[str, Counter[str]] = {}
    used = 0
    since: datetime | None = None

    for task in tasks:
        result = task.result
        if not isinstance(result, dict):
            continue
        outcomes = result.get("outcomes")
        if not isinstance(outcomes, list) or not outcomes:
            continue
        used += 1
        if task.finished_at is not None and (since is None or task.finished_at < since):
            since = task.finished_at
        for item in outcomes:
            if not isinstance(item, dict):
                continue
            site = str(item.get("site") or "")
            if not site:
                continue
            kind = str(item.get("outcome") or "")
            bucket = counts.setdefault(site, Counter())
            # 未知取值并入 failed: 有结果却不是成功, 就要能被看见.
            if kind == "ok":
                bucket["ok"] += 1
            elif kind == "cache_hit":
                bucket["cache_hit"] += 1
            else:
                bucket["failed"] += 1
                reasons.setdefault(site, Counter())[str(item.get("reason") or _UNKNOWN_REASON)] += 1

    items: list[SiteStatItem] = []
    for site, bucket in counts.items():
        site_reasons = reasons.get(site) or Counter()
        items.append(
            SiteStatItem(
                site=site,
                ok=bucket["ok"],
                failed=bucket["failed"],
                cache_hit=bucket["cache_hit"],
                reasons=[
                    SiteStatReason(reason=reason or None, count=count) for reason, count in site_reasons.most_common()
                ],
            )
        )
    # 失败多的排前面: 出问题的来源要第一眼看到, 同分按站点名稳定排序.
    items.sort(key=lambda item: (-item.failed, item.site))
    return SiteStatsResponse(tasks=used, since=since, items=items)
