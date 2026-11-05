"""纯规则检索：不接大模型、不接 embedding。

打分规则（与 docs/客服机器人知识库接入说明.md 保持一致）：
- 完全一致：100
- 用户问法包含标准问题：92
- 关键词命中：2 字 +55，3 字及以上 +65，累计上限 100
- 字二元组 Dice 相似度：×60
- 上一轮命中同一分类：+8
阈值 55，低于阈值视为未命中。
"""

from dataclasses import dataclass, field

from app.kb.normalize import bigrams, normalize, split_keywords

EXACT_SCORE = 100
CONTAINED_SCORE = 92
KEYWORD_SHORT_SCORE = 55
KEYWORD_LONG_SCORE = 65
KEYWORD_SCORE_CAP = 100
DICE_WEIGHT = 60
SAME_CATEGORY_BONUS = 8
THRESHOLD = 55


@dataclass
class KbEntry:
    id: int
    category: str
    question: str
    answer: str
    keywords: list[str]
    priority: int = 0
    hit_count: int = 0
    norm_question: str = ""
    grams: set[str] = field(default_factory=set)

    def __post_init__(self) -> None:
        self.keywords = [normalize(k) for k in split_keywords(",".join(self.keywords))]
        self.keywords = [k for k in self.keywords if len(k) >= 2]
        self.norm_question = normalize(self.question)
        self.grams = bigrams(self.norm_question)


@dataclass
class MatchResult:
    entry: KbEntry
    score: float


def _dice(left: set[str], right: set[str]) -> float:
    """Dice 系数：交集 ×2 / 两边长度之和。"""
    if not left or not right:
        return 0.0
    return 2 * len(left & right) / (len(left) + len(right))


def score_entry(entry: KbEntry, user_text: str, user_grams: set[str],
                last_category: str = "") -> float:
    """给单条知识打分；user_text / user_grams 是归一化后的用户输入。"""
    question = entry.norm_question
    if not question:
        return 0.0
    if user_text == question:
        return float(EXACT_SCORE)
    if question in user_text or user_text in question:
        return float(CONTAINED_SCORE)

    score = 0.0
    keyword_score = 0
    for keyword in entry.keywords:
        if keyword in user_text:
            length = len(keyword)
            keyword_score += KEYWORD_LONG_SCORE if length >= 3 else KEYWORD_SHORT_SCORE
            if keyword_score >= KEYWORD_SCORE_CAP:
                break
    score += min(keyword_score, KEYWORD_SCORE_CAP)
    score += _dice(user_grams, entry.grams) * DICE_WEIGHT
    if last_category and entry.category == last_category:
        score += SAME_CATEGORY_BONUS
    return score


def match_best(entries: list[KbEntry], user_message: str,
               last_category: str = "") -> MatchResult | None:
    """返回得分最高的知识条；同分时 priority 大的优先，再照顾命中少的。"""
    user_text = normalize(user_message)
    if not user_text or not entries:
        return None
    user_grams = bigrams(user_text)

    best: MatchResult | None = None
    for entry in entries:
        score = score_entry(entry, user_text, user_grams, last_category)
        if score <= 0:
            continue
        if best is None or score > best.score:
            best = MatchResult(entry, score)
        elif score == best.score:
            current = (entry.priority, -entry.hit_count)
            picked = (best.entry.priority, -best.entry.hit_count)
            if current > picked:
                best = MatchResult(entry, score)
    return best
