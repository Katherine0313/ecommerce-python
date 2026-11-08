"""客服机器人接口：严格兼容小程序现有契约，前端只改「发消息」这一处。

- POST /api/agent/chat        非流式，返回 {success, data:{reply, contact, matched}}
- POST /api/agent/chat/stream SSE，逐块纯文本 data: 行（保留兼容，小程序默认不走）

所有回答都来自知识库（ai_kb_qa），不调用任何大模型；未命中统一兜底并引导人工客服。
"""

import asyncio
import logging
import time
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel

from app.config import settings
from app.kb.service import answer as kb_answer
from app.services.auth import AuthError, require_user
from app.services.history import normalize_session_id, save_turn
from app.services.rate_limit import check_and_consume
from app.services.sse import sse_pack

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/agent", tags=["客服机器人"])

_ERROR_REPLY = "抱歉，这边暂时有点忙，请稍后再试，或点「人工客服」直接联系我们。"
_RATE_LIMIT_REPLY = "今天聊得有点多啦，休息一会儿再来找我吧。"
_BUSY_REPLY = "现在咨询的人有点多，请稍后再试。"

# 全局并发保护：同时进行的对话数有上限
_chat_semaphore = asyncio.Semaphore(settings.max_concurrent_chats)

# 流式输出时每块的字数：只影响观感，不影响总耗时
_STREAM_CHUNK_CHARS = 14


class ChatRequest(BaseModel):
    # 字段名与前端 JSON 完全一致（camelCase）
    message: str = ""
    sessionId: str = ""
    scene: str | None = None


def _error(status_code: int, message: str) -> JSONResponse:
    return JSONResponse(status_code=status_code, content={"success": False, "message": message})


async def _guard(request: Request) -> tuple[str | None, JSONResponse | None]:
    """鉴权 + 按用户限流；通过返回 (user_id, None)。"""
    try:
        user_id = require_user(request.headers.get("authorization"))
    except AuthError as e:
        return None, _error(e.status_code, e.message)

    allowed, _, _ = await check_and_consume(user_id)
    if not allowed:
        logger.info("用户触发限流 user=%s", user_id)
        return None, _error(429, _RATE_LIMIT_REPLY)
    return user_id, None


def _validate(req: ChatRequest) -> JSONResponse | None:
    message = (req.message or "").strip()
    if not message:
        return _error(400, "消息不能为空")
    if len(message) > settings.max_message_chars:
        return _error(400, f"消息太长了，请控制在 {settings.max_message_chars} 字以内")
    return None


def _data(result: Any) -> dict[str, Any]:
    """对外返回结构：保留 reply，新增 contact / matched，旧前端可忽略新字段。"""
    return {
        "reply": result.reply,
        "contact": result.contact,
        "feedback": result.feedback,
        "matched": result.matched,
    }


@router.post("/chat")
async def chat(req: ChatRequest, request: Request):
    """非流式对话：小程序默认路径。"""
    user_id, blocked = await _guard(request)
    if blocked:
        return blocked
    invalid = _validate(req)
    if invalid:
        return invalid

    user_message = req.message.strip()
    session_id = normalize_session_id(req.sessionId)
    try:
        await asyncio.wait_for(_chat_semaphore.acquire(), timeout=0.2)
    except TimeoutError:
        return _error(503, _BUSY_REPLY)

    try:
        result = await asyncio.wait_for(
            kb_answer(user_message, user_id or "", session_id or ""),
            timeout=settings.stream_total_timeout_seconds,
        )
        await save_turn(session_id, user_message, result.reply)
        return {"success": True, "data": _data(result)}
    except TimeoutError:
        logger.warning("知识库检索超时 sessionId=%s", session_id)
        return _error(504, "响应超时，请稍后再试")
    except Exception:
        logger.exception("客服问答失败 sessionId=%s", session_id)
        return _error(500, "客服服务暂时不可用")
    finally:
        _chat_semaphore.release()


@router.post("/chat/stream")
async def chat_stream(req: ChatRequest, request: Request):
    """流式对话：保留兼容老前端；知识库答案按固定字数切块下发。"""
    user_id, blocked = await _guard(request)
    if blocked:
        return blocked
    invalid = _validate(req)
    if invalid:
        return invalid

    user_message = req.message.strip()
    session_id = normalize_session_id(req.sessionId)
    try:
        await asyncio.wait_for(_chat_semaphore.acquire(), timeout=0.2)
    except TimeoutError:
        return _error(503, _BUSY_REPLY)

    async def event_stream():
        parts: list[str] = []
        started = time.monotonic()
        try:
            async with asyncio.timeout(settings.stream_total_timeout_seconds):
                result = await kb_answer(user_message, user_id or "", session_id or "")
                reply = result.reply or _ERROR_REPLY
                for start in range(0, len(reply), _STREAM_CHUNK_CHARS):
                    chunk = reply[start:start + _STREAM_CHUNK_CHARS]
                    parts.append(chunk)
                    yield sse_pack(chunk)
            logger.info("知识库流式完成 %sms sessionId=%s",
                        int((time.monotonic() - started) * 1000), session_id)
        except TimeoutError:
            logger.warning("知识库流式超时 sessionId=%s", session_id)
            if not parts:
                parts.append(_ERROR_REPLY)
                yield sse_pack(_ERROR_REPLY)
        except Exception:
            logger.exception("知识库流式失败 sessionId=%s", session_id)
            if not parts:
                parts.append(_ERROR_REPLY)
                yield sse_pack(_ERROR_REPLY)
        finally:
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
