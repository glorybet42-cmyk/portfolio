import sqlite3
import json
import os
from datetime import datetime

DB_PATH = os.path.join(os.path.dirname(__file__), "history.db")

def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS queries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            timestamp TEXT NOT NULL,
            question TEXT NOT NULL,
            answer TEXT NOT NULL,
            sources TEXT
        )
    """)
    conn.commit()
    conn.close()

def save_query(user_id, question, answer, sources=None):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        "INSERT INTO queries (user_id, timestamp, question, answer, sources) VALUES (?, ?, ?, ?, ?)",
        (user_id, datetime.now().isoformat(timespec='seconds'), question, answer,
         json.dumps(sources or [], ensure_ascii=False))
    )
    conn.commit()
    conn.close()

def get_history(user_id=None, limit=100):
    """Если user_id=None — вернуть всю историю (для админа)."""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    if user_id is not None:
        c.execute(
            "SELECT id, user_id, timestamp, question, answer, sources FROM queries WHERE user_id = ? ORDER BY id DESC LIMIT ?",
            (user_id, limit)
        )
    else:
        c.execute(
            "SELECT id, user_id, timestamp, question, answer, sources FROM queries ORDER BY id DESC LIMIT ?",
            (limit,)
        )
    rows = c.fetchall()
    conn.close()
    return [
        {"id": r[0], "user_id": r[1], "timestamp": r[2], "question": r[3],
         "answer": r[4], "sources": json.loads(r[5]) if r[5] else []}
        for r in rows
    ]

def clear_history(user_id=None):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    if user_id is not None:
        c.execute("DELETE FROM queries WHERE user_id = ?", (user_id,))
    else:
        c.execute("DELETE FROM queries")
    conn.commit()
    conn.close()

def count_history(user_id=None):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    if user_id is not None:
        c.execute("SELECT COUNT(*) FROM queries WHERE user_id = ?", (user_id,))
    else:
        c.execute("SELECT COUNT(*) FROM queries")
    count = c.fetchone()[0]
    conn.close()
    return count

def get_history_paginated(user_id, page: int = 1, page_size: int = 50):
    """
    Возвращает одну страницу истории пользователя.
    page: 1-based номер страницы.
    page_size: сколько записей на странице.
    """
    if page < 1:
        page = 1
    if page_size < 1:
        page_size = 50
    offset = (page - 1) * page_size
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        "SELECT id, user_id, timestamp, question, answer, sources "
        "FROM queries WHERE user_id = ? ORDER BY id DESC LIMIT ? OFFSET ?",
        (user_id, page_size, offset)
    )
    rows = c.fetchall()
    conn.close()
    return [
        {"id": r[0], "user_id": r[1], "timestamp": r[2], "question": r[3],
         "answer": r[4], "sources": json.loads(r[5]) if r[5] else []}
        for r in rows
    ]


init_db()