"""AI 数据表 DAO：ai_tool / ai_chat_role / ai_chat_role_tool / ai_chat_conversation / ai_chat_message / ai_call_log。

对齐项目「Java 写业务 / Python 只做编排和模型调用」的分工：
这里只读写 ai_ 前缀的 AI 自有表，商品/订单等业务数据一律走 internal_client。
"""

import json
import logging
import time
from typing import Any, Optional

import pymysql

from app.config import settings

logger = logging.getLogger(__name__)


def _conn():
    return pymysql.connect(
        host=settings.mysql_host,
        port=settings.mysql_port,
        user=settings.db_username,
        password=settings.db_password,
        database=settings.mysql_database,
        charset="utf8mb4",
        autocommit=True,
        cursorclass=pymysql.cursors.DictCursor,
    )


def _query_one(sql: str, args: tuple = ()) -> Optional[dict[str, Any]]:
    conn = _conn()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, args)
            return cur.fetchone()
    finally:
        conn.close()


def _query_all(sql: str, args: tuple = ()) -> list[dict[str, Any]]:
    conn = _conn()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, args)
            return list(cur.fetchall())
    finally:
        conn.close()


def _execute(sql: str, args: tuple = ()) -> int:
    conn = _conn()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, args)
            return cur.lastrowid or cur.rowcount
    finally:
        conn.close()


# ---------- 工具 ----------

def list_enabled_tools() -> list[dict[str, Any]]:
    return _query_all(
        "SELECT id, name, description, perm_code, scope, status FROM ai_tool WHERE status = 1 ORDER BY sort_order"
    )


def list_role_tool_names(role_id: int) -> set[str]:
    rows = _query_all(
        """SELECT t.name FROM ai_chat_role_tool rt
           JOIN ai_tool t ON t.id = rt.tool_id AND t.status = 1
           WHERE rt.role_id = %s""",
        (role_id,),
    )
    return {r["name"] for r in rows}


# ---------- 角色 ----------

def get_role(role_id: int) -> Optional[dict[str, Any]]:
    return _query_one(
        """SELECT id, name, system_message, status FROM ai_chat_role WHERE id = %s""",
        (role_id,),
    )


def list_roles(scope: str, admin_id: Optional[int] = None) -> list[dict[str, Any]]:
    """商城端取公开启用的角色；后台端取公开角色 + 本人私有角色。"""
    if scope == "mall":
        return _query_all(
            """SELECT id, name, avatar, description, category, sort
               FROM ai_chat_role WHERE status = 1 AND public_status = 1 ORDER BY sort"""
        )
    return _query_all(
        """SELECT id, name, avatar, description, category, admin_id, public_status, status, sort
           FROM ai_chat_role WHERE status = 1 AND (public_status = 1 OR admin_id = %s) ORDER BY sort""",
        (admin_id or 0,),
    )


# ---------- 对话 ----------

def create_conversation(
    source: str,
    role_id: Optional[int],
    system_message: Optional[str],
    admin_id: Optional[int] = None,
    user_id: Optional[int] = None,
) -> int:
    return _execute(
        """INSERT INTO ai_chat_conversation
           (title, admin_id, user_id, source, role_id, system_message)
           VALUES ('新对话', %s, %s, %s, %s, %s)""",
        (admin_id, user_id, source, role_id, system_message),
    )


def get_conversation(conv_id: int) -> Optional[dict[str, Any]]:
    return _query_one(
        """SELECT id, title, admin_id, user_id, source, role_id, system_message,
                  temperature, max_tokens, max_contexts, pinned
           FROM ai_chat_conversation WHERE id = %s""",
        (conv_id,),
    )


