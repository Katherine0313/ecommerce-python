"""Redis 会话历史：沿用 Java 版的 key 与格式，切换后端时旧会话也能继续用。

key: agent:chat:history:{sessionId}
list 元素: "user:xxx" / "assistant:xxx"，保留最近 12 条，TTL 2 小时。
Redis 故障时全部降级为单轮，不影响对话主流程。
"""

import logging
from typing import Any, Optional

from app.services.redis_client import get_client

logger = logging.getLogger(__name__)

_HISTORY_PREFIX = "agent:chat:history:"
_MAX_MESSAGES = 12
_TTL_SECONDS = 2 * 60 * 60


def normalize_session_id(session_id: Optional[str]) -> Optional[str]:
    """去空格、截断 64 字符；空值视为单轮。"""
    if not session_id or not session_id.strip():
        return None
    return session_id.strip()[:64]


def _key(session_id: str) -> str:
    return _HISTORY_PREFIX + session_id


async def load_history(session_id: Optional[str]) -> list[dict[str, Any]]:
    """读取最近历史，转成 OpenAI messages 格式。"""
    sid = normalize_session_id(session_id)
    if not sid:
        return []
    try:
        rows = await get_client().lrange(_key(sid), -_MAX_MESSAGES, -1)
    except Exception:
        logger.warning("读取会话历史失败，降级为单轮 sessionId=%s", sid, exc_info=True)
        return []

    messages: list[dict[str, Any]] = []
    for row in rows or []:
        if row.startswith("user:"):
            messages.append({"role": "user", "content": row[5:]})
        elif row.startswith("assistant:"):
            messages.append({"role": "assistant", "content": row[10:]})
    return messages


async def save_turn(session_id: Optional[str], user_message: str, reply: str) -> None:
    """保存本轮问答，只保留最近 N 条。"""
    sid = normalize_session_id(session_id)
    if not sid or not reply:
        return
    try:
        client = get_client()
        key = _key(sid)
        # pipeline 减少往返
        async with client.pipeline(transaction=False) as pipe:
            pipe.rpush(key, "user:" + user_message)
            pipe.rpush(key, "assistant:" + reply)
            pipe.ltrim(key, -_MAX_MESSAGES, -1)
            pipe.expire(key, _TTL_SECONDS)
            await pipe.execute()
    except Exception:
        logger.warning("保存会话历史失败 sessionId=%s", sid, exc_info=True)
