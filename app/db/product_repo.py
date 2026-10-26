"""商品只读仓库：从上架商品里按关键词 / 价格筛选，供导购机器人回答使用。

只执行 SELECT，不建表、不写入。商品数量少（本地约 100 条），
整表加载到内存并做 60 秒缓存，比每次请求都查库更快。
"""

import asyncio
import logging
import re
import time
from typing import Any, Optional

import pymysql
from pymysql.cursors import DictCursor

from app.config import settings

logger = logging.getLogger(__name__)

# 内存缓存：商品列表 + 加载时间
_cache: list[dict[str, Any]] = []
_cache_at: float = 0.0
_cache_lock = asyncio.Lock()

# 价格表达：500-1000、1000以内、1000以上、5千以内
_PRICE_RANGE = re.compile(r"(\d+(?:\.\d+)?)\s*(?:元|块)?\s*[-~～到至]\s*(\d+(?:\.\d+)?)")
_PRICE_MAX = re.compile(r"(\d+(?:\.\d+)?)\s*(千|k|K)?\s*(?:元|块)?\s*(?:以内|以下|之内|内|左右|封顶)")
_PRICE_MIN = re.compile(r"(\d+(?:\.\d+)?)\s*(千|k|K)?\s*(?:元|块)?\s*(?:以上|起)")
_CN_PRICE = re.compile(r"([零一二两三四五六七八九十百千万]+)\s*(?:元|块)?\s*(?:以内|以下|之内|内|左右|以上|起)")

_LATIN = re.compile(r"[a-z0-9]+")
_CJK = re.compile(r"[一-鿿]+")

# 用户表达「想看看 / 要推荐」的词，命中但没匹配到具体商品时给一组分类兜底
_RECOMMEND_WORDS = (
    "推荐", "买", "选", "哪款", "哪个", "有什么", "有没有", "预算",
    "好用", "值得", "介绍", "看看", "适合", "想要",
)


def _cn_number(text: str) -> Optional[int]:
    """解析简单中文数字：一千 / 两千 / 五千 / 一万。"""
    digits = {"零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
              "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
    units = {"十": 10, "百": 100, "千": 1000, "万": 10000}
    total = section = num = 0
    for ch in text:
        if ch in digits:
            num = digits[ch]
        elif ch in units:
            unit = units[ch]
            if unit == 10000:
                total += (section + num) * unit
                section = num = 0
            else:
                section += (num or 1) * unit
                num = 0
        else:
            return None
    return total + section + num


def _parse_price_range(message: str) -> tuple[Optional[float], Optional[float]]:
    """从自然语言里解析价格区间，解析不到返回 (None, None)。"""
    text = message.replace(" ", "")
    m = _PRICE_RANGE.search(text)
    if m:
        return float(m.group(1)), float(m.group(2))

    factor = 1
    m = _PRICE_MAX.search(text)
    if m:
        value = float(m.group(1))
        if m.group(2):
            factor = 1000
        return None, value * factor

    m = _PRICE_MIN.search(text)
    if m:
        value = float(m.group(1))
        if m.group(2):
            factor = 1000
        return value * factor, None

    m = _CN_PRICE.search(text)
    if m:
        value = _cn_number(m.group(1))
        if value:
            if "以上" in m.group(0):
                return float(value), None
            return None, float(value)

    return None, None


def _tokens(text: str) -> set[str]:
    """中文按 2/3 字滑窗、英文数字按整词，生成匹配用的 token 集合。"""
    text = (text or "").lower()
    tokens = {t for t in _LATIN.findall(text) if len(t) >= 2}
    for chunk in _CJK.findall(text):
        for n in (2, 3):
            for i in range(len(chunk) - n + 1):
                tokens.add(chunk[i:i + n])
    return tokens


def _load_on_sale() -> list[dict[str, Any]]:
    """同步查库：上架且有库存的商品 + 分类名。"""
    conn = pymysql.connect(
        host=settings.mysql_host,
        port=settings.mysql_port,
        user=settings.db_username,
        password=settings.db_password,
        database=settings.mysql_database,
        charset="utf8mb4",
        cursorclass=DictCursor,
        connect_timeout=3,
        read_timeout=5,
        autocommit=True,
    )
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT p.id, p.name, p.sub_title, p.brand, p.sales_price, p.stock,
                       p.category_id, c.name AS category_name
                FROM products p
                LEFT JOIN categories c ON p.category_id = c.id
                WHERE p.shop_id = %s AND p.listing_status = 1 AND p.stock > 0
                """,
                (settings.shop_id,),
            )
            rows = cursor.fetchall()
    finally:
        conn.close()

    # Decimal -> float，避免后续比较/序列化麻烦
    for row in rows:
        row["sales_price"] = float(row["sales_price"] or 0)
    return rows


async def get_products() -> list[dict[str, Any]]:
    """带缓存地读取上架商品；缓存过期或为空时重新查库。"""
    global _cache, _cache_at
    now = time.monotonic()
    if _cache and now - _cache_at < settings.product_cache_ttl:
        return _cache

    async with _cache_lock:
        now = time.monotonic()
        if _cache and now - _cache_at < settings.product_cache_ttl:
            return _cache
        try:
            _cache = await asyncio.to_thread(_load_on_sale)
            _cache_at = time.monotonic()
            logger.info("商品缓存已刷新：%s 条", len(_cache))
        except Exception:
            logger.exception("读取商品失败，沿用旧缓存")
            if not _cache:
                return []
    return _cache


def _score(product: dict[str, Any], message_tokens: set[str], raw_message: str) -> int:
    """商品与用户消息的相关度：token 交集 + 品牌/分类加权。"""
    text = " ".join(filter(None, [
        product.get("name"), product.get("sub_title"),
        product.get("brand"), product.get("category_name"),
    ]))
    score = len(message_tokens & _tokens(text))
    brand = (product.get("brand") or "").lower()
    if brand and brand in raw_message.lower():
        score += 3
    category = (product.get("category_name") or "").lower()
    if category and category in raw_message.lower():
        score += 2
    return score


def _spread_by_category(products: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    """没有关键词命中时的兜底：每个分类挑一个，保证推荐不扎堆。"""
    picked: list[dict[str, Any]] = []
    seen_categories: set[Any] = set()
    for product in products:
        category = product.get("category_name") or product.get("category_id")
        if category in seen_categories:
            continue
        seen_categories.add(category)
        picked.append(product)
        if len(picked) >= limit:
            break
    return picked


async def search_products(message: str, limit: int = 6) -> list[dict[str, Any]]:
    """按用户消息检索商品，返回最多 limit 条。"""
    products = await get_products()
    if not products:
        return []

    min_price, max_price = _parse_price_range(message)
    candidates = [
        p for p in products
        if (min_price is None or p["sales_price"] >= min_price)
        and (max_price is None or p["sales_price"] <= max_price)
    ]

    message_tokens = _tokens(message)
    scored = []
    for product in candidates:
        score = _score(product, message_tokens, message)
        if score > 0:
            scored.append((score, product))

    if scored:
        # 相关度优先，同分时价格低的在前
        scored.sort(key=lambda item: (-item[0], item[1]["sales_price"]))
        return [product for _, product in scored[:limit]]

    # 明确表达「想看看」但没匹配到词：给各分类各一个兜底
    if any(word in message for word in _RECOMMEND_WORDS):
        candidates.sort(key=lambda p: p["sales_price"])
        return _spread_by_category(candidates, limit)

    return []
