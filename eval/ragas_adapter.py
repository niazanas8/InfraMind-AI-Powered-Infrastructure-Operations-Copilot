from __future__ import annotations

from typing import Any, cast

from datasets import Dataset
from openai import OpenAI
from ragas import evaluate
from ragas.embeddings import HuggingFaceEmbeddings
from ragas.llms import llm_factory
from ragas.metrics import (
    answer_relevancy,
    context_precision,
    context_recall,
    faithfulness,
)

from app.config import settings

METRICS = [
    faithfulness,
    context_precision,
    context_recall,
    answer_relevancy,
]



def _get_ragas_llm():
    """Create a Ragas-compatible judge LLM using Groq's OpenAI-compatible API."""
    client = OpenAI(
        api_key=settings.groq_api_key,
        base_url=settings.groq_base_url,
    )
    return llm_factory(
        settings.llm_model_grader,
        client=client,
        provider="openai",
        temperature=0,
    )

def _get_ragas_embeddings():
    """Create Ragas-compatible embeddings using the local BGE model."""
    return HuggingFaceEmbeddings(model=settings.embedding_model)

def build_dataset(rows: list[dict]) -> Dataset:
    return Dataset.from_dict(
        {
            "user_input": [r["question"] for r in rows],
            "response": [r["answer"] for r in rows],
            "retrieved_contexts": [r["contexts"] for r in rows],
            "reference": [r["ground_truth"] for r in rows],
        }
    )

def run(rows: list[dict]) -> list[dict]:
    if not rows:
        return []

    ds = build_dataset(rows)
    result = cast(
        Any,
        evaluate(
            ds,
            metrics=METRICS,
            llm=_get_ragas_llm(),
            embeddings=_get_ragas_embeddings(),
            show_progress=False,
        ),
    )
    records = result.to_pandas().to_dict(orient="records")
    return cast(list[dict[str, Any]], records)


