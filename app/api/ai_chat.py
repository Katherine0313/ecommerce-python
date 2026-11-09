"""AI 对话接口：商城端 /api/mall/ai/** + 后台端 /api/admin/ai/**。

对齐 docs/ai §5.1：前端统一经 Java 网关转发，Python 校验 JWT 完成最终鉴权。
SSE 事件格式：data: {"type": "...", ...}\\n\\n
"""

import asyncio
import json
import logging
from pathlib import Path
from typing import Any, AsyncGenerator, Optional

from fastapi import APIRouter, File, Form, Header, Query, Request, UploadFile
from fastapi.responses import JSONResponse, Response, StreamingResponse

from app import internal_client
from app.ai import agent, copy as copy_service, image as image_service, repo
from app.config import settings
from app.kb import embedding as kb_embedding, importer as kb_importer, repo as kb_repo
from app.services.auth import AuthError, get_identity, require_admin
from app.services.rate_limit import check_and_consume, check_and_consume_with_window

router = APIRouter(tags=["ai"])

logger = logging.getLogger(__name__)

_SSE_HEADERS = {
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",  # 关 Nginx/网关缓冲，SSE 逐块下发
}


def _event(data: dict[str, Any]) -> str:
    return "data: " + json.dumps(data, ensure_ascii=False) + "\n\n"


def _err_response(exc: AuthError) -> JSONResponse:
    return JSONResponse(status_code=exc.status_code, content={"code": exc.status_code, "message": exc.message, "data": None})


def _fmt(value: Any) -> Optional[str]:
    return value.strftime("%Y-%m-%d %H:%M:%S") if value else None


# ================= 商城端 =================

@router.post("/api/mall/ai/chat/stream")
async def mall_chat_stream(
    request: Request,
    authorization: Optional[str] = Header(default=None),
    x_anonymous_id: Optional[str] = Header(default=None, alias="X-Anonymous-Id"),
):
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
        ident = get_identity(authorization, x_anonymous_id)
    except AuthError as exc:
        return _err_response(exc)
    uid = ident["id"]
    anon_id = ident["anonymous_id"]

    # 已转人工的会话：只落库用户消息，不再调用模型、不计咨询次数
    conv_id = body.get("conversationId")
    conv = repo.get_conversation(int(conv_id)) if str(conv_id or "").isdigit() else None
    if conv and conv["source"] == "mall":
        owned = (uid and conv["user_id"] == uid) or (anon_id and conv["anonymous_id"] == anon_id)
        if owned and conv.get("human_mode"):
            repo.save_message(conv["id"], "user", message, user_id=uid, anonymous_id=anon_id)
            repo.touch_conversation(conv["id"])

            async def gen_human() -> AsyncGenerator[str, None]:
                yield _event({"type": "meta", "conversationId": conv["id"], "humanMode": True})
                yield _event({"type": "done"})

            return StreamingResponse(gen_human(), media_type="text/event-stream", headers=_SSE_HEADERS)

    # 限流：登录用户按 user_id，匿名用户按 anonymous_id
    rate_key = str(uid) if uid else anon_id
    allowed, _, left_day = await check_and_consume(rate_key)
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
            anonymous_id=anon_id,
        ):
            yield _event(ev)

    return StreamingResponse(gen(), media_type="text/event-stream", headers=_SSE_HEADERS)


