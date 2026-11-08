"""AI 对话编排：LangGraph StateGraph + SSE 事件流。

图结构：
  intent(意图识别)
    ├─ chat   → 固定话术直接回复
    ├─ danger → 交给 LLM（update_product 工具内部挂起为待确认动作）
    └─ llm    → decide(LLM+工具决策) ⇄ tools(执行) → 汇总回复

流式约定（对齐 docs/ai §6.3）：工具调用阶段不流式，工具执行完成后流式输出最终回复。
SSE 事件：meta / token / products / confirm / done / error。
待确认动作存 Redis（key: ai:pending:{conv_id}，5 分钟 TTL），用户回复「确认」后执行。
"""

import json
import logging
import re
from typing import Any, AsyncGenerator, Optional, TypedDict

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage
from langgraph.graph import END, StateGraph

from app import internal_client
from app.ai import intent as intent_mod
from app.ai import repo, tools as ai_tools
from app.ai.client import get_llm
from app.config import settings
from app.services.redis_client import get_client

logger = logging.getLogger(__name__)

_MAX_TOOL_ROUNDS = 3
_PENDING_KEY = "ai:pending:"
_PENDING_TTL = 300

_CONFIRM_WORDS = re.compile(r"^(确认|确定|好的|好|是的|对|ok|yes|执行)[!！。.\s]*$", re.IGNORECASE)
_CANCEL_WORDS = re.compile(r"^(取消|算了|不用|别|no|不)[!！。.\s]*$", re.IGNORECASE)

# 工具调用强约束：追加在角色提示词之后，覆盖所有角色，避免模型用通用政策话术
# 回答本该查真实数据的问题（如「我的最新订单发货了吗」只回一段发货时效规则）。
_TOOL_RULES = """
【工具调用强制规则，优先级高于上文】
1. 用户提到"我的订单""我的订单记录""订单状态""我的物流""发货了吗""我买的东西"等，必须先调用 query_order；
2. 用户提到"我的售后""退款进度""退货进度""我的工单"等，必须先调用 query_aftersale；
3. 用户询问某商品库存/有没有货/还剩多少，必须先调用 query_stock；
4. 用户询问具体商品的价格、参数、详情，必须先调用 query_product 或 search_product；
5. 只有与具体订单/商品无关的通用问题（退换货政策、发货时效规则、支付方式、发票规则等）才用 search_knowledge；
6. 禁止在未调用工具的情况下编造订单号、物流单号、库存、价格等任何业务数据；
7. 工具返回空结果时如实告知"没查到相关记录"，并给出下一步建议，不要用政策话术替代；
8. 用户按品类找商品（"推荐手机""有什么平板""看看相机"）时，search_products / recommend_product 的关键词只传品类名本身（手机/电脑/平板/耳机音箱/手表/相机/数码配件/智能家居），不要传"推荐手机"这类整句、不要加"推荐"二字，否则会把手机壳等配件一起搜出来；
9. 用户给出订单号（如"订单号 2026xxx，发货了吗"）时，必须把订单号原样作为 query_order 的 keyword 传入；给出商品名时同理传商品名；只有完全没提订单、也没提商品时才不传 keyword（默认查最近 5 笔）。
"""


# ---------- LangGraph 状态 ----------

class AgentState(TypedDict, total=False):
    messages: list[BaseMessage]
    allowed_tools: set[str]
    intent: str
    fixed_reply: Optional[str]
    answer: Optional[str]          # decide 节点直接产出的非流式答案
    need_summary: bool             # 工具执行过 → 需要再流式汇总
    ui_events: list[dict]          # 商品卡片等 UI 事件
    pending_action: Optional[dict]
    tool_rounds: int
    error: Optional[str]


# ---------- 节点 ----------

def _intent_node(state: AgentState) -> AgentState:
    last_user = ""
    for m in reversed(state["messages"]):
        if isinstance(m, HumanMessage):
            last_user = str(m.content)
            break
    it = intent_mod.classify(last_user)
    out: AgentState = {"intent": it}
    if it == "chat":
        out["fixed_reply"] = intent_mod.CHAT_REPLY
    return out


def _route_intent(state: AgentState) -> str:
    return "end" if state.get("fixed_reply") else "decide"