def list_conversations(
    source: str,
    admin_id: Optional[int] = None,
    user_id: Optional[int] = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    if source == "mall":
        return _query_all(
            """SELECT id, title, role_id, pinned, updated_at FROM ai_chat_conversation
               WHERE source = 'mall' AND user_id = %s
               ORDER BY pinned DESC, updated_at DESC LIMIT %s""",
            (user_id, limit),
        )
    return _query_all(
        """SELECT id, title, role_id, pinned, updated_at FROM ai_chat_conversation
           WHERE source = 'admin' AND admin_id = %s
           ORDER BY pinned DESC, updated_at DESC LIMIT %s""",
        (admin_id, limit),
    )


def touch_conversation(conv_id: int, title: Optional[str] = None) -> None:
    if title:
        _execute(
            "UPDATE ai_chat_conversation SET title = %s WHERE id = %s",
            (title, conv_id),
        )
    else:
        _execute(
            "UPDATE ai_chat_conversation SET updated_at = NOW() WHERE id = %s",
            (conv_id,),
        )


def delete_conversation(conv_id: int) -> None:
    _execute("DELETE FROM ai_chat_message WHERE conversation_id = %s", (conv_id,))
    _execute("DELETE FROM ai_chat_conversation WHERE id = %s", (conv_id,))


# ---------- 消息 ----------

def save_message(
    conv_id: int,
    msg_type: str,
    content: Optional[str],
    admin_id: Optional[int] = None,
    user_id: Optional[int] = None,
    role_id: Optional[int] = None,
    tool_calls: Optional[list] = None,
    tool_call_id: Optional[str] = None,
    tool_name: Optional[str] = None,
) -> int:
    return _execute(
        """INSERT INTO ai_chat_message
           (conversation_id, admin_id, user_id, role_id, type, content, tool_calls, tool_call_id, tool_name)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)""",
        (
            conv_id,
            admin_id,
            user_id,
            role_id,
            msg_type,
            content,
            json.dumps(tool_calls, ensure_ascii=False) if tool_calls else None,
            tool_call_id,
            tool_name,
        ),
    )


def load_history(conv_id: int, max_messages: int) -> list[dict[str, Any]]:
    """读最近 N 条消息，转成 OpenAI messages 格式（含 tool 消息配对）。"""
    rows = _query_all(
        """SELECT type, content, tool_calls, tool_call_id, tool_name
           FROM (SELECT * FROM ai_chat_message WHERE conversation_id = %s ORDER BY id DESC LIMIT %s) t
           ORDER BY t.id""",
        (conv_id, max_messages),
    )
    messages: list[dict[str, Any]] = []
    for r in rows:
        t = r["type"]
        if t == "user":
            messages.append({"role": "user", "content": r["content"] or ""})
        elif t == "assistant":
            msg: dict[str, Any] = {"role": "assistant", "content": r["content"] or ""}
            if r["tool_calls"]:
                try:
                    msg["tool_calls"] = json.loads(r["tool_calls"])
                except (TypeError, ValueError):
                    pass
            messages.append(msg)
        elif t == "tool":
            messages.append({
                "role": "tool",
                "tool_call_id": r["tool_call_id"] or "",
                "name": r["tool_name"] or "",
                "content": r["content"] or "",
            })
    return messages


def list_messages(conv_id: int, limit: int = 200) -> list[dict[str, Any]]:
    """前端拉历史：只要 user / assistant 两种，tool 消息不展示。"""
    return _query_all(
        """SELECT id, type, content, created_at FROM ai_chat_message
           WHERE conversation_id = %s AND type IN ('user', 'assistant')
           ORDER BY id LIMIT %s""",
        (conv_id, limit),
    )


# ---------- 审计 ----------

def save_call_log(
    scene: str,
    model: Optional[str],
    duration_ms: int,
    success: bool,
    admin_id: Optional[int] = None,
    user_id: Optional[int] = None,
    conversation_id: Optional[int] = None,
    prompt_tokens: Optional[int] = None,
    completion_tokens: Optional[int] = None,
    total_tokens: Optional[int] = None,
    error_msg: Optional[str] = None,
) -> None:
    try:
        _execute(
            """INSERT INTO ai_call_log
               (admin_id, user_id, conversation_id, scene, model,
                prompt_tokens, completion_tokens, total_tokens, duration_ms, success, error_msg)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
            (
                admin_id,
                user_id,
                conversation_id,
                scene,
                model,
                prompt_tokens,
                completion_tokens,
                total_tokens,
                duration_ms,
                1 if success else 0,
                (error_msg or "")[:500] or None,
            ),
        )
    except Exception:
        logger.warning("AI 调用日志写入失败 scene=%s", scene, exc_info=True)


def monotonic_ms() -> int:
    return int(time.monotonic() * 1000)