@router.post("/api/mall/ai/handoff")
async def mall_handoff(
    request: Request,
    authorization: Optional[str] = Header(default=None),
    x_anonymous_id: Optional[str] = Header(default=None, alias="X-Anonymous-Id"),
):
    """转人工：新建或复用会话，标记人工模式，之后该会话不再走模型。"""
    try:
        ident = get_identity(authorization, x_anonymous_id)
    except AuthError as exc:
        return _err_response(exc)
    uid = ident["id"]
    anon_id = ident["anonymous_id"]
    try:
        body = await request.json()
    except Exception:
        body = {}
    message = str(body.get("message") or "").strip()[:2000]

    conv_id = body.get("conversationId")
    conv = repo.get_conversation(int(conv_id)) if str(conv_id or "").isdigit() else None
    owned = (
        conv
        and conv["source"] == "mall"
        and ((uid and conv["user_id"] == uid) or (anon_id and conv["anonymous_id"] == anon_id))
    )
    if not owned:
        role_id = body.get("roleId")
        new_id = repo.create_conversation(
            "mall",
            int(role_id) if str(role_id or "").isdigit() else None,
            None,
            user_id=uid,
            anonymous_id=anon_id,
        )
        if message:
            repo.touch_conversation(new_id, title=message[:30])
        conv = repo.get_conversation(new_id)
    if not conv:
        return JSONResponse(status_code=500, content={"code": 500, "message": "转人工失败", "data": None})

    if message:
        repo.save_message(conv["id"], "user", message, user_id=uid, anonymous_id=anon_id)
    if not conv.get("human_mode"):
        repo.set_human_mode(conv["id"], True)
        repo.save_message(
            conv["id"],
            "assistant",
            "已为您转接人工客服，请直接留言，客服会尽快回复。",
        )
    repo.touch_conversation(conv["id"])
    return {"code": 0, "message": "ok", "data": {"conversationId": conv["id"], "humanMode": True}}


def _mall_owned(conv: Optional[dict], uid: Optional[int], anon_id: Optional[str]) -> bool:
    if not conv or conv["source"] != "mall":
        return False
    if uid:
        return conv["user_id"] == uid
    if anon_id:
        return conv["anonymous_id"] == anon_id
    return False


@router.get("/api/mall/ai/conversations/{conv_id}")
def mall_conversation_detail(
    conv_id: int,
    authorization: Optional[str] = Header(default=None),
    x_anonymous_id: Optional[str] = Header(default=None, alias="X-Anonymous-Id"),
):
    """商城端轮询人工状态用：只返回会话本身，不带消息。"""
    try:
        ident = get_identity(authorization, x_anonymous_id)
    except AuthError as exc:
        return _err_response(exc)
    conv = repo.get_conversation(conv_id)
    if not _mall_owned(conv, ident["id"], ident["anonymous_id"]):
        return JSONResponse(status_code=404, content={"code": 404, "message": "会话不存在", "data": None})
    return {
        "code": 0,
        "message": "ok",
        "data": {
            "id": conv["id"],
            "title": conv["title"],
            "humanMode": bool(conv.get("human_mode")),
            "humanAt": _fmt(conv.get("human_at")),
        },
    }


@router.get("/api/mall/ai/conversations")
def mall_conversations(
    authorization: Optional[str] = Header(default=None),
    x_anonymous_id: Optional[str] = Header(default=None, alias="X-Anonymous-Id"),
):
    try:
        ident = get_identity(authorization, x_anonymous_id)
    except AuthError as exc:
        return _err_response(exc)
    rows = repo.list_conversations("mall", user_id=ident["id"], anonymous_id=ident["anonymous_id"])
    for r in rows:
        r["updatedAt"] = _fmt(r.pop("updated_at"))
        r["roleId"] = r.pop("role_id")
        r["humanMode"] = bool(r.get("human_mode"))
        r.pop("human_mode", None)
    return {"code": 0, "message": "ok", "data": rows}


@router.get("/api/mall/ai/conversations/{conv_id}/messages")
def mall_messages(
    conv_id: int,
    authorization: Optional[str] = Header(default=None),
    x_anonymous_id: Optional[str] = Header(default=None, alias="X-Anonymous-Id"),
):
    try:
        ident = get_identity(authorization, x_anonymous_id)
    except AuthError as exc:
        return _err_response(exc)
    conv = repo.get_conversation(conv_id)
    if not _mall_owned(conv, ident["id"], ident["anonymous_id"]):
        return JSONResponse(status_code=404, content={"code": 404, "message": "会话不存在", "data": None})
    rows = repo.list_messages(conv_id)
    for r in rows:
        r["createdAt"] = _fmt(r.pop("created_at"))
        r["senderAvatar"] = r.pop("sender_avatar", None)
    return {"code": 0, "message": "ok", "data": rows, "humanMode": bool(conv.get("human_mode"))}


