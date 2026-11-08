"""
启动时迁移脚本：执行 sql/ 目录下所有 .sql 文件。

设计要点：
- 按文件名排序执行（用 YYYYMMDD_ 或 NNN_ 前缀控制顺序）
- 每个 .sql 文件可以包含多条语句，按 ; 切分（自动跳过 -- 和 /* */ 注释）
- 所有 SQL 必须幂等（CREATE TABLE IF NOT EXISTS / INSERT ... ON DUPLICATE KEY UPDATE / 等）
- 任一语句失败则进程以非零码退出，容器启动中止（避免带病上线）
"""
import os
import re
import sys
from pathlib import Path

import pymysql

# 让 migrate.py 既能在项目根目录直接运行，也能被 docker entrypoint 调用
sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.config import settings  # noqa: E402

SQL_DIR = Path(__file__).resolve().parent / "sql"


def _split_sql(script: str) -> list[str]:
    """把 SQL 脚本拆成单条语句，忽略 -- 和 /* */ 注释，保留字符串内的分号。"""
    # 先去掉 /* ... */ 注释（支持跨行）
    script = re.sub(r"/\*.*?\*/", "", script, flags=re.DOTALL)
    stmts: list[str] = []
    buf: list[str] = []
    in_single = False
    in_double = False
    in_backtick = False
    line_comment = False
    i = 0
    while i < len(script):
        ch = script[i]
        nxt = script[i + 1] if i + 1 < len(script) else ""

        if line_comment:
            if ch == "\n":
                line_comment = False
                buf.append("\n")
            i += 1
            continue

        if not in_single and not in_double and not in_backtick:
            if ch == "-" and nxt == "-":
                line_comment = True
                i += 2
                continue
            if ch == "#":
                line_comment = True
                i += 1
                continue

        if ch == "'" and not in_double and not in_backtick:
            in_single = not in_single
        elif ch == '"' and not in_single and not in_backtick:
            in_double = not in_double
        elif ch == "`" and not in_single and not in_double:
            in_backtick = not in_backtick
        elif ch == ";" and not in_single and not in_double and not in_backtick:
            stmt = "".join(buf).strip()
            if stmt:
                stmts.append(stmt)
            buf = []
            i += 1
            continue

        buf.append(ch)
        i += 1

    tail = "".join(buf).strip()
    if tail:
        stmts.append(tail)
    return stmts


def run_migrations() -> int:
    if not SQL_DIR.is_dir():
        print(f"[migrate] sql/ 目录不存在，跳过迁移: {SQL_DIR}")
        return 0

    sql_files = sorted(SQL_DIR.glob("*.sql"))
    if not sql_files:
        print(f"[migrate] {SQL_DIR} 下没有 .sql 文件，跳过迁移")
        return 0

    print(
        f"[migrate] 连接 MySQL {settings.mysql_host}:{settings.mysql_port}/{settings.mysql_database}"
    )
    conn = pymysql.connect(
        host=settings.mysql_host,
        port=settings.mysql_port,
        user=settings.db_username,
        password=settings.db_password,
        database=settings.mysql_database,
        charset="utf8mb4",
        autocommit=True,
    )

    try:
        for sql_file in sql_files:
            print(f"[migrate] >>> 执行 {sql_file.name}")
            script = sql_file.read_text(encoding="utf-8")
            stmts = _split_sql(script)
            with conn.cursor() as cur:
                for idx, stmt in enumerate(stmts, 1):
                    try:
                        cur.execute(stmt)
                    except Exception as e:
                        print(
                            f"[migrate] !!! {sql_file.name} 第 {idx} 条语句失败: {e}",
                            file=sys.stderr,
                        )
                        # 打印前 200 字符便于定位
                        print(f"--- SQL ---\n{stmt[:200]}", file=sys.stderr)
                        return 1
            print(f"[migrate] <<< {sql_file.name} 完成（{len(stmts)} 条语句）")
    finally:
        conn.close()

    print(f"[migrate] 全部迁移完成，共 {len(sql_files)} 个文件")
    return 0


if __name__ == "__main__":
    sys.exit(run_migrations())
