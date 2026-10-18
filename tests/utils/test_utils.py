import pytest

from amane.enums import Language
from amane.utils.language import has_kana, is_ascii_only, needs_llm_translation


@pytest.mark.parametrize(
    "s,expected",
    [
        ("", False),
        ("こんにちは", True),
        ("カタカナ", True),
        ("abc123", False),
        ("テスト123", True),
        ("Hello世界", False),
        ("ﾃｽﾄ", True),  # 半角片假名
        ("中文標題", False),  # 纯汉字: 句子级下不视为日文
    ],
)
def test_has_kana(s, expected):
    assert has_kana(s) == expected


@pytest.mark.parametrize(
    "s,expected",
    [
        ("", False),
        ("Hello, world!", True),
        ("1234567890", True),
        ("This is a test.", True),
        ("こんにちは", False),
        ("テスト123", False),
        ("中文", False),
        ("abc@#%&*()", True),
        ("abc中文", False),
        ("[HD] FC2-PPV-123456", True),  # 方括号必须算 ASCII
        ("Title {4K}", True),  # 花括号
    ],
)
def test_is_ascii_only(s, expected):
    assert is_ascii_only(s) == expected


@pytest.mark.parametrize(
    "s,target,expected",
    [
        # 日文 → 各目标
        ("こんにちは世界", Language.ZH_CN, True),
        ("こんにちは世界", Language.EN, True),
        ("こんにちは世界", Language.JP, False),  # 已是日文
        # 英文 → 各目标
        ("Hello world", Language.ZH_CN, True),
        ("Hello world", Language.JP, True),
        ("Hello world", Language.EN, False),  # 已是英文
        # 中文 (含简繁) → 一律不走 LLM
        ("你好世界", Language.ZH_CN, False),
        ("後愛上你", Language.ZH_CN, False),  # 繁→简由 zhconv 处理
        ("后爱上你", Language.ZH_TW, False),
        # 英文里带全角标点 (弯引号 / 长破折号) 仍算外语: 旧的 is_ascii_only 判据会在这里漏翻
        ("they’re back in LA", Language.ZH_CN, True),
        ("Eva moved — a big time", Language.ZH_CN, True),
        ("they’re back in LA", Language.EN, False),
        # 边界: 空串
        ("", Language.ZH_CN, False),
        # 纯数字/符号: ASCII, 目标非英文时形式上 True 但内容无意义 (调用方按需跳过)
        ("123-456", Language.EN, False),
    ],
)
def test_needs_llm_translation(s, target, expected):
    assert needs_llm_translation(s, target) == expected
