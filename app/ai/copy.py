"""商品文案生成：后台 CopyAssistant 用。

流程：内部接口取商品 → 大模型按场景生成 → 调用方决定回填（前端确认后再走
update_product / 后台商品编辑接口），本模块只生成不落库。
"""

import json
import logging
import re
from typing import Any, AsyncGenerator, Optional

from app import internal_client
from app.ai import repo
from app.ai.client import get_llm
from app.config import settings

logger = logging.getLogger(__name__)

_SCENE_PROMPT = {
    "title": "为商品起一个吸引人的名称，不超过 20 个字，只输出名称本身",
    "subtitle": "为商品写一句副标题，突出卖点，不超过 30 个字，只输出副标题本身",
    "description": "为商品写一段详情描述，150 字左右，分 3 个自然段，只输出描述本身",
    "all": "为商品分别生成：name(名称，≤20字)、subTitle(副标题，≤30字)、description(详情，150字左右)。"
           "严格输出 JSON：{\"name\":\"...\",\"subTitle\":\"...\",\"description\":\"...\"}，不要输出其他内容",
}


async def generate(
    product_id: int,
    scene: str,
    extra_requirement: Optional[str] = None,
    admin_id: Optional[int] = None,
) -> dict[str, Any]:
    """非流式生成。返回 {product, fields: {name?, subTitle?, description?}}。"""
    start = repo.monotonic_ms()
    try:
        product = await internal_client.get_product(product_id)
        if not product:
            return {"found": False, "message": "商品不存在"}

        scene = scene if scene in _SCENE_PROMPT else "all"
        name = product.get("name") or ""
        category = product.get("categoryName") or product.get("category_name") or ""
        prompt = (
            f"你是资深电商文案。商品信息：名称「{name}」，分类「{category}」，"
            f"价格 ¥{product.get('price')}，已有副标题「{product.get('subTitle') or product.get('sub_title') or '无'}」。\n"
            f"任务：{_SCENE_PROMPT[scene]}"
        )
        if extra_requirement:
            prompt += f"\n额外要求：{extra_requirement}"

        llm = get_llm(temperature=0.9)
        resp = await llm.ainvoke(prompt)
        text = str(resp.content or "").strip()

        fields: dict[str, str] = {}
        if scene == "all":
            fields = _parse_all(text)
        else:
            key = {"title": "name", "subtitle": "subTitle", "description": "description"}[scene]
            fields[key] = _strip_quotes(text)

        repo.save_call_log(
            scene="generate_copy", model=settings.chat_model,
            duration_ms=repo.monotonic_ms() - start, success=True, admin_id=admin_id,
        )
        return {"found": True, "productId": product_id, "productName": name, "scene": scene, "fields": fields}
    except Exception as exc:
        logger.exception("文案生成失败 product=%s", product_id)
        repo.save_call_log(
            scene="generate_copy", model=settings.chat_model,
            duration_ms=repo.monotonic_ms() - start, success=False,
            admin_id=admin_id, error_msg=str(exc),
        )
        raise


def _strip_quotes(text: str) -> str:
    return text.strip().strip('"').strip("「」").strip()


def _parse_all(text: str) -> dict[str, str]:
    """容错解析大模型返回的 JSON。"""
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        return {"description": _strip_quotes(text)}
    try:
        data = json.loads(match.group(0))
        return {k: _strip_quotes(str(v)) for k, v in data.items() if k in ("name", "subTitle", "description") and v}
    except (ValueError, TypeError):
        return {"description": _strip_quotes(text)}
