"""编辑草稿翻译复用现有配置与缓存, 保存前不得修改影片或 NFO."""

import json
import xml.etree.ElementTree as ET
from typing import TYPE_CHECKING

import httpx2
import pytest

from amane.api.routes import translations
from amane.enums import Language, MetadataField
from amane.media.nfo import write_nfo

if TYPE_CHECKING:
    from pathlib import Path

    from fastapi import FastAPI

    from amane.db.repository import Repository


@pytest.mark.asyncio
async def test_translate_drafts_reuses_config_cache_and_syncs_only_on_save(
    client: httpx2.AsyncClient,
    app: FastAPI,
    repo: Repository,
    safe_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: list[dict[str, object]] = []
    response_text = "完整译文"

    def upstream(request: httpx2.Request) -> httpx2.Response:
        requests.append(json.loads(request.content))
        return httpx2.Response(
            200,
            json={
                "id": "translation-test",
                "object": "chat.completion",
                "created": 0,
                "model": "configured-model",
                "choices": [
                    {"index": 0, "message": {"role": "assistant", "content": response_text}, "finish_reason": "stop"}
                ],
            },
        )

    real_client = httpx2.AsyncClient
    transports: list[httpx2.AsyncClient] = []

    def provider_client(*, proxy: str | None, timeout: httpx2.Timeout, follow_redirects: bool) -> httpx2.AsyncClient:
        instance = real_client(transport=httpx2.MockTransport(upstream), timeout=timeout)
        transports.append(instance)
        return instance

    monkeypatch.setattr(translations, "AsyncClient", provider_client)
    runtime = app.state.runtime
    llm = runtime.config.hot.llm
    llm.enabled = False
    llm.api_key = "test-credential"
    llm.model = "configured-model"
    llm.base_url = "https://translation.example/v1"
    llm.system_prompt = "翻译为{target_lang}并保持原意"
    llm.field_prompts = {MetadataField.TITLE: "标题字段说明", MetadataField.PLOT: "剧情字段说明"}
    runtime.config.hot.scraping.field_language[MetadataField.TITLE] = Language.ZH_CN
    runtime.config.hot.scraping.field_language[MetadataField.PLOT] = Language.ZH_TW

    library = await repo.create_library("source", str(safe_path))
    metadata = await repo.upsert_metadata(
        "ABC-001", title="Original", plot="Original plot", raw={"test": {"title": "原文"}}
    )
    assert metadata.id is not None and library.id is not None
    video = safe_path / "ABC-001.mp4"
    video.parent.mkdir(parents=True, exist_ok=True)
    video.write_bytes(b"media")
    nfo = video.with_suffix(".nfo")
    assert await write_nfo(metadata, nfo)
    assert await repo.remember_nfo_output(metadata.id, library.id, video, nfo)
    before = nfo.read_bytes()

    body = {"field": "title", "text": "現在入力した原題"}
    response = await client.post("translations", json=body)
    assert response.status_code == 200
    assert response.json() == {"text": "完整译文", "changed": True, "target_lang": "zh_cn"}
    repeated = await client.post("translations", json=body)
    assert repeated.json() == response.json() and len(requests) == 1
    messages = requests[0]["messages"]
    assert isinstance(messages, list)
    assert messages[-1]["content"] == "現在入力した原題"
    assert "标题字段说明" in messages[0]["content"] and "简体中文" in messages[0]["content"]
    assert requests[0]["model"] == "configured-model"
    saved = await repo.get_metadata(metadata.id)
    assert saved is not None and saved.title == "Original" and saved.raw == metadata.raw
    assert nfo.read_bytes() == before

    plot = await client.post("translations", json={"field": "plot", "text": "現在入力した剧情"})
    assert plot.status_code == 200 and plot.json()["target_lang"] == "zh_tw"
    plot_messages = requests[-1]["messages"]
    assert isinstance(plot_messages, list)
    assert "剧情字段说明" in plot_messages[0]["content"] and "繁體中文" in plot_messages[0]["content"]
    assert len(requests) == 2

    unchanged = await client.post("translations", json={"field": "title", "text": "中文标题"})
    assert unchanged.status_code == 200 and unchanged.json()["changed"] is False
    assert len(requests) == 2

    for invalid in (
        {"field": "tags", "text": "test"},
        {"field": "title", "text": " "},
        {"field": "plot", "text": "x" * 50001},
        {"field": "title", "text": 123},
        {"field": "title", "text": "test", "save": True},
    ):
        rejected = await client.post("translations", json=invalid)
        assert rejected.status_code == 422
    assert len(requests) == 2

    response_text = ""
    failed = await client.post("translations", json={"field": "title", "text": "翻訳サービスの空応答"})
    assert failed.status_code == 502 and "有效译文" in failed.json()["detail"]
    assert nfo.read_bytes() == before
    llm.api_key = None
    missing = await client.post("translations", json=body)
    assert missing.status_code == 503 and "配置" in missing.json()["detail"]
    assert all(transport.is_closed for transport in transports)

    patch = await client.patch(
        f"metadata/{metadata.id}", json={"title": response.json()["text"], "plot": plot.json()["text"]}
    )
    assert patch.status_code == 200 and patch.json()["nfo_sync"]["written"] == 1
    assert ET.parse(nfo).findtext("title") == "ABC-001 完整译文"
    assert ET.parse(nfo).findtext("plot") == "完整译文"
    saved = await repo.get_metadata(metadata.id)
    assert saved is not None and saved.raw == metadata.raw
