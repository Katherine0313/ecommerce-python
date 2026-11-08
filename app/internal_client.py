"""Java 后端 /internal/ai/** 内部数据接口客户端。

AI 工具的所有数据都来自 Java 数据库接口，不允许大模型生成业务数据。
鉴权方式：X-Internal-Token 共享密钥（仅内网可达，公网 nginx 不代理 /internal/**）。

Java Result 结构：{success: bool, data, message, code(字符串错误码)}
404 语义：HTTP 404 + success=false + message 文案。
"""

import logging
from typing import Any, Optional

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

_TIMEOUT = 10.0


class InternalNotFoundError(Exception):
    """目标资源不存在（Java 侧 404）。"""


def _headers() -> dict[str, str]:
    return {
        "X-Internal-Call": "true",
        "X-Internal-Token": settings.ai_internal_token,
    }


def _parse(resp: httpx.Response, path: str) -> Any:
    try:
        body = resp.json()
    except ValueError:
        body = {}
    if resp.status_code == 404:
        raise InternalNotFoundError(body.get("message") or "资源不存在")
    if resp.status_code != 200 or not body.get("success"):
        raise RuntimeError(f"内部接口调用失败 {path}: HTTP {resp.status_code} {body.get('message')}")
    return body.get("data")


async def _get(path: str, params: Optional[dict[str, Any]] = None) -> Any:
    url = f"{settings.ai_internal_base_url}{path}"
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        resp = await client.get(url, headers=_headers(), params=params)
    return _parse(resp, path)


async def _put(path: str, payload: dict[str, Any]) -> Any:
    url = f"{settings.ai_internal_base_url}{path}"
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        resp = await client.put(url, headers=_headers(), json=payload)
    return _parse(resp, path)


async def get_product(product_id: int) -> Optional[dict]:
    """商品详情；不存在返回 None。"""
    try:
        return await _get(f"/internal/ai/product/{product_id}")
    except InternalNotFoundError:
        return None


async def list_categories() -> list[dict]:
    """C 端分类树（一级含 children）。用于把「手机」这类品类词映射成分类 ID。"""
    data = await _get("/api/categories")
    return data or []


async def search_products(
    keyword: Optional[str] = None,
    category_id: Optional[int] = None,
    min_price: Optional[float] = None,
    max_price: Optional[float] = None,
    order_by: str = "comprehensive",
    page: int = 1,
    size: int = 6,
) -> dict:
    params: dict[str, Any] = {"page": page, "size": size, "orderBy": order_by}
    if keyword:
        params["keyword"] = keyword
    if category_id:
        params["categoryId"] = category_id
    if min_price is not None:
        params["minPrice"] = min_price
    if max_price is not None:
        params["maxPrice"] = max_price
    return await _get("/internal/ai/products", params)


_FIELD_TO_JAVA = {"name": "name", "sub_title": "subTitle", "subTitle": "subTitle", "description": "description"}


async def update_product(product_id: int, fields: dict[str, Any]) -> dict:
    """白名单文案字段更新：name / subTitle / description（自动转 Java 驼峰）。"""
    payload = {_FIELD_TO_JAVA.get(k, k): v for k, v in fields.items() if k in _FIELD_TO_JAVA}
    return await _put(f"/internal/ai/product/{product_id}", payload)


async def get_stock(sku_id: int) -> Optional[dict]:
    try:
        return await _get(f"/internal/ai/stock/{sku_id}")
    except InternalNotFoundError:
        return None


async def list_orders(user_id: int, keyword: Optional[str] = None, limit: int = 5) -> list:
    params: dict[str, Any] = {"userId": user_id, "limit": limit}
    if keyword:
        params["keyword"] = keyword
    return await _get("/internal/ai/orders", params)


async def get_aftersale(aftersale_id: int, user_id: Optional[int] = None) -> Optional[dict]:
    params: dict[str, Any] = {}
    if user_id is not None:
        params["userId"] = user_id
    try:
        return await _get(f"/internal/ai/aftersale/{aftersale_id}", params)
    except InternalNotFoundError:
        return None
