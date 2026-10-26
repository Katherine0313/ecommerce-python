# 杰米多多导购机器人（recommender-python）

商城小程序的 AI 导购服务：**FastAPI + LangGraph**，负责多轮对话和商品问答。商品数据直连 MySQL（只读），会话历史存 Redis，模型走硅基流动的 OpenAI 兼容接口。

> 本仓库早期是「客户分群 + 商品推荐」实验项目，2026-10-26 已全部清空，现在只服务导购机器人。

## 架构

```
小程序 → Cloudflare → Nginx api.jiemids.com → gateway:8080
                                                  └─ /api/agent/** → ai-service:8000（本服务）
                                                                        ├─ MySQL 只读（products/categories）
                                                                        ├─ Redis（多轮历史）
                                                                        └─ 硅基流动 DeepSeek
```

## 目录

```
app/
├── main.py              # FastAPI 入口、CORS、健康检查
├── config.py            # 环境变量
├── api/agent.py         # /api/agent/chat 与 /chat/stream
├── graph/guide.py       # LangGraph：检索 → 生成
├── graph/state.py       # 图状态
├── db/product_repo.py   # 商品只读查询 + 60s 内存缓存
├── services/history.py  # Redis 会话历史
├── services/sse.py      # SSE 分帧
└── prompts.py           # system prompt 与商品上下文
```

## 本地运行

```bash
# 1. 依赖（已有 .venv 则跳过）
python -m venv .venv
.venv/Scripts/pip install -r requirements.txt      # Windows
# source .venv/bin/activate && pip install -r requirements.txt   # macOS/Linux

# 2. 配置
cp .env.example .env    # 填 MySQL / Redis / SILICONFLOW_API_KEY

# 3. 启动
.venv/Scripts/python -m uvicorn app.main:app --port 8000
```

### 经 Java 网关联调

网关已配置 `/api/agent/**` 默认转发到 `http://127.0.0.1:8000`，本地不用再配环境变量；Docker 里由
`deploy/docker-compose.yml` 的 `AI_SERVICE_URL=http://ai-service:8000` 覆盖。

```bash
# 先起 Python（8000），再在 IDEA 起 gateway（8080）
curl -X POST http://127.0.0.1:8080/api/agent/chat \
  -H 'Content-Type: application/json' \
  -d '{"message":"hello","sessionId":"gw-test"}'
```

小程序联调要用 HBuilderX 的「运行 → 小程序模拟器」（`dist/dev`，指向 `127.0.0.1:8080`）；
「发行」包固定指向线上 `api.jiemids.com`，测不到本地服务。

## 接口

前端契约保持不变，小程序零改动。

| 接口 | 说明 |
|---|---|
| `POST /api/agent/chat` | 非流式，返回 `{"success": true, "data": {"reply": "..."}}` |
| `POST /api/agent/chat/stream` | SSE，逐块 `data: <纯文本>`，LF 行尾 |
| `GET /health` | 健康检查 |

请求体：`{"message": "...", "sessionId": "...", "scene": "customer-service"}`。`sessionId` 由前端生成，用于 Redis 多轮历史（12 条 / 2 小时）。

## 鉴权与防滥用

对话接口**必须带登录令牌**，否则直接 401，避免匿名调用消耗模型额度：

```
Authorization: Bearer <小程序用户 JWT>
```

- 令牌与 Java 服务共用 `JWT_SECRET`（只校验，不签发），要求 `type=user`
- 每用户限流：10 分钟 20 次、每天 100 次（Redis 计数，超出 429）
- 全局并发：同时最多 10 条对话，超出 503
- 单条消息 ≤ 500 字；单次回复 ≤ 600 token
- 首字 12 秒、整条 55 秒超时，超时直接结束，让前端走本地规则兜底
- Redis 故障时限流放行（可用性优先），但会打告警日志

相关环境变量见 `.env.example` 的 `JWT_SECRET` / `AI_*` 段。

## 验证

```bash
curl -s http://127.0.0.1:8000/api/agent/chat \
  -H 'Content-Type: application/json' \
  -d '{"message":"hello","sessionId":"t1"}'

curl -N http://127.0.0.1:8000/api/agent/chat/stream \
  -H 'Content-Type: application/json' \
  -d '{"message":"hello","sessionId":"t1"}'
```

Windows 终端直接内联中文可能被 GBK 影响，建议把 JSON 写到文件后用 `--data-binary @req.json`。

## 部署

部署到 `/www/java/ai-service/`，只上传 `app/`、`requirements.txt`、`Dockerfile`、`.dockerignore`；`docker-compose.yml` 里的 `ai-service` 会读取统一的 `.env`。

网关按静态地址转发，不注册 Nacos：本地默认 `http://127.0.0.1:8000`，Docker 里由 compose 的
`AI_SERVICE_URL` 覆盖为 `http://ai-service:8000`。
