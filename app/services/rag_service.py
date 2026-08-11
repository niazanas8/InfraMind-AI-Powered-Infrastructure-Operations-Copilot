from __future__ import annotations

import json as _json
from typing import Any

from loguru import logger

from app.config import settings
from app.models import (
    ChatResponse,
    ResponseMetadata,
    RetrievedChunk,
    RetrievedChunkPreview,
)
from app.security.spotlighting import build_spotlighted_context
from app.security.system_prompt import build_system_prompt
from app.services.crag import crag_pipeline
from app.services.embedding_service import embed_texts
from app.services.hyde import HyDERetriever
from app.services.llm_service import generate
from app.services.query_cache_service import query_cache
from app.services.reranking import Reranker
from app.services.router_service import classify_intent
from app.services.self_reflective import reflect_on_answer, should_regenerate
from app.services.sql_service import SQLService
from app.services.vector_store import hybrid_search, search, sparse_search


Flags = dict[str, Any]


def _flag(flags: Flags | None, key: str, default: Any) -> Any:
    if not isinstance(flags, dict):
        return default
    return flags.get(key, default)


def _retrieve(question: str, flags: Flags | None = None) -> list[RetrievedChunk]:
    final_top_k = int(_flag(flags, "top_k", 5))
    mode = str(_flag(flags, "search_mode", "dense"))
    rerank = bool(_flag(flags, "enable_rerank", False))
    hyde = bool(_flag(flags, "enable_hyde", False))
    enable_crag = bool(_flag(flags, "enable_crag", settings.crag_enabled_by_default))

    retrieve_k = settings.reranker_initial_top_k if rerank else final_top_k

    if hyde:
        chunks = HyDERetriever().retrieve(question, top_k=retrieve_k)
    elif mode == "sparse":
        chunks = sparse_search(question, top_k=retrieve_k)
    elif mode == "hybrid":
        query_embedding = embed_texts([question])[0]
        chunks = hybrid_search(query_embedding, question, top_k=retrieve_k)
    else:
        query_embedding = embed_texts([question])[0]
        chunks = search(query_embedding, top_k=retrieve_k)

    if rerank and chunks:
        chunks = Reranker().rerank(question, chunks, top_k=final_top_k)
    else:
        chunks = chunks[:final_top_k]

    chunks, evaluation, used_web = crag_pipeline(
        question=question,
        chunks=chunks,
        enable_crag=enable_crag,
    )

    logger.info(
        "CRAG | enabled={} score={} label={} used_web={}",
        enable_crag,
        evaluation.relevance_score,
        evaluation.relevance_label,
        used_web,
    )

    return chunks


def _generate(
    question: str,
    chunks: list[RetrievedChunk],
    flags: Flags | None = None,
) -> ChatResponse:
    enable_self_reflective = bool(_flag(flags, "enable_self_reflective", False))

    spotlighted = build_spotlighted_context(chunks)
    system = build_system_prompt()

    def _raw(q: str) -> str:
        return generate(
            system_prompt=system,
            user_message=f"{spotlighted}\n\nQuestion: {q}",
        )["text"]

    working_q = question
    raw = _raw(working_q)

    iterations = 0
    last_score: float | None = None
    final_refined: str | None = None

    if enable_self_reflective:
        while True:
            reflection = reflect_on_answer(
                question=working_q,
                answer=raw,
                context=spotlighted,
            )

            last_score = float(reflection.reflection_score)

            if not should_regenerate(reflection, iterations):
                break

            final_refined = reflection.refined_question or working_q
            working_q = final_refined
            raw = _raw(working_q)
            iterations += 1

    chunk_previews = [
        RetrievedChunkPreview(
            text=chunk.text,
            source=chunk.source,
            score=chunk.score,
        )
        for chunk in chunks
    ]

    return ChatResponse(
        answer=raw,
        sources=list({chunk.source for chunk in chunks}),
        confidence=0.7,
        metadata=ResponseMetadata(
            route="rag",
            retrieved_chunks=chunk_previews,
            reflection_iterations=iterations,
            reflection_score=last_score,
            refined_question=final_refined,
        ),
    )


