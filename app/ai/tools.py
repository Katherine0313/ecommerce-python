"""AI 工具集：9 个 LangChain @tool。

数据来源规则：
- 商品/库存/订单/售后全部走 internal_client（Java /internal/ai/**），禁止大模型编造
- 知识库直接查 ai_kb_qa 复用现有匹配逻辑
- update_product 是危险写操作：本模块只负责「暂存待确认动作」，真实执行在 agent 确认节点

前端商品卡片约定：工具结果里的 _ui 字段（type=product_list）会被 agent 单独
抽出来作为 SSE products 事件推给前端，不进入大模型上下文。
"""

import contextvars
import json
import logging
import time
from typing import Any, Optional

from langchain_core.tools import tool

from app import internal_client
from app.kb.matcher import THRESHOLD, match_best
from app.kb.repo import load_entries, record_hit

logger = logging.getLogger(__name__)

# 危险写操作工具名（需要用户二次确认）
DANGEROUS_TOOLS = {"update_product"}

# 当前对话上下文（user_id / 待确认动作 / UI 事件收集），由 agent 在调用前注入。
# 用 ContextVar 保证并发请求之间互不串数据。
_request_context: contextvars.ContextVar[dict[str, Any]] = contextvars.ContextVar(
    "ai_request_context", default={}
)


def set_context(ctx: dict[str, Any]) -> None:
    _request_context.set(ctx)


def _ctx() -> dict[str, Any]:
    return _request_context.get()


def _user_id() -> Optional[int]:
    return _ctx().get("user_id")


def _product_card(p: dict) -> dict:
    """商品卡片字段：卡片点击跳转只认 id，其余字段仅展示。"""
    return {
        "id": p.get("id"),
        "name": p.get("name"),
        "subTitle": p.get("subTitle") or p.get("sub_title"),
        "price": p.get("price"),
        "originalPrice": p.get("originalPrice") or p.get("original_price"),
        "mainImage": p.get("mainImage") or p.get("main_image"),
        "sales": p.get("sales"),
    }


# 品类词 → 分类 ID 缓存。
# 背景：关键词搜索是 name/sub_title/brand LIKE '%词%'，「手机」会把「华为素皮手机壳」一起搜出来；
# 品类词必须先映射成分类再查（分类含子分类），才能只出整机、不混配件。
_CATEGORY_TTL_SECONDS = 600
_category_cache: dict[str, int] = {}
_category_cache_at = 0.0


def _normalize_keyword(keyword: Optional[str]) -> Optional[str]:
    """去掉「推荐/看看/商品/有哪些」等口头前后缀，让「推荐手机」也能命中分类「手机」。"""
    if not keyword:
        return None
    word = keyword.strip().strip("，。,.!！?？ 的")
    for suffix in ("的商品", "商品", "产品", "推荐", "有哪些", "类"):
        if word.endswith(suffix) and len(word) > len(suffix):
            word = word[: -len(suffix)]
    for prefix in ("推荐一下", "推荐", "介绍一下", "介绍", "看看", "我想买", "想买", "我要买", "想要", "求推荐"):
        if word.startswith(prefix) and len(word) > len(prefix):
            word = word[len(prefix):]
    return word or None


async def _category_id_of(keyword: Optional[str]) -> Optional[int]:
    """关键词恰好等于分类名时返回分类 ID；否则返回 None 走原关键词搜索。"""
    global _category_cache, _category_cache_at
    if not keyword:
        return None
    now = time.monotonic()
    if not _category_cache or now - _category_cache_at > _CATEGORY_TTL_SECONDS:
        try:
            tree = await internal_client.list_categories()
        except Exception as exc:  # 分类接口异常时退化为关键词搜索，不阻断对话
            logger.warning("加载分类树失败，退化为关键词搜索: %s", exc)
            return None
        mapping: dict[str, int] = {}
        for parent in tree:
            if parent.get("name") and parent.get("id"):
                mapping[parent["name"]] = parent["id"]
            for child in parent.get("children") or []:
                if child.get("name") and child.get("id"):
                    mapping[child["name"]] = child["id"]
        _category_cache = mapping
        _category_cache_at = now
    return _category_cache.get(keyword)


