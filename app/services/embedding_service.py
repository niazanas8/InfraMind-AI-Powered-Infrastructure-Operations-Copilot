from sentence_transformers import SentenceTransformer

from app.config import settings
from app.services.query_cache_service import query_cache

_embedding_model: SentenceTransformer | None = None


def _get_embedding_model() -> SentenceTransformer:
    global _embedding_model
    if _embedding_model is None:
        _embedding_model = SentenceTransformer(settings.embedding_model)
    return _embedding_model

def embed_texts(texts: list[str], model: str | None = None) -> list[list[float]]:
    if not texts:
        return []

    results: list[list[float] | None] = [None] * len(texts)
    miss_indices: list[int] = []
    miss_texts: list[str] = []

    for i, text in enumerate(texts):
        cached = query_cache.get_embedding(text)
        if cached is not None:
            results[i] = cached

        else:
            miss_indices.append(i)
            miss_texts.append(text)

    if miss_texts:
        encoder = _get_embedding_model()
        embeddings = encoder.encode(miss_texts, normalize_embeddings=True).tolist()
        for idx_in_misses, vector in enumerate(embeddings):
            original_idx = miss_indices[idx_in_misses]
            results[original_idx] = vector
            query_cache.set_embedding(miss_texts[idx_in_misses], vector)

    return [r for r in results if r is not None]
