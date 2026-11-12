from fastapi import APIRouter, HTTPException
from httpx2 import AsyncClient, Timeout

from ...enums import Language, MetadataField
from ...llm import build_translator
from ...utils.language import needs_llm_translation
from ..deps import RuntimeDep
from ..models.translations import TranslateTextRequest, TranslateTextResponse

router = APIRouter(prefix="/translations", tags=["translations"])


@router.post("")
async def translate_metadata_text(req: TranslateTextRequest, runtime: RuntimeDep) -> TranslateTextResponse:
    """翻译编辑草稿, 只返回译文; Metadata 与库内 NFO 仍由用户保存时提交."""
    hot = runtime.config.hot.model_copy(deep=True)
    config = hot.llm
    if not config.api_key or not config.model.strip():
        raise HTTPException(status_code=503, detail="请先在翻译设置中配置 API 密钥与模型")
    field = MetadataField(req.field)
    target = hot.scraping.field_language.get(field, Language.ZH_CN)
    requires_llm = needs_llm_translation(req.text.strip(), target)
    async with (
        runtime.manual_translations,
        AsyncClient(proxy=hot.network.proxy, timeout=Timeout(60.0), follow_redirects=True) as client,
    ):
        translator = build_translator(
            enabled=True,
            api_type=config.api_type,
            api_key=config.api_key,
            base_url=config.base_url,
            model=config.model,
            rate_limit=config.rate_limit,
            system_prompt=config.system_prompt,
            field_prompts=config.field_prompts,
            cache=runtime.translation_cache,
            coalescer=runtime.translation_coalescer,
            http_client=client,
        )
        assert translator is not None
        translated = await translator.translate(req.text, target, field)
    if translated is None and requires_llm:
        raise HTTPException(status_code=502, detail="翻译服务未返回有效译文, 请检查服务配置后重试")
    text = translated if translated is not None else req.text
    return TranslateTextResponse(text=text, changed=text != req.text, target_lang=target)