def _decide(state: AgentState) -> AgentState:
    """LLM + bind_tools 决策：返回工具调用则进 tools，否则产出答案。"""
    allowed = state.get("allowed_tools") or set()
    llm_tools = ai_tools.resolve_tools(allowed)
    llm = get_llm()
    if llm_tools:
        llm = llm.bind_tools(llm_tools)
    resp = llm.invoke(state["messages"])
    if isinstance(resp, AIMessage) and resp.tool_calls:
        return {"messages": [resp], "tool_rounds": state.get("tool_rounds", 0) + 1}
    return {"messages": [resp], "answer": str(resp.content or "")}


def _route_decide(state: AgentState) -> str:
    if state.get("answer") is not None:
        return "end"
    if state.get("tool_rounds", 0) > _MAX_TOOL_ROUNDS:
        return "end"  # 超轮次兜底：拿现有上下文直接出答案
    return "tools"


async def _tools_node(state: AgentState) -> AgentState:
    """执行工具调用；收集 UI 事件与待确认动作。"""
    last = state["messages"][-1]
    if not isinstance(last, AIMessage) or not last.tool_calls:
        return {"need_summary": True}

    allowed = state.get("allowed_tools") or set()
    tool_msgs: list[ToolMessage] = []
    ui_events: list[dict] = list(state.get("ui_events") or [])
    pending = state.get("pending_action")

    for call in last.tool_calls:
        name = call.get("name")
        args = call.get("args") or {}
        tool_fn = ai_tools.ALL_TOOLS.get(name)

        if not tool_fn or name not in allowed:
            result = json.dumps({"ok": False, "message": "该功能暂不可用"}, ensure_ascii=False)
        else:
            try:
                result = await tool_fn.ainvoke(args)
            except Exception as exc:
                logger.warning("工具执行失败 %s args=%s", name, args, exc_info=True)
                result = json.dumps({"ok": False, "message": f"查询失败：{exc}"}, ensure_ascii=False)

        # 抽取 UI 事件（不进入大模型上下文的部分由 _ui 承载）
        try:
            parsed = json.loads(result)
            ui = parsed.pop("_ui", None)
            if ui and ui.get("type") == "product_list":
                ui_events.append({"type": "products", "items": ui.get("items") or []})
                result = json.dumps(parsed, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            pass

        tool_msgs.append(ToolMessage(content=result, tool_call_id=call.get("id") or "", name=name))

    ctx_pending = ai_tools._ctx().get("pending_action")
    if ctx_pending:
        pending = ctx_pending

    return {
        "messages": tool_msgs,
        "ui_events": ui_events,
        "pending_action": pending,
        "need_summary": True,
    }


def _build_graph():
    graph = StateGraph(AgentState)
    graph.add_node("intent", _intent_node)
    graph.add_node("decide", _decide)
    graph.add_node("tools", _tools_node)
    graph.set_entry_point("intent")
    graph.add_conditional_edges("intent", _route_intent, {"end": END, "decide": "decide"})
    graph.add_conditional_edges("decide", _route_decide, {"end": END, "tools": "tools"})
    graph.add_edge("tools", "decide")
    return graph.compile()


_graph = _build_graph()


# ---------- 待确认动作（Redis） ----------

async def _load_pending(conv_id: int) -> Optional[dict]:
    try:
        raw = await get_client().get(f"{_PENDING_KEY}{conv_id}")
        return json.loads(raw) if raw else None
    except Exception:
        return None


async def _save_pending(conv_id: int, action: dict) -> None:
    try:
        await get_client().set(f"{_PENDING_KEY}{conv_id}", json.dumps(action, ensure_ascii=False), ex=_PENDING_TTL)
    except Exception:
        logger.warning("待确认动作写入失败 conv=%s", conv_id, exc_info=True)


async def _clear_pending(conv_id: int) -> None:
    try:
        await get_client().delete(f"{_PENDING_KEY}{conv_id}")
    except Exception:
        pass


# ---------- 权限过滤 ----------

def _filter_allowed_tools(
    source: str,
    role_tool_names: set[str],
    admin_perms: Optional[list[str]],
) -> set[str]:
    """两层过滤：角色绑定工具 ∩ 用户权限。商城用户无权限概念，直接用角色绑定 ∩ scope。"""
    enabled = {t["name"]: t for t in repo.list_enabled_tools()}
    # scope 过滤
    scoped = {n for n, t in enabled.items() if t["scope"] in ("both", source)}
    allowed = role_tool_names & scoped
    if source == "admin":
        # 后台再叠一层 JWT 权限码；超管 *:*:* 不过滤
        if admin_perms is not None and "*:*:*" not in admin_perms:
            perm_set = set(admin_perms)
            allowed = {n for n in allowed if enabled[n].get("perm_code") in perm_set}
    return allowed


# ---------- 主入口 ----------

async def chat_stream(
    message: str,
    source: str,
    conversation_id: Optional[int] = None,
    role_id: Optional[int] = None,
    user_id: Optional[int] = None,
    admin_id: Optional[int] = None,
    admin_perms: Optional[list[str]] = None,
) -> AsyncGenerator[dict, None]:
    """对话主流程，yield SSE 事件 dict。

    事件序列：meta → (tool/product 事件) → token* → done；异常时 error。
    """
    start = repo.monotonic_ms()
    answer_parts: list[str] = []

    def _log(scene: str, success: bool, error: Optional[str] = None) -> None:
        repo.save_call_log(
            scene=scene,
            model=settings.chat_model,
            duration_ms=repo.monotonic_ms() - start,
            success=success,
            admin_id=admin_id,
            user_id=user_id,
            conversation_id=conversation_id,
            error_msg=error,
        )

    try:
        # 1. 会话归属校验
        conv = None
        if conversation_id:
            conv = repo.get_conversation(conversation_id)
            if not conv or conv["source"] != source:
                yield {"type": "error", "message": "会话不存在", "code": 404}
                return
            if source == "mall" and user_id and conv["user_id"] != user_id:
                yield {"type": "error", "message": "无权访问该会话", "code": 403}
                return
            if source == "admin" and admin_id and conv["admin_id"] != admin_id:
                yield {"type": "error", "message": "无权访问该会话", "code": 403}
                return

        # 2. 角色与系统提示词
        role = None
        if role_id:
            role = repo.get_role(role_id)
        if not role:
            role = repo.get_role(1 if source == "mall" else 2)  # 内置默认
        if not role:
            yield {"type": "error", "message": "AI 角色未配置", "code": 500}
            return
        role_id = role["id"]
        system_message = (conv or {}).get("system_message") or role.get("system_message") or ""

        # 3. 新建会话（首轮）
        is_new = False
        if not conv:
            conversation_id = repo.create_conversation(
                source=source, role_id=role_id, system_message=system_message,
                admin_id=admin_id, user_id=user_id,
            )
            is_new = True

        yield {"type": "meta", "conversationId": conversation_id, "roleId": role_id}

        # 4. 待确认动作分支（仅后台有危险写）
        pending = await _load_pending(conversation_id)
        text = message.strip()
        if pending:
            if _CONFIRM_WORDS.match(text):
                try:
                    result = await ai_tools.execute_pending_action(pending)
                    reply = f"已完成修改：{json.dumps(result.get('result'), ensure_ascii=False, default=str)[:200]}"
                except Exception as exc:
                    reply = f"执行失败：{exc}"
                await _clear_pending(conversation_id)
                repo.save_message(conversation_id, "user", message, admin_id=admin_id, user_id=user_id)
                repo.save_message(conversation_id, "assistant", reply, role_id=role_id)
                repo.touch_conversation(conversation_id, title=message[:30] if is_new else None)
                yield {"type": "token", "text": reply}
                yield {"type": "done"}
                _log("chat", True)
                return
            if _CANCEL_WORDS.match(text):
                await _clear_pending(conversation_id)
                reply = "好的，已取消该操作。"
                repo.save_message(conversation_id, "user", message, admin_id=admin_id, user_id=user_id)
                repo.save_message(conversation_id, "assistant", reply, role_id=role_id)
                yield {"type": "token", "text": reply}
                yield {"type": "done"}
                _log("chat", True)
                return
            # 非确认/取消：清掉旧动作，走正常对话
            await _clear_pending(conversation_id)

        # 5. 组装消息：system + 历史 + 当前
        max_ctx = int((conv or {}).get("max_contexts") or 20)
        history = repo.load_history(conversation_id, max_ctx) if not is_new else []
        messages: list[BaseMessage] = [SystemMessage(content=system_message + _TOOL_RULES)]
        for h in history:
            r = h.get("role")
            if r == "user":
                messages.append(HumanMessage(content=h.get("content") or ""))
            elif r == "assistant" and h.get("content"):
                messages.append(AIMessage(content=h["content"]))
            # tool 消息历史不回放（tool_call 配对易断，重放价值低）
        messages.append(HumanMessage(content=message))

        # 6. 工具权限
        role_tools = repo.list_role_tool_names(role_id)
        allowed = _filter_allowed_tools(source, role_tools, admin_perms)

        # 7. 注入工具上下文（user_id 等）
        ai_tools.set_context({"user_id": user_id, "admin_id": admin_id, "source": source})

        # 8. 跑图
        final_state: AgentState = await _graph.ainvoke(
            {
                "messages": messages,
                "allowed_tools": allowed,
                "ui_events": [],
                "tool_rounds": 0,
                "need_summary": False,
            }
        )

        # 9. 固定话术（闲聊）
        if final_state.get("fixed_reply"):
            reply = final_state["fixed_reply"]
            repo.save_message(conversation_id, "user", message, admin_id=admin_id, user_id=user_id)
            repo.save_message(conversation_id, "assistant", reply, role_id=role_id)
            repo.touch_conversation(conversation_id, title=message[:30] if is_new else None)
            yield {"type": "token", "text": reply}
            yield {"type": "done"}
            _log("chat", True)
            return

        # 10. UI 事件（商品卡片）
        for ev in final_state.get("ui_events") or []:
            yield ev

        # 11. 待确认动作
        if final_state.get("pending_action"):
            await _save_pending(conversation_id, final_state["pending_action"])
            pa = final_state["pending_action"]
            field_label = {"name": "商品名称", "sub_title": "副标题", "description": "商品详情"}.get(
                pa["field"], pa["field"]
            )
            confirm_text = (
                f"我准备将商品 #{pa['product_id']} 的{field_label}"
                f"修改为「{pa['new_value']}」。请确认是否执行？"
            )
            yield {"type": "confirm", "action": pa, "text": confirm_text}
            repo.save_message(conversation_id, "user", message, admin_id=admin_id, user_id=user_id)
            repo.save_message(conversation_id, "assistant", confirm_text, role_id=role_id)
            repo.touch_conversation(conversation_id, title=message[:30] if is_new else None)
            yield {"type": "done"}
            _log("chat", True)
            return

        # 12. 产出最终回复
        if final_state.get("need_summary"):
            # 工具已执行：流式汇总（bind_tools 的完整消息已在 state 里）
            llm = get_llm(streaming=True)
            async for chunk in llm.astream(final_state["messages"]):
                piece = chunk.content
                if isinstance(piece, list):
                    piece = "".join(str(p.get("text", "")) for p in piece if isinstance(p, dict))
                if piece:
                    answer_parts.append(str(piece))
                    yield {"type": "token", "text": str(piece)}
        else:
            reply = final_state.get("answer") or "我暂时没想好怎么回答，换个说法试试？"
            answer_parts.append(reply)
            # 非流式答案按句号粗切，模拟打字机效果
            for piece in re.split(r"(?<=[。！？!?\n])", reply):
                if piece:
                    yield {"type": "token", "text": piece}

        # 13. 落库
        full_answer = "".join(answer_parts)
        repo.save_message(conversation_id, "user", message, admin_id=admin_id, user_id=user_id)
        repo.save_message(conversation_id, "assistant", full_answer, role_id=role_id)
        repo.touch_conversation(conversation_id, title=message[:30] if is_new else None)
        yield {"type": "done"}
        _log("chat", True)

    except Exception as exc:
        logger.exception("AI 对话失败 source=%s conv=%s", source, conversation_id)
        _log("chat", False, str(exc))
        yield {"type": "error", "message": "AI 服务暂时开小差了，请稍后再试", "code": 500}
