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
from app.ai.prompts import NO_AI_STYLE, SUMMARY_RULES, build_system_prompt
from app.config import settings
from app.services.redis_client import get_client

logger = logging.getLogger(__name__)

_MAX_TOOL_ROUNDS = 3
_PENDING_KEY = "ai:pending:"
_PENDING_TTL = 300

_CONFIRM_WORDS = re.compile(r"^(确认|确定|好的|好|是的|对|ok|yes|执行)[!！。.\s]*$", re.IGNORECASE)
_CANCEL_WORDS = re.compile(r"^(取消|算了|不用|别|no|不)[!！。.\s]*$", re.IGNORECASE)
# 政策类问题（发货时效/售后/发票/运费等）：首轮强制查知识库，避免模型用通用话术敷衍
_POLICY_KEYWORDS = re.compile(
    r"(发货|配送|多久到|几天到|物流|快递|运费|包邮|退货|退款|换货|售后|"
    r"发票|开票|支付方式|付款方式|优惠券|会员|积分|保修|质保)"
)
# 投诉/不满类问题：知识库里有安抚话术，命中先直答，避免模型自由发挥或漏掉道歉
_COMPLAINT_KEYWORDS = re.compile(
    r"(投诉|差评|举报|维权|12315|消协|质量太差|质量问题|态度差|态度不好|"
    r"假货|翻新|发错货|少发|漏发|破损|磕碰|开不了机|坏了|漏液|花屏|鼓包|"
    r"诱导好评|信息泄露|承诺没兑现|不给退|不给保修|赔不赔)"
)
# 人工/客服类问题：知识库已有标准答案（在线时间、如何联系、能做什么），命中直答不调模型
_HUMAN_INFO = re.compile(r"(人工|真人客服|客服)")
# 涉及本人订单时优先查订单，不强制知识库
_PERSONAL_ORDER = re.compile(r"(我的订单|订单号|我买的|我的物流|我的快递|我下的单|我下单的)")
# 店铺介绍类问题（卖什么/经营什么/有什么产品）：知识库里有标准答案，首轮强制检索
_STORE_INTRO = re.compile(
    r"(卖什么|卖哪些|经营什么|经营哪些|主营|有什么产品|有什么商品|有哪些产品|有哪些商品|卖什么东西|"
    r"口号|slogan|标语|老板|创始人|店名|店铺名|公司名|品牌|联系方式|客服电话)"
)
# 优惠券/活动类问题：直接查后台实时配置，避免知识库静态文案与当前活动脱节
_PROMO_QUERY = re.compile(
    r"(有什么优惠|有哪些优惠|什么优惠|优惠活动|有什么活动|有哪些活动|有活动吗|活动内容|"
    r"有优惠券|有什么券|能领什么券|领券|新人券|满减|立减|秒杀|拼团|促销|打折|买更便宜|划算)"
)

# 小程序端要求对外文案不出现「AI」：流式分片逐块过滤；尾部单字母留到下一块拼接，
# 避免「A」「I」被 token 拆开时漏过滤。
_AI_WORD = re.compile(r"(?<![A-Za-z])AI(?![A-Za-z])", re.IGNORECASE)


def _mask_ai(text: str) -> str:
    # 先替换常见自称，避免出现「智能 客服小助手」这种别扭表述
    text = re.sub(r"(?i)(?<![A-Za-z])AI\s*客服", "客服", text)
    text = re.sub(r"(?i)(?<![A-Za-z])AI\s*助手", "客服助手", text)
    return _AI_WORD.sub("智能", text)


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


def _last_user_text(state: AgentState) -> str:
    """当前轮用户原话（跳过历史与工具消息）。"""
    for m in reversed(state["messages"]):
        if isinstance(m, HumanMessage):
            return str(m.content or "").strip()
    return ""


def _is_kb_direct(text: str) -> bool:
    """政策/店铺介绍/投诉安抚/人工客服类问题走知识库直答（不经过模型改写问题）。"""
    if _PERSONAL_ORDER.search(text):
        return False
    return bool(
        _POLICY_KEYWORDS.search(text)
        or _STORE_INTRO.search(text)
        or _COMPLAINT_KEYWORDS.search(text)
        or _HUMAN_INFO.search(text)
    )


def _forced_tool(state: AgentState, allowed: set[str]) -> Optional[dict]:
    """政策类问题首轮强制调 search_knowledge。

    实测模型遇到「我今天下单能发货吗」这类问题会直接回通用问候话术、不查知识库，
    因此这里在工具选择上强制一次，保证答案来自知识库。
    """
    if state.get("tool_rounds", 0) > 0 or "search_knowledge" not in allowed:
        return None
    text = _last_user_text(state)
    if _PERSONAL_ORDER.search(text):
        return None
    if _POLICY_KEYWORDS.search(text) or _STORE_INTRO.search(text):
        return {"type": "function", "function": {"name": "search_knowledge"}}
    return None


