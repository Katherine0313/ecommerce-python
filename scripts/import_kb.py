"""把 docs/QandA.xlsx 导入 MySQL 知识库表 ai_kb_qa。

用法（在 ecommerce-python 目录下执行）：
    python scripts/import_kb.py
    python scripts/import_kb.py --xlsx ../e-commerce-boot/docs/QandA.xlsx
    python scripts/import_kb.py --dry-run

幂等：按 (分类, 问题) 唯一键 upsert，重复执行只更新答案和关键词，
不会产生重复数据，也不会清零已有的 hit_count。
"""

import argparse
import sys
from pathlib import Path

import pymysql
from openpyxl import load_workbook
from pymysql.cursors import DictCursor

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

from app.config import settings  # noqa: E402

DEFAULT_XLSX = BASE_DIR.parent / "e-commerce-boot" / "docs" / "QandA.xlsx"
DEFAULT_SQL = BASE_DIR.parent / "e-commerce-boot" / "docs" / "sql" / "20261105_kb.sql"

# 关键词里常见的多种分隔符
_SEPARATORS = (",", "，", "、", ";", "；", " ", "|")


def _clean(value) -> str:
    return str(value or "").strip()


def _split_keywords(raw: str) -> list[str]:
    text = _clean(raw)
    if not text:
        return []
    for sep in _SEPARATORS:
        text = text.replace(sep, ",")
    return [item.strip() for item in text.split(",") if item.strip()]


def load_rows(xlsx: Path) -> list[dict]:
    """读取 Excel，返回待导入的行；跳过表头和空行。"""
    workbook = load_workbook(xlsx, read_only=True, data_only=True)
    sheet = workbook["qa"] if "qa" in workbook.sheetnames else workbook.worksheets[0]

    rows: list[dict] = []
    seen: set[tuple[str, str]] = set()
    skipped = 0
    for index, raw in enumerate(sheet.iter_rows(min_row=2, values_only=True), start=2):
        if not raw or all(cell is None or _clean(cell) == "" for cell in raw):
            continue
        category = _clean(raw[0]) if len(raw) > 0 else ""
        question = _clean(raw[1]) if len(raw) > 1 else ""
        answer = _clean(raw[2]) if len(raw) > 2 else ""
        keywords = _clean(raw[3]) if len(raw) > 3 else ""
        if not question or not answer:
            print(f"  第 {index} 行缺少问题或答案，已跳过")
            skipped += 1
            continue
        key = (category, question)
        if key in seen:
            print(f"  第 {index} 行与前面重复（{question}），已跳过")
            skipped += 1
            continue
        seen.add(key)
        rows.append({
            "category": category or "未分类",
            "question": question,
            "answer": answer,
            "keywords": ",".join(_split_keywords(keywords)),
        })
    workbook.close()
    if skipped:
        print(f"  跳过 {skipped} 行")
    return rows


def ensure_schema(conn, sql_path: Path) -> None:
    """表不存在时执行建表 SQL；已存在则跳过（CREATE TABLE IF NOT EXISTS）。"""
    if not sql_path.exists():
        print(f"未找到建表脚本 {sql_path}，假定表已存在")
        return
    statements = [
        part.strip()
        for part in sql_path.read_text(encoding="utf-8").split(";")
        if part.strip() and any(
            line.strip() and not line.strip().startswith("--")
            for line in part.splitlines()
        )
    ]
    with conn.cursor() as cursor:
        for statement in statements:
            cursor.execute(statement)
    conn.commit()
    print(f"建表脚本已执行：{sql_path.name}")


def upsert_rows(conn, rows: list[dict], dry_run: bool) -> tuple[int, int]:
    inserted = updated = 0
    with conn.cursor() as cursor:
        for row in rows:
            cursor.execute(
                "SELECT id FROM ai_kb_qa WHERE category = %s AND question = %s",
                (row["category"], row["question"]),
            )
            existed = cursor.fetchone() is not None
            if dry_run:
                if existed:
                    updated += 1
                else:
                    inserted += 1
                continue
            cursor.execute(
                """
                INSERT INTO ai_kb_qa (category, question, answer, keywords)
                VALUES (%s, %s, %s, %s)
                ON DUPLICATE KEY UPDATE
                    answer = VALUES(answer),
                    keywords = VALUES(keywords),
                    status = 1
                """,
                (row["category"], row["question"], row["answer"], row["keywords"]),
            )
            if existed:
                updated += 1
            else:
                inserted += 1
    if not dry_run:
        conn.commit()
    return inserted, updated


def main() -> int:
    parser = argparse.ArgumentParser(description="导入客服知识库到 ai_kb_qa")
    parser.add_argument("--xlsx", type=Path, default=DEFAULT_XLSX, help="QandA.xlsx 路径")
    parser.add_argument("--sql", type=Path, default=DEFAULT_SQL, help="建表 SQL 路径")
    parser.add_argument("--dry-run", action="store_true", help="只统计，不写库")
    args = parser.parse_args()

    if not args.xlsx.exists():
        print(f"找不到 Excel：{args.xlsx}")
        return 1

    rows = load_rows(args.xlsx)
    if not rows:
        print("Excel 里没有可导入的数据")
        return 1
    print(f"读取到 {len(rows)} 条问答")

    conn = pymysql.connect(
        host=settings.mysql_host,
        port=settings.mysql_port,
        user=settings.db_username,
        password=settings.db_password,
        database=settings.mysql_database,
        charset="utf8mb4",
        cursorclass=DictCursor,
        connect_timeout=5,
        autocommit=False,
    )
    try:
        if not args.dry_run:
            ensure_schema(conn, args.sql)
        inserted, updated = upsert_rows(conn, rows, args.dry_run)
    finally:
        conn.close()

    action = "待新增" if args.dry_run else "新增"
    print(f"导入完成：{action} {inserted} 条，更新 {updated} 条")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
