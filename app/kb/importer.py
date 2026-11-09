"""知识库 Excel 解析 / 模板 / 归档：CLI(scripts/import_kb.py) 与后台导入接口共用。

模板 7 列（顺序固定）：分类 / 问题 / 答案 / 关键词 / 优先级 / 状态 / 适用范围
- 适用范围：填「客服」则商城 AI 客服用，填「运营」则电商后台 AI 助手用；不填则按导入时选定的默认值归库；
- 前 4 列必须与模板一致（与旧 QandA.xlsx 4 列文件兼容）；
- 后 3 列（优先级/状态/适用范围）可整组省略，出现则必须按序匹配，否则整单拒绝；
- 判重键 (scope, category, question)，依赖唯一键 uk_ai_kb_scope_category_question；
- AI 检索读 ai_kb_qa 表，不读本模块归档的 Excel 文件（文件只作留档/追溯）。
"""

import json
import logging
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional

from openpyxl import Workbook, load_workbook

from app.config import settings

logger = logging.getLogger(__name__)

HEADER = ["分类", "问题", "答案", "关键词", "优先级", "状态", "适用范围"]
# 后 3 列为可选扩展列；前 4 列必须与旧脚本口径一致
REQUIRED_HEADER = HEADER[:4]
SCOPES = ("客服", "运营")
DEFAULT_CATEGORY = "通用"
MAX_ROWS = 500
MAX_FILE_BYTES = 2 * 1024 * 1024
_LIMITS = {"category": 50, "question": 200, "answer": 2000, "keywords": 200}

# 关键词里常见的多种分隔符
_SEPARATORS = (",", "，", "、", ";", "；", " ", "|")


class ImportReject(Exception):
    """文件级校验失败（表头不符/空数据/超限），整单拒绝，不解析数据行。"""


def _clean(value) -> str:
    return str(value or "").strip()


def split_keywords(raw: str) -> str:
    """多种分隔符统一归一成英文逗号连接（与 matcher 的 split_keywords 口径互逆）。"""
    text = _clean(raw)
    if not text:
        return ""
    for sep in _SEPARATORS:
        text = text.replace(sep, ",")
    return ",".join(item for item in (x.strip() for x in text.split(",")) if item)


def build_template() -> bytes:
    """内存生成导入模板 xlsx：表头 + 两行示例，不落盘。"""
    wb = Workbook()
    ws = wb.active
    ws.title = "qa"
    ws.append(HEADER)
    ws.append(["发货物流", "多久能发货？", "工作日 16 点前下单当天发，其余次日发。",
               "发货,物流,多久", 10, 1, "客服"])
    ws.append(["运营示例", "如何配置首页 Banner？", "后台「运营位管理 → Banner」上传图片并保存。",
               "Banner,运营位", 0, 1, "运营"])
    from io import BytesIO
    buf = BytesIO()
    wb.save(buf)
    return buf.getvalue()


def archive_upload(content: bytes, default_scope: str) -> str:
    """上传文件落盘归档（保留原始内容），并写 sidecar 记录默认 scope 供 confirm 复用。

    返回 importFileId（即文件名）。
    """
    import_dir = Path(settings.kb_import_dir)
    import_dir.mkdir(parents=True, exist_ok=True)
    file_id = f"{datetime.now():%Y%m%d}_{uuid.uuid4().hex[:8]}.xlsx"
    (import_dir / file_id).write_bytes(content)
    (import_dir / f"{file_id}.json").write_text(
        json.dumps({"defaultScope": default_scope}, ensure_ascii=False), encoding="utf-8")
    return file_id


def resolve_archive(file_id: str) -> tuple[Path, str]:
    """按 importFileId 找回归档文件与默认 scope；非法/不存在抛 ImportReject。"""
    name = Path(str(file_id or "")).name  # 防路径穿越
    if not name.endswith(".xlsx"):
        raise ImportReject("importFileId 非法")
    path = Path(settings.kb_import_dir) / name
    if not path.exists():
        raise ImportReject("导入文件不存在或已清理，请重新上传")
    default_scope = "客服"
    sidecar = path.with_suffix(path.suffix + ".json")
    if sidecar.exists():
        try:
            default_scope = json.loads(sidecar.read_text(encoding="utf-8")).get("defaultScope") or "客服"
        except Exception:
            logger.warning("导入 sidecar 解析失败，默认 scope=客服 file=%s", name, exc_info=True)
    return path, default_scope


