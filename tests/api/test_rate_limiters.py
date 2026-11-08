"""验证 build_network_stack 为所有爬虫域名创建限速器."""

import httpx2 as httpx
import pytest

from amane.app.runtime import build_network_stack
from amane.config import HotSettings
from amane.config.manager import SiteConfig
from amane.crawlers import registry
from amane.net.http import RateLimiters


@pytest.mark.asyncio
async def test_all_crawler_domains_have_limiters():
    """通过 DI 构造: build_network_stack → from_config
    为所有爬虫 profile 域名预创建限速器, 无裸域名."""
    hot = HotSettings()
    for site in registry.sites():
        hot.scraping.site_config[site] = SiteConfig(rate_limit=5.0)

    stack = build_network_stack(hot)
    limiters = stack.web_client._limiters._limiters

    missing: list[str] = []
    bare: list[str] = []
    for site in registry.sites():
        crawler_cls = registry.get(site)
        if crawler_cls is None:
            continue
        for url in [*crawler_cls.profile().urls, crawler_cls.profile().base_url]:
            if not url:
                continue
            host = httpx.URL(url).host
            if not host:
                bare.append(f"{site}: {url!r}")
                continue
            if host not in limiters:
                missing.append(f"{site}: {host}")

    assert not bare, "裸域名:\n" + "\n".join(bare)
    assert not missing, "未预创建限速器:\n" + "\n".join(missing)

    await stack.browser.close()
    await stack.web_client.close()


@pytest.mark.parametrize("default_rate,expected_period", [(5, 1 / 5), (2, 1 / 2), (0.5, 1 / 0.5)])
def test_from_config_default_rate(default_rate: float, expected_period: float):
    """未配置的域名使用传入的 default_rate."""
    limiters = RateLimiters.from_config({}, {}, {}, default_rate=default_rate)
    limiter = limiters.get("never-configured.example.com")
    assert limiter.time_period == pytest.approx(expected_period)


def test_build_network_stack_propagates_default_rate():
    """HotSettings.network.default_rate_limit 经 build_network_stack 生效."""
    hot = HotSettings()
    hot.network.default_rate_limit = 3
    stack = build_network_stack(hot)
    limiter = stack.web_client._limiters.get("never-configured.example.com")
    assert limiter.time_period == pytest.approx(1 / 3)
