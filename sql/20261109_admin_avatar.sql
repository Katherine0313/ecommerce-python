-- ============================================================
-- admin 表新增 admin_avatar 列：后台管理员头像（AI 聊天窗用户头像来源）
-- 与 C 端 user.avatar 分开：后台聊天头像读 admin.admin_avatar，
-- 商城 H5 / 小程序聊天头像读 user.avatar，避免拉错表。
-- 幂等：用 information_schema 判断列是否存在，已存在则跳过
-- （随 ai-service 容器启动由 migrate.py 自动执行）
-- ============================================================

SET @exists := (
  SELECT COUNT(*) FROM information_schema.COLUMNS
  WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'admin' AND COLUMN_NAME = 'admin_avatar'
);
SET @sql := IF(@exists = 0,
  'ALTER TABLE `admin` ADD COLUMN admin_avatar VARCHAR(255) DEFAULT NULL COMMENT ''管理员头像URL'' AFTER phone',
  'SELECT 1');
PREPARE stmt FROM @sql; EXECUTE stmt; DEALLOCATE PREPARE stmt;
