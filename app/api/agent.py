"""导购机器人接口：严格兼容小程序现有契约，前端零改动。

- POST /api/agent/chat        非流式，返回 {success, data:{reply}}
- POST /api/agent/chat/stream SSE，逐块纯文本 data: 行

防止模型额度被滥用：所有对话接口都要求登录令牌，并按用户限流。
"""

import asyncio
import logging
import time
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel

from app.config import settings
from app.graph.guide import guide_graph
from app.services.auth import AuthError, require_user
from app.services.history import load_history, normalize_session_id, save_turn
from app.services.rate_limit import check_and_consume
from app.services.sse import sse_pack

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/agent", tags=["导购机器人"])

# 首字兜底文案：模型或网络异常时至少给用户一句可读的话
_ERROR_REPLY = "抱歉，我这边暂时出了点问题，请稍后再试。"
_RATE_LIMIT_REPLY = "今天聊得有点多啦，休息一会儿再来找我吧。"
_BUSY_REPLY = "现在咨询的人有点多，请稍后再试。"

# 全局并发保护：同时进行的对话数有上限，防止一瞬间打满模型额度
_chat_semaphore = asyncio.Semaphore(settings.max_concurrent_chats)


class ChatRequest(BaseModel):
    # 字段名与前端 JSON 完全一致（camelCase）
    message: str = ""
    sessionId: str = ""
    scene: str | None = None


def _build_state(history: list[dict[str, Any]], user_message: str) -> dict[str, Any]:
    return {
        "messages": history + [{"role": "user", "content": user_message}],
        "products": [],
        "reply": "",
    }


def _error(status_code: int, message: str) -> JSONResponse:
    return JSONResponse(status_code=status_code, content={"success": False, "message": message})


async def _guard(request: Request) -> JSONResponse | None:
    """鉴权 + 按用户限流；通过返回 None。"""
    try:
        user_id = require_user(request.headers.get("authorization"))
    except AuthError as e:
        return _error(e.status_code, e.message)

    allowed, _, _ = await check_and_consume(user_id)
    if not allowed:
        logger.info("用户触发限流 user=%s", user_id)
        return _error(429, _RATE_LIMIT_REPLY)
    return None


@router.post("/chat")
async def chat(req: ChatRequest, request: Request):
    """非流式对话：H5 或低版本微信的降级路径。"""
    blocked = await _guard(request)
    if blocked:
        return blocked

    user_message = (req.message or "").strip()
    if not user_message:
        return _error(400, "消息不能为空")
    if len(user_message) > settings.max_message_chars:
        return _error(400, f"消息太长了，请控制在 {settings.max_message_chars} 字以内")

    session_id = normalize_session_id(req.sessionId)
    try:
        await asyncio.wait_for(_chat_semaphore.acquire(), timeout=0.2)
    except TimeoutError:
        return _error(503, _BUSY_REPLY)

    try:
        history = await load_history(session_id)
        result = await asyncio.wait_for(
            guide_graph.ainvoke(_build_state(history, user_message)),
            timeout=settings.stream_total_timeout_seconds,
        )
        reply = result.get("reply") or ""
        await save_turn(session_id, user_message, reply)
        return {"success": True, "data": {"reply": reply}}
    except TimeoutError:
        logger.warning("AI 非流式超时 sessionId=%s", session_id)
        return _error(504, "AI 响应超时，请稍后再试")
    except Exception:
        logger.exception("AI 非流式对话失败 sessionId=%s", session_id)
        return _error(500, "AI 服务暂时不可用")
    finally:
        _chat_semaphore.release()


@router.post("/chat/stream")
async def chat_stream(req: ChatRequest, request: Request):
    """流式对话：前端逐块拼接，首字越快越好。"""
    blocked = await _guard(request)
    if blocked:
        return blocked

    user_message = (req.message or "").strip()
    if not user_message:
        return _error(400, "消息不能为空")
    if len(user_message) > settings.max_message_chars:
        return _error(400, f"消息太长了，请控制在 {settings.max_message_chars} 字以内")

    session_id = normalize_session_id(req.sessionId)
    try:
        await asyncio.wait_for(_chat_semaphore.acquire(), timeout=0.2)
    except TimeoutError:
        return _error(503, _BUSY_REPLY)

    async def event_stream():
        parts: list[str] = []
        first_chunk = True
        started = time.monotonic()
        iterator = None
        try:
            async with asyncio.timeout(settings.stream_total_timeout_seconds):
                history = await load_history(session_id)
                state = _build_state(history, user_message)
                iterator = guide_graph.astream(state, stream_mode="custom").__aiter__()
                while True:
                    try:
                        if first_chunk:
                            chunk = await asyncio.wait_for(
                                anext(iterator),
                                timeout=settings.first_token_timeout_seconds,
                            )
                        else:
                            chunk = await anext(iterator)
                    except StopAsyncIteration:
                        break
                    if not chunk:
                        continue
                    if first_chunk:
                        logger.info("AI 首字耗时 %sms sessionId=%s",
                                    int((time.monotonic() - started) * 1000), session_id)
                        first_chunk = False
                    parts.append(chunk)
                    yield sse_pack(chunk)
        except TimeoutError:
            # 首字或总时长超时：直接结束，让前端走本地规则兜底
            logger.warning("AI 流式超时 sessionId=%s firstChunk=%s", session_id, first_chunk)
        except Exception:
            logger.exception("AI 流式对话失败 sessionId=%s", session_id)
            # 还没吐出内容时才补一句兜底，避免半截回复后面接一段道歉
            if not parts:
                parts.append(_ERROR_REPLY)
                yield sse_pack(_ERROR_REPLY)
        finally:
            if iterator is not None:
                try:
                    await iterator.aclose()
                except Exception:
                    logger.debug("关闭流式迭代器失败", exc_info=True)
            _chat_semaphore.release()
            await save_turn(session_id, user_message, "".join(parts))

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            # 禁止 Nginx / 代理缓冲，保证逐块下发
            "X-Accel-Buffering": "no",
        },
    )
