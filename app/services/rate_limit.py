"""按用户限流：Redis 固定窗口，同时限制短窗口和每日总量。

Redis 故障时放行（可用性优先），并打告警日志。
"""

import datetime
import logging
import time

from app.config import settings
from app.services.redis_client import get_client

logger = logging.getLogger(__name__)

_WINDOW_PREFIX = "agent:rate:window:"
_DAY_PREFIX = "agent:rate:day:"


async def check_and_consume(user_id: str) -> tuple[bool, int, int]:
    """检查并消耗一次额度。

    返回 (是否放行, 短窗口剩余, 今日剩余)；Redis 异常时直接放行。
    """
    window = max(1, settings.rate_limit_window_seconds)
    now = time.time()
    bucket = int(now // window)
    key_window = f"{_WINDOW_PREFIX}{user_id}:{bucket}"
    day = datetime.datetime.now().strftime("%Y%m%d")
    key_day = f"{_DAY_PREFIX}{user_id}:{day}"

    try:
        client = get_client()
        async with client.pipeline(transaction=False) as pipe:
            pipe.incr(key_window)
            pipe.expire(key_window, window * 2)
            pipe.incr(key_day)
            pipe.expire(key_day, 86400 * 2)
            window_count, _, day_count, _ = await pipe.execute()
    except Exception:
        logger.warning("限流检查失败，放行 user=%s", user_id, exc_info=True)
        return True, settings.rate_limit_per_window, settings.rate_limit_per_day

    allowed = (
        window_count <= settings.rate_limit_per_window
        and day_count <= settings.rate_limit_per_day
    )
    return (
        allowed,
        max(0, settings.rate_limit_per_window - window_count),
        max(0, settings.rate_limit_per_day - day_count),
    )