def _run_sql_inline(question: str) -> ChatResponse:
    svc = SQLService()

    try:
        generated = svc.generate_sql(question)
        sql = generated["sql"]
        rows = svc.execute_sql(sql)

        if not rows:
            answer = "No results."
            row_chunks: list[RetrievedChunkPreview] = []
        else:
            answer = f"Query results:\n```json\n{_json.dumps(rows, indent=2, default=str)}\n```"
            row_chunks = [
                RetrievedChunkPreview(
                    text=_json.dumps(row, default=str),
                    source="query_results",
                    score=1.0,
                )
                for row in rows
            ]

        return ChatResponse(
            answer=answer,
            sources=["query_results"],
            confidence=0.9,
            metadata=ResponseMetadata(
                route="sql",
                retrieved_chunks=row_chunks,
            ),
        )

    except Exception as exc:
        logger.exception("SQL path failed: {}", exc)
        return ChatResponse(
            answer=f"SQL generation/execution failed: {exc}",
            sources=[],
            confidence=0.0,
            metadata=ResponseMetadata(
                route="sql",
                retrieved_chunks=[],
            ),
        )


def _run_hybrid_inline(
    question: str,
    flags: Flags | None = None,
) -> tuple[ChatResponse, list[RetrievedChunk]]:
    chunks = _retrieve(question, flags=flags)

    svc = SQLService()
    rows: list[dict[str, Any]] = []

    try:
        generated = svc.generate_sql(question)
        sql = generated.get("sql", "")
        rows = svc.execute_sql(sql)
    except Exception as exc:
        logger.warning("Hybrid SQL leg failed: {}", exc)

    spotlighted = build_spotlighted_context(chunks)

    system = (
        "You are an SRE assistant. Synthesize the database query results "
        "AND the retrieved documents into a single coherent answer. "
        "Cite [database query] for SQL results and [filename] for documents."
    )

    sql_section = ""
    if rows:
        sql_section = (
            f"\n=== Database Results ===\n"
            f"```json\n{_json.dumps(rows, indent=2, default=str)}\n```\n"
        )

    user_msg = f"{sql_section}{spotlighted}\n\nQuestion: {question}"

    raw = generate(
        system_prompt=system,
        user_message=user_msg,
    )["text"]

    response = ChatResponse(
        answer=raw,
        sources=["database query"] + list({chunk.source for chunk in chunks}),
        confidence=0.8,
        metadata=ResponseMetadata(
            route="hybrid",
            retrieved_chunks=[
                RetrievedChunkPreview(
                    text=chunk.text,
                    source=chunk.source,
                    score=chunk.score,
                )
                for chunk in chunks
            ],
        ),
    )

    return response, chunks


def _cache_context(flags: Flags | None) -> dict[str, Any]:
    return {
        "search_mode": _flag(flags, "search_mode", "dense"),
        "enable_hyde": bool(_flag(flags, "enable_hyde", False)),
        "enable_rerank": bool(_flag(flags, "enable_rerank", False)),
        "enable_crag": bool(_flag(flags, "enable_crag", settings.crag_enabled_by_default)),
        "enable_self_reflective": bool(_flag(flags, "enable_self_reflective", False)),
        "top_k": int(_flag(flags, "top_k", 5)),
    }


def run_rag(question: str, flags: Flags | None = None) -> ChatResponse:
    cache_ctx = _cache_context(flags)

    cached = query_cache.get_rag_answer(question, cache_ctx)
    if cached is not None:
        response = ChatResponse(**cached)
        response.cache_hit = True
        response.metadata.cache_hit = True
        return response

    intent = classify_intent(question)

    logger.info(
        "L8 query | intent={} mode={} rerank={} hyde={} crag={} self_rag={} top_k={}",
        intent,
        _flag(flags, "search_mode", "dense"),
        _flag(flags, "enable_rerank", False),
        _flag(flags, "enable_hyde", False),
        _flag(flags, "enable_crag", settings.crag_enabled_by_default),
        _flag(flags, "enable_self_reflective", False),
        int(_flag(flags, "top_k", 5)),
    )

    if intent == "sql":
        response = _run_sql_inline(question)
    elif intent == "hybrid":
        response, _ = _run_hybrid_inline(question, flags)
    else:
        chunks = _retrieve(question, flags)
        response = _generate(question, chunks, flags)

    query_cache.set_rag_answer(question, response.model_dump(), cache_ctx)

    return response


def run_rag_with_trace(
    question: str,
    flags: Flags | None = None,
) -> tuple[ChatResponse, list[RetrievedChunk]]:
    intent = classify_intent(question)

    if intent == "sql":
        response = _run_sql_inline(question)
        chunks = [
            RetrievedChunk(
                text=chunk.text,
                source=chunk.source,
                score=chunk.score,
            )
            for chunk in response.metadata.retrieved_chunks
        ]
        return response, chunks

    if intent == "hybrid":
        return _run_hybrid_inline(question, flags)

    chunks = _retrieve(question, flags)
    response = _generate(question, chunks, flags)

    return response, chunks


run_rag_with_trace_no_cache = run_rag_with_trace