# ---------- 1. 查单个商品 ----------

@tool
async def query_product(product_id: int) -> str:
    """查询单个商品的详细信息（名称、价格、库存、规格、销量等）。传入商品 ID。"""
    product = await internal_client.get_product(product_id)
    if not product:
        return json.dumps({"found": False, "message": "商品不存在或已下架"}, ensure_ascii=False)
    return json.dumps({"found": True, "product": product}, ensure_ascii=False, default=str)


# ---------- 2. 搜索商品 ----------

@tool
async def search_products(
    keyword: Optional[str] = None,
    category_id: Optional[int] = None,
    min_price: Optional[float] = None,
    max_price: Optional[float] = None,
    order_by: str = "comprehensive",
) -> str:
    """按关键词、分类、价格区间搜索在售商品。order_by: comprehensive / new / price_asc / price_desc。

    keyword 直接传品类名（手机 / 电脑 / 平板 / 耳机音箱 / 手表 / 相机 / 数码配件 / 智能家居）时
    会自动转为分类筛选，只出该品类整机，不会混入手机壳、贴膜等配件；
    传品牌或型号时按名称/副标题/品牌模糊匹配。
    """
    keyword = _normalize_keyword(keyword)
    if category_id is None:
        matched = await _category_id_of(keyword)
        if matched:
            category_id, keyword = matched, None
    data = await internal_client.search_products(
        keyword=keyword,
        category_id=category_id,
        min_price=min_price,
        max_price=max_price,
        order_by=order_by,
        size=6,
    )
    records = data.get("records") or []
    cards = [_product_card(p) for p in records]
    return json.dumps(
        {
            "total": data.get("total", 0),
            "products": cards,
            "_ui": {"type": "product_list", "items": cards},
        },
        ensure_ascii=False,
        default=str,
    )


# ---------- 3. 好物推荐 ----------

@tool
async def recommend_product(scene: Optional[str] = None, max_price: Optional[float] = None) -> str:
    """根据用户描述的场景推荐在售商品，返回商品卡片。scene 如「送女友」「宿舍用」。

    scene 是品类词（如「手机」）时按分类筛选；其它场景词按关键词搜索。
    """
    scene = _normalize_keyword(scene)
    category_id = await _category_id_of(scene)
    # P0 简化策略：有预算区间按价格升序取，否则按销量取在售商品
    data = await internal_client.search_products(
        keyword=None if category_id else scene,
        category_id=category_id,
        max_price=max_price,
        order_by="comprehensive",
        size=6,
    )
    records = data.get("records") or []
    if not records and scene and not category_id:
        # 关键词无结果时退化为热销兜底，避免大模型编造
        data = await internal_client.search_products(order_by="comprehensive", size=6)
        records = data.get("records") or []
    cards = [_product_card(p) for p in records]
    return json.dumps(
        {
            "scene": scene,
            "count": len(cards),
            "products": cards,
            "_ui": {"type": "product_list", "items": cards},
        },
        ensure_ascii=False,
        default=str,
    )


# ---------- 4. 知识库检索 ----------

@tool
async def search_knowledge(question: str) -> str:
    """检索客服知识库，回答退换货、发货时间、尺码、运费、正品保障等规则类问题。"""
    entries = await load_entries()
    best = match_best(entries, question, "")
    if not best or best.score < THRESHOLD:
        return json.dumps({"matched": False}, ensure_ascii=False)
    await record_hit(best.entry.id)
    return json.dumps(
        {"matched": True, "answer": best.entry.answer, "matched_question": best.entry.question},
        ensure_ascii=False,
    )


# ---------- 5. 查库存 ----------

