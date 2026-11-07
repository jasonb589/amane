"""通知显示实际发送图片的来源, 隐藏可选来源失败列表并保留任务错误."""

import json
from pathlib import Path
from typing import TYPE_CHECKING, cast

import pytest

from amane.config import HotSettings
from amane.config.manager import NotifyChannelConfig, NotifyConfig, NotifyProvider
from amane.events import Event, EventType
from amane.media import ResourceStore, materialize_images
from amane.notify.service import NotificationService, build_request
from amane.notify.telegram import plain_html, text_length
from tests.media.test_pipeline import FakeClient

if TYPE_CHECKING:
    from amane.db.repository import Repository
    from amane.net.http import WebClient


@pytest.mark.parametrize("mode", ["", "HTML", "MarkdownV2"])
def test_caption_keeps_source_after_long_content(mode: str) -> None:
    channel = NotifyChannelConfig(provider=NotifyProvider.TELEGRAM, send_photo=True, parse_mode=mode, text="{title}")
    _, _, body = build_request(
        channel,
        {
            "title": "长标题😀" * 800,
            "poster_url": "https://tenhow.net/a.jpg",
            "poster_source": "Tenhow 原生海报 · 1055×1500",
        },
    )
    caption = json.loads(body)["caption"]
    assert caption.endswith("🖼 来源：Tenhow 原生海报 · 1055×1500")
    assert text_length(plain_html(caption) if mode == "HTML" else caption) <= 1024


@pytest.mark.parametrize("panel", ["", "https://panel.example"])
def test_photo_source_tracks_actual_fallback_image(panel: str) -> None:
    channel = NotifyChannelConfig(provider=NotifyProvider.TELEGRAM, send_photo=True, text="{number}")
    _, _, body = build_request(
        channel,
        {
            "number": "ABC-123",
            "poster_url": "/api/resources/crop",
            "thumb_url": "https://cdn.example/cover.jpg",
            "poster_source": "DMM 高清横图裁剪",
            "thumb_source": "DMM 高清横图",
        },
        panel_url=panel,
        image_token="private",
    )
    payload = json.loads(body)
    assert payload["caption"] == "ABC-123\n🖼 来源：" + ("DMM 高清横图裁剪" if panel else "DMM 高清横图")
    assert "private" not in payload["caption"]
    assert payload["photo"].startswith(panel or "https://cdn.example")


def test_custom_source_placeholder_is_not_duplicated_or_reinterpreted() -> None:
    channel = NotifyChannelConfig(
        provider=NotifyProvider.TELEGRAM, send_photo=True, parse_mode="HTML", text="{title}\n来源：{photo_source}"
    )
    _, _, body = build_request(
        channel,
        {"title": "<影片>", "poster_url": "https://image.test/p.jpg", "poster_source": "插件 <custom> {token}"},
    )
    caption = json.loads(body)["caption"]
    assert caption == "&lt;影片&gt;\n来源：插件 &lt;custom&gt; {token}"
    assert caption.count("来源") == 1


@pytest.mark.parametrize("mode", ["", "HTML", "MarkdownV2"])
def test_thumb_fallback_adds_actual_source_when_template_names_poster(mode: str) -> None:
    channel = NotifyChannelConfig(
        provider=NotifyProvider.TELEGRAM,
        send_photo=True,
        parse_mode=mode,
        text="{number}\n海报：{poster_source}",
    )
    _, _, body = build_request(
        channel,
        {
            "number": "ABC-123",
            "poster_url": "/api/resources/crop",
            "poster_source": "DMM 高清横图裁剪",
            "thumb_url": "https://tenhow.net/a.jpg",
            "thumb_source": "Tenhow 横图",
        },
    )
    payload = json.loads(body)
    assert payload["photo"] == "https://tenhow.net/a.jpg"
    assert payload["caption"].endswith("🖼 来源：Tenhow 横图")
    assert payload["caption"].count("Tenhow 横图") == 1


