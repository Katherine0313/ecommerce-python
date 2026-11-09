"""products.body 文字补全（一次性回填脚本）

现状：存量商品 body 只有 <img>，小程序/商城详情页纯图无文字。
做法：不用 AI，用已有字段拼文字段落插到 body 头部——
  主标题(title) / 副标题(sub_title) / 规格参数(specs JSON)
幂等：去标签纯文本 >= 30 字的商品自动跳过。

用法：
  python scripts/backfill_body_text.py            # dry-run 预览，不写库
  python scripts/backfill_body_text.py --apply    # 备份后批量更新
"""
import argparse
import datetime
import html
import json
import os
import re
import sys

import pymysql

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from app.config import settings  # noqa: E402

MIN_TEXT_LEN = 30
MARK = 'data-bf-text="1"'  # 插入块标记，二次执行可识别


def strip_tags(html_text):
    return re.sub(r'<[^>]+>', '', html_text or '').strip()


def build_block(title, sub_title, specs):
    parts = []
    if title:
        parts.append(f'<p {MARK}><strong>{html.escape(title)}</strong></p>')
    if sub_title:
        parts.append(f'<p {MARK}>{html.escape(sub_title)}</p>')
    items = []
    try:
        for s in json.loads(specs) if specs else []:
            n, v = (s.get('name') or '').strip(), (s.get('value') or '').strip()
            if n and v:
                items.append(f'{html.escape(n)}：{html.escape(v)}')
    except (ValueError, AttributeError):
        pass
    if items:
        parts.append(f'<p {MARK}><strong>产品参数</strong></p>')
        parts.extend(f'<p {MARK}>{line}</p>' for line in items)
    return ''.join(parts)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--apply', action='store_true', help='实际写库（默认只预览）')
    args = ap.parse_args()

    conn = pymysql.connect(
        host=settings.mysql_host, port=settings.mysql_port,
        user=settings.db_username, password=settings.db_password,
        database=settings.mysql_database, charset='utf8mb4', autocommit=False)
    cur = conn.cursor(pymysql.cursors.DictCursor)
    cur.execute('SELECT id, name, title, sub_title, specs, body FROM products')
    rows = cur.fetchall()

    todo = []
    for r in rows:
        body = r['body'] or ''
        if MARK in body:
            continue  # 已补过
        if len(strip_tags(body)) >= MIN_TEXT_LEN:
            continue  # 已有足够文字
        block = build_block(r['title'], r['sub_title'], r['specs'])
        if block:
            todo.append((r['id'], block, body))

    print(f'总商品 {len(rows)}，待补 {len(todo)}')
    for pid, block, _ in todo[:2]:
        print(f'--- 预览 id={pid} ---')
        print(block[:500])

    if not args.apply:
        print('dry-run 结束，未写库。加 --apply 执行。')
        conn.close()
        return

    if not todo:
        conn.close()
        return

    # 备份受影响行（id + 原 body），落 SQL 文件便于回滚
    os.makedirs('backups', exist_ok=True)
    ts = datetime.datetime.now().strftime('%Y%m%d_%H%M%S')
    backup = os.path.join('backups', f'body_backup_{ts}.sql')
    with open(backup, 'w', encoding='utf8') as f:
        for pid, _, body in todo:
            esc = body.replace('\\', '\\\\').replace("'", "\\'")
            f.write(f"UPDATE products SET body='{esc}' WHERE id={pid};\n")
    print(f'已备份 {len(todo)} 行原 body -> {backup}')

    try:
        cur.executemany(
            'UPDATE products SET body=%s WHERE id=%s',
            [(block + body, pid) for pid, block, body in todo])
        conn.commit()
        print(f'已更新 {cur.rowcount} 行')
    except Exception:
        conn.rollback()
        raise
    conn.close()


if __name__ == '__main__':
    main()
