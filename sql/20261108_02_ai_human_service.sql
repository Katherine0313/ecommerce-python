-- AI 客服 · 人工客服（站内转人工）
-- Python AI 服务仅负责维护 ai_chat_conversation 表的人工模式标记列。
-- 后台菜单（admin_menu 1007）由 Java 后端自己的迁移管理，不在这里执行。

-- 1) 会话表增加人工模式标记
SET @exists := (
  SELECT COUNT(*) FROM information_schema.COLUMNS
  WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'ai_chat_conversation' AND COLUMN_NAME = 'human_mode'
);
SET @sql := IF(@exists = 0,
  'ALTER TABLE ai_chat_conversation ADD COLUMN human_mode TINYINT NOT NULL DEFAULT 0 COMMENT ''1=已转人工，AI 不再自动回复'' AFTER pinned',
  'SELECT 1');
PREPARE stmt FROM @sql; EXECUTE stmt; DEALLOCATE PREPARE stmt;

SET @exists := (
  SELECT COUNT(*) FROM information_schema.COLUMNS
  WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'ai_chat_conversation' AND COLUMN_NAME = 'human_at'
);
SET @sql := IF(@exists = 0,
  'ALTER TABLE ai_chat_conversation ADD COLUMN human_at DATETIME DEFAULT NULL COMMENT ''转人工时间'' AFTER human_mode',
  'SELECT 1');
PREPARE stmt FROM @sql; EXECUTE stmt; DEALLOCATE PREPARE stmt;
