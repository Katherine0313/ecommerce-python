"""LLM 客户端：硅基流动 OpenAI 兼容协议。

密钥只在服务端配置，前端永远不接触（项目硬约束）。
"""

from langchain_openai import ChatOpenAI

from app.config import settings


def get_llm(streaming: bool = False, temperature: float | None = None) -> ChatOpenAI:
    return ChatOpenAI(
        model=settings.chat_model,
        api_key=settings.siliconflow_api_key,
        base_url=settings.openai_base_url,
        temperature=settings.chat_temperature if temperature is None else temperature,
        max_tokens=settings.ai_max_output_tokens,
        streaming=streaming,
        timeout=settings.stream_total_timeout_seconds,
    )
