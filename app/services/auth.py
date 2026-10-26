"""JWT 校验：与 Java 服务共用 JWT_SECRET（jjwt 签发的 HS256/384/512）。

只做校验，不签发。要求 type=user，避免后台 admin 令牌混用。
"""

import logging
from typing import Any, Optional

import jwt

from app.config import settings

logger = logging.getLogger(__name__)


class AuthError(Exception):
    """鉴权失败：status_code 直接映射到 HTTP 状态码。"""

    def __init__(self, message: str, status_code: int = 401):
        super().__init__(message)
        self.message = message
        self.status_code = status_code


def _extract_bearer(authorization: Optional[str]) -> Optional[str]:
    """取 Authorization 头里的令牌，兼容带/不带 Bearer 前缀。"""
    if not authorization or not authorization.strip():
        return None
    value = authorization.strip()
    if value.lower().startswith("bearer "):
        return value[7:].strip()
    return value


def require_user(authorization: Optional[str]) -> str:
    """校验令牌并返回用户 ID（subject）。失败抛 AuthError。"""
    if not settings.jwt_secret:
        # 配置错误不能当成登录失败，给 500 方便排查
        raise AuthError("服务未配置 JWT_SECRET", 500)

    token = _extract_bearer(authorization)
    if not token:
        raise AuthError("未登录或登录已过期", 401)

    try:
        payload: dict[str, Any] = jwt.decode(
            token,
            settings.jwt_secret,
            algorithms=["HS256", "HS384", "HS512"],
            options={"require": ["sub", "exp"]},
        )
    except jwt.ExpiredSignatureError:
        raise AuthError("登录已过期，请重新登录", 401)
    except jwt.InvalidTokenError:
        raise AuthError("未登录或登录已过期", 401)

    if payload.get("type") != "user":
        raise AuthError("无权访问", 403)

    subject = str(payload.get("sub") or "").strip()
    if not subject or not subject.isdigit():
        raise AuthError("登录信息不完整，请重新登录", 401)
    return subject