@router.delete("/api/mall/ai/conversations/{conv_id}")
def mall_delete_conversation(
    conv_id: int,
    authorization: Optional[str] = Header(default=None),
    x_anonymous_id: Optional[str] = Header(default=None, alias="X-Anonymous-Id"),
):
    try:
        ident = get_identity(authorization, x_anonymous_id)
    except AuthError as exc:
        return _err_response(exc)
    conv = repo.get_conversation(conv_id)
    if not _mall_owned(conv, ident["id"], ident["anonymous_id"]):
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
        r["senderAvatar"] = r.pop("sender_avatar", None)
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
    # 内联模式：新增商品没有 ID，直接带名称/分类/卖点生成，不查库
    inline = body.get("product") if isinstance(body.get("product"), dict) else None
    if not product_id and not (inline and str(inline.get("name") or "").strip()):
        return JSONResponse(status_code=400, content={"code": 400, "message": "缺少 productId 或商品名称", "data": None})
    result = await copy_service.generate(
        product_id=int(product_id) if product_id else None,
        scene=str(body.get("scene") or "all"),
        extra_requirement=body.get("requirement"),
        admin_id=int(payload["sub"]),
        product=None if product_id else inline,
    )
    if not result.get("found"):
        return JSONResponse(status_code=404, content={"code": 404, "message": result.get("message"), "data": None})
    return {"code": 0, "message": "ok", "data": result}


# 允许回填的白名单字段（与 Java CopyUpdateRequest 一致）
_COPY_APPLY_FIELDS = ("name", "subTitle", "title", "description", "image")


@router.post("/api/admin/ai/copy/apply")
async def admin_copy_apply(request: Request, authorization: Optional[str] = Header(default=None)):
    """把 AI 生成结果回填到已有商品（主标题 / 主图等），走 Java 内部接口白名单。"""
    try:
        payload = require_admin(authorization, "ai:copy:generate")
    except AuthError as exc:
        return _err_response(exc)
    try:
        body = await request.json()
    except Exception:
        body = {}
    product_id = body.get("productId")
    fields = body.get("fields") if isinstance(body.get("fields"), dict) else {}
    allowed = {k: str(v).strip() for k, v in fields.items() if k in _COPY_APPLY_FIELDS and str(v or "").strip()}
    if not product_id:
        return JSONResponse(status_code=400, content={"code": 400, "message": "缺少 productId", "data": None})
    if not allowed:
        return JSONResponse(status_code=400, content={"code": 400, "message": "没有可回填的字段", "data": None})
    try:
        await internal_client.update_product(int(product_id), allowed)
    except internal_client.InternalNotFoundError:
        return JSONResponse(status_code=404, content={"code": 404, "message": "商品不存在或已下架", "data": None})
    repo.save_call_log(
        scene="apply_copy", model=None, duration_ms=0, success=True,
        admin_id=int(payload["sub"]), error_msg=f"fields={','.join(allowed)}",
    )
    return {"code": 0, "message": "ok", "data": {"id": int(product_id), "fields": list(allowed)}}


# ================= 后台人工客服 =================

@router.get("/api/admin/ai/human/conversations")
def admin_human_conversations(authorization: Optional[str] = Header(default=None)):
    try:
        require_admin(authorization, "ai:human:use")
    except AuthError as exc:
        return _err_response(exc)
    rows = repo.list_human_conversations()
    data = [
        {
            "id": r["id"],
            "title": r["title"],
            "userId": r["user_id"],
            "userName": r.get("user_name") or f"用户{r['user_id']}",
            "userAvatar": r.get("user_avatar"),
            "humanAt": _fmt(r.get("human_at")),
            "updatedAt": _fmt(r.get("updated_at")),
            "lastMessage": r.get("last_message"),
            "lastType": r.get("last_type"),
            "lastAt": _fmt(r.get("last_at")),
        }
        for r in rows
    ]
    return {"code": 0, "message": "ok", "data": data}


def _human_conv(conv_id: int) -> Optional[dict[str, Any]]:
    """后台只能操作商城来源的会话。"""
    conv = repo.get_conversation(conv_id)
    if not conv or conv["source"] != "mall":
        return None
    return conv


@router.get("/api/admin/ai/human/conversations/{conv_id}/messages")
def admin_human_messages(conv_id: int, authorization: Optional[str] = Header(default=None)):
    try:
        payload = require_admin(authorization, "ai:human:use")
    except AuthError as exc:
        return _err_response(exc)
    conv = _human_conv(conv_id)
    if not conv:
        return JSONResponse(status_code=404, content={"code": 404, "message": "会话不存在", "data": None})
    rows = repo.list_messages(conv_id)
    for r in rows:
        r["createdAt"] = _fmt(r.pop("created_at"))
        r["senderAvatar"] = r.pop("sender_avatar", None)
    return {
        "code": 0,
        "message": "ok",
        "data": rows,
        "humanMode": bool(conv.get("human_mode")),
        "userId": conv["user_id"],
        "adminId": int(payload["sub"]),
    }


