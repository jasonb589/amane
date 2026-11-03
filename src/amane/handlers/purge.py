"""PURGE handler - 一键重置: 清空片库 → 清理孤儿数据 → 清空任务历史.

三个阶段按顺序串行, 各自可关; 缺省全开, 提交一个「全部清空」任务即可完成面板上原本要点
十几下的三套操作 (选全片库 → 删除, 跑清理, 清已完成 + 清失败).
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import structlog

from ..db.models import TaskStatus
from ..library import prune_empty_dirs
from ..observability import remove_task_dir
from ..utils.path import is_descendant
from .file import CleanupHandler
from .models import CleanupPayload, CleanupResult, PurgePayload, PurgeResult
from .protocol import TaskHandler, TaskResult

if TYPE_CHECKING:
    from collections.abc import Sequence

    from ..db.models import MediaFile
    from ..db.repository import Repository
    from ..media import ResourceStore

logger = structlog.get_logger()

_PAGE = 500
"""逐页读取媒体文件, 避免一次性把整个索引读进内存."""

_CHUNK = 200
"""单次删除语句的 id 数; 太大会顶到 SQLite 的变量上限."""


class PurgeHandler(TaskHandler[PurgePayload, PurgeResult]):
    """删记录 / 删文件 / 清孤儿 / 清任务历史; 只改本机数据, 不碰远程站点."""

    def __init__(self, repo: Repository, resource_store: ResourceStore, log_dir: Path | None = None) -> None:
        super().__init__(payload_t=PurgePayload, result_t=PurgeResult)
        self._repo = repo
        self._resource_store = resource_store
        self._log_dir = log_dir
        # 复用 CLEANUP 的回收逻辑, 保证「清理」这一步与单独的清理任务逐行一致.
        self._cleanup = CleanupHandler(repo=repo, resource_store=resource_store)

    async def handle(self, payload: PurgePayload) -> TaskResult[PurgeResult]:
        metadata_removed = 0
        media_removed = 0
        files_removed = 0
        files_failed = 0
        pruned_dirs = 0

        if payload.purge_media:
            media = await self._list_media()
            await self.report_progress(0, len(media), f"待删除影片 {len(media)} 条")
            if payload.delete_files:
                files_removed, files_failed, pruned_dirs = await self._delete_files(media, payload.prune_empty_dirs)
            media_removed = await self._delete_media_rows([mf.id for mf in media if mf.id is not None])
            metadata_removed = await self._delete_metadata()
            await self.report_progress(len(media), len(media), "片库已清空")

        cleanup = CleanupResult()
        if payload.run_cleanup:
            outcome = await self._cleanup.handle(
                CleanupPayload(
                    remove_missing_files=True,
                    remove_unreferenced_resources=payload.remove_unreferenced_resources,
                    remove_orphan_actors=payload.remove_orphan_actors,
                    remove_empty_facets=payload.remove_empty_facets,
                    remove_empty_resource_dirs=payload.remove_empty_resource_dirs,
                )
            )
            cleanup = outcome.result if isinstance(outcome.result, CleanupResult) else CleanupResult()

        tasks_removed = await self._purge_tasks() if payload.purge_task_history else 0

        result = PurgeResult(
            metadata_removed=metadata_removed,
            media_removed=media_removed,
            files_removed=files_removed,
            files_failed=files_failed,
            pruned_dirs=pruned_dirs,
            resources_removed=cleanup.resources_removed,
            actors_removed=cleanup.actors_removed,
            facets_removed=cleanup.facets_removed,
            resource_dirs_removed=cleanup.dirs_removed,
            tasks_removed=tasks_removed,
        )
        logger.info("purge completed", **result.model_dump(mode="json", exclude={"type"}))
        # 清任务历史时连自己这条已完成记录也删掉: 用户点一次就什么都不剩.
        return TaskResult(True, result=result, drop_when_done=payload.purge_task_history)

    # --- 阶段一: 片库 ---

    async def _list_media(self) -> list[MediaFile]:
        rows: list[MediaFile] = []
        offset = 0
        while True:
            batch = await self._repo.list_media_files(limit=_PAGE, offset=offset)
            rows.extend(batch)
            offset += len(batch)
            if len(batch) < _PAGE:
                return rows

    async def _delete_files(self, media: Sequence[MediaFile], prune: bool) -> tuple[int, int, int]:
        """只删媒体库目录内的文件; 库外路径 (云盘挂载异常 / 手工登记的记录) 一律不动.

        返回 (已删除, 失败, 腾空目录数).
        """
        roots = [Path(lib.path) for lib in await self._repo.list_libraries()]
        prune_buckets: dict[Path, list[Path]] = {}
        removed = 0
        failed = 0
        for mf in media:
            path = Path(mf.path)
            root = _owning_root(path, roots)
            if root is None:
                continue
            try:
                path.unlink(missing_ok=True)
            except OSError:
                logger.warning("purge unlink failed", path=str(path), exc_info=True)
                failed += 1
                continue
            removed += 1
            if prune:
                prune_buckets.setdefault(root, []).append(path.parent)

        pruned = 0
        for root, dirs in prune_buckets.items():
            pruned += (await prune_empty_dirs(dirs, library_root=root)).removed
        return removed, failed, pruned

    async def _delete_media_rows(self, ids: Sequence[int]) -> int:
        removed = 0
        for start in range(0, len(ids), _CHUNK):
            deleted, _missing = await self._repo.batch_delete_media_files(ids[start : start + _CHUNK])
            removed += deleted
        return removed

    async def _delete_metadata(self) -> int:
        ids = await self._repo.list_metadata_ids()
        removed = 0
        for start in range(0, len(ids), _CHUNK):
            deleted, _missing = await self._repo.batch_delete_metadata(ids[start : start + _CHUNK])
            removed += deleted
        return removed

    # --- 阶段三: 任务历史 ---

    async def _purge_tasks(self) -> int:
        """删已完成 / 失败的任务; 运行中的本任务与排队任务不在删除范围内."""
        found = await self._repo.find_tasks(statuses=[TaskStatus.DONE, TaskStatus.FAILED])
        ids = [task.id for task in found if task.id is not None]
        if not ids:
            return 0
        deleted = await self._repo.delete_tasks(ids)
        if deleted and self._log_dir is not None:
            # 有外部后裔的行会被跳过, 只清真正删掉的日志目录.
            leftover = {task.id for task in await self._repo.find_tasks(task_ids=ids)}
            for task_id in ids:
                if task_id not in leftover:
                    remove_task_dir(self._log_dir, task_id)
        return deleted


def _owning_root(path: Path, roots: Sequence[Path]) -> Path | None:
    """返回包含该文件的媒体库根; 多重包含时取最深的一层 (库中有库)."""
    candidates = [root for root in roots if is_descendant(str(path), str(root))]
    if not candidates:
        return None
    return max(candidates, key=lambda root: len(root.parts))
