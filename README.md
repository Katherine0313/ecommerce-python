# 杰米多多客服机器人（ecommerce-python）

商城小程序的 AI 客服服务：**FastAPI + 知识库检索**。回答全部来自 MySQL 的 `kb_qa` 表，
**不调用任何大模型**（微信侧不允许接入），未命中统一兜底并引导人工客服。

> 本仓库早期是「客户分群 + 商品推荐」实验项目，2026-10-26 已全部清空，现在只服务客服机器人。

## 架构

```
小程序 → Cloudflare → Nginx api.jiemids.com → gateway:8080
                                                  └─ /api/agent/** → ai-service:8000（本服务）
                                                                        ├─ MySQL kb_qa（知识库，60s 缓存）
                                                                        ├─ Redis（会话历史 / 限流 / 上一轮分类）
                                                                        └─ 纯规则检索（关键词 + 二元组相似度）
```

## 目录

```
app/
├── main.py              # FastAPI 入口、CORS、健康检查
├── config.py            # 环境变量
├── api/agent.py         # /api/agent/chat 与 /chat/stream
├── kb/normalize.py      # 归一化、关键词拆分、二元组
├── kb/matcher.py        # 纯规则打分与阈值
├── kb/repo.py           # kb_qa 读取（60s 缓存）+ 命中/未命中埋点
├── kb/service.py        # 意图拦截 → 检索 → 命中/兜底
├── db/product_repo.py   # 商品只读查询（导购能力预留，客服链路不再使用）
├── services/history.py  # Redis 会话历史
├── services/sse.py      # SSE 分帧
scripts/import_kb.py     # 把 docs/QandA.xlsx 导入 kb_qa
```

## 本地运行

```bash
# 1. 依赖（已有 .venv 则跳过）
python -m venv .venv
.venv/Scripts/pip install -r requirements.txt      # Windows
# source .venv/bin/activate && pip install -r requirements.txt   # macOS/Linux

# 2. 配置
cp .env.example .env    # 填 MySQL / Redis / JWT_SECRET

# 3. 建表 + 导入知识库（Excel 在 e-commerce-boot/docs/QandA.xlsx）
.venv/Scripts/python scripts/import_kb.py

# 4. 启动
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

小程序契约保持不变，只新增 `contact` / `matched` 字段（旧前端可忽略）。

| 接口 | 说明 |
|---|---|
| `POST /api/agent/chat` | 非流式，返回 `{"success": true, "data": {"reply": "...", "contact": false, "matched": true}}` |
| `POST /api/agent/chat/stream` | SSE，逐块 `data: <纯文本>`，LF 行尾；保留兼容，小程序默认不走 |
| `GET /health` | 健康检查 |

请求体：`{"message": "...", "sessionId": "...", "scene": "customer-service"}`。`sessionId` 由前端生成，用于 Redis 会话历史（12 条 / 2 小时）和「上一轮分类」弱加权。

## 回答规则

- 命中知识库：直接返回 `kb_qa.answer` 原文，不做二次加工
- 未命中：固定兜底话术 + `contact=true`，同时写 `kb_miss` 供补库
- 用户说「人工 / 转人工 / 找客服」：不查库，直接返回转人工话术
- 投诉 / 差评 / 纠纷：转接售后专员话术
- 分数阈值 55：完全一致 100、包含 92、关键词 55/65、二元组 Dice ×60、同分类 +8

## 鉴权与防滥用

对话接口**必须带登录令牌**，否则直接 401：

```
Authorization: Bearer <小程序用户 JWT>
```

- 令牌与 Java 服务共用 `JWT_SECRET`（只校验，不签发），要求 `type=user`
- 每用户限流：10 分钟 20 次、每天 100 次（Redis 计数，超出 429）
- 全局并发：同时最多 10 条对话，超出 503
- 单条消息 ≤ 500 字
- 整条回复 55 秒超时，超时让前端走本地规则兜底
- Redis 故障时限流放行（可用性优先），但会打告警日志

相关环境变量见 `.env.example` 的 `JWT_SECRET` / `AI_*` 段。

## 验证

```bash
curl -s http://127.0.0.1:8000/api/agent/chat \
  -H 'Content-Type: application/json' \
  -H 'Authorization: Bearer <用户 JWT>' \
  -d '{"message":"什么时候发货","sessionId":"t1"}'

curl -N http://127.0.0.1:8000/api/agent/chat/stream \
  -H 'Content-Type: application/json' \
  -H 'Authorization: Bearer <用户 JWT>' \
  -d '{"message":"怎么退换货","sessionId":"t1"}'
```

Windows 终端直接内联中文可能被 GBK 影响，建议把 JSON 写到文件后用 `--data-binary @req.json`。

## 部署

部署到 `/www/java/ai-service/`，只上传 `app/`、`requirements.txt`、`Dockerfile`、`.dockerignore`；`docker-compose.yml` 里的 `ai-service` 会读取统一的 `.env`。

网关按静态地址转发，不注册 Nacos：本地默认 `http://127.0.0.1:8000`，Docker 里由 compose 的
`AI_SERVICE_URL` 覆盖为 `http://ai-service:8000`。
