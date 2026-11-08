"""知识库语义检索：硅基流动 bge-m3 embedding + 余弦相似度。

词面规则（matcher）先跑；词面分不够时才走这里，覆盖「你们店里卖什么 / 经营什么 /
有什么产品」这类同一语义的不同问法。embedding 接口异常时静默退化为纯词面检索，
不影响对话主流程。
"""

import asyncio
import logging

import httpx

from app.config import settings
from app.kb.matcher import KbEntry, MatchResult

logger = logging.getLogger(__name__)

_BATCH_SIZE = 32
_MAX_QUERY_CACHE = 256
# entry_id -> (问题原文, 向量)；问题被运营改过会自动重算
_index: dict[int, tuple[str, list[float]]] = {}
_query_cache: dict[str, list[float]] = {}
_lock = asyncio.Lock()
_warmed = False


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = sum(x * x for x in a) ** 0.5
    norm_b = sum(y * y for y in b) ** 0.5
    return dot / (norm_a * norm_b) if norm_a and norm_b else 0.0


async def _embed(texts: list[str]) -> list[list[float]]:
    vectors: list[list[float]] = []
    async with httpx.AsyncClient(timeout=30) as client:
        for start in range(0, len(texts), _BATCH_SIZE):
            chunk = texts[start:start + _BATCH_SIZE]
            resp = await client.post(
                f"{settings.openai_base_url.rstrip('/')}/embeddings",
                headers={"Authorization": f"Bearer {settings.siliconflow_api_key}"},
                json={"model": settings.embedding_model, "input": chunk},
            )
            resp.raise_for_status()
            data = resp.json().get("data") or []
            vectors.extend(item["embedding"] for item in data)
    return vectors


async def _ensure_vectors(entries: list[KbEntry]) -> bool:
    """补齐缺失/变更条目的向量；已有向量不重算。失败返回 False（调用方退化为词面检索）。"""
    async with _lock:
        missing = [e for e in entries if _index.get(e.id, ("",))[0] != e.question]
        if not missing:
            return True
        try:
            vectors = await _embed([e.question for e in missing])
        except Exception:
            logger.warning("知识库语义向量计算失败，暂时只用词面检索", exc_info=True)
            return False
        for entry, vector in zip(missing, vectors):
            _index[entry.id] = (entry.question, vector)
        logger.info("知识库语义索引更新：%s 条", len(_index))
        return True


async def warm_up(entries: list[KbEntry]) -> None:
    """启动时后台建索引：几百条问题一次性向量化，首次提问就不用等。"""
    global _warmed
    _warmed = await _ensure_vectors(entries)


async def semantic_match(entries: list[KbEntry], user_message: str) -> MatchResult | None:
    """语义兜底：最高余弦相似度达到阈值才算命中，避免把泛化相似当命中。"""
    if not _warmed or not _index or not user_message.strip():
        return None
    # 运营新增/修改的知识：增量补向量，避免必须重启才生效
    if any(_index.get(e.id, ("",))[0] != e.question for e in entries):
        if not await _ensure_vectors(entries):
            return None

    vector = _query_cache.get(user_message)
    if vector is None:
        try:
            vector = (await _embed([user_message]))[0]
        except Exception:
            logger.warning("问题向量化失败，退化为词面检索", exc_info=True)
            return None
        if len(_query_cache) >= _MAX_QUERY_CACHE:
            _query_cache.clear()
        _query_cache[user_message] = vector

    best_entry: KbEntry | None = None
    best_score = 0.0
    for entry in entries:
        cached = _index.get(entry.id)
        if not cached:
            continue
        score = _cosine(vector, cached[1])
        if score > best_score:
            best_entry, best_score = entry, score
    if not best_entry or best_score < settings.kb_semantic_threshold:
        return None
    return MatchResult(best_entry, best_score * 100)
