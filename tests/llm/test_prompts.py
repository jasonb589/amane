"""自定义翻译提示词的组装与配置校验.

覆盖: 拼接顺序与回退、目标语言占位符、逐字段覆盖的作用范围, 以及非法输入的拒绝.
内置文案在用例里复制一份: 文案改动应当在这里被看见.
"""

import pytest
from pydantic import ValidationError

from amane.config import LLMConfig
from amane.enums import Language, MetadataField
from amane.llm import TARGET_LANG_PLACEHOLDER, build_system_prompt

_TITLE_HINT = (
    "输入是影片标题. 译文须简洁自然, 符合{target_lang}的标题习惯; "
    "番号(如 ABC-001)、人名、片商与系列名等专有名词原样保留, 不译、不音译."
)
_PLOT_HINT = (
    "输入是影片简介. 逐句完整翻译, 不省略、不概括、不合并句子; "
    "保持原文的段落与换行, 语气与原文一致; 同一专有名词的译法在全文中统一."
)
_OUTPUT_CONSTRAINT = "只输出译文正文: 不加解释、说明、前缀或后缀, 不加引号或书名号, 不使用代码块, 不重复原文."


def _hint(template: str, lang_name: str) -> str:
    """内置说明含占位符, 期望值按目标语言替换后再拼接."""
    return template.replace(TARGET_LANG_PLACEHOLDER, lang_name)


def _instruction(lang_name: str) -> str:
    return (
        f"你是专业的影视元数据翻译, 负责把影片标题或简介等元数据翻译为{lang_name}. "
        "译文须完整传达原意: 不增删信息, 不改写内容, 不添加原文没有的解释."
    )


_ZH = _instruction("简体中文")
_ZH_TW = _instruction("繁體中文")
_TITLE_ZH = _hint(_TITLE_HINT, "简体中文")
_TITLE_TW = _hint(_TITLE_HINT, "繁體中文")


@pytest.mark.parametrize(
    ("target", "field", "system_prompt", "field_prompts", "expected"),
    [
        # 未配置: 指令 + 内置字段说明 + 输出约束
        (Language.ZH_CN, MetadataField.TITLE, None, None, f"{_ZH} {_TITLE_ZH} {_OUTPUT_CONSTRAINT}"),
        # 无内置说明的字段: 不追加空说明, 也不留下多余空格
        (Language.ZH_CN, MetadataField.SCORE, None, None, f"{_ZH} {_OUTPUT_CONSTRAINT}"),
        # 自定义指令: 占位符替换为目标语言名
        (
            Language.ZH_TW,
            MetadataField.TITLE,
            f"将输入翻译为{TARGET_LANG_PLACEHOLDER}, 只使用中性词汇.",
            None,
            f"将输入翻译为繁體中文, 只使用中性词汇. {_TITLE_TW} {_OUTPUT_CONSTRAINT}",
        ),
        # 不写占位符时按原文使用, 不自动补充目标语言
        (
            Language.ZH_CN,
            MetadataField.TITLE,
            "只输出中文译文.",
            None,
            f"只输出中文译文. {_TITLE_ZH} {_OUTPUT_CONSTRAINT}",
        ),
        # 空白指令回退内置
        (Language.ZH_CN, MetadataField.TITLE, "   ", None, f"{_ZH} {_TITLE_ZH} {_OUTPUT_CONSTRAINT}"),
        # 字段说明覆盖该字段的内置说明, 且同样支持占位符
        (
            Language.ZH_TW,
            MetadataField.PLOT,
            None,
            {MetadataField.PLOT: f"用{TARGET_LANG_PLACEHOLDER}書面語."},
            f"{_ZH_TW} 用繁體中文書面語. {_OUTPUT_CONSTRAINT}",
        ),
        # 覆盖只作用于列出的字段
        (
            Language.ZH_CN,
            MetadataField.PLOT,
            None,
            {MetadataField.TITLE: "标题不超过 30 字."},
            f"{_ZH} {_PLOT_HINT} {_OUTPUT_CONSTRAINT}",
        ),
        # 花括号按字面保留 (允许 JSON 示例), 不触发格式化
        (
            Language.ZH_CN,
            MetadataField.TITLE,
            '按 {"title": "值"} 结构理解输入.',
            {MetadataField.TITLE: "保留 {番号} 原样."},
            f'按 {{"title": "值"}} 结构理解输入. 保留 {{番号}} 原样. {_OUTPUT_CONSTRAINT}',
        ),
    ],
)
def test_build_system_prompt(
    target: Language,
    field: MetadataField,
    system_prompt: str | None,
    field_prompts: dict[MetadataField, str] | None,
    expected: str,
) -> None:
    assert build_system_prompt(target, field, system_prompt=system_prompt, field_prompts=field_prompts) == expected


@pytest.mark.parametrize(
    ("payload", "expected_system", "expected_fields"),
    [
        ({}, None, {}),
        # 首尾空白去除
        ({"system_prompt": "  只用中性词汇.  "}, "只用中性词汇.", {}),
        # 纯空白等价于未配置
        ({"system_prompt": "   "}, None, {}),
        # 空白条目与缺席等价, 只保留有内容的字段
        (
            {"field_prompts": {"title": " 标题简洁 ", "plot": "   "}},
            None,
            {MetadataField.TITLE: "标题简洁"},
        ),
    ],
)
def test_llm_config_prompt_normalization(
    payload: dict, expected_system: str | None, expected_fields: dict[MetadataField, str]
) -> None:
    cfg = LLMConfig.model_validate(payload)
    assert cfg.system_prompt == expected_system
    assert cfg.field_prompts == expected_fields


@pytest.mark.parametrize(
    ("payload", "match"),
    [
        # 字段名受 MetadataField 约束
        ({"field_prompts": {"nope": "标题简洁"}}, "field_prompts.nope"),
    ],
)
def test_llm_config_rejects_invalid_prompts(payload: dict, match: str) -> None:
    with pytest.raises(ValidationError, match=match):
        LLMConfig.model_validate(payload)


def test_llm_config_accepts_long_prompts() -> None:
    """提示词不设长度上限: 规则+示例写长了也必须能存下来."""
    long_prompt = "x" * 20_000
    cfg = LLMConfig(system_prompt=long_prompt, field_prompts={MetadataField.TITLE: long_prompt})
    assert cfg.system_prompt == long_prompt
    assert cfg.field_prompts[MetadataField.TITLE] == long_prompt
