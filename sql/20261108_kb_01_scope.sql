-- ============================================================
-- 知识库按角色分库：新增 scope 字段
-- scope='客服' → 商城客服机器人使用
-- scope='运营' → 后台运营助手使用
-- 现有数据默认为 '客服'
-- 幂等：用 information_schema 判断列/索引是否存在，已存在则跳过
-- ============================================================

-- ai_kb_qa.scope 列
SET @exists := (
  SELECT COUNT(*) FROM information_schema.COLUMNS
  WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'ai_kb_qa' AND COLUMN_NAME = 'scope'
);
SET @sql := IF(@exists = 0,
  'ALTER TABLE ai_kb_qa ADD COLUMN scope VARCHAR(16) NOT NULL DEFAULT ''客服'' COMMENT ''知识适用范围：客服/运营'' AFTER category',
  'SELECT 1');
PREPARE stmt FROM @sql; EXECUTE stmt; DEALLOCATE PREPARE stmt;

-- ai_kb_qa.idx_kb_scope 索引
SET @idx_exists := (
  SELECT COUNT(*) FROM information_schema.STATISTICS
  WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'ai_kb_qa' AND INDEX_NAME = 'idx_kb_scope'
);
SET @sql := IF(@idx_exists = 0,
  'ALTER TABLE ai_kb_qa ADD KEY idx_kb_scope (scope)',
  'SELECT 1');
PREPARE stmt FROM @sql; EXECUTE stmt; DEALLOCATE PREPARE stmt;