@router.post("/api/admin/ai/human/conversations/{conv_id}/reply")
async def admin_human_reply(conv_id: int, request: Request, authorization: Optional[str] = Header(default=None)):
    try:
        payload = require_admin(authorization, "ai:human:use")
    except AuthError as exc:
        return _err_response(exc)
    conv = _human_conv(conv_id)
    if not conv or not conv.get("human_mode"):
        return JSONResponse(status_code=404, content={"code": 404, "message": "会话已结束或不存在", "data": None})
    try:
        body = await request.json()
    except Exception:
        body = {}
    message = str(body.get("message") or "").strip()
    if not message:
        return JSONResponse(status_code=400, content={"code": 400, "message": "消息不能为空", "data": None})
    if len(message) > 2000:
        return JSONResponse(status_code=400, content={"code": 400, "message": "消息过长", "data": None})

    msg_id = repo.save_message(conv_id, "assistant", message, admin_id=int(payload["sub"]))
    repo.touch_conversation(conv_id)
    return {"code": 0, "message": "ok", "data": {"id": msg_id}}


@router.post("/api/admin/ai/human/conversations/{conv_id}/close")
def admin_human_close(conv_id: int, authorization: Optional[str] = Header(default=None)):
    try:
        require_admin(authorization, "ai:human:use")
    except AuthError as exc:
        return _err_response(exc)
    conv = _human_conv(conv_id)
    if not conv:
        return JSONResponse(status_code=404, content={"code": 404, "message": "会话不存在", "data": None})
    if conv.get("human_mode"):
        repo.set_human_mode(conv_id, False)
        repo.save_message(conv_id, "assistant", "人工客服已结束本次会话，如有其他问题可以继续咨询 AI 客服。")
        repo.touch_conversation(conv_id)
    return {"code": 0, "message": "ok", "data": {"conversationId": conv_id, "humanMode": False}}


# 角色列表（两端共用，按 scope 过滤）
@router.get("/api/mall/ai/roles")
def mall_roles(
    authorization: Optional[str] = Header(default=None),
    x_anonymous_id: Optional[str] = Header(default=None, alias="X-Anonymous-Id"),
):
    try:
        get_identity(authorization, x_anonymous_id)
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


# ==================== 知识库管理（运营 / 客服分库） ====================

@router.get("/api/admin/ai/kb")
def admin_kb_list(
    authorization: Optional[str] = Header(default=None),
    scope: Optional[str] = Query(default=None),
    category: Optional[str] = Query(default=None),
    status: Optional[int] = Query(default=None),
    keyword: Optional[str] = Query(default=None),
    page: int = Query(default=1, ge=1),
    pageSize: int = Query(default=20, ge=1, le=100),
):
    try:
        require_admin(authorization, "ai:chat:use")
    except AuthError as exc:
        return _err_response(exc)
    rows = repo.list_kb_entries(
        scope=scope, category=category, status=status, keyword=keyword,
        page=page, page_size=pageSize,
    )
    total = repo.count_kb_entries(scope=scope, category=category, status=status, keyword=keyword)
    for r in rows:
        r["createdAt"] = _fmt(r.pop("created_at"))
        r["updatedAt"] = _fmt(r.pop("updated_at"))
    return {"code": 0, "message": "ok", "data": {"list": rows, "total": total, "page": page, "pageSize": pageSize}}


@router.get("/api/admin/ai/kb/categories")
def admin_kb_categories(
    authorization: Optional[str] = Header(default=None),
    scope: Optional[str] = Query(default=None),
):
    try:
        require_admin(authorization, "ai:chat:use")
    except AuthError as exc:
        return _err_response(exc)
    return {"code": 0, "message": "ok", "data": repo.list_kb_categories(scope)}


