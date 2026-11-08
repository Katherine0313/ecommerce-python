"""优惠券 / 促销活动实时查询。

知识库里的券和活动是静态文案，运营在后台改活动后会立刻过期。
这里直接读 coupon_template / promotion / promotion_item，保证回答与后台配置一致。
"""

import asyncio
import logging
from datetime import datetime, timezone

import pymysql
from pymysql.cursors import DictCursor

from app.config import settings

logger = logging.getLogger(__name__)


def _connect() -> pymysql.connections.Connection:
    return pymysql.connect(
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


def _fmt_date(value) -> str:
    if not isinstance(value, datetime):
        return ""
    return f"{value.month}月{value.day}日"


def _coupon_text(coupon: dict) -> str:
    """一张券的中文说明：名称、优惠力度、门槛、限领、有效期。"""
    rate = coupon.get("discount_rate")
    face = coupon.get("face_value")
    if coupon.get("type") == "DISCOUNT" and rate:
        rate = float(rate)
        face_text = f"{rate * 10:.1f} 折" if rate <= 1 else f"{rate / 10:.1f} 折"
    else:
        face_text = f"减 {float(face or 0):g} 元"
    threshold = float(coupon.get("threshold_amount") or 0)
    cond = "无门槛" if threshold <= 0 else f"满 {threshold:g} 元可用"
    limit = int(coupon.get("per_user_limit") or 1)
    end = _fmt_date(coupon.get("valid_end"))
    tail = f"，有效期至{end}" if end else ""
    return f"{coupon['name']}（{face_text}，{cond}，每人限领 {limit} 张{tail}）"


def _promotion_text(promotion: dict, items: list[dict]) -> str:
    tag = promotion.get("tag_name") or promotion.get("name") or "促销"
    end = _fmt_date(promotion.get("end_time"))
    tail = f"（至{end}）" if end else ""
    if not items:
        return f"「{tag}」活动{tail}"
    detail = "、".join(
        f"{item['name']} {float(item['promotion_price']):g} 元" for item in items
    )
    return f"「{tag}」活动{tail}：{detail}"


def _fetch() -> tuple[list[dict], list[dict], list[dict]]:
    """读取当前进行中的券和活动。MySQL 存的是 UTC，不能用 NOW()。"""
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    conn = _connect()
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT id, name, type, face_value, discount_rate, threshold_amount,
                       per_user_limit, valid_end
                FROM coupon_template
                WHERE is_deleted = 0 AND manual_status = 0
                  AND receive_start <= %s AND receive_end > %s
                  AND remain_quantity > 0
                ORDER BY id
                """,
                (now, now),
            )
            coupons = cursor.fetchall()

            cursor.execute(
                """
                SELECT id, name, tag_name, start_time, end_time
                FROM promotion
                WHERE is_deleted = 0 AND manual_status = 0
                  AND start_time <= %s AND end_time > %s
                ORDER BY id
                """,
                (now, now),
            )
            promotions = cursor.fetchall()

            items: list[dict] = []
            if promotions:
                ids = [p["id"] for p in promotions]
                placeholders = ",".join(["%s"] * len(ids))
                cursor.execute(
                    f"""
                    SELECT pi.promotion_id, pi.promotion_price, p.name
                    FROM promotion_item pi
                    JOIN products p ON p.id = pi.product_id
                    WHERE pi.promotion_id IN ({placeholders})
                    ORDER BY pi.id
                    """,
                    ids,
                )
                items = cursor.fetchall()
    finally:
        conn.close()
    return coupons, promotions, items


def _build_answer() -> str:
    coupons, promotions, items = _fetch()
    parts: list[str] = []
    if coupons:
        marks = "①②③④⑤"
        detail = "；".join(
            f"{marks[idx] if idx < len(marks) else f'{idx + 1}. '}{_coupon_text(c)}"
            for idx, c in enumerate(coupons)
        )
        parts.append(f"当前有 {len(coupons)} 张优惠券可领：{detail}")
    for p in promotions:
        parts.append(_promotion_text(p, [i for i in items if i["promotion_id"] == p["id"]]))
    if not parts:
        return "当前暂无进行中的优惠券或促销活动，全场包邮长期有效，有新活动会在首页展示。"
    parts.append("领取后可在「我的-优惠券」查看，下单结算时勾选使用，一笔订单限用 1 张。")
    return "。".join(parts)


async def promo_answer() -> str:
    """优惠券/活动类问题的实时答案；查询失败返回空串，调用方退回知识库。"""
    try:
        return await asyncio.to_thread(_build_answer)
    except Exception:
        logger.warning("优惠券/活动实时查询失败", exc_info=True)
        return ""
