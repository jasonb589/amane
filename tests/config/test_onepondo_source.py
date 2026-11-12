"""一本道原生来源的配置发现与显式类型路由保留."""

from pathlib import Path

import pytest
import tomli_w

from amane.aggregate import compile_priority
from amane.config import ColdSettings, ConfigManager, ScrapingConfig
from amane.crawlers import registry
from amane.crawlers.site_roles import FILM_METADATA_SITES
from amane.enums import MetadataField, SiteName
from amane.parsing import ContentType


@pytest.mark.parametrize("route", [None, [], ["javdb", "javbus"], ["caribbean", "1pondo"]])
def test_source_discovery_preserves_explicit_uncensored_route(tmp_path: Path, route: list[str] | None) -> None:
    cold = ColdSettings(data_dir=tmp_path)
    routes = {} if route is None else {"uncensored": route}
    cold.config_path.write_text(
        tomli_w.dumps({"_format": {"version": 1}, "scraping": {"content_routes": routes}}), encoding="utf-8"
    )
    before = cold.config_path.read_bytes()
    config = ConfigManager.with_cold(cold).hot.scraping
    expected = ScrapingConfig().content_routes[ContentType.UNCENSORED] if route is None else route
    assert config.content_routes[ContentType.UNCENSORED] == expected
    assert SiteName.ONEPONDO in config.site_config
    assert SiteName.ONEPONDO in FILM_METADATA_SITES and registry.get(SiteName.ONEPONDO) is not None
    assert all(
        SiteName.ONEPONDO not in sources
        for kind, sources in config.content_routes.items()
        if kind != ContentType.UNCENSORED
    )
    assert cold.config_path.read_bytes() == before


def test_native_source_field_rules_only_apply_inside_its_route() -> None:
    config = ScrapingConfig.model_validate(
        {"field_priority": {"title": ["1pondo"]}, "field_blacklist": {"poster_urls": ["1pondo"]}}
    )
    assert config.content_routes[ContentType.UNCENSORED][0] == "1pondo"
    chain = compile_priority(
        config.content_routes[ContentType.UNCENSORED], config.field_priority, config.field_blacklist
    )
    assert chain[MetadataField.TITLE][0] == "1pondo"
    assert "1pondo" not in chain[MetadataField.POSTER_URLS]
    censored = compile_priority(
        config.content_routes[ContentType.CENSORED], config.field_priority, config.field_blacklist
    )
    assert all("1pondo" not in sources for sources in censored.values())
