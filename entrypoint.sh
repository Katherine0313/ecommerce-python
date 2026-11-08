#!/bin/sh
set -e

echo "[entrypoint] 开始执行数据库迁移..."
python migrate.py

echo "[entrypoint] 迁移完成，启动 ai-service..."
exec uvicorn app.main:app --host 0.0.0.0 --port 8000
