"""新增 FC2 原生来源的设置、工厂与连通性接线."""

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import pytest
from curl_cffi.requests import Response

from amane.config import ConfigManager
from amane.crawlers.base import Crawler
from amane.crawlers.registry import registry
from amane.enums import SiteName
from amane.parsing import ContentType

if TYPE_CHECKING:
    from fastapi import FastAPI
    from httpx2 import AsyncClient

    from amane.app.runtime import AppRuntime


@pytest.mark.asyncio
async def test_native_fc2_sources_visible_configurable_and_probed(
    app: FastAPI, client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime: AppRuntime = app.state.runtime
    config = (await client.get("config")).json()
    assert config["scraping"]["content_routes"]["fc2"][:2] == ["javarchive", "fc2ppv_db"]
    assert "_format" not in config
    schema = (await client.get("config/schema")).json()
    scraping_properties = schema["$defs"]["ScrapingConfig"]["properties"]
    for key in ("content_routes", "field_priority", "field_blacklist"):
        site_enum = scraping_properties[key]["additionalProperties"]["items"]["enum"]
        assert {"javarchive", "fc2ppv_db", "fc2ppvdb"} <= set(site_enum)
    for site in (SiteName.JAVARCHIVE, SiteName.FC2PPV_DB):
        assert site in config["scraping"]["site_config"]
        instance = await runtime.factory.get(site)
        assert isinstance(instance, Crawler)
        assert type(instance) is registry.get(site)
        assert runtime.plugin_manager is not None
        assert site not in runtime.plugin_manager.plugin_ids()

    patched = await client.patch(
        "config",
        json={
            "scraping": {
                "content_routes": {"fc2": ["javarchive", "fc2ppv_db", "fc2ppvdb"]},
                "site_config": {
                    "javarchive": {"base_url": "https://archive.example/mirror", "cookie": {"session": "one"}},
                    "fc2ppv_db": {"base_url": "https://fc2.example/mirror", "cookie": {"session": "two"}},
                },
            }
        },
    )
    assert patched.status_code == 200, patched.text
    reloaded = ConfigManager.with_cold(runtime.config.cold)
    assert reloaded.hot.scraping.content_routes[ContentType.FC2] == ["javarchive", "fc2ppv_db", "fc2ppvdb"]

    async def request(method: str, url: str, **kwargs: object) -> Response:
        response = Response()
        response.url = url
        response.status_code = 200
        response.content = b"<html><body>available</body></html>"
        return response

    transport = AsyncMock(side_effect=request)
    monkeypatch.setattr(runtime.web_client, "request", transport)
    checked = await client.post("network/check", json={"source_ids": ["javarchive", "fc2ppv_db"]})
    assert checked.status_code == 200, checked.text
    assert [(item["source_id"], item["kind"], item["status"]) for item in checked.json()["items"]] == [
        ("javarchive", "film", "ok"),
        ("fc2ppv_db", "film", "ok"),
    ]
    requests = transport.await_args_list
    assert len(requests) == 2
    archive = next(call for call in requests if "archive.example" in call.args[1])
    index = next(call for call in requests if "fc2.example" in call.args[1])
    assert archive.args[1].startswith("https://archive.example/mirror")
    assert index.args[1].startswith("https://fc2.example/mirror")
    assert archive.kwargs["cookies"]["session"] == "one"
    assert index.kwargs["cookies"]["session"] == "two"
