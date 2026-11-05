"""客服问答编排：意图拦截 → 知识库检索 → 命中返回 / 未命中转人工。"""

import logging
from dataclasses import dataclass

from app.kb.matcher import THRESHOLD, match_best
from app.kb.repo import load_entries, record_hit, record_miss
from app.services.redis_client import get_client

logger = logging.getLogger(__name__)

# 兜底与转人文案：小程序气泡直接展示，不要 Markdown
HUMAN_REPLY = "好的，正在为您转接人工客服，请稍等～"
COMPLAINT_REPLY = "非常抱歉给您带来不好的体验，我马上为您转接售后专员跟进，请稍等。"
FALLBACK_REPLY = "不好意思，这个我不太确定，您可以直接联系人工客服，他们会更快帮您解决。"

# 明确要人：命中后不查库、直接转人工
_HUMAN_WORDS = ("人工", "真人", "转人工", "人工客服", "找客服", "人工服务", "转客服")
# 投诉/纠纷：同样转人工，但话术体现歉意
_COMPLAINT_WORDS = ("投诉", "差评", "举报", "纠纷", "维权", "骗子", "欺诈")

_LAST_CATEGORY_PREFIX = "agent:kb:lastcat:"
_LAST_CATEGORY_TTL = 2 * 60 * 60


@dataclass
class KbAnswer:
    reply: str
    contact: bool = False
    feedback: bool = False
    matched: bool = False
    category: str = ""
    score: float = 0.0
    entry_id: int = 0
    intent: str = ""


def detect_intent(message: str) -> str:
    """识别人工 / 投诉意图；普通咨询返回空串。"""
    text = str(message or "")
    if any(word in text for word in _COMPLAINT_WORDS):
        return "complaint"
    if any(word in text for word in _HUMAN_WORDS):
        return "human"
    return ""


def _last_category_key(session_id: str) -> str:
    return _LAST_CATEGORY_PREFIX + session_id


async def _get_last_category(session_id: str) -> str:
    if not session_id:
        return ""
    try:
        return await get_client().get(_last_category_key(session_id)) or ""
    except Exception:
        logger.debug("读取上一轮分类失败 sessionId=%s", session_id, exc_info=True)
        return ""


async def _set_last_category(session_id: str, category: str) -> None:
    if not session_id or not category:
        return
    try:
        await get_client().set(
            _last_category_key(session_id), category, ex=_LAST_CATEGORY_TTL
        )
    except Exception:
        logger.debug("保存上一轮分类失败 sessionId=%s", session_id, exc_info=True)


async def answer(message: str, user_id: str = "", session_id: str = "") -> KbAnswer:
    """回答一条用户消息：知识库优先，未命中统一兜底转人工。"""
    intent = detect_intent(message)
    if intent == "human":
        return KbAnswer(reply=HUMAN_REPLY, contact=True, intent=intent)
    if intent == "complaint":
        return KbAnswer(reply=COMPLAINT_REPLY, contact=True, intent=intent)

    entries = await load_entries()
    last_category = await _get_last_category(session_id)
    best = match_best(entries, message, last_category)

    if best and best.score >= THRESHOLD:
        await record_hit(best.entry.id)
        await _set_last_category(session_id, best.entry.category)
        logger.info(
            "知识库命中 id=%s category=%s score=%s question=%s",
            best.entry.id, best.entry.category, round(best.score, 1), best.entry.question,
        )
        return KbAnswer(
            reply=best.entry.answer,
            matched=True,
            category=best.entry.category,
            score=best.score,
            entry_id=best.entry.id,
        )

    best_score = best.score if best else 0.0
    logger.info("知识库未命中 message=%s bestScore=%s", message, round(best_score, 1))
    await record_miss(message, user_id, session_id, best_score)
    return KbAnswer(
        reply=FALLBACK_REPLY,
        contact=True,
        feedback=True,
        score=best_score,
    )
