"""服务配置：统一从环境变量 / .env 读取，不在代码里写死凭据。"""

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

# 项目根目录（app/ 的上一级），用于定位 .env
BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")


def _env(name: str, default: str = "") -> str:
    """读环境变量，空字符串按未设置处理，避免把空值传给驱动。"""
    value = os.getenv(name)
    return value if value not in (None, "") else default


@dataclass(frozen=True)
class Settings:
    # MySQL：只读查询商品数据。沿用后端 .env 里的 DB_USERNAME/DB_PASSWORD
    mysql_host: str = _env("MYSQL_HOST", "127.0.0.1")
    mysql_port: int = int(_env("MYSQL_PORT", "3306"))
    mysql_database: str = _env("MYSQL_DATABASE", "e_commerce_db")
    db_username: str = _env("DB_USERNAME", "e_commerce_db")
    db_password: str = _env("DB_PASSWORD", "123456")

    # Redis：会话历史。沿用后端 .env 的变量名，部署时不用维护两份密码
    redis_host: str = _env("REDIS_HOST", "127.0.0.1")
    redis_port: int = int(_env("REDIS_PORT", "6379"))
    redis_password: str = _env("SPRING_DATA_REDIS_PASSWORD", _env("REDIS_PASSWORD", ""))

    # 商品缓存刷新间隔（秒）：商品少，60 秒足够，避免每次请求都扫库
    product_cache_ttl: float = float(_env("PRODUCT_CACHE_TTL", "60"))

    # 知识库缓存刷新间隔（秒）：运营改库后最多这么久生效
    kb_cache_ttl: float = float(_env("KB_CACHE_TTL", "60"))

    # ===== 鉴权与防滥用 =====
    # 与 Java 服务共用同一个 JWT_SECRET，只校验不签发
    jwt_secret: str = _env("JWT_SECRET", "")
    # 单条用户消息最大长度，防止异常长文本拖慢检索
    max_message_chars: int = int(_env("AI_MAX_MESSAGE_CHARS", "500"))
    # 全局同时进行的对话数，超出直接 503
    max_concurrent_chats: int = int(_env("AI_MAX_CONCURRENT_CHATS", "10"))
    # 每用户限流：短窗口 + 每日总量
    rate_limit_window_seconds: int = int(_env("AI_RATE_WINDOW_SECONDS", "600"))
    rate_limit_per_window: int = int(_env("AI_RATE_PER_WINDOW", "20"))
    rate_limit_per_day: int = int(_env("AI_RATE_PER_DAY", "100"))
    # 整条回复的超时，必须小于前端 60s 看门狗
    stream_total_timeout_seconds: float = float(_env("AI_STREAM_TOTAL_TIMEOUT", "55"))

    # ===== 大模型（硅基流动，OpenAI 兼容协议）=====
    siliconflow_api_key: str = _env("SILICONFLOW_API_KEY", "")
    openai_base_url: str = _env("OPENAI_BASE_URL", "https://api.siliconflow.cn/v1")
    chat_model: str = _env("CHAT_MODEL", "deepseek-ai/DeepSeek-V4-Flash")
    chat_temperature: float = float(_env("CHAT_TEMPERATURE", "0.7"))
    ai_max_output_tokens: int = int(_env("AI_MAX_OUTPUT_TOKENS", "2048"))
    embedding_model: str = _env("EMBEDDING_MODEL", "BAAI/bge-m3")

    # Java 内部数据接口（/internal/ai/**）：共享密钥 + 网关地址
    # 本地开发走网关 8080；Docker 里由 AI_INTERNAL_BASE_URL 覆盖为 http://gateway:8080
    ai_internal_base_url: str = _env("AI_INTERNAL_BASE_URL", "http://127.0.0.1:8080")
    ai_internal_token: str = _env("AI_INTERNAL_TOKEN", "change-me-in-prod")


settings = Settings()
