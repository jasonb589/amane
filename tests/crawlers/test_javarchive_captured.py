"""JavArchive 完整 HTTP 响应的 TOML 回归, 不依赖外部 fixture 仓库."""

from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from amane.crawlers.http import HttpClient
from amane.crawlers.models import SearchQuery
from amane.crawlers.sites.javarchive import JavArchiveCrawler

from .driven import assert_expected, build_mock, call_step, load_toml

_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "javarchive"


@pytest.mark.asyncio
@pytest.mark.parametrize("case", sorted(_FIXTURES.glob("*.toml")), ids=lambda path: path.stem)
async def test_captured_pages(case: Path, http_client: HttpClient, mock_web_client: AsyncMock) -> None:
    config = load_toml(case)["fetch"]
    build_mock(mock_web_client, case.parent, config["responses"])
    result = await call_step(JavArchiveCrawler(http_client).fetch(SearchQuery(config["number"])), config)
    if config.get("expected_none"):
        assert result is None
        mock_web_client.get_text.assert_awaited_once()
    else:
        assert result is not None
        assert_expected(result, config["expected"])
        assert mock_web_client.get_text.await_count == 2
