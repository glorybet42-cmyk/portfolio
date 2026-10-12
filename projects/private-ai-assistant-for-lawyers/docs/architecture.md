# 🏗 Архитектура

## Общая схема

**Поток данных:**

1. **Пользователь** → Nginx (HTTPS 443)
2. **Nginx** → Streamlit (127.0.0.1:8501)
3. **Streamlit** → BGE-M3 (векторизация)
4. **Streamlit** → BGE-reranker-v2-m3 (пересортировка)
5. **Streamlit** → Ollama qwen2.5:14b (генерация)
6. **BGE-M3 / reranker** → Qdrant (хранение и поиск векторов)

**Компоненты:**

| Компонент | Роль | Порт |
|---|---|---|
| Streamlit | UI + RAG-логика | 8501 |
| Ollama | LLM-сервер (qwen2.5:14b) | 11434 |
| Qdrant | Векторная БД | 6333 |
| Nginx | Reverse proxy + SSL | 80, 443 |
| BGE-M3 | Эмбеддинги | — |
| BGE-reranker-v2-m3 | Реранкинг | — |
| Tesseract + EasyOCR | OCR | — |

## RAG-пайплайн

### Индексация
1. Загрузка файла (PDF, DOCX, TXT, JPG, PNG)
2. Извлечение текста (pypdf / python-docx / Tesseract OCR)
3. Чанкинг (~1500 символов, перекрытие 150)
4. Извлечение номеров статей → payload `article_nums`
5. Векторизация BGE-M3 → 1024-dim
6. Загрузка в Qdrant (батчи по 100)

### Поиск
1. Векторизация вопроса
2. ARTICLE-FILTER (если «статья N»)
3. Векторный поиск топ-20
4. Реранкинг BGE-reranker-v2-m3 → топ-5
5. Генерация (qwen2.5:14b через Ollama)
6. Постпроверка (anti-hallucination)
7. Извлечение перечня для «что относится к...»

---

## Обоснование выбора стека

### Qdrant
- Payload-фильтры нативно (`Filter(must=[FieldCondition(...)])`)
- REST API + Python SDK
- Docker-first, открытый исходник (Apache 2.0)
- Индексы по payload (`create_payload_index`)

### BGE-M3
- SOTA для многоязычных (ru + en)
- 1024-dim dense-вектора
- Работает на CPU
- Бесплатно с HuggingFace

### BGE-reranker-v2-m3
- Кросс-энкодер — оценивает пару «вопрос-документ» целиком
- Отлично на русском
- +30% к точности (статья 155 → топ-1)
- 20 кандидатов за 1–2 секунды на CPU

### Ollama + qwen2.5:14b
- 9 ГБ, отличный русский
- 30–60 сек на CPU, 2–5 сек на GPU
- Квантизация Q4

### Streamlit
- UI на Python
- Real-time обновления через `st.fragment(run_every=2)`
- Встроенные виджеты

### Nginx
- Reverse proxy, SSL termination
- Проксирование WebSocket для Streamlit

---

## Структура данных в Qdrant

### Коллекция `legal_docs`

- **id** — `md5(filename)_chunkindex`
- **vector** — 1024-dim (BGE-M3)
- **payload:**
  - `source` — путь к файлу
  - `chunk_index` — номер чанка
  - `text` — полный текст
  - `title` — первые 80 символов
  - `uploaded_by` — ID пользователя
  - `article_nums` — массив номеров статей `[155, 156, 157]`

**Индексы по payload:**
- `article_nums` (INTEGER) — ARTICLE-FILTER
- `uploaded_by` (INTEGER) — «только мои документы»
- `source` (KEYWORD) — «выбрать вручную»

### Коллекция `file_hashes`

- **id** — `md5(file_path)`
- **payload:** hash, path, uploaded_by

Используется для пропуска уже проиндексированных файлов.

---

## Безопасность

### Изоляция пользователей
1. Qdrant payload-фильтры по `uploaded_by`
2. Файловая система: `user_documents/{user_id}/`
3. Админские эндпоинты: проверка `user['role'] == 'admin'`

### Cookie-сессия
- Токен `uid:exp.sig` (HMAC-SHA256)
- TTL 30 дней
- Cookie `rag_auth` + URL `?auth=`

### HTTPS
- Let's Encrypt на `ai.` и `admin.`
- Автопродление через certbot.timer

---

## Производительность

| Компонент | Время |
|---|---|
| Векторизация BGE-M3 (CPU) | 80–100 чанков/мин |
| Реранкинг (20 пар) | 1–2 сек |
| Генерация qwen2.5:14b (CPU) | 30–60 сек |
| Поиск в Qdrant | <100 мс |
| OCR Tesseract | 3–10 сек/страница |

### Как ускорить
1. **GPU** — 30× индексация, 10–15× генерация
2. **CHUNK_SIZE 3000** — вдвое меньше чанков
3. **Лёгкие эмбеддинги** — bge-small (10× быстрее)
4. **Батчи 16–32** — при достатке RAM

---

## Масштабирование

Текущая конфигурация (12 CPU, 32 GB RAM) — **5–20 одновременных пользователей**.

Для роста:
- Streamlit → несколько инстансов за Nginx
- Qdrant → кластер из 3 нод
- Ollama → отдельный GPU-сервер

---

## Мониторинг

```bash
htop                          # CPU, RAM
df -h                         # Диск
docker stats                  # Контейнеры
journalctl -u streamlit -f    # Логи
curl http://localhost:6333/collections/legal_docs
Метрика	Норма	Тревога
RAM	< 60%	> 85%
CPU load	< 2.0	> 8.0
Диск	< 60%	> 85%
Известные проблемы
Проблема	Решение
400 Bad Request от Qdrant	Батчи по 100 точек
Модель выдумывает	anti-math + anti-fake-article
Streamlit выкидывает	Cookie + URL-токен
OCR не работает	shutil.which("tesseract")
SSH рвётся	Отключить fail2ban
Медленная индексация	GPU или chunk_size больше
Заключение
Простая, но масштабируемая архитектура:

Monolith (Streamlit + RAG)

Docker для Qdrant + Ollama

Гибридный поиск (вектор + реранкинг + payload)

On-premise

Готов к продакшену для команд 5–20 человек.
