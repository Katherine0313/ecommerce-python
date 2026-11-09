"""意图识别：LangGraph 第一个节点，关键词前置路由。

三类：
- CHAT    打招呼/闲聊/感谢 → 固定话术，不调工具不查库
- DANGER  危险写操作（改商品文案）→ 走确认节点
- LLM     其他一律交给大模型 + 工具
"""

import re
from typing import Literal

Intent = Literal["chat", "danger", "llm"]

_GREETING = re.compile(
    r"^(你好|您好|hi|hello|嗨|喂|在吗|在不|在么|早上好|晚上好|下午好|谢谢|感谢|thanks|拜拜|再见|拜拜啦)[!！~。.\s]*$",
    re.IGNORECASE,
)

# 修改商品文案的触发词（配合「改/修改/换成」+ 商品字段词）
_DANGER_VERB = re.compile(r"(修改|改成|改为|换成|换成是|更名|改成叫)")
_DANGER_FIELD = re.compile(r"(商品名称|商品名|标题|副标题|详情|描述|文案|description|sub_title|name)")


def classify(message: str) -> Intent:
    text = (message or "").strip()
    if not text:
        return "chat"
    if _GREETING.match(text):
        return "chat"
    if _DANGER_VERB.search(text) and _DANGER_FIELD.search(text):
        return "danger"
    return "llm"


CHAT_REPLY = (
    "你好呀，我是杰米电商的 AI 客服小助手～可以帮你查商品、找好物、"
    "查订单进度、看物流，也能解答退换货问题。有什么可以帮你的吗？"
)
