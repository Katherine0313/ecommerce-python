"""AI 生图：硅基流动 /v1/images/generations（OpenAI 兼容协议）。

生成图下载后落盘 settings.ai_image_dir（与 Java UploadServiceImpl 的 upload/image/ 目录
约定平级），返回可直接访问的 /image/ai/{file} 相对 url；前端预览后决定设主图 / 插详情 / 下载。
注意：AI_IMAGE_DIR 若用环境变量覆盖，必须仍位于 Java 的 upload/image/ 托管路径之下，
否则 /image/ai/** 无法访问。
"""

import asyncio
import logging
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

import httpx

from app import internal_client
from app.ai import repo
from app.config import settings

logger = logging.getLogger(__name__)

# 场景 → 默认尺寸（前端可显式传 size 覆盖）
_SCENE_SIZE = {
    "main": "1024x1024",    # 商品主图
    "detail": "1024x1024",  # 详情图（插 body 富文本）
    "poster": "1024x1536",  # 营销海报
    "banner": "1280x720",   # 首页 Banner
}

# 通用电商安全约束：规避人物肖像与品牌侵权风险
_SAFE_SUFFIX = "画面干净专业，电商摄影风格，纯白或简约场景背景，不含人物肖像，不含任何品牌 logo 和商标文字。"

_TIMEOUT = 120.0


async def generate_images(
    prompt: str,
    scene: str = "main",
    size: Optional[str] = None,
    count: int = 1,
    product_id: Optional[int] = None,
    product: Optional[dict] = None,
    model_key: Optional[str] = None,
    admin_id: Optional[int] = None,
) -> dict[str, Any]:
    """生成并落盘，返回 {images: [{url}], model, size, prompt}。"""
    start = repo.monotonic_ms()
    model = settings.image_model_fast if model_key == "fast" else settings.image_model
    final_size = size or _SCENE_SIZE.get(scene, "1024x1024")
    try:
        full_prompt = await _build_prompt(prompt, product_id, product)
        remote_urls = await _request_images(model, full_prompt, final_size, count)
        images = [{"url": url} for url in [await _save(u) for u in remote_urls]]
        repo.save_call_log(
            scene="generate_image", model=model,
            duration_ms=repo.monotonic_ms() - start, success=True,
            admin_id=admin_id, error_msg=f"size={final_size} count={len(images)}",
        )
        return {"images": images, "model": model, "size": final_size, "prompt": full_prompt}
    except Exception as exc:
        logger.exception("AI 生图失败 scene=%s model=%s", scene, model)
        repo.save_call_log(
            scene="generate_image", model=model,
            duration_ms=repo.monotonic_ms() - start, success=False,
            admin_id=admin_id, error_msg=str(exc),
        )
        raise


async def _build_prompt(prompt: str, product_id: Optional[int], product: Optional[dict]) -> str:
    """商品场景自动拼「商品名 + 卖点 + 电商摄影风格」；运营自定义 prompt 也保留。"""
    info = product or {}
    if product_id:
        info = await internal_client.get_product(int(product_id)) or {}
    name = info.get("name") or ""
    selling = str(info.get("sellingPoints") or "").strip() or info.get("subTitle") or info.get("sub_title") or ""
    parts: list[str] = []
    if name:
        parts.append(f"商品「{name}」")
        if selling:
            parts.append(f"，卖点：{selling}")
        parts.append("。")
    if prompt.strip():
        parts.append(prompt.strip())
    parts.append(_SAFE_SUFFIX)
    return "".join(parts)


async def _request_images(model: str, prompt: str, size: str, count: int) -> list[str]:
    payload = {
        "model": model,
        "prompt": prompt,
        "image_size": size,
        "batch_size": min(max(1, count), 4),
    }
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        resp = await client.post(
            f"{settings.openai_base_url.rstrip('/')}/images/generations",
            headers={"Authorization": f"Bearer {settings.siliconflow_api_key}"},
            json=payload,
        )
        resp.raise_for_status()
        body = resp.json()
    # 硅基流动返回 {"images": [{url}]}；OpenAI 兼容返回 {"data": [{url}]}，两种都接
    items = body.get("images") or body.get("data") or []
    urls = [item["url"] for item in items if item.get("url")]
    if not urls:
        raise RuntimeError("生图接口未返回图片 url")
    return urls


async def _save(remote_url: str) -> str:
    """下载生成图并落盘，返回 /image/ai/{file} 相对 url。"""
    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.get(remote_url)
        resp.raise_for_status()
        content = resp.content
        content_type = resp.headers.get("content-type", "")
    ext = ".png" if "png" in content_type else ".jpg"
    out_dir = Path(settings.ai_image_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    name = f"{datetime.now():%Y%m%d}_{uuid.uuid4().hex[:8]}{ext}"
    await asyncio.to_thread(out_dir.joinpath(name).write_bytes, content)
    return f"/image/ai/{name}"
