"""全局测试 fixtures"""

import os
from contextlib import contextmanager
from typing import TYPE_CHECKING

import pytest
import pytest_asyncio
from sqlalchemy import event
from sqlalchemy.ext.asyncio import create_async_engine

from amane.db.embeddings import EmbeddingStore
from amane.db.repository import Repository
from amane.media import ResourceStore
from tests.schema_template import copy_schema

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Iterator, Sequence
    from pathlib import Path

    from sqlalchemy.ext.asyncio import AsyncEngine


@pytest.fixture
def candidates_first_scandir(monkeypatch: pytest.MonkeyPatch) -> None:
    """固定目录枚举顺序: 名字以 `ad-` 开头的候选排在媒体之前.

    清单触顶的用例, 前提是截断点落在媒体收集完成之前; 文件系统给的顺序是环境属性,
    靠文件名巧合会让用例失去鉴别力. 触顶用例与需要同一前提的入库扫描用例都用它.

    `amane.library.cleanup.inventory.os` 就是 `os` 模块, 因此这是整个进程内的替换, 只给单元级用例用:
    带 worker 的用例上会连带影响别的调用方.
    """
    real_scandir = os.scandir

    @contextmanager
    def ordered(path: str | os.PathLike[str]) -> Iterator[Sequence[os.DirEntry[str]]]:
        with real_scandir(path) as scanned:
            yield sorted(scanned, key=lambda entry: (not entry.name.startswith("ad-"), entry.name))

    monkeypatch.setattr("amane.library.cleanup.inventory.os.scandir", ordered)


def _file_engine(db_path: Path) -> AsyncEngine:
    """文件 SQLite 引擎 (与生产同构: 多连接 + WAL).

    SQLite 自身通过连接级事务隔离 + WAL 控制并发写, 应用层无需加锁.
    """
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}", connect_args={"timeout": 5})

    @event.listens_for(engine.sync_engine, "connect")
    def _set_sqlite_pragma(dbapi_conn, _connection_record):
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        # 测试库数据可丢弃: 关闭 fsync 刷盘加速写入 (生产保持默认 FULL).
        cursor.execute("PRAGMA synchronous=OFF")
        # 旧内存库 (StaticPool) 不启用 FK; 保持该语义, 避免无归属 MediaFile 的用例集体失败.
        cursor.execute("PRAGMA foreign_keys=OFF")
        cursor.close()

    return engine


@pytest_asyncio.fixture
async def repo(tmp_path: Path) -> AsyncGenerator[Repository]:
    """
    基于文件 SQLite 的异步 Repository (与生产同构).

    每个测试在 tmp_path 下独立数据库文件, 测试结束自动清理.
    """
    db_path = tmp_path / "amane.db"
    copy_schema(db_path)
    engine = _file_engine(db_path)
    embeddings = EmbeddingStore(tmp_path / "embeddings.db")
    yield Repository(engine, embeddings=embeddings)

    await embeddings.close()

    await engine.dispose()


@pytest_asyncio.fixture
async def resource_store(tmp_path: Path) -> AsyncGenerator[ResourceStore]:
    """独立的 ResourceStore (文件 DB + tmp 目录).

    handler 现强制注入 ResourceStore; 测试不触发真实下载时仅作占位.
    """
    db_path = tmp_path / "resources.db"
    copy_schema(db_path)
    engine = _file_engine(db_path)
    yield ResourceStore(engine=engine, base_dir=tmp_path / "resources")

    await engine.dispose()


#: 本分支的有意差异: **删除就是纯删除** —— 删 facet 只删实体与关联, 不写屏蔽规则,
#: 也不回填已有影片的真值 (见 ``db/repos/facet_helpers.py`` 里 delete_* 的说明).
#: 上游这几个用例断言的正是相反行为 (删了就写成屏蔽规则、并清洗历史), 在这里统一跳过:
#: 直接改上游测试文件的话每次同步都要解冲突, 集中登记在这里更省事, 差异也一目了然.
_DIVERGED_REASON = "本分支『删除就是纯删除』: 不给删除写屏蔽规则, 上游断言相反行为"
DIVERGED_FROM_UPSTREAM: dict[str, str] = dict.fromkeys(
    (
        "tests/db/test_facets.py::TestFacetRenameMergeDelete::test_delete_blocks_and_alias_chain",
        "tests/db/test_facets.py::TestFacetRenameMergeDelete::test_delete_rule_allows_name_again",
        "tests/api/test_facets_write.py::test_facet_http_actor_rename_merge_delete",
        "tests/db/test_repository.py::TestMetadataRepo::test_delete_blocks_own_names_keeps_shared",
        "tests/db/test_repository.py::TestMetadataRepo::test_locked_actors_still_filtered_by_block_rule",
    ),
    _DIVERGED_REASON,
)


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """把与上述有意差异冲突的上游用例标成 skipped, 其余照常跑."""
    for item in items:
        reason = DIVERGED_FROM_UPSTREAM.get(item.nodeid)
        if reason is not None:
            item.add_marker(pytest.mark.skip(reason=reason))
