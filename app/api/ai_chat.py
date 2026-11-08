"""AI 对话接口：商城端 /api/mall/ai/** + 后台端 /api/admin/ai/**。

对齐 docs/ai §5.1：前端统一经 Java 网关转发，Python 校验 JWT 完成最终鉴权。
SSE 事件格式：data: {"type": "...", ...}\\n\\n
"""

import json
from typing import Any, AsyncGenerator, Optional

from fastapi import APIRouter, Header, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse

from app.ai import agent, copy as copy_service, repo
from app.services.auth import AuthError, require_admin, require_user
from app.services.rate_limit import check_and_consume

router = APIRouter(tags=["ai"])

_SSE_HEADERS = {
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",  # 关 Nginx/网关缓冲，SSE 逐块下发
}


def _event(data: dict[str, Any]) -> str:
    return "data: " + json.dumps(data, ensure_ascii=False) + "\n\n"


def _err_response(exc: AuthError) -> JSONResponse:
    return JSONResponse(status_code=exc.status, content={"code": exc.status, "message": exc.message, "data": None})


# ================= 商城端 =================

@router.post("/api/mall/ai/chat/stream")
async def mall_chat_stream(request: Request, authorization: Optional[str] = Header(default=None)):
    try:
        body = await request.json()
    except Exception:
        body = {}
    message = str(body.get("message") or "").strip()
    if not message:
        return JSONResponse(status_code=400, content={"code": 400, "message": "消息不能为空", "data": None})
    if len(message) > 2000:
        return JSONResponse(status_code=400, content={"code": 400, "message": "消息过长", "data": None})

    try:
        uid = int(require_user(authorization))
    except AuthError as exc:
        return _err_response(exc)

    allowed, _, left_day = await check_and_consume(str(uid))
    if not allowed:
        return JSONResponse(
            status_code=429,
            content={"code": 429, "message": f"今日咨询次数已达上限（{left_day} 次剩余），明天再来吧", "data": None},
        )

    async def gen() -> AsyncGenerator[str, None]:
        async for ev in agent.chat_stream(
            message=message,
            source="mall",
            conversation_id=body.get("conversationId"),
            role_id=body.get("roleId"),
            user_id=uid,
        ):
            yield _event(ev)

    return StreamingResponse(gen(), media_type="text/event-stream", headers=_SSE_HEADERS)


@router.get("/api/mall/ai/conversations")
def mall_conversations(authorization: Optional[str] = Header(default=None)):
    try:
        uid = int(require_user(authorization))
    except AuthError as exc:
        return _err_response(exc)
    rows = repo.list_conversations("mall", user_id=uid)
    for r in rows:
        r["updatedAt"] = r.pop("updated_at").strftime("%Y-%m-%d %H:%M:%S") if r.get("updated_at") else None
        r["roleId"] = r.pop("role_id")
    return {"code": 0, "message": "ok", "data": rows}


@router.get("/api/mall/ai/conversations/{conv_id}/messages")
def mall_messages(conv_id: int, authorization: Optional[str] = Header(default=None)):
    try:
        uid = int(require_user(authorization))
    except AuthError as exc:
        return _err_response(exc)
    conv = repo.get_conversation(conv_id)
    if not conv or conv["source"] != "mall" or conv["user_id"] != uid:
        return JSONResponse(status_code=404, content={"code": 404, "message": "会话不存在", "data": None})
    rows = repo.list_messages(conv_id)
    for r in rows:
        r["createdAt"] = r.pop("created_at").strftime("%Y-%m-%d %H:%M:%S") if r.get("created_at") else None
    return {"code": 0, "message": "ok", "data": rows}


@router.delete("/api/mall/ai/conversations/{conv_id}")
def mall_delete_conversation(conv_id: int, authorization: Optional[str] = Header(default=None)):
    try:
        uid = int(require_user(authorization))
    except AuthError as exc:
        return _err_response(exc)
    conv = repo.get_conversation(conv_id)
    if not conv or conv["source"] != "mall" or conv["user_id"] != uid:
        return JSONResponse(status_code=404, content={"code": 404, "message": "会话不存在", "data": None})
    repo.delete_conversation(conv_id)
    return {"code": 0, "message": "ok", "data": None}


# ================= 后台端 =================

