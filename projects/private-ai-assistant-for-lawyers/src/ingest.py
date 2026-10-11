import os
import re
import json
import hashlib
import sys
import fcntl
from datetime import datetime

# Делаем stdout построчно-буферизованным — print сразу идёт в journal
try:
    sys.stdout.reconfigure(line_buffering=True)
except Exception:
    pass

# ============ ГЛОБАЛЬНЫЙ LOCK — только ОДИН ингест за раз ============
_ingest_lock_fp = None


def acquire_ingest_lock():
    """Пытается взять эксклюзивный lock. Возвращает True/False."""
    global _ingest_lock_fp
    # При старте — сбросить флаг остановки, если остался от прошлого запуска
    try:
        if os.path.exists("/tmp/rag_stop_requested"):
            os.remove("/tmp/rag_stop_requested")
    except Exception:
        pass
    try:
        _ingest_lock_fp = open("/tmp/rag_ingest.lock", "w")
        fcntl.flock(_ingest_lock_fp, fcntl.LOCK_EX | fcntl.LOCK_NB)
        _ingest_lock_fp.write(str(os.getpid()))
        _ingest_lock_fp.flush()
        return True
    except (BlockingIOError, OSError):
        if _ingest_lock_fp:
            try:
                _ingest_lock_fp.close()
            except Exception:
                pass
            _ingest_lock_fp = None
        return False


def release_ingest_lock():
    global _ingest_lock_fp
    if _ingest_lock_fp:
        try:
            fcntl.flock(_ingest_lock_fp, fcntl.LOCK_UN)
            _ingest_lock_fp.close()
        except Exception:
            pass
        _ingest_lock_fp = None
from pypdf import PdfReader
from docx import Document
from qdrant_client import QdrantClient
from qdrant_client.models import Distance, VectorParams, PointStruct, Filter, FieldCondition, MatchValue, FilterSelector
from ocr import extract_text_from_image, extract_text_from_scanned_pdf

QDRANT_URL = "http://localhost:6333"
COLLECTION_NAME = "legal_docs"
FILES_HASH_COLLECTION = "file_hashes"
EMBEDDING_MODEL = 'BAAI/bge-m3'
CHUNK_SIZE = 1500
CHUNK_OVERLAP = 150
BATCH_SIZE = 8
MAX_LENGTH = 512

STATUS_FILE = os.path.join(os.path.dirname(__file__), "index_status.json")

client = QdrantClient(url=QDRANT_URL)