@tool
async def query_stock(sku_id: int) -> str:
    """查询指定 SKU 的实时库存数量。"""
    stock = await internal_client.get_stock(sku_id)
    if not stock:
        return json.dumps({"found": False, "message": "SKU 不存在"}, ensure_ascii=False)
    return json.dumps({"found": True, "stock": stock}, ensure_ascii=False, default=str)


# ---------- 6. 查订单 ----------

@tool
async def query_order(keyword: Optional[str] = None) -> str:
    """查询当前登录用户的订单列表与状态。可按订单号或商品名过滤。"""
    uid = _user_id()
    if not uid:
        return json.dumps({"needLogin": True, "message": "请先登录后查询订单"}, ensure_ascii=False)
    orders = await internal_client.list_orders(user_id=uid, keyword=keyword, limit=5)
    return json.dumps({"count": len(orders), "orders": orders}, ensure_ascii=False, default=str)


# ---------- 7. 查售后 ----------

@tool
async def query_aftersale(aftersale_id: int) -> str:
    """按售后单 ID 查询售后进度与处理状态。"""
    data = await internal_client.get_aftersale(aftersale_id, user_id=_user_id())
    if not data:
        return json.dumps({"found": False, "message": "未查询到该售后单"}, ensure_ascii=False)
    return json.dumps({"found": True, "aftersale": data}, ensure_ascii=False, default=str)


# ---------- 8. 生成文案（后台） ----------

@tool
async def generate_copy(product_id: int, scene: str = "all") -> str:
    """为商品生成营销文案。scene: title(名称) / subtitle(副标题) / description(详情) / all(全部)。"""
    product = await internal_client.get_product(product_id)
    if not product:
        return json.dumps({"found": False, "message": "商品不存在"}, ensure_ascii=False)
    # 生成在 agent 里走专用流程（需要流式 + 审计），工具路径只返回商品信息供 LLM 起草
    return json.dumps(
        {
            "found": True,
            "scene": scene,
            "product": {
                "id": product.get("id"),
                "name": product.get("name"),
                "subTitle": product.get("subTitle") or product.get("sub_title"),
                "categoryName": product.get("categoryName") or product.get("category_name"),
            },
        },
        ensure_ascii=False,
        default=str,
    )


# ---------- 9. 修改商品（危险写，需确认） ----------

@tool
def update_product(product_id: int, field: str, new_value: str) -> str:
    """修改商品的文案类字段。field 仅允许 name / sub_title / description。执行前必须向用户说明变更并等其确认。"""
    if field not in ("name", "sub_title", "description"):
        return json.dumps(
            {"ok": False, "message": f"不允许修改字段 {field}，仅支持 name / sub_title / description"},
            ensure_ascii=False,
        )
    # 不直接执行：把动作挂到上下文，由 agent 返回 confirm 事件，用户确认后下一轮执行
    _ctx()["pending_action"] = {
        "tool": "update_product",
        "product_id": product_id,
        "field": field,
        "new_value": new_value,
    }
    field_label = {"name": "商品名称", "sub_title": "副标题", "description": "商品详情"}[field]
    return json.dumps(
        {
            "ok": True,
            "pending": True,
            "message": f"准备将商品 #{product_id} 的{field_label}修改为「{new_value}」，请回复「确认」执行，回复其他内容取消。",
        },
        ensure_ascii=False,
    )


async def execute_pending_action(action: dict) -> dict:
    """用户确认后真正执行 update_product。"""
    result = await internal_client.update_product(
        action["product_id"],
        {action["field"]: action["new_value"]},
    )
    return {"ok": True, "result": result}


# name -> tool 映射
ALL_TOOLS = {
    t.name: t
    for t in (
        query_product,
        search_products,
        recommend_product,
        search_knowledge,
        query_stock,
        query_order,
        query_aftersale,
        generate_copy,
        update_product,
    )
}


def resolve_tools(allowed_names: set[str]) -> list:
    """按「角色绑定 ∩ 用户权限」过滤后的工具列表，交给 LLM bind_tools。"""
    return [t for name, t in ALL_TOOLS.items() if name in allowed_names]
