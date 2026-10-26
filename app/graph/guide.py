"""导购机器人主流程：检索商品 → 调大模型生成回复。

流式 token 通过 LangGraph 的 get_stream_writer 推给接口层，
接口层再编码成前端约定的 SSE 格式。
"""

import logging
from typing import Any, Optional

from langgraph.config import get_stream_writer
from langgraph.graph import END, START, StateGraph
from openai import AsyncOpenAI

from app.config import settings
from app.db.product_repo import search_products
from app.graph.state import GuideState
from app.prompts import SYSTEM_PROMPT, build_product_context

logger = logging.getLogger(__name__)

# 硅基流动兼容 OpenAI 接口；缺 key 时请求会失败，由接口层统一兜底
_client = AsyncOpenAI(
    api_key=settings.siliconflow_api_key or "missing-key",
    base_url=settings.openai_base_url,
    timeout=60.0,
    max_retries=1,
)


def _last_user_message(state: GuideState) -> str:
    for message in reversed(state.get("messages") or []):
        if message.get("role") == "user":
            return message.get("content") or ""
    return ""


async def _retrieve(state: GuideState) -> dict[str, Any]:
    """按用户最新一句话检索商品；问候类消息大概率检索为空。"""
    message = _last_user_message(state)
    products = await search_products(message) if message else []
    logger.info("商品检索命中 %s 条", len(products))
    return {"products": products}


async def _generate(state: GuideState) -> dict[str, Any]:
    """拼 system prompt，流式调用模型，并逐块写给接口层。"""
    products = state.get("products") or []
    messages = [{
        "role": "system",
        "content": SYSTEM_PROMPT + build_product_context(products),
    }]
    messages.extend(state.get("messages") or [])

    try:
        writer = get_stream_writer()
    except Exception:
        writer = None

    parts: list[str] = []
    stream = await _client.chat.completions.create(
        model=settings.chat_model,
        messages=messages,
        temperature=settings.temperature,
        max_tokens=settings.max_output_tokens,
        stream=True,
    )
    async for chunk in stream:
        if not chunk.choices:
            continue
        delta = chunk.choices[0].delta.content
        if not delta:
            continue
        parts.append(delta)
        if writer is not None:
            writer(delta)

    reply = "".join(parts)
    return {
        "reply": reply,
        "messages": list(state.get("messages") or []) + [
            {"role": "assistant", "content": reply}
        ],
    }


def _build_graph() -> Any:
    graph = StateGraph(GuideState)
    graph.add_node("retrieve", _retrieve)
    graph.add_node("generate", _generate)
    graph.add_edge(START, "retrieve")
    graph.add_edge("retrieve", "generate")
    graph.add_edge("generate", END)
    return graph.compile()


# 进程内编译一次，复用
guide_graph = _build_graph()
