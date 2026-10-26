"""共享 Redis 客户端：会话历史、限流都用同一份连接。"""

import logging
from typing import Optional

import redis.asyncio as redis

from app.config import settings

logger = logging.getLogger(__name__)

_client: Optional[redis.Redis] = None


def get_client() -> redis.Redis:
    """懒加载；超时设短，避免 Redis 异常拖慢首字。"""
    global _client
    if _client is None:
        _client = redis.Redis(
            host=settings.redis_host,
            port=settings.redis_port,
            password=settings.redis_password or None,
            decode_responses=True,
            # 本地老版本 Redis 不支持 RESP3 的 HELLO，固定用 RESP2 兼容
            protocol=2,
            socket_connect_timeout=0.5,
            socket_timeout=0.5,
        )
    return _client


async def close() -> None:
    """应用关闭时释放连接。"""
    global _client
    if _client is not None:
        try:
            await _client.aclose()
        except Exception:
            logger.debug("关闭 Redis 连接失败", exc_info=True)
        finally:
            _client = None
