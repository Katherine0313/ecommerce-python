# 发现与决策

> 只记录事实与结论，配套 `task_plan.md` / `progress.md` 使用。

## 项目定位

杰米多多商城的 AI 导购机器人，2026-10-26 起接管原 Java `ai-agent-service` 的能力。第一阶段只做**多轮对话 + 商品问答**；不做推荐算法（原 RFM/KMeans/猜你喜欢已全部删除）、不做下单写操作、不做 RAG。

## 架构决策

| 决策 | 结论 | 原因 |
|---|---|---|
| Python 技术形态 | FastAPI + LangGraph | LangGraph 管状态与后续扩展，FastAPI 原生支持 SSE |
| 部署形态 | 独立容器 `ai-service`，端口 8000 | 与 Java 微服务解耦，重启不影响商城主链路 |
| 网关接入 | 静态地址，本地默认 `http://127.0.0.1:8000`，Docker 用 `AI_SERVICE_URL` 覆盖 | Python 不引入 Nacos 客户端，少一层依赖；本地无需额外配环境变量 |
| 商品数据 | 直连 MySQL 只读 | 商品少（上架约百条），实现简单、延迟低 |
| 会话历史 | Redis，沿用 Java 的 key/格式 | 切换时旧会话可继续用，前端无感 |
| 模型 | 硅基流动 OpenAI 兼容接口 | 与 Java 版同一个 key、同一个模型 |
| 防滥用 | 强制 JWT + 每用户限流 + 全局并发 + 单次 token 上限 | 接口公网可达，避免匿名调用刷爆模型额度 |

## 前端契约（不能破坏）

- `POST /api/agent/chat/stream`：SSE 逐块纯文本 `data: <增量>\n\n`
  - 必须 LF 行尾；禁止 `[DONE]`、JSON delta、`event:` 名、`:` 心跳注释
  - 增量内换行要拆成多条 `data:` 行（前端按 `\n\n` 分事件、按 `\n` 分 data 行再拼回）
  - 结束即关闭响应；首字 < 15 秒（前端看门狗），整体 < 60 秒
- `POST /api/agent/chat`：返回 `{"success": true, "data": {"reply": "..."}}`
- 请求体固定 `{message, sessionId, scene}`；`scene` 第一阶段接收但不区分
- 前端只渲染纯文本气泡，不支持 Markdown / 商品卡片 / 图片 / 链接

## 商品检索

- 只读 `products` + `categories`，条件 `listing_status = 1 AND stock > 0`（2026-10-29 多租户下线，已去掉 `shop_id`）
- 结果内存缓存 60 秒（`asyncio.Lock` 防并发击穿）
- 价格解析：`1000以内`、`500-1000`、`1000以上`、`5千以内`、`两千以内`
- 关键词匹配：中文 2/3 字滑窗 + 英文数字整词，与商品名/副标题/品牌/分类名求交集；品牌、分类命中额外加权
- 没匹配到但用户明确说「推荐/有没有/预算」时，按分类各取一个兜底；连意图都没有就返回空，让模型正常寒暄

## 鉴权与限流

- 对话接口校验 `Authorization: Bearer <token>`，与 Java 共用 `JWT_SECRET`（PyJWT，HS256/384/512），要求 `type=user`
- 每用户固定窗口限流：10 分钟 20 次、每天 100 次；Redis key `agent:rate:window:*` / `agent:rate:day:*`
- 全局 `asyncio.Semaphore(10)`；单条消息 500 字上限；`max_tokens=600`
- 首字 12 秒、总时长 55 秒超时；超时/异常不返回错误 JSON，直接结束流，让前端走本地兜底
- Redis 故障时鉴权仍生效，限流放行（可用性优先）
- 前端 `store/ai-chat.js` 在发送前调用 `ensureLogin()`，避免冷启动无 token 导致 401

## Redis 会话历史

- key：`agent:chat:history:{sessionId}`
- list：`user:xxx` / `assistant:xxx`，保留最近 12 条，TTL 2 小时
- sessionId 去空格、截断 64；空值按单轮处理
- Redis 读写全部 try/catch，故障时降级单轮

## 踩坑记录

| 问题 | 原因 | 处理 |
|---|---|---|
| 本地 Redis 报 `unknown command HELLO` | 老版本 Redis 不支持 RESP3 | redis-py 固定 `protocol=2` |
| Windows 终端里中文/日志乱码 | 控制台默认 GBK | 不影响服务；调试可设 `PYTHONIOENCODING=utf-8` |
| curl 内联中文报请求体解析失败 | Git Bash 传参编码 | 把 JSON 写文件后 `--data-binary @file` |
| 流式回复里换行破坏事件边界 | SSE 规范要求逐行 `data:` | `sse_pack()` 统一拆行 |
| 中文分词无第三方库 | 不想引入 jieba | 2/3 字滑窗交集，商品少时足够 |
| 本地网关 `/api/agent` 返回 500 | 网关默认 URI 是 Docker 主机名 `ai-service`，本机解析不了 | 默认值改为 `http://127.0.0.1:8000`，Docker 由 compose 的 `AI_SERVICE_URL` 覆盖 |
| 发行包连不上本地 AI | 「发行」构建固定用生产域名 | 本地联调必须用「运行」构建（`dist/dev` → `127.0.0.1:8080`） |

## 与 Java 的关系

- Java 侧 `ai-agent-service`、Spring AI BOM、网关 Swagger 聚合项已删除
- 网关 `/api/agent,/api/agent/**` → `${AI_SERVICE_URL:http://127.0.0.1:8000}`；compose 里 gateway 设 `AI_SERVICE_URL=http://ai-service:8000`
- `common` 的 `/api/agent` 免登录白名单已移除（Java 侧不再有该路径；匿名访问由网关直达 Python 保证）
- `deploy/docker-compose.yml` 用 `ai-service` 替代原 `ai-agent-service`
