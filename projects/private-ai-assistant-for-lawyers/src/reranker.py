from FlagEmbedding import FlagReranker
import os

_reranker = None


def get_reranker():
    global _reranker
    if _reranker is None:
        print("Загружаю BGE-reranker-v2-m3...", flush=True)
        _reranker = FlagReranker('BAAI/bge-reranker-v2-m3', use_fp16=False)
        print("Реранкер загружен", flush=True)
    return _reranker


def rerank(query: str, documents: list, top_k: int = 5) -> list:
    """
    Пересортировывает список документов по релевантности к запросу.
    
    documents: список словарей с ключом 'text' (обязательно) 
               и любыми другими (например 'original')
    top_k: сколько лучших оставить
    
    Возвращает список той же структуры, но отсортированный и обрезанный.
    """
    if not documents:
        return []
    if len(documents) <= top_k:
        # Реранкер всё равно применим — для правильной сортировки
        pass

    rrk = get_reranker()
    pairs = [[query, doc['text']] for doc in documents]
    scores = rrk.compute_score(pairs, normalize=True)

    # scores может быть float если один документ, делаем список
    if isinstance(scores, float):
        scores = [scores]

    ranked = sorted(zip(scores, documents), key=lambda x: x[0], reverse=True)
    return [doc for _, doc in ranked[:top_k]]


if __name__ == "__main__":
    # Тест на простых данных
    query = "продолжительность основного отпуска"
    docs = [
        {"text": "Продолжительность основного отпуска не может быть менее 24 календарных дней."},
        {"text": "Работникам, получающим высшее образование в вечерней форме, предоставляется отпуск до 28 календарных дней."},
        {"text": "Отпуск по семейно-бытовым причинам предоставляется до 30 дней без сохранения зарплаты."},
    ]
    top = rerank(query, docs, top_k=3)
    print("\n=== РЕЗУЛЬТАТ РЕРАНКИНГА ===")
    for i, d in enumerate(top):
        print(f"{i+1}. {d['text'][:80]}...")
