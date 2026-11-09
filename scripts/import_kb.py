"""把 docs/QandA.xlsx 导入 MySQL 知识库表 ai_kb_qa。

用法（在 ecommerce-python 目录下执行）：
    python scripts/import_kb.py
    python scripts/import_kb.py --xlsx ../e-commerce-boot/docs/QandA.xlsx
    python scripts/import_kb.py --scope 运营 --dry-run

幂等：按 (scope, 分类, 问题) 唯一键 upsert，重复执行只更新答案/关键词/优先级/状态，
不会产生重复数据，也不会清零已有的 hit_count。
解析与 upsert 逻辑与后台「知识库批量导入」接口共用 app/kb/importer.py + app/kb/repo.py。
前置：表结构已含 scope 列与 uk_ai_kb_scope_category_question 唯一键（docs/sql 迁移已执行）。
"""

import argparse
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

from app.kb import importer  # noqa: E402
from app.kb.repo import list_kb_keys, upsert_kb_entries_batch  # noqa: E402

DEFAULT_XLSX = BASE_DIR.parent / "e-commerce-boot" / "docs" / "QandA.xlsx"


def main() -> int:
    parser = argparse.ArgumentParser(description="导入知识库到 ai_kb_qa")
    parser.add_argument("--xlsx", type=Path, default=DEFAULT_XLSX, help="QandA.xlsx 路径")
    parser.add_argument("--scope", default="客服", choices=importer.SCOPES,
                        help="行内 scope 列留空时归入的默认库")
    parser.add_argument("--dry-run", action="store_true", help="只预检统计，不写库")
    args = parser.parse_args()

    if not args.xlsx.exists():
        print(f"找不到 Excel：{args.xlsx}")
        return 1
    try:
        # CLI 做存量迁移，行数上限放开（Web 上传接口仍限 500 行）
        rows = importer.parse_rows(args.xlsx, args.scope, max_rows=None)
    except importer.ImportReject as exc:
        print(f"文件校验失败：{exc}")
        return 1
    importer.mark_existing(rows, list_kb_keys())
    for r in rows:
        if r["result"] == "failed":
            print(f"  第 {r['row']} 行失败：{r['reason']}")
        elif r["result"] == "duplicate":
            print(f"  第 {r['row']} 行表内重复，已跳过：{r['reason']}")
    summary = importer.summarize(rows)
    print(f"预检：新增 {summary['ok']} 条，更新 {summary['updated']} 条，"
          f"失败 {summary['failed']} 条，重复跳过 {summary['duplicate']} 条")

    if args.dry_run:
        return 0
    valid = [r for r in rows if r["result"] in ("ok", "updated")]
    if not valid:
        print("没有可导入的有效行")
        return 1
    inserted, updated = upsert_kb_entries_batch(valid)
    print(f"导入完成：新增 {inserted} 条，更新 {updated} 条")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
