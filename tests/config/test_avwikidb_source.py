"""新原生源的配置发现与既有自定义路由保留."""

from pathlib import Path

import pytest
import tomli_w

from amane.aggregate import compile_priority
from amane.config import ColdSettings, ConfigManager, ScrapingConfig
from amane.crawlers import registry
from amane.crawlers.site_roles import FILM_METADATA_SITES
from amane.enums import MetadataField, SiteName
from amane.parsing import ContentType


@pytest.mark.parametrize("route", [None, [], ["mgstage", "dmm"], ["avwikidb", "mgstage"]])
def test_old_config_keeps_explicit_routes_and_adds_configurable_source(tmp_path: Path, route: list[str] | None) -> None:
    cold = ColdSettings(data_dir=tmp_path)
    routes = {} if route is None else {"amateur": route}
    cold.config_path.write_text(
        tomli_w.dumps({"_format": {"version": 1}, "scraping": {"content_routes": routes}}), encoding="utf-8"
    )
    before = cold.config_path.read_bytes()
    manager = ConfigManager.with_cold(cold)
    scraping = manager.hot.scraping
    expected = ScrapingConfig().content_routes[ContentType.AMATEUR] if route is None else route
    assert scraping.content_routes[ContentType.AMATEUR] == expected
    assert SiteName.AVWIKIDB in scraping.site_config
    assert SiteName.AVWIKIDB in FILM_METADATA_SITES and registry.get(SiteName.AVWIKIDB) is not None
    assert SiteName.AVWIKIDB in scraping.content_routes[ContentType.CENSORED]
    assert scraping.content_routes[ContentType.FC2][:2] == ["javarchive", "fc2ppv_db"]
    assert SiteName.AVWIKIDB not in scraping.content_routes[ContentType.FC2]
    assert cold.config_path.read_bytes() == before


def test_source_field_priorities_and_blacklist_respect_type_route() -> None:
    config = ScrapingConfig.model_validate(
        {"field_priority": {"actors": ["avwikidb"]}, "field_blacklist": {"poster_urls": ["avwikidb"]}}
    )
    chain = compile_priority(config.content_routes[ContentType.AMATEUR], config.field_priority, config.field_blacklist)
    assert chain[MetadataField.ACTORS][0] == "avwikidb"
    assert "avwikidb" not in chain[MetadataField.POSTER_URLS]
    fc2 = compile_priority(config.content_routes[ContentType.FC2], config.field_priority, config.field_blacklist)
    assert all("avwikidb" not in sources for sources in fc2.values())
