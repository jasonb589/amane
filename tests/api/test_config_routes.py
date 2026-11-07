"""配置 API 端点的集成测试"""

import asyncio
from typing import TYPE_CHECKING

import pytest
from httpx2 import ASGITransport, AsyncClient

from amane.config import HotSettings
from amane.config.manager import WatcherConfig
from amane.handlers.models import RefreshPayload
from amane.handlers.refresh import RefreshHandler
from amane.parsing import parse_file_info
from amane.parsing.file_info import set_escape_strings
from tests.api.conftest import hot_for_tests, make_app

if TYPE_CHECKING:
    from pathlib import Path


class TestConfigHttp:
    @pytest.mark.asyncio(loop_scope="function")
    async def test_get_patch_schema(self, client: AsyncClient):
        resp = await client.get("config")
        assert resp.status_code == 200
        assert HotSettings.model_validate(resp.json()) == hot_for_tests()
        data = resp.json()
        assert isinstance(data["scraping"]["download_resources"], list)
        assert isinstance(data["worker"]["concurrency"], int)
        assert isinstance(data["worker"]["poll_interval"], float)

        schema = await client.get("config/schema")
        assert schema.status_code == 200
        assert schema.json()["type"] == "object"
        assert "properties" in schema.json()

        empty = await client.patch("config", json={})
        assert empty.status_code == 200
        assert HotSettings.model_validate(empty.json()) == hot_for_tests()
        assert (await client.patch("config", json={"worker": {"concurrency": 999}})).status_code == 422

        patched = await client.patch(
            "config",
            json={
                "network": {"proxy": "http://new"},
                "worker": {"concurrency": 8},
                "scraping": {
                    "crop_poster": False,
                    "field_priority": {"actors": ["javdb", "theporndb"]},
                    "field_blacklist": {"title": ["javbus"]},
                },
            },
        )
        assert patched.status_code == 200
        body = patched.json()
        assert body["worker"]["concurrency"] == 8
        assert body["scraping"]["crop_poster"] is False
        assert body["scraping"]["field_priority"]["actors"] == ["javdb", "theporndb"]
        assert body["scraping"]["field_blacklist"]["title"] == ["javbus"]

        await client.patch("config", json={"network": {"proxy": "socks5://test"}})
        got = await client.get("config")
        assert got.json()["network"]["proxy"] == "socks5://test"
        assert got.json()["worker"]["concurrency"] == 8


class TestConfigRebuild:
    """PUT /config 触发 runtime rebuild + event broadcast"""

    @pytest.mark.asyncio(loop_scope="function")
    async def test_update_triggers_worker_rebuild(self, client: AsyncClient, app):
        old_concurrency = app.state.runtime.worker._concurrency

        resp = await client.patch("config", json={"worker": {"concurrency": 3}})
        assert resp.status_code == 200
        assert resp.json()["worker"]["concurrency"] == 3

        new_concurrency = app.state.runtime.worker._concurrency
        assert new_concurrency == 3
        assert new_concurrency != old_concurrency

    @pytest.mark.asyncio(loop_scope="function")
    async def test_update_preserves_worker_functionality(self, client: AsyncClient, safe_path):
        scan_dir = safe_path / "after-rebuild"
        scan_dir.mkdir()

        await client.patch("config", json={"worker": {"concurrency": 5}})

        lib = (await client.post("libraries", json={"path": str(scan_dir)})).json()
        resp = await client.post("tasks", json={"type": "refresh", "library_id": lib["id"]})
        task_id = resp.json()["id"]

        for _ in range(40):
            check = await client.get(f"tasks/{task_id}")
            if check.json()["status"] in ("done", "failed"):
                break
            await asyncio.sleep(0.05)

        final = await client.get(f"tasks/{task_id}")
        assert final.json()["status"] == "done"

    @pytest.mark.asyncio(loop_scope="function")
    async def test_r18_dsn_change_rebuilds_read_engine(self, client: AsyncClient, app):
        runtime = app.state.runtime
        assert runtime.r18_db is None

        resp = await client.patch(
            "config", json={"r18": {"dsn": "postgresql://u:p@127.0.0.1:5432/postgres", "db_name": "r18dev"}}
        )
        assert resp.status_code == 200

        assert runtime.r18_db is not None
        assert runtime.r18_handle is not None
        assert runtime.r18_handle.engine is runtime.r18_db
        crawler = await runtime.factory.get("r18dev")
        assert crawler is not None
        assert crawler._db is runtime.r18_db

    @pytest.mark.asyncio(loop_scope="function")
    async def test_browser_change_rebuilds_pool_and_unrelated_change_keeps_it(self, client: AsyncClient, app):
        runtime = app.state.runtime
        pool = runtime.browser
        assert pool is not None

        resp = await client.patch("config", json={"network": {"browser": {"backend": "solver"}}})
        assert resp.status_code == 200

        rebuilt = runtime.browser
        assert rebuilt is not None
        assert rebuilt is not pool

        # 旧池随使用它的退役 worker 排空后关闭
        await asyncio.gather(*list(runtime._retire_tasks), return_exceptions=True)
        assert pool._closed is True

        resp = await client.patch("config", json={"worker": {"concurrency": 4}})
        assert resp.status_code == 200
        assert runtime.browser is rebuilt
        assert rebuilt._closed is False


@pytest.mark.asyncio
async def test_persisted_filename_filters_apply_before_first_scan_and_after_rebuild(tmp_path: Path) -> None:
    patterns = ["4k688.com@", "489155.com@"]
    files = tmp_path / "files"
    app = make_app(
        HotSettings(watcher=WatcherConfig(escape_strings=patterns)), tmp_path / "data", tmp_path / "logs", files
    )
    source = files / "4K688.com@ABC-123.mp4"
    source.write_bytes(b"video")
    western = files / "489155.com@BLACKED..17.04.15.AJ.APPLEGATE.4K-C.mp4"
    western.write_bytes(b"video")
    set_escape_strings([])
    try:
        async with app.router.lifespan_context(app):
            runtime = app.state.runtime
            # 不经配置PATCH即可生效, 对应服务重启后第一次扫描.
            assert parse_file_info(source).number == "ABC-123"
            assert parse_file_info(source).definition is None
            assert parse_file_info(western).number == "Blacked.17.04.15"
            assert parse_file_info(western).definition == "4K"
            await runtime.stop_workers(closing=False)
            library = await runtime.repo.create_library(name="filters", path=str(files), min_file_size=0)
            assert library.id is not None
            result = await RefreshHandler(runtime.repo).handle(RefreshPayload(library_id=library.id, path=str(files)))
            assert result.result is not None and result.result.added == 2
            assert {followup.payload["number"] for followup in result.followups} == {
                "ABC-123",
                "Blacked.17.04.15",
            }
            media = await runtime.repo.list_media_files(library_id=library.id, limit=None)
            assert {item.path: item.definition for item in media} == {
                str(source): None,
                str(western): "4K",
            }
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test/api/") as client:
                before = await client.get("config")
                assert before.json()["watcher"]["escape_strings"] == patterns
                invalid = await client.patch("config", json={"watcher": {"escape_strings": ["r:["]}})
                assert invalid.status_code == 422
                assert (await client.get("config")).json()["watcher"]["escape_strings"] == patterns
                updated = await client.patch("config", json={"watcher": {"escape_strings": [*patterns, "PREFIX-"]}})
                assert updated.status_code == 200
                assert parse_file_info("PREFIX-ABC-123.mp4").number == "ABC-123"
            assert source.name == "4K688.com@ABC-123.mp4" and source.is_file()
    finally:
        set_escape_strings([])
