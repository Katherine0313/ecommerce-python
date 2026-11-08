"""知识库只读仓库 + 命中/未命中埋点。

- 启动后整表加载到内存，60 秒缓存，运营改库后最多 1 分钟生效
- hit_count / ai_kb_miss 写入失败只记日志，不影响回答
"""

import asyncio
import logging
import time

import pymysql
from pymysql.cursors import DictCursor

from app.config import settings
from app.kb.matcher import KbEntry
from app.kb.normalize import split_keywords

logger = logging.getLogger(__name__)

_cache: list[KbEntry] = []
_cache_at: float = 0.0
_cache_lock = asyncio.Lock()


def _connect() -> pymysql.connections.Connection:
    return pymysql.connect(
        host=settings.mysql_host,
        port=settings.mysql_port,
        user=settings.db_username,
        password=settings.db_password,
        database=settings.mysql_database,
        charset="utf8mb4",
        cursorclass=DictCursor,
        connect_timeout=3,
        read_timeout=5,
        autocommit=True,
    )


def _load_entries() -> list[KbEntry]:
    conn = _connect()
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT id, category, question, answer, keywords, priority, hit_count
                FROM ai_kb_qa
                WHERE status = 1
                ORDER BY priority DESC, id ASC
                """
            )
            rows = cursor.fetchall()
    finally:
        conn.close()

    return [
        KbEntry(
            id=row["id"],
            category=row["category"] or "",
            question=row["question"],
            answer=row["answer"],
            keywords=split_keywords(row["keywords"]),
            priority=int(row["priority"] or 0),
            hit_count=int(row["hit_count"] or 0),
        )
        for row in rows
    ]


async def load_entries() -> list[KbEntry]:
    """带缓存读取启用的知识条；库挂了就沿用旧缓存。"""
    global _cache, _cache_at
    now = time.monotonic()
    if _cache and now - _cache_at < settings.kb_cache_ttl:
        return _cache

    async with _cache_lock:
        now = time.monotonic()
        if _cache and now - _cache_at < settings.kb_cache_ttl:
            return _cache
        try:
            entries = await asyncio.to_thread(_load_entries)
            _cache = entries
            _cache_at = time.monotonic()
            logger.info("知识库缓存已刷新：%s 条", len(entries))
        except Exception:
            logger.exception("读取知识库失败，沿用旧缓存")
    return _cache


def _record_hit(entry_id: int) -> None:
    conn = _connect()
    try:
        with conn.cursor() as cursor:
            cursor.execute("UPDATE ai_kb_qa SET hit_count = hit_count + 1 WHERE id = %s", (entry_id,))
    finally:
        conn.close()


async def record_hit(entry_id: int) -> None:
    try:
        await asyncio.to_thread(_record_hit, entry_id)
    except Exception:
        logger.warning("写入知识库命中次数失败 id=%s", entry_id, exc_info=True)


def _record_miss(question: str, user_id: str, session_id: str, best_score: float) -> None:
    conn = _connect()
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO ai_kb_miss (question, user_id, session_id, best_score)
                VALUES (%s, %s, %s, %s)
                """,
                (question[:255], user_id[:64], (session_id or "")[:64], int(best_score)),
            )
    finally:
        conn.close()


async def record_miss(question: str, user_id: str, session_id: str, best_score: float) -> None:
    try:
        await asyncio.to_thread(_record_miss, question, user_id, session_id, best_score)
    except Exception:
        logger.warning("写入知识库未命中记录失败", exc_info=True)
