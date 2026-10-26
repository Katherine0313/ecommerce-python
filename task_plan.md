# 任务计划：导购机器人（FastAPI + LangGraph）

## 目标

把小程序的 AI 能力从 Java `ai-agent-service` 迁到 Python，做**多轮对话 + 商品问答**的导购机器人，前端零改动。

## 已确认决策

| 决策 | 结论 |
|---|---|
| 技术栈 | FastAPI + LangGraph + openai SDK（不引入 langchain-openai） |
| 商品数据 | 直连 MySQL 只读，60 秒内存缓存 |
| 会话 | Redis，沿用 Java 的 key 与格式 |
| 部署 | 独立容器 `ai-service:8000`，网关静态转发 |
| 不做 | 推荐算法、下单写操作、RAG、商品卡片 |

## 阶段

### 阶段 1：服务骨架与本地跑通 — 完成
- [x] 清空旧推荐项目代码与文档
- [x] FastAPI 入口、配置、CORS、健康检查
- [x] 商品只读仓库 + 价格/关键词检索 + 缓存
- [x] Redis 多轮历史
- [x] LangGraph 检索 → 生成
- [x] 两个接口（SSE / 非流式）兼容前端契约
- [x] 本地验证：多轮、价格筛选、首字约 600ms

### 阶段 2：Java 侧摘除与网关切换 — 完成
- [x] 删除 `ai-agent-service`、Spring AI BOM、Swagger 聚合项
- [x] 网关 `/api/agent/**` → `http://ai-service:8000`
- [x] 删除 common 的 `/api/agent` 免登录白名单
- [x] compose 增加 `ai-service`，移除 `ai-agent-service`
- [x] 网关默认 URI 改为 `127.0.0.1:8000`，Docker 由 `AI_SERVICE_URL` 覆盖
- [x] `./mvnw clean package -DskipTests` 通过
- [x] 本地网关联调通过（小程序运行包 → 8080 → Python 8000）

### 阶段 3：生产部署 — 待做（顺序不能乱）
- [ ] 数据库：`orders` 加 `is_subscribe_unpaid` / `unpaid_notify_sent` + `idx_orders_unpaid_notify`
- [ ] 上传 `upload/image/opt/` 与 `upload/image/category/`，再把 `categories.image` 改成 `.webp`
- [ ] 服务器 `.env` 补 `MYSQL_HOST` / `MYSQL_PORT` / `MYSQL_DATABASE` / `SHOP_ID`
- [ ] 上传 8 个 Java 服务 jar、Python 代码到 `/www/java/ai-service/`、新 compose
- [ ] `docker compose build ai-service gateway` → 起业务服务 → 起 ai-service → 起 gateway
- [ ] 验证通过后 `stop/rm ai-agent-service`，Nacos 剩 8 个实例
- [ ] 清 Cloudflare 缓存 `/image/category/*`、`/image/upload/*`、`/image/body/*`
- [ ] 验证 `/api/home`、`/api/cart/count`、`/api/agent/chat/stream`
- [ ] 小程序「发行」包上传提审

### 阶段 4：防滥用 — 完成
- [x] 对话接口强制 JWT（与 Java 共用 `JWT_SECRET`，只允许 `type=user`）
- [x] 每用户限流：10 分钟 20 次、每天 100 次（Redis）
- [x] 全局并发 10、单条消息 500 字、`max_tokens=600`
- [x] 首字 12 秒 / 总时长 55 秒超时
- [x] 前端发送前 `ensureLogin()`
- [ ] 可选：Cloudflare/Nginx 层再加一层 IP 限流

### 阶段 5：能力扩展 — 待排期
- [ ] 商品卡片渲染（需前端一起改）
- [ ] 下单/加购（写操作，二次确认）
- [ ] RAG 知识库（售后政策、发货说明）

## 风险与待确认

| # | 风险 | 处置 |
|---|---|---|
| 1 | SSE 被代理缓冲导致首字延迟 | Nginx 已 `proxy_buffering off`，Python 响应加 `X-Accel-Buffering: no`；Cloudflare 沿用现有可用配置 |
| 2 | 模型编造商品参数 | prompt 限定只用注入资料；后续可加答案校验 |
| 3 | MySQL 只读权限 | 代码只执行 SELECT；生产可用只读账号进一步收紧 |
| 4 | 单实例内存缓存 | 商品少，60 秒缓存足够；多实例时需改 Redis 缓存 |
| 5 | 前端只渲染纯文本 | 第一阶段不承诺卡片效果 |
