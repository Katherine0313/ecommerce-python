"""FastAPI 入口。"""

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.agent import router as agent_router
from app.api.ai_chat import router as ai_chat_router
from app.kb import embedding as kb_embedding
from app.kb.repo import load_entries
from app.services import redis_client

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)

logger = logging.getLogger(__name__)
# 持有引用，避免后台索引任务被垃圾回收
_warm_task: asyncio.Task | None = None


@asynccontextmanager
async def lifespan(_: FastAPI):
    global _warm_task
    # 后台预热知识库语义索引，失败不影响启动（检索会自动退化为词面匹配）
    try:
        entries = await load_entries()
        _warm_task = asyncio.create_task(kb_embedding.warm_up(entries))
    except Exception:
        logger.warning("知识库语义索引预热失败", exc_info=True)
    yield
    await redis_client.close()


app = FastAPI(title="杰米多多导购机器人", version="1.0.0", lifespan=lifespan)

# H5 降级路径跨域；小程序请求不校验 CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(agent_router)
app.include_router(ai_chat_router)


@app.get("/health")
async def health():
    return {"status": "ok"}
