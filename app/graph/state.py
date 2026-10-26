"""LangGraph 状态定义。"""

from typing import Any, TypedDict


class GuideState(TypedDict):
    # OpenAI messages：历史 + 本轮用户消息
    messages: list[dict[str, Any]]
    # 本轮检索到的商品
    products: list[dict[str, Any]]
    # 模型完整回复（非流式接口用）
    reply: str
