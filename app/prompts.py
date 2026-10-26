"""导购机器人的提示词与商品上下文拼装。"""

from typing import Any

SYSTEM_PROMPT = """你是「杰米多多」3C商城的导购助手，负责根据用户的预算和需求推荐合适的商品。

回答规则：
1. 只能使用下方「商品资料」里出现的信息，不要编造商品、价格、参数或库存。
2. 回答用简体中文纯文本，不要使用 Markdown 符号（不要用 **、#、-、表格）。
3. 推荐商品时给出商品名称和价格，并用一两句话说明推荐理由；控制在 3~5 句话。
4. 商品资料为空或没有合适商品时，如实说明，并询问用户的预算或用途，不要硬推。
5. 与购物无关的问题（问候、售后、物流等）正常简短回答；没有依据时直接说不知道。
6. 不要提及「商品资料」「系统提供」这类内部信息。
7. 你只负责商城导购和购物咨询；遇到写代码、写作业、生成文章等与购物无关的请求，礼貌拒绝并引导回商品咨询。"""


def build_product_context(products: list[dict[str, Any]]) -> str:
    """把检索结果拼成模型可读的商品资料块。"""
    if not products:
        return "\n\n商品资料：本轮没有检索到匹配商品。"

    lines = ["\n\n商品资料（来自商城在售商品）："]
    for index, product in enumerate(products, start=1):
        name = product.get("name") or ""
        price = product.get("sales_price") or 0
        brand = product.get("brand") or ""
        sub_title = product.get("sub_title") or ""
        stock = product.get("stock") or 0
        category = product.get("category_name") or ""
        parts = [f"{index}. {name}", f"价格 {price:g} 元"]
        if brand:
            parts.append(f"品牌 {brand}")
        if category:
            parts.append(f"分类 {category}")
        if sub_title:
            parts.append(f"卖点 {sub_title}")
        parts.append(f"库存 {stock}")
        lines.append("｜".join(parts))
    return "\n".join(lines)
