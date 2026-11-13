# 进度日志

## 会话：2026-10-26

### 阶段 1：清空旧项目，重建导购服务
- **状态：** complete
- 删除旧 `src/`（仅剩 `__pycache__`）与客户分群/商品推荐相关文档内容
- 新建 `app/` 分层：`api` / `graph` / `db` / `services`，新增 `requirements.txt`、`Dockerfile`、`.dockerignore`、`.env.example`
- 依赖安装成功：fastapi 0.162、uvicorn 0.54、pymysql 1.2、redis 8.1（langgraph/openai 已有）
- 实现商品检索（价格区间 + 中文滑窗关键词 + 分类兜底）、Redis 历史、LangGraph 图、SSE 分帧

### 阶段 2：Java 侧摘除
- **状态：** complete
- 删除 `ai-agent-service` 目录、根 POM 模块与 Spring AI BOM
- 网关路由改为静态地址：本地默认 `http://127.0.0.1:8000`，compose 用 `AI_SERVICE_URL` 覆盖为 `http://ai-service:8000`
- 删除 Swagger 聚合项，compose 用 `ai-service` 替换 `ai-agent-service`，补部署文档与环境变量说明
- `./mvnw clean package -DskipTests` 退出码 0

### 阶段 3：本地网关联调
- **状态：** complete
- 本地起 Python 8000 + Java gateway 8080，经网关调用 `/api/agent/chat` 正常返回
- 修复网关 500：原默认 URI 是 Docker 主机名 `ai-service`，本机无法解析；改为 `127.0.0.1` 默认 + compose 覆盖
- 确认小程序「运行」包（`dist/dev`）指向本地网关；「发行」包固定指向线上，不能用于本地联调

### 阶段 4：防滥用
- **状态：** complete
- 对话接口强制 JWT（PyJWT 校验，与 Java 共用 `JWT_SECRET`，只允许 `type=user`）
- 每用户限流：10 分钟 20 次、每天 100 次；Redis 故障时放行
- 全局并发 10、单条消息 500 字、`max_tokens=600`、首字 12 秒 / 总时长 55 秒超时
- 前端 `store/ai-chat.js` 发送前调用 `ensureLogin()`，避免冷启动 401

## 测试结果

| 测试 | 输入 | 预期 | 实际 | 状态 |
|---|---|---|---|---|
| 商品检索 | 「推荐一款降噪耳机」 | 返回耳机类商品 | 命中 6 条（AirPods/WF/WH 等） | 通过 |
| 价格解析 | 1000以内 / 500-1000 / 两千以内 | 上限 1000 / 区间 / 上限 2000 | 一致 | 通过 |
| 非流式对话 | `你好` | 返回 `{success,data:{reply}}` | 正常回复 | 通过 |
| 流式对话 | 「1000元以内的降噪耳机」 | 逐块 `data:` 纯文本 | 首字约 593ms，格式正确 | 通过 |
| 多轮对话 | 同一 sessionId 追问续航 | 能引用上一轮商品与预算 | 正确引用 AirPods 4 与 1000 元预算 | 通过 |
| Redis 历史 | 保存后读取 | 2 条/轮，最多 12 条 | 正常 | 通过 |
| 网关联调 | `POST 127.0.0.1:8080/api/agent/chat` | `{success,data:{reply}}` | 正常返回 | 通过 |
| 无令牌 | 直接调 `/api/agent/chat` | 401 | 401 | 通过 |
| 有效令牌 | 带 JWT 调非流式/流式 | 正常回复 | 正常 | 通过 |
| 用户限流 | 同一用户连续 22 次 | 第 21 次拒绝 | 第 21 次 `allowed=False` | 通过 |
| Java 构建 | `mvnw clean package -DskipTests` | 构建成功 | EXIT=0 | 通过 |

## 错误日志

| 时间 | 错误 | 解决 |
|---|---|---|
| 2026-10-26 | 本地 Redis `unknown command HELLO` | redis-py 固定 `protocol=2` |
| 2026-10-26 | curl 内联中文请求体解析失败 | 改用 UTF-8 文件 + `--data-binary`（仅 Windows 调试问题） |
| 2026-10-26 | PyCharm/终端日志中文乱码 | 控制台编码问题，不影响服务；调试设 `PYTHONIOENCODING=utf-8` |
| 2026-10-26 | 经网关调 `/api/agent` 返回 500 | 网关默认 URI 是 Docker 主机名 `ai-service`，本机解析不了；默认改 `127.0.0.1:8000`，Docker 用 `AI_SERVICE_URL` 覆盖 |

## 当前状态

- 本地 Python 服务 + Java 网关全链路已跑通，接口与前端契约一致
- Java 侧已删除 AI 模块，构建通过
- 待做：生产部署与真机验证（数据库 ALTER 必须在部署 order-service 之前）