@router.post("/api/admin/ai/kb")
async def admin_kb_create(request: Request, authorization: Optional[str] = Header(default=None)):
    try:
        require_admin(authorization, "ai:chat:use")
    except AuthError as exc:
        return _err_response(exc)
    body = await request.json()
    scope = str(body.get("scope") or "客服").strip()
    category = str(body.get("category") or "").strip()
    question = str(body.get("question") or "").strip()
    answer = str(body.get("answer") or "").strip()
    if not question or not answer:
        return JSONResponse(status_code=400, content={"code": 400, "message": "问题和答案不能为空", "data": None})
    if scope not in ("客服", "运营"):
        return JSONResponse(status_code=400, content={"code": 400, "message": "scope 必须为 客服 或 运营", "data": None})
    entry_id = repo.create_kb_entry(
        scope=scope,
        category=category,
        question=question,
        answer=answer,
        keywords=str(body.get("keywords") or "").strip(),
        priority=int(body.get("priority") or 0),
        status=int(body.get("status", 1)),
    )
    return {"code": 0, "message": "ok", "data": {"id": entry_id}}


@router.put("/api/admin/ai/kb/{entry_id}")
async def admin_kb_update(entry_id: int, request: Request, authorization: Optional[str] = Header(default=None)):
    try:
        require_admin(authorization, "ai:chat:use")
    except AuthError as exc:
        return _err_response(exc)
    if not repo.get_kb_entry(entry_id):
        return JSONResponse(status_code=404, content={"code": 404, "message": "条目不存在", "data": None})
    body = await request.json()
    fields = {}
    for k in ("scope", "category", "question", "answer", "keywords", "priority", "status"):
        if k in body:
            fields[k] = body[k]
    if fields.get("scope") and fields["scope"] not in ("客服", "运营"):
        return JSONResponse(status_code=400, content={"code": 400, "message": "scope 必须为 客服 或 运营", "data": None})
    repo.update_kb_entry(entry_id, **fields)
    return {"code": 0, "message": "ok", "data": None}


@router.delete("/api/admin/ai/kb/{entry_id}")
def admin_kb_delete(entry_id: int, authorization: Optional[str] = Header(default=None)):
    try:
        require_admin(authorization, "ai:chat:use")
    except AuthError as exc:
        return _err_response(exc)
    if not repo.get_kb_entry(entry_id):
        return JSONResponse(status_code=404, content={"code": 404, "message": "条目不存在", "data": None})
    repo.delete_kb_entry(entry_id)
    return {"code": 0, "message": "ok", "data": None}


# ==================== 知识库 Excel 批量导入 ====================
# 流程：下载模板 → 上传 preview（落盘归档 + 表头校验 + 逐行预检，不落库）
#       → 前端确认 → confirm 重读归档文件重新解析 → 批量 upsert（不信前端回传的行数据）

def _kb_row_view(row: dict) -> dict:
    """预检结果行只回传展示字段，答案等长字段不给列表。"""
    return {
        "row": row["row"], "scope": row["scope"], "category": row["category"],
        "question": row["question"], "result": row["result"], "reason": row["reason"],
    }


@router.get("/api/admin/ai/kb/import/template")
def admin_kb_import_template(authorization: Optional[str] = Header(default=None)):
    try:
        require_admin(authorization, "ai:chat:use")
    except AuthError as exc:
        return _err_response(exc)
    return Response(
        content=kb_importer.build_template(),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": "attachment; filename=kb_import_template.xlsx"},
    )


@router.post("/api/admin/ai/kb/import/preview")
async def admin_kb_import_preview(
    authorization: Optional[str] = Header(default=None),
    file: UploadFile = File(...),
    scope: str = Form(default="客服"),
):
    try:
        require_admin(authorization, "ai:chat:use")
    except AuthError as exc:
        return _err_response(exc)
    if scope not in kb_importer.SCOPES:
        return JSONResponse(status_code=400, content={"code": 400, "message": "scope 必须为 客服 或 运营", "data": None})
    if not (file.filename or "").lower().endswith(".xlsx"):
        return JSONResponse(status_code=400, content={"code": 400, "message": "只支持 .xlsx 文件，请使用模板", "data": None})
    content = await file.read()
    if len(content) > kb_importer.MAX_FILE_BYTES:
        return JSONResponse(status_code=400, content={"code": 400, "message": "文件不能超过 2MB", "data": None})

    # 先落盘归档（表头校验失败的文件也留档，便于排查），再解析预检
    file_id = kb_importer.archive_upload(content, scope)
    try:
        rows = kb_importer.parse_rows(Path(settings.kb_import_dir) / file_id, scope)
    except kb_importer.ImportReject as exc:
        return JSONResponse(status_code=400, content={
            "code": 400, "message": str(exc), "data": {"importFileId": file_id}})
    kb_importer.mark_existing(rows, kb_repo.list_kb_keys())
    return {"code": 0, "message": "ok", "data": {
        "importFileId": file_id,
        "summary": kb_importer.summarize(rows),
        "rows": [_kb_row_view(r) for r in rows],
    }}


