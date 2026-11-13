# 杰米多多 AI 服务（ecommerce-python）

商城的 AI 服务：**FastAPI + LangChain + 知识库检索 + 大模型**。同时服务两条链路 —— C 端用户的智能客服，以及后台管理员的运营助手。

后端主仓库是 [`e-commerce-boot`](https://github.com/Katherine0313/e-commerce-boot.git)（Java 微服务），本仓库通过 REST 调它取业务数据。

## 仓库清单

| 仓库 | 内容 |
|---|---|
| **[ecommerce-python](https://github.com/Katherine0313/ecommerce-python.git)** | **AI 客服服务（Python FastAPI + LangGraph + 知识库检索），本仓库** |
| [e-commerce-boot](https://github.com/Katherine0313/e-commerce-boot.git) | Java 后端微服务（核心） |
| [e-commerce](https://github.com/Katherine0313/e-commerce.git) | 后台管理系统（Vue 3 + Ant Design Vue） |
| [ecommerce-mall](https://github.com/Katherine0313/ecommerce-mall.git) | PC 商城前端（Vue 3 + Vite） |
| [e-commerce-uniapp](https://github.com/Katherine0313/e-commerce-uniapp.git) | 微信小程序（uni-app） |

## 架构

```
小程序 / PC 商城 / 后台管理
        ↓
  api.jiemids.com（Nginx）
        ↓
  gateway:8080（Java）
        ├── /api/mall/ai/**    → ai-service:8000（本服务）→ MySQL ai_kb_qa 知识库
        └── /api/admin/ai/**   → ai-service:8000   → 硅基流动 DeepSeek（OpenAI 兼容）
                                        ↑
                                 Redis（会话历史 / 限流 / 上一轮上下文）
                                        ↓
                          X-Internal-Token 调 Java /internal/ai/** 取业务数据
```

**数据只从 Java 侧来**：AI 不允许编造业务数据，所有商品 / 库存 / 订单 / 售后信息都通过内部接口实时查询，知识库只提供规则类问答（退换货、发货、运费等）。

## 能力

| 链路 | 入口 | 用途 |
|---|---|---|
| C 端智能客服 | `/api/mall/ai/**` | 商品咨询、订单查询、售后进度、规则问答；支持 SSE 流式与转人工 |
| 后台运营助手 | `/api/admin/ai/**` | 查数据、生成商品文案、改商品文案（需确认）、AI 生图、人工客服工作台 |

大模型走**硅基流动**（OpenAI 兼容协议），模型与密钥只在服务端，前端永远接触不到。

## 工具集

`app/ai/tools.py` 注册了 9 个 LangChain `@tool`，大模型只能通过这些工具拿业务数据：

| 类别 | 工具 |
|---|---|
| 商品 | 查单个商品详情、按需求推荐在售商品 |
| 库存 | 查指定 SKU 实时库存 |
| 订单 | 查当前登录用户的订单列表与状态 |
| 售后 | 按售后单 ID 查处理进度 |
| 知识库 | 检索店铺规则类问答（退换货、发货、运费、正品保障…） |
| 文案 | 生成商品营销文案（名称 / 副标题 / 主标题 / 详情） |
| 写入 | 修改商品文案类字段 —— **执行前必须向用户说明变更并等确认**，禁止模型口头承诺修改 |

工具可按角色授权（`ai_chat_role_tool`），后台运营助手与 C 端客服拿到的工具集不同。

## 目录

```
app/
├── main.py              FastAPI 入口、CORS、生命周期、健康检查
├── config.py            环境变量配置
├── api/ai_chat.py       C 端 / 后台全部路由
├── ai/
│   ├── agent.py         LangChain Agent 主循环
│   ├── client.py        硅基流动 ChatOpenAI 客户端
│   ├── tools.py         9 个 @tool 工具集
│   ├── prompts.py       各角色提示词
│   ├── intent.py        意图识别
│   ├── copy.py          商品文案生成
│   ├── image.py         AI 生图
│   └── repo.py          知识库读写（ai_kb_qa）
├── kb/                  归一化 / 匹配打分 / embedding / 导入 / 促销
├── db/product_repo.py   商品只读查询
├── services/            鉴权、会话历史、限流、Redis、SSE 分帧
└── internal_client.py   调 Java /internal/ai/** 的客户端（X-Internal-Token）

sql/                    建表与种子脚本（migrate.py 启动时自动执行）
scripts/
├── import_kb.py         把 Excel 知识库导入 ai_kb_qa
└── backfill_body_text.py
docs/
├── planning/            规划三件套：findings / task_plan / progress
└── improvement.md       意图分层与检索策略设计
migrate.py              启动时执行 sql/ 下的迁移脚本
```

## 接口

### C 端（需要用户令牌，`type=user`）

| 接口 | 说明 |
|---|---|
| `POST /api/mall/ai/chat/stream` | SSE 流式对话 |
| `POST /api/mall/ai/handoff` | 转人工客服 |
| `GET /api/mall/ai/conversations` | 会话列表 |
| `GET /api/mall/ai/conversations/{id}` | 会话详情 |
| `GET /api/mall/ai/conversations/{id}/messages` | 会话消息 |
| `DELETE /api/mall/ai/conversations/{id}` | 删除会话 |
| `GET /api/mall/ai/roles` | 可用角色 |

### 后台（需要管理员令牌，`type=admin`）

| 接口 | 说明 |
|---|---|
| `POST /api/admin/ai/chat/stream` | SSE 流式对话 |
| `POST /api/admin/ai/copy/generate` | 生成商品文案 |
| `POST /api/admin/ai/copy/apply` | 应用文案到商品 |
| `GET /api/admin/ai/conversations` | 会话列表 |
| `GET /api/admin/ai/human/conversations` | 人工客服工作台 |
| `POST /api/admin/ai/human/conversations/{id}/reply` | 人工回复 |
| `POST /api/admin/ai/human/conversations/{id}/close` | 结束人工会话 |
| `GET /api/admin/ai/roles` | 可用角色 |

`GET /health` 健康检查（无需鉴权）。

## 本地运行

```bash
# 1. 依赖（已有 .venv 则跳过）
python -m venv .venv
.venv/Scripts/pip install -r requirements.txt      # Windows
# source .venv/bin/activate && pip install -r requirements.txt   # macOS/Linux

# 2. 配置
cp .env.example .env    # 填 MySQL / Redis / JWT_SECRET / 硅基流动 key / AI_INTERNAL_TOKEN

# 3. 导入知识库
#    Excel 源文件（QandA.xlsx）不在版本库内 —— 从运营处获取后放到项目根再执行：
.venv/Scripts/python scripts/import_kb.py

# 4. 启动（migrate.py 会先执行 sql/ 下的建表脚本）
.venv/Scripts/python -m uvicorn app.main:app --port 8000
```

### 经 Java 网关联调

网关已配置 `/api/mall/ai/**` 默认转发到 `http://127.0.0.1:8000`，本地不用再配环境变量；Docker 里由 `deploy/docker-compose.yml` 的 `AI_SERVICE_URL=http://ai-service:8000` 覆盖。

```bash
# 先起 Python（8000），再在 IDEA 起 gateway（8080）
curl -X POST http://127.0.0.1:8080/api/mall/ai/chat/stream \
  -H 'Content-Type: application/json' \
  -H 'Authorization: Bearer <小程序用户 JWT>' \
  -d '{"message":"什么时候发货","sessionId":"gw-test"}'
```

Windows 终端直接内联中文可能被 GBK 影响，建议把 JSON 写到文件后用 `--data-binary @req.json`。

## 鉴权与防滥用

对话接口**必须带登录令牌**，否则直接 401：

```
Authorization: Bearer <JWT>
```

- 令牌与 Java 服务共用 `JWT_SECRET`（只校验，不签发）；C 端要求 `type=user`，后台要求 `type=admin`
- 每用户限流：10 分钟 20 次、每天 100 次（Redis 计数，超出 429）
- 全局并发：同时最多 10 条对话，超出 503
- 单条消息 ≤ 500 字；整条回复 55 秒超时，超时让前端走本地规则兜底
- Redis 故障时限流放行（可用性优先），但会打告警日志

**调 Java 内部接口**时带 `X-Internal-Token` 头，值取 `AI_INTERNAL_TOKEN`，两端必须一致；Java 侧未配置该密钥时一律拒绝（fail-closed）。

## 部署

部署到 `/www/java/ai-service/`，只上传 `app/`、`sql/`、`scripts/`、`requirements.txt`、`Dockerfile`、`.dockerignore`；`docker-compose.yml` 里的 `ai-service` 会读取统一的 `.env`。

网关按静态地址转发，不注册 Nacos：本地默认 `http://127.0.0.1:8000`，Docker 里由 compose 的 `AI_SERVICE_URL` 覆盖为 `http://ai-service:8000`。