@pytest.mark.parametrize("mode", ["", "HTML", "MarkdownV2"])
def test_truncated_explicit_source_is_reinserted_within_caption_limit(mode: str) -> None:
    channel = NotifyChannelConfig(
        provider=NotifyProvider.TELEGRAM,
        send_photo=True,
        parse_mode=mode,
        text="{plot}\n来源：{photo_source}",
    )
    _, _, body = build_request(
        channel,
        {
            "plot": "长剧情😀" * 800,
            "poster_url": "https://tenhow.net/a.jpg",
            "poster_source": "Tenhow 原生海报 · 1055×1500",
        },
    )
    caption = json.loads(body)["caption"]
    assert caption.endswith("🖼 来源：Tenhow 原生海报 · 1055×1500")
    assert text_length(plain_html(caption) if mode == "HTML" else caption) <= 1024


@pytest.mark.parametrize("mode", ["", "HTML", "MarkdownV2"])
def test_raw_source_suffix_does_not_duplicate_source_line(mode: str) -> None:
    channel = NotifyChannelConfig(
        provider=NotifyProvider.TELEGRAM,
        send_photo=True,
        parse_mode=mode,
        text="{number}\n来源：{photo_source_r}",
    )
    _, _, body = build_request(
        channel,
        {
            "number": "ABC-123",
            "poster_url": "https://tenhow.net/a.jpg",
            "poster_source": "Tenhow 原生海报 · 1055×1500",
        },
    )
    caption = json.loads(body)["caption"]
    assert caption.count("Tenhow 原生海报") == 1
    assert caption.count("来源") == 1
    assert "🖼" not in caption


@pytest.mark.parametrize("show", [False, True])
def test_saved_telegram_template_hides_source_list_but_keeps_task_error(show: bool) -> None:
    channel = NotifyChannelConfig(
        provider=NotifyProvider.TELEGRAM,
        text="{number}\\n🚫 {failed_sites}\\n{site_failures}\\n❌ {error_short}",
        show_site_failures=show,
    )
    _, _, body = build_request(
        channel,
        {
            "number": "ABC-123",
            "failed_sites": "tenhow、amazon",
            "site_failures": "amazon: 超时",
            "error_short": "全部来源失败",
        },
    )
    text = json.loads(body)["text"]
    assert ("tenhow" in text) is show
    assert ("amazon" in text) is show
    assert ("🚫" in text) is show
    assert "全部来源失败" in text


@pytest.mark.asyncio
async def test_batch_hides_partial_source_failures() -> None:
    channel = NotifyChannelConfig(provider=NotifyProvider.TELEGRAM, throttle_seconds=60)
    service = NotificationService()
    service._dispatch(0, channel, False, {"number": "ABC-123", "type": "scrape", "failed_sites": "tenhow、amazon"})
    pending = next(iter(service._pending.values()))
    assert pending.items == ["ABC-123 · scrape"]
    await service.close()


@pytest.mark.asyncio
async def test_variables_trace_materialized_crop_to_dmm(
    repo: Repository, resource_store: ResourceStore, tmp_path: Path
) -> None:
    source = "https://awsimgsrc.dmm.co.jp/pics_dig/mono/movie/118abf295/118abf295pl.jpg"
    output = await materialize_images(
        [], [source], [], resource_store, cast("WebClient", FakeClient({source: (2184, 1477)})), HotSettings(), tmp_path
    )
    await repo.upsert_metadata(
        "ABF-295",
        poster_urls=output.poster_urls,
        thumb_urls=output.thumb_urls,
        raw={"javbus": {"thumb_urls": [source]}},
    )
    config = NotifyConfig(enabled=True)
    service = NotificationService()
    service.configure(config, web_client=None, repo=repo, log_dir=None, resource_store=resource_store)
    variables = await service._variables(Event(type=EventType.FILE_DISCOVERED, data={"path": "ABF-295.mp4"}), config)
    assert variables["poster_source"].startswith("DMM 高清横图裁剪 · 源图 2184×1477")
    assert variables["thumb_source"] == "DMM 高清横图 · 2184×1477"
    assert "javbus" not in variables["poster_source"]