@router.post("/api/admin/ai/kb/import/confirm")
async def admin_kb_import_confirm(request: Request, authorization: Optional[str] = Header(default=None)):
    try:
        payload = require_admin(authorization, "ai:chat:use")
    except AuthError as exc:
        return _err_response(exc)
    try:
        body = await request.json()
    except Exception:
        body = {}
    try:
        path, default_scope = kb_importer.resolve_archive(body.get("importFileId"))
        rows = kb_importer.parse_rows(path, default_scope)
    except kb_importer.ImportReject as exc:
        return JSONResponse(status_code=400, content={"code": 400, "message": str(exc), "data": None})
    kb_importer.mark_existing(rows, kb_repo.list_kb_keys())
    valid = [r for r in rows if r["result"] in ("ok", "updated")]
    if not valid:
        return JSONResponse(status_code=400, content={"code": 400, "message": "没有可导入的有效行", "data": None})

    inserted, updated = kb_repo.upsert_kb_entries_batch(valid)
    # 清全部缓存立即生效（含 load_entries(None) 的全量缓存）；语义索引异步重建（失败有懒加载兜底）
    kb_repo.clear_kb_cache()
    try:
        entries = await kb_repo.load_entries()
        asyncio.create_task(kb_embedding.warm_up(entries))
    except Exception:
        logger.warning("导入后语义索引预热失败，检索将自动懒加载", exc_info=True)
    summary = kb_importer.summarize(rows)
    logger.info("知识库导入完成 file=%s admin=%s 新增=%s 更新=%s 失败=%s 跳过=%s",
                path.name, payload["sub"], inserted, updated, summary["failed"], summary["duplicate"])
    return {"code": 0, "message": "ok", "data": {
        "importFileId": path.name,
        "inserted": inserted,
        "updated": updated,
        "failed": summary["failed"],
        "duplicate": summary["duplicate"],
    }}


# ==================== AI 生图 ====================

@router.post("/api/admin/ai/image/generate")
async def admin_image_generate(request: Request, authorization: Optional[str] = Header(default=None)):
    try:
        payload = require_admin(authorization, "ai:copy:generate")
    except AuthError as exc:
        return _err_response(exc)
    admin_id = int(payload["sub"])
    try:
        body = await request.json()
    except Exception:
        body = {}
    prompt = str(body.get("prompt") or "").strip()
    product_id = body.get("productId")
    # 内联商品信息：新增商品没有 ID 也能生图（与文案内联模式同口径）
    inline = body.get("product") if isinstance(body.get("product"), dict) else None
    if not prompt and not product_id and not (inline and str(inline.get("name") or "").strip()):
        return JSONResponse(status_code=400, content={"code": 400, "message": "缺少画面描述或商品信息", "data": None})

    allowed, left = await check_and_consume_with_window(f"img:{admin_id}", 60, settings.image_rate_per_minute)
    if not allowed:
        return JSONResponse(status_code=429, content={
            "code": 429, "message": f"生图太频繁，每分钟最多 {settings.image_rate_per_minute} 次，请稍后再试", "data": None})
    try:
        result = await image_service.generate_images(
            prompt=prompt,
            scene=str(body.get("scene") or "main"),
            size=body.get("size") or None,
            count=int(body.get("count") or 1),
            product_id=int(product_id) if product_id else None,
            product=None if product_id else inline,
            model_key=body.get("model"),
            admin_id=admin_id,
        )
    except Exception:
        return JSONResponse(status_code=502, content={"code": 502, "message": "图片生成失败，请稍后重试", "data": None})
    return {"code": 0, "message": "ok", "data": result}
