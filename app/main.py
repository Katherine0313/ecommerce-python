"""FastAPI 入口。"""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.agent import router as agent_router
from app.api.ai_chat import router as ai_chat_router
from app.services import redis_client

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)


@asynccontextmanager
async def lifespan(_: FastAPI):
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
