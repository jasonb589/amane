from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from ...enums import Language


class TranslateTextRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    field: Literal["title", "plot"]
    text: str = Field(min_length=1, max_length=50000, strict=True)

    @field_validator("text")
    @classmethod
    def _nonblank(cls, text: str) -> str:
        if not text.strip():
            raise ValueError("翻译内容不能为空")
        return text


class TranslateTextResponse(BaseModel):
    text: str
    changed: bool
    target_lang: Language
