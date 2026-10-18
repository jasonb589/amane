"""文本语言判定启发式.

仅服务于"目标语言是否已满足, 是否需要翻译"的决策, 不追求通用语言识别.
适用范围是句子级文本 (标题 / 简介). 该前提下:
- 日文句子必然含假名 (助词 は/が/を/の 或送假名), 故"含假名"足以判定日文;
  纯汉字写法只出现在词级 (标签/人名), 不在适用范围内.
- 中文简繁是字形差异而非语言差异, 大量字简繁同形, 无法也无需在此区分:
  中文内部互转由 zhconv 完成 (幂等), 不经 LLM. 见 amane/llm.
"""

import re

from ..enums import Language

# https://www.compart.com/en/unicode/plane/U+0000
# 平假名 + 片假名. 半角片假名 (U+FF66-FF9D) 一并纳入: 日站数据偶见.
KANA = re.compile(r"[぀-ヿｦ-ﾝ]")

# 仅 ASCII 可见字符: 英文字母, 数字, 空格与常用标点 (含番号常见的方/花括号 [HD] {4K}).
# 命名为 ASCII_ONLY 而非 "英文": 纯数字/纯符号也匹配, 但它们本就无需翻译, 不影响决策.
ASCII_ONLY = re.compile(r"^[a-zA-Z0-9\s.,;:!?()\[\]{}\-\"'`~@#$%^&*+=_/\\|<>]+$")


def has_kana(s: str) -> bool:
    """文本是否含假名 (平/片/半角片假名). 句子级文本中等价于"是日文"."""
    return bool(KANA.search(s))


def is_ascii_only(s: str) -> bool:
    """文本是否仅由 ASCII 可见字符组成. 句子级文本中近似于"是英文"."""
    return bool(ASCII_ONLY.match(s))

# 中日文字符 (CJK 统一表意文字: 基本区 + 扩展 A + 兼容区). 不含假名, 假名由 ``KANA`` 判.
CJK = re.compile(r"[\u4e00-\u9fff\u3400-\u4dbf\uf900-\ufaff]")


def has_cjk(s: str) -> bool:
    """文本是否含中日文字符 (不含假名). 句子级文本中等价于"是中文"."""
    return bool(CJK.search(s))


def needs_llm_translation(s: str, target: Language) -> bool:
    """文本译至 ``target`` 是否需要 LLM (跨语系).

    仅日↔中/英, 英↔中/日 等跨语系才需 LLM. 中文文本 (含简繁互转, 共用字)
    一律返回 ``False`` -- 交由 zhconv 处理或本就无需变更.

    判定顺序: 含假名 → 日文; 含中日文字符 → 中文 (含日文纯汉字这一边角);
    其余一律按外语处理. **不再用「是否纯 ASCII」当判据** —— 一个弯引号 (``’``) 或长破折号
    就足以让整段英文简介被判成中文而跳过翻译 (欧美条目的简介几乎必然踩中).
    """
    if not s:
        return False
    if has_kana(s):
        return target != Language.JP
    if has_cjk(s):
        return False
    return target != Language.EN
