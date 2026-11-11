-- ============================================================
-- AI 聊天放开匿名访问：新增 anonymous_id 列，用于跟踪未登录访客的会话
-- 登录用户：user_id 非空，anonymous_id 为空
-- 匿名用户：user_id 为空，anonymous_id 为前端生成的 UUID
-- 幂等：用 information_schema 判断列是否存在，已存在则跳过
-- ============================================================

-- ai_chat_conversation.anonymous_id
SET @exists := (
  SELECT COUNT(*) FROM information_schema.COLUMNS
  WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'ai_chat_conversation' AND COLUMN_NAME = 'anonymous_id'
);
SET @sql := IF(@exists = 0,
  'ALTER TABLE ai_chat_conversation ADD COLUMN anonymous_id VARCHAR(64) DEFAULT NULL COMMENT ''匿名访客标识（未登录用户）'' AFTER user_id',
  'SELECT 1');
PREPARE stmt FROM @sql; EXECUTE stmt; DEALLOCATE PREPARE stmt;

-- ai_chat_conversation.idx_conv_anon 索引
SET @idx_exists := (
  SELECT COUNT(*) FROM information_schema.STATISTICS
  WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'ai_chat_conversation' AND INDEX_NAME = 'idx_conv_anon'
);
SET @sql := IF(@idx_exists = 0,
  'ALTER TABLE ai_chat_conversation ADD KEY idx_conv_anon (anonymous_id, source)',
  'SELECT 1');
PREPARE stmt FROM @sql; EXECUTE stmt; DEALLOCATE PREPARE stmt;

-- ai_chat_message.anonymous_id
SET @exists := (
  SELECT COUNT(*) FROM information_schema.COLUMNS
  WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'ai_chat_message' AND COLUMN_NAME = 'anonymous_id'
);
SET @sql := IF(@exists = 0,
  'ALTER TABLE ai_chat_message ADD COLUMN anonymous_id VARCHAR(64) DEFAULT NULL COMMENT ''匿名访客标识'' AFTER user_id',
  'SELECT 1');
PREPARE stmt FROM @sql; EXECUTE stmt; DEALLOCATE PREPARE stmt;
