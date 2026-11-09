"""商品文案生成：后台 CopyAssistant / AddGoods 内嵌 AI 入口共用。

流程：内部接口取商品（或直接用前端内联商品信息）→ 大模型按场景生成 → 调用方决定回填
（前端确认后再走 update_product / 后台商品编辑接口），本模块只生成不落库。
"""

import json
import logging
import re
from typing import Any, Optional

from app import internal_client
from app.ai import repo
from app.ai.client import get_llm
from app.config import settings

logger = logging.getLogger(__name__)

_SCENE_PROMPT = {
    "name": "为商品起一个吸引人的名称，不超过 20 个字，只输出名称本身",
    "subTitle": "为商品写一句副标题，突出卖点，不超过 30 个字，只输出副标题本身",
    "title": "为商品写一条主标题（抬头），不超过 30 个字，一条一行、不堆砌关键词，只输出主标题本身",
    "description": "为商品写一段详情描述，150 字左右，分 3 个自然段，只输出描述本身",
    "all": "为商品分别生成：name(名称，≤20字)、subTitle(副标题，≤30字)、title(主标题，≤30字)、description(详情，150字左右)。"
           "严格输出 JSON：{\"name\":\"...\",\"subTitle\":\"...\",\"title\":\"...\",\"description\":\"...\"}，不要输出其他内容",
}

# 旧 scene key 兼容（长期有效）：subtitle 是旧版「副标题」的 key，映射到 subTitle。
# 旧版 title（含义=名称）与新版 title（主标题）冲突，统一按新义处理；要生成名称请传 name。
_SCENE_ALIAS = {"subtitle": "subTitle"}

# 主标题长度口径与前端 AddGoods / EditModal 的 maxLength 一致，AI 侧硬截断兜底
_TITLE_MAX = 30


def _normalize_scene(scene: str) -> str:
    scene = (scene or "all").strip()
    if scene in _SCENE_ALIAS:
        logger.warning("scene=%s 是废弃旧 key，已按 %s 处理", scene, _SCENE_ALIAS[scene])
        scene = _SCENE_ALIAS[scene]
    return scene if scene in _SCENE_PROMPT else "all"


async def generate(
    product_id: Optional[int] = None,
    scene: str = "all",
    extra_requirement: Optional[str] = None,
    admin_id: Optional[int] = None,
    product: Optional[dict] = None,
) -> dict[str, Any]:
    """非流式生成。返回 {found, productId?, productName, scene, fields: {name?, subTitle?, title?, description?}}。

    product_id 优先（查库取商品信息）；没有 product_id 时用内联 product（新增商品无 ID），
    内联模式不查库，found 恒为 True。
    """
    start = repo.monotonic_ms()
    scene = _normalize_scene(scene)
    try:
        if product_id:
            product = await internal_client.get_product(int(product_id))
            if not product:
                return {"found": False, "message": "商品不存在"}

        info = product or {}
        name = info.get("name") or ""
        category = info.get("categoryName") or info.get("category_name") or ""
        sub_title = info.get("subTitle") or info.get("sub_title") or "无"
        selling_points = str(info.get("sellingPoints") or "").strip()
        prompt = (
            f"你是资深电商文案。商品信息：名称「{name}」，分类「{category}」，"
            f"价格 ¥{info.get('price') or '待定'}，已有副标题「{sub_title}」。\n"
        )
        if selling_points:
            prompt += f"卖点：{selling_points}\n"
        prompt += f"任务：{_SCENE_PROMPT[scene]}"
        if extra_requirement:
            prompt += f"\n额外要求：{extra_requirement}"

        llm = get_llm(temperature=0.9)
        resp = await llm.ainvoke(prompt)
        text = str(resp.content or "").strip()

        fields: dict[str, str] = {}
        if scene == "all":
            fields = _parse_all(text)
        else:
            fields[scene] = _strip_quotes(text)
        # 主标题硬截断：prompt 约束不可靠，超 30 字落库会被前端 maxLength 卡住
        if fields.get("title") and len(fields["title"]) > _TITLE_MAX:
            logger.info("主标题超长 %s 字，截断到 %s：%s", len(fields["title"]), _TITLE_MAX, fields["title"])
            fields["title"] = fields["title"][:_TITLE_MAX]

        repo.save_call_log(
            scene="generate_copy", model=settings.chat_model,
            duration_ms=repo.monotonic_ms() - start, success=True, admin_id=admin_id,
        )
        return {"found": True, "productId": product_id, "productName": name, "scene": scene, "fields": fields}
    except Exception as exc:
        logger.exception("文案生成失败 product=%s scene=%s", product_id or "(内联)", scene)
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
        return {
            k: _strip_quotes(str(v))
            for k, v in data.items()
            if k in ("name", "subTitle", "title", "description") and v
        }
    except (ValueError, TypeError):
        return {"description": _strip_quotes(text)}
