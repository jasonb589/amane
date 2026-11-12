"""一本道原生源经设置 schema、配置与网络检测同一路径发现."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import pytest
from curl_cffi.requests import Response

from amane.crawlers.sites.onepondo import OnePondoCrawler

if TYPE_CHECKING:
    from fastapi import FastAPI
    from httpx2 import AsyncClient

    from amane.app.runtime import AppRuntime


@pytest.mark.asyncio
async def test_native_source_is_configurable_and_probes_its_http_catalog(
    app: FastAPI, client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime: AppRuntime = app.state.runtime
    config = (await client.get("config")).json()
    assert config["scraping"]["content_routes"]["uncensored"][0] == "1pondo"
    assert "1pondo" in config["scraping"]["site_config"]
    schema = (await client.get("config/schema")).json()
    properties = schema["$defs"]["ScrapingConfig"]["properties"]
    for field in ("content_routes", "field_priority", "field_blacklist"):
        assert "1pondo" in properties[field]["additionalProperties"]["items"]["enum"]
    assert isinstance(await runtime.factory.get("1pondo"), OnePondoCrawler)
    assert runtime.plugin_manager is not None and "1pondo" not in runtime.plugin_manager.plugin_ids()
    site_config = config["scraping"]["site_config"]
    site_config["1pondo"].update(
        {"base_url": "https://mirror.example/onepondo", "cookie": {"session": "site-cookie"}, "use_browser": "always"}
    )
    updated = await client.patch("config", json={"scraping": {"site_config": site_config}})
    assert updated.status_code == 200, updated.text

    async def request(method: str, url: str, **kwargs: object) -> Response:
        response = Response()
        response.url = url
        response.status_code = 200
        response.content = b'{"TotalRows":1,"Rows":[]}'
        return response

    transport = AsyncMock(side_effect=request)
    monkeypatch.setattr(runtime.web_client, "request", transport)
    checked = await client.post("network/check", json={"source_ids": ["1pondo"]})
    assert checked.status_code == 200, checked.text
    assert [(item["source_id"], item["kind"], item["status"]) for item in checked.json()["items"]] == [
        ("1pondo", "film", "ok")
    ]
    transport.assert_awaited_once_with(
        "GET",
        "https://mirror.example/onepondo/dyn/phpauto/movie_lists/list_newest_0.json",
        cookies={"session": "site-cookie"},
        headers={"Referer": "https://mirror.example/onepondo/"},
        max_attempts=1,
    )
