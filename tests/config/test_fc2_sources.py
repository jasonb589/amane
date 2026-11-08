"""FC2 来源升级只执行一次, 保留后续手动路由与字段排除."""

import tomllib
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest
import tomli_w
from pydantic import ValidationError

from amane.aggregate import aggregate, compile_priority
from amane.config import ColdSettings, ConfigManager
from amane.crawlers.base import Crawler
from amane.crawlers.models import MediaMetadata, SearchQuery
from amane.enums import MetadataField
from amane.net.errors import FailureReason, SourceError
from amane.parsing import ContentType


@pytest.mark.parametrize("route", [None, [], ["fc2", "fc2ppvdb", "javdb", "fc2"]])
def test_existing_fc2_routes_upgrade_once_and_preserve_exclusions(tmp_path: Path, route: list[str] | None) -> None:
    cold = ColdSettings(data_dir=tmp_path)
    routes: dict[str, list[str]] = {"censored": ["javdb", "dmm"]}
    if route is not None:
        routes["fc2"] = route
    before: dict[str, Any] = {
        "scraping": {
            "content_routes": routes,
            "field_priority": {"title": ["javdb", "fc2"], "poster_urls": ["dmm"]},
            "field_blacklist": {"title": ["javarchive"]},
            "site_config": {"fc2ppvdb": {"base_url": "https://old.example", "cookie": {"session": "kept"}}},
        },
        "network": {"proxy": "http://proxy.example:8080"},
    }
    cold.config_path.write_text(tomli_w.dumps(before), encoding="utf-8")
    manager = ConfigManager.with_cold(cold)
    scraping = manager.hot.scraping
    chain = compile_priority(
        scraping.content_routes[ContentType.FC2], scraping.field_priority, scraping.field_blacklist
    )
    if route == []:
        assert scraping.content_routes[ContentType.FC2] == []
        assert chain[MetadataField.TITLE] == []
        assert scraping.field_priority[MetadataField.TITLE] == ["javdb", "fc2"]
    else:
        assert scraping.content_routes[ContentType.FC2][:2] == ["javarchive", "fc2ppv_db"]
        assert chain[MetadataField.TITLE][0] == "fc2ppv_db"
        assert "javarchive" not in chain[MetadataField.TITLE]
        assert chain[MetadataField.POSTER_URLS][0] == "javarchive"
        if route:
            assert scraping.content_routes[ContentType.FC2][2:] == ["fc2", "fc2ppvdb", "javdb"]
    assert scraping.content_routes[ContentType.CENSORED] == ["javdb", "dmm"]
    other_chain = compile_priority(scraping.content_routes[ContentType.CENSORED], scraping.field_priority)
    assert other_chain[MetadataField.TITLE] == ["javdb", "dmm"]
    assert other_chain[MetadataField.POSTER_URLS] == ["dmm", "javdb"]
    assert scraping.site_config["fc2ppvdb"].base_url == "https://old.example"
    assert scraping.site_config["fc2ppvdb"].cookie == {"session": "kept"}
    assert scraping.site_config["fc2ppv_db"].base_url is None
    assert manager.hot.network.proxy == "http://proxy.example:8080"

    persisted = cold.config_path.read_bytes()
    assert tomllib.loads(persisted.decode())["_format"] == {"version": 1}
    assert ConfigManager.with_cold(cold).hot == manager.hot
    assert cold.config_path.read_bytes() == persisted

    # 显式重排或移除新增来源后, 无关修改与重启不得重新前置.
    for custom in (["fc2ppv_db", "javarchive", "fc2"], ["fc2"], []):
        manager.update({"scraping": {"content_routes": {"fc2": custom}, "field_priority": {"title": ["fc2"]}}})
        manager.update({"worker": {"concurrency": 3}})
        reloaded = ConfigManager.with_cold(cold)
        assert reloaded.hot.scraping.content_routes[ContentType.FC2] == custom
        assert reloaded.hot.scraping.field_priority[MetadataField.TITLE] == ["fc2"]
        assert reloaded.hot.scraping.field_blacklist[MetadataField.TITLE] == ["javarchive"]
        assert "_format" not in reloaded.hot.model_dump()


@pytest.mark.parametrize("format_data", [{"version": 2}, {"version": -1}, {"version": "1"}, {"unknown": 1}])
def test_invalid_config_format_does_not_rewrite_file(tmp_path: Path, format_data: dict[str, object]) -> None:
    cold = ColdSettings(data_dir=tmp_path)
    original = tomli_w.dumps({"_format": format_data, "worker": {"concurrency": 3}})
    cold.config_path.write_text(original, encoding="utf-8")
    with pytest.raises(ValidationError):
        ConfigManager.with_cold(cold)
    assert cold.config_path.read_text(encoding="utf-8") == original


def test_failed_config_upgrade_retains_original_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cold = ColdSettings(data_dir=tmp_path)
    original = '[scraping.content_routes]\nfc2 = ["fc2"]\n'
    cold.config_path.write_text(original, encoding="utf-8")

    def fail_replace(self: Path, target: str | Path) -> Path:
        raise OSError("write denied")

    monkeypatch.setattr(Path, "replace", fail_replace)
    with pytest.raises(OSError, match="write denied"):
        ConfigManager.with_cold(cold)
    assert cold.config_path.read_text(encoding="utf-8") == original
    assert not list(tmp_path.glob(".config_*.toml.tmp"))


@pytest.mark.asyncio
@pytest.mark.parametrize("first_result", ["ok", "missing", "blocked"])
async def test_fc2_new_sources_fill_cached_entry_and_fallback(tmp_path: Path, first_result: str) -> None:
    cold = ColdSettings(data_dir=tmp_path)
    cold.config_path.write_text(
        '[scraping.content_routes]\nfc2 = ["javdb"]\n[scraping.field_priority]\ntitle = ["javdb"]\n',
        encoding="utf-8",
    )
    scraping = ConfigManager.with_cold(cold).hot.scraping
    first, second, old = (AsyncMock(spec=Crawler) for _ in range(3))
    first.fetch.return_value = (
        MediaMetadata(number="FC2-1234567", title="archive title", poster_urls=["https://images.test/a.jpg"])
        if first_result == "ok"
        else None
    )
    if first_result == "blocked":
        first.fetch.side_effect = SourceError(FailureReason.CLOUDFLARE_CHALLENGE)
    second.fetch.return_value = MediaMetadata(number="FC2-1234567", title="index title", runtime=60)
    old.fetch.side_effect = AssertionError("旧源已有缓存, 不应请求")
    result = await aggregate(
        SearchQuery("FC2-1234567", content_type=ContentType.FC2),
        {"javarchive": first, "fc2ppv_db": second, "javdb": old},
        compile_priority(scraping.content_routes[ContentType.FC2], scraping.field_priority, scraping.field_blacklist),
        cache={"javdb": {"number": "FC2-1234567", "title": "old title"}},
    )
    first.fetch.assert_awaited_once()
    second.fetch.assert_awaited_once()
    old.fetch.assert_not_awaited()
    expected_site = "javarchive" if first_result == "ok" else "fc2ppv_db"
    assert result.metadata.title == ("archive title" if first_result == "ok" else "index title")
    assert result.field_sources[MetadataField.TITLE] == expected_site
    assert result.metadata.runtime == 60