@router.post("/api/admin/ai/chat/stream")
async def admin_chat_stream(request: Request, authorization: Optional[str] = Header(default=None)):
    try:
        payload = require_admin(authorization, "ai:chat:use")
    except AuthError as exc:
        return _err_response(exc)
    admin_id = int(payload["sub"])
    admin_perms = payload.get("permissions") or []

    try:
        body = await request.json()
    except Exception:
        body = {}
    message = str(body.get("message") or "").strip()
    if not message:
        return JSONResponse(status_code=400, content={"code": 400, "message": "消息不能为空", "data": None})

    async def gen() -> AsyncGenerator[str, None]:
        async for ev in agent.chat_stream(
            message=message,
            source="admin",
            conversation_id=body.get("conversationId"),
            role_id=body.get("roleId"),
            admin_id=admin_id,
            admin_perms=admin_perms,
        ):
            yield _event(ev)

    return StreamingResponse(gen(), media_type="text/event-stream", headers=_SSE_HEADERS)


@router.get("/api/admin/ai/conversations")
def admin_conversations(authorization: Optional[str] = Header(default=None)):
    try:
        payload = require_admin(authorization, "ai:chat:use")
    except AuthError as exc:
        return _err_response(exc)
    rows = repo.list_conversations("admin", admin_id=int(payload["sub"]))
    for r in rows:
        r["updatedAt"] = r.pop("updated_at").strftime("%Y-%m-%d %H:%M:%S") if r.get("updated_at") else None
        r["roleId"] = r.pop("role_id")
    return {"code": 0, "message": "ok", "data": rows}


@router.get("/api/admin/ai/conversations/{conv_id}/messages")
def admin_messages(conv_id: int, authorization: Optional[str] = Header(default=None)):
    try:
        payload = require_admin(authorization, "ai:chat:use")
    except AuthError as exc:
        return _err_response(exc)
    conv = repo.get_conversation(conv_id)
    if not conv or conv["source"] != "admin" or conv["admin_id"] != int(payload["sub"]):
        return JSONResponse(status_code=404, content={"code": 404, "message": "会话不存在", "data": None})
    rows = repo.list_messages(conv_id)
    for r in rows:
        r["createdAt"] = r.pop("created_at").strftime("%Y-%m-%d %H:%M:%S") if r.get("created_at") else None
    return {"code": 0, "message": "ok", "data": rows}


@router.delete("/api/admin/ai/conversations/{conv_id}")
def admin_delete_conversation(conv_id: int, authorization: Optional[str] = Header(default=None)):
    try:
        payload = require_admin(authorization, "ai:chat:use")
    except AuthError as exc:
        return _err_response(exc)
    conv = repo.get_conversation(conv_id)
    if not conv or conv["source"] != "admin" or conv["admin_id"] != int(payload["sub"]):
        return JSONResponse(status_code=404, content={"code": 404, "message": "会话不存在", "data": None})
    repo.delete_conversation(conv_id)
    return {"code": 0, "message": "ok", "data": None}


@router.post("/api/admin/ai/copy/generate")
async def admin_copy_generate(request: Request, authorization: Optional[str] = Header(default=None)):
    try:
        payload = require_admin(authorization, "ai:copy:generate")
    except AuthError as exc:
        return _err_response(exc)
    try:
        body = await request.json()
    except Exception:
        body = {}
    product_id = body.get("productId")
    if not product_id:
        return JSONResponse(status_code=400, content={"code": 400, "message": "缺少 productId", "data": None})
    result = await copy_service.generate(
        product_id=int(product_id),
        scene=str(body.get("scene") or "all"),
        extra_requirement=body.get("requirement"),
        admin_id=int(payload["sub"]),
    )
    if not result.get("found"):
        return JSONResponse(status_code=404, content={"code": 404, "message": result.get("message"), "data": None})
    return {"code": 0, "message": "ok", "data": result}


# 角色列表（两端共用，按 scope 过滤）
@router.get("/api/mall/ai/roles")
def mall_roles(authorization: Optional[str] = Header(default=None)):
    try:
        require_user(authorization)
    except AuthError as exc:
        return _err_response(exc)
    return {"code": 0, "message": "ok", "data": repo.list_roles("mall")}


@router.get("/api/admin/ai/roles")
def admin_roles(authorization: Optional[str] = Header(default=None)):
    try:
        payload = require_admin(authorization, "ai:chat:use")
    except AuthError as exc:
        return _err_response(exc)
    return {"code": 0, "message": "ok", "data": repo.list_roles("admin", admin_id=int(payload["sub"]))}
