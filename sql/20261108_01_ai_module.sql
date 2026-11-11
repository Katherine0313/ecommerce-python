-- ============================================================
-- AI 客服模块建库脚本（对应 docs/ai 设计方案 §7 + §9.4）
-- 内容：9 张 ai_ 表 + ai_kb_qa 加 embedding 列 + 9 个工具种子
--       + 2 个内置聊天角色
--       菜单/权限/角色授权属于 Java 后台，由后端自己的 Flyway/Liquibase 管理，
--       不在 Python AI 服务的迁移职责内。
-- 幂等：可重复执行。
-- ============================================================

-- ---------- 7.1 工具定义表 ----------
CREATE TABLE IF NOT EXISTS ai_tool (
  id          BIGINT       NOT NULL AUTO_INCREMENT,
  name        VARCHAR(128) NOT NULL COMMENT '工具名，与代码 @tool 函数名一致',
  description VARCHAR(512)          DEFAULT NULL COMMENT '工具描述（给大模型看）',
  perm_code   VARCHAR(100)          DEFAULT NULL COMMENT '权限码，对应 admin_menu 一条 F 类型记录',
  scope       VARCHAR(16)  NOT NULL DEFAULT 'both' COMMENT 'mall / admin / both',
  status      TINYINT      NOT NULL DEFAULT 1 COMMENT '1 启用 0 停用',
  sort_order  INT          NOT NULL DEFAULT 0,
  created_at  DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at  DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  UNIQUE KEY uk_ai_tool_name (name)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci COMMENT='AI 工具定义表';

-- ---------- 7.2 聊天角色表 ----------
CREATE TABLE IF NOT EXISTS ai_chat_role (
  id             BIGINT        NOT NULL AUTO_INCREMENT,
  name           VARCHAR(128)  NOT NULL COMMENT '角色名称',
  avatar         VARCHAR(256)           DEFAULT NULL COMMENT '头像',
  description    VARCHAR(256)           DEFAULT NULL,
  category       VARCHAR(32)            DEFAULT NULL COMMENT '分类',
  system_message VARCHAR(1024)          DEFAULT NULL COMMENT '角色设定（system prompt）',
  admin_id       BIGINT                 DEFAULT NULL COMMENT '创建者，NULL=系统内置',
  public_status  TINYINT       NOT NULL DEFAULT 0 COMMENT '0 私有 1 公开',
  status         TINYINT       NOT NULL DEFAULT 1,
  sort           INT           NOT NULL DEFAULT 0,
  created_at     DATETIME      NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at     DATETIME      NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci COMMENT='AI 聊天角色表';

-- ---------- 7.3 角色-工具绑定表 ----------
CREATE TABLE IF NOT EXISTS ai_chat_role_tool (
  id      BIGINT NOT NULL AUTO_INCREMENT,
  role_id BIGINT NOT NULL,
  tool_id BIGINT NOT NULL,
  PRIMARY KEY (id),
  UNIQUE KEY uk_role_tool (role_id, tool_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci COMMENT='AI 角色-工具绑定表';

-- ---------- 7.4 角色-知识库绑定表（P1 用，先建表） ----------
CREATE TABLE IF NOT EXISTS ai_chat_role_knowledge (
  id         BIGINT NOT NULL AUTO_INCREMENT,
  role_id    BIGINT NOT NULL,
  dataset_id BIGINT NOT NULL,
  PRIMARY KEY (id),
  UNIQUE KEY uk_role_dataset (role_id, dataset_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci COMMENT='AI 角色-知识库绑定表';

-- ---------- 7.5 对话表 ----------
CREATE TABLE IF NOT EXISTS ai_chat_conversation (
  id             BIGINT        NOT NULL AUTO_INCREMENT,
  title          VARCHAR(256)  NOT NULL DEFAULT '新对话',
  admin_id       BIGINT                 DEFAULT NULL COMMENT '后台对话发起人，商城为 NULL',
  user_id        BIGINT                 DEFAULT NULL COMMENT '商城对话发起人，后台为 NULL',
  source         VARCHAR(16)   NOT NULL COMMENT 'mall / admin',
  role_id        BIGINT                 DEFAULT NULL,
  system_message VARCHAR(1024)          DEFAULT NULL,
  temperature    DOUBLE        NOT NULL DEFAULT 0.7,
  max_tokens     INT           NOT NULL DEFAULT 2048,
  max_contexts   INT           NOT NULL DEFAULT 20 COMMENT '保留的上下文轮数',
  pinned         TINYINT       NOT NULL DEFAULT 0,
  created_at     DATETIME      NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at     DATETIME      NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  KEY idx_conv_admin (admin_id, source),
  KEY idx_conv_user (user_id, source)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci COMMENT='AI 对话表';

-- ---------- 7.6 消息表 ----------
CREATE TABLE IF NOT EXISTS ai_chat_message (
  id              BIGINT       NOT NULL AUTO_INCREMENT,
  conversation_id BIGINT       NOT NULL,
  admin_id        BIGINT                DEFAULT NULL,
  user_id         BIGINT                DEFAULT NULL,
  role_id         BIGINT                DEFAULT NULL,
  type            VARCHAR(16)  NOT NULL COMMENT 'user / assistant / tool',
  content         TEXT,
  tool_calls      TEXT                  COMMENT 'assistant 发起的工具调用 JSON',
  tool_call_id    VARCHAR(128)          DEFAULT NULL COMMENT 'type=tool 时',
  tool_name       VARCHAR(128)          DEFAULT NULL COMMENT 'type=tool 时',
  created_at      DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  KEY idx_msg_conv (conversation_id, id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci COMMENT='AI 消息表';

-- ---------- 7.7 调用审计表 ----------
CREATE TABLE IF NOT EXISTS ai_call_log (
  id                BIGINT       NOT NULL AUTO_INCREMENT,
  admin_id          BIGINT                DEFAULT NULL,
  user_id           BIGINT                DEFAULT NULL,
  conversation_id   BIGINT                DEFAULT NULL,
  scene             VARCHAR(64)  NOT NULL COMMENT 'chat / generate_copy / tool:xxx',
  model             VARCHAR(128)          DEFAULT NULL,
  prompt_tokens     INT                   DEFAULT NULL,
  completion_tokens INT                   DEFAULT NULL,
  total_tokens      INT                   DEFAULT NULL,
  duration_ms       INT                   DEFAULT NULL,
  success           TINYINT      NOT NULL DEFAULT 1,
  error_msg         VARCHAR(512)          DEFAULT NULL,
  created_at        DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  KEY idx_call_conv (conversation_id),
  KEY idx_call_time (created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci COMMENT='AI 调用审计表';

-- ---------- 7.9 运营知识库表（P1 用，先建表） ----------
CREATE TABLE IF NOT EXISTS ai_kb_dataset (
  id          BIGINT       NOT NULL AUTO_INCREMENT,
  name        VARCHAR(256) NOT NULL COMMENT '知识库名称',
  description VARCHAR(512)          DEFAULT NULL,
  category    VARCHAR(64)           DEFAULT NULL,
  status      TINYINT      NOT NULL DEFAULT 1,
  created_at  DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at  DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci COMMENT='AI 运营知识库表';

-- ---------- 7.10 运营知识分段表（P1 用，先建表） ----------
CREATE TABLE IF NOT EXISTS ai_kb_segment (
  id         BIGINT       NOT NULL AUTO_INCREMENT,
  dataset_id BIGINT       NOT NULL COMMENT '所属知识库',
  source_doc VARCHAR(256)          DEFAULT NULL COMMENT '来源文档名',
  title      VARCHAR(256)          DEFAULT NULL COMMENT '分段标题',
  content    TEXT         NOT NULL COMMENT '分段正文',
  embedding  TEXT                  DEFAULT NULL COMMENT '向量 JSON 数组',
  status     TINYINT      NOT NULL DEFAULT 1,
  created_at DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  KEY idx_segment_dataset (dataset_id, status)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci COMMENT='AI 运营知识分段表';

-- ---------- 7.8 ai_kb_qa 加 embedding 列（幂等） ----------
SET @col := (
  SELECT COUNT(*) FROM information_schema.COLUMNS
  WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'ai_kb_qa' AND COLUMN_NAME = 'embedding'
);
SET @ddl := IF(@col = 0,
  'ALTER TABLE ai_kb_qa ADD COLUMN embedding TEXT NULL COMMENT ''向量 JSON 数组（P1 RAG）'' AFTER hit_count',
  'SELECT 1');
PREPARE stmt FROM @ddl;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

-- ============================================================
-- 工具种子（9 个）
-- ============================================================
INSERT INTO ai_tool (id, name, description, perm_code, scope, status, sort_order, created_at, updated_at) VALUES
  (1, 'query_product',     '查询单个商品的详细信息，传入商品ID返回名称、价格、库存、规格等', 'ai:tool:query',     'both',  1, 1, NOW(), NOW()),
  (2, 'search_product',    '按关键词、分类、价格区间搜索商品，返回商品列表',               'ai:tool:search',    'both',  1, 2, NOW(), NOW()),
  (3, 'recommend_product', '根据用户需求推荐商品，返回商品卡片数据（名称、价格、主图）',     'ai:tool:recommend', 'both',  1, 3, NOW(), NOW()),
  (4, 'search_knowledge',  '检索客服知识库，回答退换货、发货时间、尺码、运费等问题',         'ai:tool:knowledge', 'both',  1, 4, NOW(), NOW()),
  (5, 'query_stock',       '查询指定 SKU 的实时库存',                                     'ai:tool:stock',     'both',  1, 5, NOW(), NOW()),
  (6, 'query_order',       '查询当前用户的订单列表与订单状态',                             'ai:tool:order',     'both',  1, 6, NOW(), NOW()),
  (7, 'query_aftersale',   '查询售后单的进度与处理状态',                                   'ai:tool:aftersale', 'both',  1, 7, NOW(), NOW()),
  (8, 'generate_copy',     '为商品生成营销文案（标题/副标题/详情描述）',                   'ai:copy:generate',  'admin', 1, 8, NOW(), NOW()),
  (9, 'update_product',    '修改商品的文案类字段（名称/副标题/详情），执行前必须二次确认',   'ai:tool:update',    'admin', 1, 9, NOW(), NOW())
ON DUPLICATE KEY UPDATE
  name = VALUES(name), description = VALUES(description), perm_code = VALUES(perm_code),
  scope = VALUES(scope), status = VALUES(status), sort_order = VALUES(sort_order), updated_at = NOW();

-- ============================================================
-- 内置聊天角色（2 个，admin_id = NULL 表示系统内置）
-- ============================================================
INSERT INTO ai_chat_role (id, name, avatar, description, category, system_message, admin_id, public_status, status, sort, created_at, updated_at) VALUES
  (1, '商城客服', NULL, '商城默认 AI 客服，解答商品、订单、售后问题并推荐商品', '客服',
   '你是「杰米多多」商城的 AI 客服小助手。职责：1. 解答顾客关于商品、库存、订单、售后的问题；2. 根据顾客需求主动推荐合适的商品；3. 回答客服类问题（退换货、发货、运费等）。规则：商品信息必须以工具查询结果为准，禁止编造价格、库存；语气亲切简洁，单次回答控制在 200 字以内；无法解决的问题引导顾客联系人工客服；不回答与购物无关的问题。',
   NULL, 1, 1, 1, NOW(), NOW()),
  (2, '运营助手', NULL, '后台运营 AI 助手，查数据、生成文案、辅助商品维护', '运营',
   '你是电商后台的运营助手。能力：查询商品、库存、订单、售后数据，生成商品营销文案，经运营确认后修改商品文案字段。规则：所有数据以工具查询结果为准，禁止编造；执行修改类操作前必须先用一句话说明将要变更的内容并等待用户确认；回答简洁专业，突出关键数据；不回答与电商运营无关的问题。',
   NULL, 1, 1, 2, NOW(), NOW())
ON DUPLICATE KEY UPDATE
  name = VALUES(name), description = VALUES(description), category = VALUES(category),
  system_message = VALUES(system_message), public_status = VALUES(public_status),
  status = VALUES(status), sort = VALUES(sort), updated_at = NOW();

-- 商城客服绑定 7 个客户工具；运营助手绑定全部 9 个
INSERT IGNORE INTO ai_chat_role_tool (role_id, tool_id)
SELECT 1, t.id FROM ai_tool t WHERE t.id BETWEEN 1 AND 7;

INSERT IGNORE INTO ai_chat_role_tool (role_id, tool_id)
SELECT 2, t.id FROM ai_tool t WHERE t.id BETWEEN 1 AND 9;