def _validate_header(header: list[str]) -> None:
    """表头校验：前 4 列必须一致；扩展列出现则按序匹配；其余一律整单拒绝。"""
    if len(header) < len(REQUIRED_HEADER):
        raise ImportReject(
            f"模板至少需要 {len(REQUIRED_HEADER)} 列（{'/'.join(REQUIRED_HEADER)}），请使用最新模板")
    for i, expect in enumerate(REQUIRED_HEADER):
        if header[i] != expect:
            raise ImportReject(f"请使用模板。")
    extras = header[len(REQUIRED_HEADER):]
    if extras:
        expect_extra = HEADER[len(REQUIRED_HEADER):len(REQUIRED_HEADER) + len(extras)]
        if extras != expect_extra or len(header) > len(HEADER):
            raise ImportReject("扩展列须按『优先级/状态/适用范围』顺序排列，请勿增删列，请重新下载模板")


def parse_rows(path: Path, default_scope: str = "客服", max_rows: Optional[int] = MAX_ROWS) -> list[dict]:
    """解析 + 逐行预检（不落库）。返回行级结果列表。

    每行：{row, scope, category, question, answer, keywords, priority, status,
           result: ok|failed|duplicate, reason}
    与库内已有键的判重（updated 标记）由调用方做（需要 DB）。
    max_rows 是 Web 上传的保护阈值；CLI 批量迁移可传 None 放开。
    """
    workbook = load_workbook(path, read_only=True, data_only=True)
    try:
        sheet = workbook["qa"] if "qa" in workbook.sheetnames else workbook.worksheets[0]
        rows_iter = sheet.iter_rows(values_only=True)
        header_raw = next(rows_iter, None)
        if not header_raw:
            raise ImportReject("文件为空，请使用最新模板")
        _validate_header([_clean(c) for c in header_raw])

        rows: list[dict] = []
        seen: dict[tuple[str, str, str], int] = {}
        for index, raw in enumerate(rows_iter, start=2):
            if not raw or all(_clean(c) == "" for c in raw):
                continue
            if max_rows is not None and len(rows) >= max_rows:
                raise ImportReject(f"单次最多导入 {max_rows} 行，请拆分文件")
            cell = lambda i: _clean(raw[i]) if len(raw) > i else ""
            row = {
                "row": index,
                "category": cell(0) or DEFAULT_CATEGORY,
                "question": cell(1),
                "answer": cell(2),
                "keywords": split_keywords(cell(3)),
                "priority": 0,
                "status": 1,
                "scope": cell(6) or default_scope,
                "result": "ok",
                "reason": "",
            }
            rows.append(row)
            # 逐行校验：任一不过 → result=failed，不影响其它行
            if not row["question"] or not row["answer"]:
                row.update(result="failed", reason="问题或答案为空")
                continue
            for field, limit in _LIMITS.items():
                if len(row[field]) > limit:
                    row.update(result="failed", reason=f"{field} 超长（≤{limit} 字，当前 {len(row[field])} 字，请删减）")
                    break
            if row["result"] == "failed":
                continue
            if cell(4):
                try:
                    row["priority"] = int(float(cell(4)))
                except ValueError:
                    row.update(result="failed", reason="优先级必须是整数")
                    continue
            if cell(5):
                if cell(5) in ("0", "1"):
                    row["status"] = int(cell(5))
                else:
                    row.update(result="failed", reason="状态只能是 1（启用）或 0（停用）")
                    continue
            if row["scope"] not in SCOPES:
                row.update(result="failed", reason=f"适用范围必须是「客服」或「运营」（当前「{row['scope']}」）")
                continue
            key = (row["scope"], row["category"], row["question"])
            if key in seen:
                row.update(result="duplicate", reason=f"与第 {seen[key]} 行重复，本行跳过")
                continue
            seen[key] = index
    finally:
        workbook.close()

    if not rows:
        raise ImportReject("数据区为空（只有表头），请填充后再上传")
    return rows


def mark_existing(rows: list[dict], existing_keys: set[tuple[str, str, str]]) -> None:
    """把 ok 行中与库内 (scope, category, question) 一致的标为 updated（upsert 覆盖语义）。"""
    for row in rows:
        if row["result"] == "ok" and (row["scope"], row["category"], row["question"]) in existing_keys:
            row["result"] = "updated"
            row["reason"] = "库内已有同键条目，导入将覆盖更新（命中次数保留）"


def summarize(rows: list[dict]) -> dict:
    """按 result 统计：ok/updated/failed/duplicate。"""
    summary = {"total": len(rows), "ok": 0, "updated": 0, "failed": 0, "duplicate": 0}
    for row in rows:
        summary[row["result"]] = summary.get(row["result"], 0) + 1
    return summary
