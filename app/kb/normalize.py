"""知识库文本归一化：检索前统一去掉噪声，保证规则打分稳定。"""

import re
import unicodedata

# 只保留中文、英文、数字；标点、空白、表情全部剔除
_NOISE = re.compile(r"[^0-9a-z\u4e00-\u9fff]+")


def normalize(text: str) -> str:
    """全角转半角、转小写、去标点与空白。"""
    value = unicodedata.normalize("NFKC", str(text or "")).lower()
    return _NOISE.sub("", value)


def bigrams(text: str) -> set[str]:
    """字二元组，用于 Dice 相似度；单字文本退化为集合本身。"""
    if not text:
        return set()
    if len(text) == 1:
        return {text}
    return {text[i:i + 2] for i in range(len(text) - 1)}


def split_keywords(raw: str) -> list[str]:
    """兼容中英文逗号、顿号、分号、竖线的关键词拆分。"""
    text = str(raw or "")
    for sep in ("，", "、", ";", "；", "|", " "):
        text = text.replace(sep, ",")
    return [item.strip() for item in text.split(",") if item.strip()]