def _load_status():
    if not os.path.exists(STATUS_FILE):
        return {}
    try:
        with open(STATUS_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return {}


def _save_status(status):
    tmp = STATUS_FILE + ".tmp"
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(status, f, ensure_ascii=False, indent=2)
    os.replace(tmp, STATUS_FILE)


def set_file_status(path, **fields):
    status = _load_status()
    entry = status.get(path, {})
    entry.update(fields)
    entry["updated_at"] = datetime.now().isoformat(timespec='seconds')
    status[path] = entry
    _save_status(status)


def get_status():
    return _load_status()


def remove_status(path):
    status = _load_status()
    if path in status:
        del status[path]
        _save_status(status)


_embedder = None


def get_embedder():
    global _embedder
    if _embedder is None:
        from FlagEmbedding import BGEM3FlagModel
        print("Загружаю BGE-M3...")
        _embedder = BGEM3FlagModel(EMBEDDING_MODEL, use_fp16=False)
        print("BGE-M3 загружена")
    return _embedder


def get_embedding(text):
    embedder = get_embedder()
    result = embedder.encode([text], batch_size=1, max_length=MAX_LENGTH)
    return result['dense_vecs'][0].tolist()


def get_embeddings_batch(texts, on_progress=None, stop_event=None):
    """
    Векторизация с разбивкой на батчи.
    stop_event — threading.Event для остановки.
    """
    import numpy as np
    embedder = get_embedder()
    n = len(texts)
    parts = []
    for i in range(0, n, BATCH_SIZE):
        if stop_event is not None and stop_event.is_set():
            raise InterruptedError("Индексация остановлена пользователем")
        if os.path.exists("/tmp/rag_stop_requested"):
            raise InterruptedError("Индексация остановлена пользователем (флаг)")
        batch = texts[i:i + BATCH_SIZE]
        res = embedder.encode(batch, batch_size=BATCH_SIZE, max_length=MAX_LENGTH)
        parts.append(res['dense_vecs'])
        if on_progress:
            try:
                on_progress(min(i + BATCH_SIZE, n), n)
            except Exception:
                pass
    if not parts:
        return np.zeros((0, 1024), dtype='float32')
    return np.vstack(parts)


def ensure_collections():
    if not client.collection_exists(COLLECTION_NAME):
        client.create_collection(
            collection_name=COLLECTION_NAME,
            vectors_config=VectorParams(size=1024, distance=Distance.COSINE)
        )
    if not client.collection_exists(FILES_HASH_COLLECTION):
        client.create_collection(
            collection_name=FILES_HASH_COLLECTION,
            vectors_config=VectorParams(size=1, distance=Distance.DOT)
        )


def get_file_id(file_path):
    return hashlib.md5(file_path.encode()).hexdigest()


def get_file_hash(file_path):
    hasher = hashlib.md5()
    with open(file_path, 'rb') as f:
        for chunk in iter(lambda: f.read(4096), b''):
            hasher.update(chunk)
    return hasher.hexdigest()


def get_stored_hash(file_path):
    file_id = get_file_id(file_path)
    try:
        points = client.retrieve(
            collection_name=FILES_HASH_COLLECTION,
            ids=[file_id],
            with_payload=True
        )
        if points:
            return points[0].payload.get("hash")
    except Exception:
        pass
    return None


def store_file_hash(file_path, file_hash, uploaded_by=None):
    file_id = get_file_id(file_path)
    client.upsert(
        collection_name=FILES_HASH_COLLECTION,
        points=[PointStruct(
            id=file_id,
            vector=[0.0],
            payload={"hash": file_hash, "path": file_path, "uploaded_by": uploaded_by}
        )]
    )


def remove_file_points(file_path):
    client.delete(
        collection_name=COLLECTION_NAME,
        points_selector=FilterSelector(
            filter=Filter(must=[FieldCondition(key="source", match=MatchValue(value=file_path))])
        )
    )


def get_current_file_paths(folder_path):
    paths = set()
    for root, _, files in os.walk(folder_path):
        for file in files:
            ext = os.path.splitext(file)[1].lower()
            if ext in ('.pdf', '.docx', '.txt', '.jpg', '.jpeg', '.png'):
                paths.add(os.path.join(root, file))
    return paths


def get_stored_paths():
    try:
        scroll = client.scroll(
            collection_name=FILES_HASH_COLLECTION,
            limit=1000,
            with_payload=True,
        )
        paths = set()
        for point in scroll[0]:
            p = point.payload.get("path")
            if p:
                paths.add(p)
        return paths
    except Exception:
        return set()


def sync_deleted_files(current_paths):
    stored_paths = get_stored_paths()
    deleted = stored_paths - current_paths
    for path in deleted:
        print(f"Удаляю из индекса: {path}")
        remove_file_points(path)
        client.delete(
            collection_name=FILES_HASH_COLLECTION,
            points_selector=FilterSelector(
                filter=Filter(must=[FieldCondition(key="path", match=MatchValue(value=path))])
            )
        )
        remove_status(path)


def extract_text_from_pdf(file_path):
    reader = PdfReader(file_path)
    text = ""
    for page in reader.pages:
        page_text = page.extract_text()
        if page_text:
            text += page_text + "\n"
    return text


def extract_text_from_docx(file_path):
    doc = Document(file_path)
    return "\n".join([para.text for para in doc.paragraphs])


def extract_text_from_txt(file_path):
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            return f.read()
    except UnicodeDecodeError:
        with open(file_path, 'r', encoding='cp1251') as f:
            return f.read()


def chunk_text(text, chunk_size=CHUNK_SIZE, overlap=CHUNK_OVERLAP):
    words = text.split()
    chunks = []
    current_chunk = []
    current_len = 0
    for word in words:
        current_chunk.append(word)
        current_len += len(word) + 1
        if current_len >= chunk_size:
            chunks.append(" ".join(current_chunk))
            overlap_words = current_chunk[-overlap:] if overlap > 0 else []
            current_chunk = overlap_words
            current_len = sum(len(w) + 1 for w in current_chunk)
    if current_chunk:
        chunks.append(" ".join(current_chunk))
    return chunks


def looks_like_clean_text(text):
    if not text or len(text.strip()) < 20:
        return False
    letters_ru = re.findall(r'[А-Яа-яЁё]', text)
    letters_en = re.findall(r'[A-Za-z]', text)
    total_letters = len(letters_ru) + len(letters_en)
    if total_letters == 0:
        return False
    ru_ratio = len(letters_ru) / total_letters
    if ru_ratio < 0.5:
        return False
    junk_chars = re.findall(r'[^A-Za-zА-Яа-яЁё0-9\s.,;:!?\-()\"\'«»]', text)
    junk_ratio = len(junk_chars) / max(1, len(text))
    if junk_ratio > 0.3:
        return False
    return True


def process_file(file_path, status_update=None):
    ext = os.path.splitext(file_path)[1].lower()
    if ext == '.pdf':
        text = extract_text_from_pdf(file_path)
        if not looks_like_clean_text(text):
            if status_update:
                status_update(progress=0.15, stage="OCR PDF")
            print("  OCR для PDF...")
            text = extract_text_from_scanned_pdf(file_path)
    elif ext == '.docx':
        text = extract_text_from_docx(file_path)
    elif ext == '.txt':
        text = extract_text_from_txt(file_path)
    elif ext in ('.jpg', '.jpeg', '.png'):
        if status_update:
            status_update(progress=0.15, stage="OCR изображения")
        print("  OCR для изображения...")
        text = extract_text_from_image(file_path)
    else:
        return []
    return chunk_text(text)


def ingest_folder(folder_path, uploaded_by=None, progress_callback=None, stop_event=None, resume_file=None):
    """
    Индексирует папку.
    stop_event: threading.Event для остановки.
    resume_file: путь к конкретному файлу для возобновления (только он будет обработан).
    """
    if not acquire_ingest_lock():
        print("[INGEST] Уже идёт другая индексация — выходим", flush=True)
        return 0, 0

    try:
        return _ingest_folder_impl(
            folder_path, uploaded_by, progress_callback, stop_event, resume_file
        )
    finally:
        release_ingest_lock()


def _ingest_folder_impl(folder_path, uploaded_by=None, progress_callback=None, stop_event=None, resume_file=None):
    ensure_collections()
    current_paths = get_current_file_paths(folder_path)
    sync_deleted_files(current_paths)

    all_files = []
    for root, _, files in os.walk(folder_path):
        for file in files:
            ext = os.path.splitext(file)[1].lower()
            if ext in ('.pdf', '.docx', '.txt', '.jpg', '.jpeg', '.png'):
                all_files.append(os.path.join(root, file))

    # Если возобновляем конкретный файл — обрабатываем только его
    if resume_file:
        all_files = [f for f in all_files if f == resume_file]

    total_files = len(all_files)
    processed = 0
    total_chunks = 0

    for idx, full_path in enumerate(all_files):
        if stop_event is not None and stop_event.is_set():
            print("Индексация остановлена пользователем (перед файлом)")
            break
        if os.path.exists("/tmp/rag_stop_requested"):
            print("Индексация остановлена пользователем (флаг перед файлом)")
            break

        try:
            size_bytes = os.path.getsize(full_path)
        except Exception:
            size_bytes = 0

        # Проверяем, есть ли точка возобновления
        status_all = _load_status()
        file_status = status_all.get(full_path, {})
        is_resume = resume_file is not None and file_status.get("status") in ("stopped", "indexing")
        start_from = int(file_status.get("last_chunk_index", 0)) if is_resume else 0

        file_hash = get_file_hash(full_path)
        stored_hash = get_stored_hash(full_path)

        # Если не resume и хеш совпадает — пропускаем
        if not is_resume and stored_hash == file_hash:
            print(f"Пропускаю: {full_path}", flush=True)
            set_file_status(
                full_path,
                status="done",
                progress=1.0,
                stage="без изменений",
                uploaded_by=uploaded_by,
                size_bytes=size_bytes,
                note="без изменений"
            )
            if progress_callback:
                progress_callback(idx + 1, total_files, f"Пропущен: {os.path.basename(full_path)}")
            continue

        if progress_callback:
            progress_callback(idx + 1, total_files, f"Обработка: {os.path.basename(full_path)}")

        # При первом запуске (не resume) — чистим старые точки файла
        # При resume — НЕ чистим, иначе потеряем уже загруженные чанки
        if not is_resume:
            set_file_status(
                full_path,
                status="indexing",
                progress=0.05,
                stage="чтение файла",
                uploaded_by=uploaded_by,
                size_bytes=size_bytes,
                started_at=datetime.now().isoformat(timespec='seconds'),
                last_chunk_index=0,
                total_chunks=0,
            )
            if stored_hash is not None:
                remove_file_points(full_path)

        print(f"Обрабатываю: {full_path}" + (f" (продолжение с {start_from})" if start_from > 0 else ""), flush=True)

        try:
            def _st(**kw):
                set_file_status(full_path, status="indexing", **kw)

            chunks = process_file(full_path, status_update=_st)
            total = len(chunks)
            print(f"  Чанков: {total}", flush=True)

            if not chunks:
                store_file_hash(full_path, file_hash, uploaded_by)
                set_file_status(
                    full_path,
                    status="done",
                    progress=1.0,
                    stage="пусто",
                    chunks=0,
                    last_chunk_index=0,
                    total_chunks=0,
                    note="пусто"
                )
                processed += 1
                continue

            # Векторизуем и заливаем блоками по 100
            UPLOAD_BATCH = 100
            uploaded = start_from

            for batch_start in range(start_from, total, UPLOAD_BATCH):
                if os.path.exists("/tmp/rag_stop_requested"):
                    set_file_status(
                        full_path,
                        status="stopped",
                        progress=batch_start / max(1, total),
                        stage=f"остановлено на {batch_start}/{total}",
                        chunks=batch_start,
                        last_chunk_index=batch_start,
                        total_chunks=total,
                        note=f"остановлено на {batch_start}/{total}"
                    )
                    print(f"Остановлено по флагу на {batch_start}/{total}", flush=True)
                    return processed, total_chunks
                if stop_event is not None and stop_event.is_set():
                    set_file_status(
                        full_path,
                        status="stopped",
                        progress=batch_start / max(1, total),
                        stage=f"остановлено на {batch_start}/{total}",
                        chunks=batch_start,
                        last_chunk_index=batch_start,
                        total_chunks=total,
                        note=f"остановлено на {batch_start}/{total}"
                    )
                    print(f"Остановлено на {batch_start}/{total}", flush=True)
                    return processed, total_chunks

                batch_end = min(batch_start + UPLOAD_BATCH, total)
                batch_texts = chunks[batch_start:batch_end]

                try:
                    embeddings = get_embeddings_batch(
                        batch_texts,
                        on_progress=None,
                        stop_event=stop_event
                    )
                except InterruptedError:
                    set_file_status(
                        full_path,
                        status="stopped",
                        progress=batch_start / max(1, total),
                        stage=f"остановлено на {batch_start}/{total}",
                        chunks=batch_start,
                        last_chunk_index=batch_start,
                        total_chunks=total,
                        note=f"остановлено на {batch_start}/{total}"
                    )
                    print(f"Остановлено внутри векторизации на {batch_start}/{total}", flush=True)
                    return processed, total_chunks

                points = []
                for i, (text, emb) in enumerate(zip(batch_texts, embeddings)):
                    chunk_idx = batch_start + i
                    point_id = hashlib.md5(f"{full_path}_{chunk_idx}".encode()).hexdigest()
                    title = text[:80].replace('\n', ' ') + ('...' if len(text) > 80 else '')
                    points.append(PointStruct(
                        id=point_id,
                        vector=emb.tolist(),
                        payload={
                            "source": full_path,
                            "chunk_index": chunk_idx,
                            "text": text,
                            "chunk_type": "paragraph",
                            "title": title,
                            "uploaded_by": uploaded_by
                        }
                    ))

                if points:
                    client.upsert(collection_name=COLLECTION_NAME, points=points)
                    total_chunks += len(points)

                uploaded = batch_end
                pct = 0.05 + 0.90 * (uploaded / max(1, total))
                set_file_status(
                    full_path,
                    status="indexing",
                    progress=min(pct, 0.98),
                    stage=f"векторизация {uploaded}/{total}",
                    chunks=uploaded,
                    last_chunk_index=uploaded,
                    total_chunks=total
                )
                if progress_callback:
                    try:
                        progress_callback(idx + 1, total_files, f"{os.path.basename(full_path)} {uploaded}/{total}")
                    except Exception:
                        pass

            # Всё залито
            store_file_hash(full_path, file_hash, uploaded_by)
            set_file_status(
                full_path,
                status="done",
                progress=1.0,
                stage="готово",
                chunks=total,
                last_chunk_index=total,
                total_chunks=total
            )
            processed += 1

        except InterruptedError:
            print(f"Остановлено на: {full_path}")
            set_file_status(
                full_path,
                status="stopped",
                stage="остановлено",
                note="пользователь остановил"
            )
            return processed, total_chunks
        except Exception as e:
            print(f"ОШИБКА на {full_path}: {e}", flush=True)
            set_file_status(
                full_path,
                status="error",
                progress=0.0,
                stage="ошибка",
                error=str(e)[:300]
            )
            continue

    print(f"\nОбработано файлов: {processed}, чанков: {total_chunks}")
    return processed, total_chunks



if __name__ == "__main__":
    import sys
    if len(sys.argv) != 2:
        print("Использование: python ingest.py <путь>")
        sys.exit(1)
    ingest_folder(sys.argv[1])