def _decide(state: AgentState) -> AgentState:
    """LLM + bind_tools 决策：返回工具调用则进 tools，否则产出答案。"""
    allowed = state.get("allowed_tools") or set()
    llm_tools = ai_tools.resolve_tools(allowed)
    llm = get_llm()
    if not llm_tools:
        resp = llm.invoke(state["messages"])
    else:
        forced = _forced_tool(state, allowed)
        try:
            bound = (
                llm.bind_tools(llm_tools, tool_choice=forced)
                if forced
                else llm.bind_tools(llm_tools)
            )
            resp = bound.invoke(state["messages"])
        except Exception:
            # 个别模型/网关不支持强制 tool_choice，退回自动选择，不影响主流程
            logger.warning("强制工具调用失败，退回自动选择", exc_info=True)
            resp = llm.bind_tools(llm_tools).invoke(state["messages"])
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
        if name == "search_knowledge":
            # 用用户原话检索：模型常把「今天下单能发货吗」改写成「怎么下单」，导致命中错误条目
            raw = _last_user_text(state)
            if raw:
                args = {**args, "question": raw}
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
    anonymous_id: Optional[str] = None,
    admin_id: Optional[int] = None,
    admin_perms: Optional[list[str]] = None,
    hide_ai: bool = False,
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
            if source == "mall":
                # 登录用户按 user_id 校验；匿名用户按 anonymous_id 校验
                if user_id and conv["user_id"] != user_id:
                    yield {"type": "error", "message": "无权访问该会话", "code": 403}
                    return
                if anonymous_id and conv["anonymous_id"] != anonymous_id:
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
            yield {"type": "error", "message": "客服角色未配置", "code": 500}
            return
        role_id = role["id"]
        system_message = (conv or {}).get("system_message") or role.get("system_message") or ""
        # 小程序端不展示「AI」：角色设定里可能自带「AI 客服」字样，追加表达约束做第一层拦截
        if hide_ai:
            system_message += NO_AI_STYLE

        # 3. 新建会话（首轮）
        is_new = False
        if not conv:
            conversation_id = repo.create_conversation(
                source=source, role_id=role_id, system_message=system_message,
                admin_id=admin_id, user_id=user_id, anonymous_id=anonymous_id,
            )
            is_new = True

        yield {"type": "meta", "conversationId": conversation_id, "roleId": role_id}

        # 3.5 转人工：先给确认卡片，用户点击后才真正切人工会话
        if source == "mall" and intent_mod.match_handoff(message):
            repo.save_message(
                conversation_id, "user", message,
                admin_id=admin_id, user_id=user_id, anonymous_id=anonymous_id,
            )
            reply = "可以的，点击下方「联系人工客服」即可接入，工作时间 9:00~18:00。"
            repo.touch_conversation(conversation_id, title=message[:30] if is_new else None)
            # 只下发可选卡片，不置 human_mode；用户点击卡片再调 /api/mall/ai/handoff
            yield {"type": "handoff_offer", "conversationId": conversation_id, "text": reply}
            yield {"type": "token", "text": reply}
            yield {"type": "done"}
            _log("chat", True)
            return

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
                repo.save_message(conversation_id, "user", message, admin_id=admin_id, user_id=user_id, anonymous_id=anonymous_id)
                repo.save_message(conversation_id, "assistant", reply, role_id=role_id)
                repo.touch_conversation(conversation_id, title=message[:30] if is_new else None)
                yield {"type": "token", "text": reply}
                yield {"type": "done"}
                _log("chat", True)
                return
            if _CANCEL_WORDS.match(text):
                await _clear_pending(conversation_id)
                reply = "好的，已取消该操作。"
                repo.save_message(conversation_id, "user", message, admin_id=admin_id, user_id=user_id, anonymous_id=anonymous_id)
                repo.save_message(conversation_id, "assistant", reply, role_id=role_id)
                yield {"type": "token", "text": reply}
                yield {"type": "done"}
                _log("chat", True)
                return
            # 非确认/取消：清掉旧动作，走正常对话
            await _clear_pending(conversation_id)

        # 4.5 优惠券/活动类问题：直接查后台实时配置，避免知识库静态文案与当前活动脱节
        direct: Optional[str] = None
        if _PROMO_QUERY.search(text):
            direct = await ai_tools.promo_answer()
        # 4.6 政策/店铺介绍类问题：知识库有标准答案就直接回，避免模型改写问题或空答
        if not direct and _is_kb_direct(text):
            direct = await ai_tools.knowledge_answer(text)
        if direct:
            repo.save_message(conversation_id, "user", message, admin_id=admin_id, user_id=user_id, anonymous_id=anonymous_id)
            repo.save_message(conversation_id, "assistant", direct, role_id=role_id)
            repo.touch_conversation(conversation_id, title=message[:30] if is_new else None)
            yield {"type": "token", "text": direct}
            yield {"type": "done"}
            _log("chat", True)
            return

        # 5. 组装消息：system + 历史 + 当前
        max_ctx = int((conv or {}).get("max_contexts") or 20)
        history = repo.load_history(conversation_id, max_ctx) if not is_new else []
        # 注入店铺真实品类，避免模型追问时编出「送人/宿舍用/运动/厨房用品」这类店里没有的品类
        catalog = await ai_tools.catalog_hint()
        messages: list[BaseMessage] = [SystemMessage(content=build_system_prompt(system_message, catalog))]
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

        # 7. 注入工具上下文（user_id / role_category 等，知识库按 scope 过滤）
        ai_tools.set_context({
            "user_id": user_id,
            "admin_id": admin_id,
            "source": source,
            "role_category": role.get("category") or ("客服" if source == "mall" else "运营"),
        })

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
            reply = _mask_ai(final_state["fixed_reply"]) if hide_ai else final_state["fixed_reply"]
            repo.save_message(conversation_id, "user", message, admin_id=admin_id, user_id=user_id, anonymous_id=anonymous_id)
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
            repo.save_message(conversation_id, "user", message, admin_id=admin_id, user_id=user_id, anonymous_id=anonymous_id)
            repo.save_message(conversation_id, "assistant", confirm_text, role_id=role_id)
            repo.touch_conversation(conversation_id, title=message[:30] if is_new else None)
            yield {"type": "done"}
            _log("chat", True)
            return

        # 12. 产出最终回复
        if final_state.get("need_summary"):
            # 工具已执行：流式汇总（bind_tools 的完整消息已在 state 里）
            summary_messages = list(final_state["messages"]) + [HumanMessage(content=SUMMARY_RULES)]
            # 模型偶发只返回空内容（尤其带 ToolMessage 时），空回答重试一次，仍空则给兜底话术，
            # 避免前端气泡空白、看起来像"AI 消失了"
            for attempt in range(2):
                answer_parts.clear()
                llm = get_llm(streaming=True)
                carry = ""
                async for chunk in llm.astream(summary_messages):
                    piece = chunk.content
                    if isinstance(piece, list):
                        piece = "".join(str(p.get("text", "")) for p in piece if isinstance(p, dict))
                    if not piece:
                        continue
                    text = str(piece)
                    if hide_ai:
                        text = carry + text
                        carry = text[-1] if text[-1] in "Aa" else ""
                        if carry:
                            text = text[:-1]
                        text = _mask_ai(text)
                    if text:
                        answer_parts.append(text)
                        yield {"type": "token", "text": text}
                if hide_ai and carry:
                    text = _mask_ai(carry)
                    answer_parts.append(text)
                    yield {"type": "token", "text": text}
                if "".join(answer_parts).strip():
                    break
                logger.warning("汇总回答为空，重试 conv=%s attempt=%s", conversation_id, attempt + 1)
            if not "".join(answer_parts).strip():
                fallback = "抱歉，这次没能生成回答。你可以换个说法再问一次，或直接说商品名、订单号，我帮你查。"
                answer_parts.append(fallback)
                yield {"type": "token", "text": fallback}
        else:
            reply = final_state.get("answer") or "我暂时没想好怎么回答，换个说法试试？"
            if hide_ai:
                reply = _mask_ai(reply)
            answer_parts.append(reply)
            # 非流式答案按句号粗切，模拟打字机效果
            for piece in re.split(r"(?<=[。！？!?\n])", reply):
                if piece:
                    yield {"type": "token", "text": piece}

        # 13. 落库
        full_answer = "".join(answer_parts)
        repo.save_message(conversation_id, "user", message, admin_id=admin_id, user_id=user_id, anonymous_id=anonymous_id)
        repo.save_message(conversation_id, "assistant", full_answer, role_id=role_id)
        repo.touch_conversation(conversation_id, title=message[:30] if is_new else None)
        yield {"type": "done"}
        _log("chat", True)

    except Exception as exc:
        logger.exception("AI 对话失败 source=%s conv=%s", source, conversation_id)
        _log("chat", False, str(exc))
        yield {"type": "error", "message": "服务暂时开小差了，请稍后再试", "code": 500}
