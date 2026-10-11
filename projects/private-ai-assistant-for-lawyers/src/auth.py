import os
import hashlib
import sqlite3
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()

DB_PATH = os.path.join(os.path.dirname(__file__), "history.db")

ADMIN_LOGIN = os.getenv("ADMIN_LOGIN", "admin")
ADMIN_PASSWORD_HASH = os.getenv("ADMIN_PASSWORD_HASH", "")


def hash_password(password: str) -> str:
    return hashlib.sha256(password.encode()).hexdigest()


def _conn():
    return sqlite3.connect(DB_PATH)


def init_users_table():
    conn = _conn()
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            role TEXT NOT NULL DEFAULT 'user',
            created_at TEXT NOT NULL
        )
    """)
    conn.commit()
    c.execute("SELECT id FROM users WHERE username = ?", (ADMIN_LOGIN,))
    if not c.fetchone() and ADMIN_PASSWORD_HASH:
        c.execute(
            "INSERT INTO users (username, password_hash, role, created_at) VALUES (?, ?, 'admin', ?)",
            (ADMIN_LOGIN, ADMIN_PASSWORD_HASH, datetime.now().isoformat(timespec='seconds'))
        )
        conn.commit()
    conn.close()


def verify_credentials(login: str, password: str):
    conn = _conn()
    c = conn.cursor()
    c.execute("SELECT id, username, password_hash, role FROM users WHERE username = ?", (login,))
    row = c.fetchone()
    conn.close()
    if not row:
        return None
    if hash_password(password) != row[2]:
        return None
    return {"id": row[0], "username": row[1], "role": row[3]}


def create_user(username: str, password: str, role: str = "user"):
    if not username or len(username) < 3:
        return False, "Логин должен быть не короче 3 символов"
    if not password or len(password) < 4:
        return False, "Пароль должен быть не короче 4 символов"
    conn = _conn()
    c = conn.cursor()
    try:
        c.execute(
            "INSERT INTO users (username, password_hash, role, created_at) VALUES (?, ?, ?, ?)",
            (username, hash_password(password), role, datetime.now().isoformat(timespec='seconds'))
        )
        conn.commit()
        return True, None
    except sqlite3.IntegrityError:
        return False, "Пользователь с таким логином уже существует"
    finally:
        conn.close()


def list_users():
    conn = _conn()
    c = conn.cursor()
    c.execute("SELECT id, username, role, created_at FROM users ORDER BY id")
    rows = c.fetchall()
    conn.close()
    return [{"id": r[0], "username": r[1], "role": r[2], "created_at": r[3]} for r in rows]


def delete_user(user_id: int):
    conn = _conn()
    c = conn.cursor()
    c.execute("DELETE FROM users WHERE id = ? AND role != 'admin'", (user_id,))
    conn.commit()
    conn.close()


def change_password(user_id: int, new_password: str):
    conn = _conn()
    c = conn.cursor()
    c.execute("UPDATE users SET password_hash = ? WHERE id = ?",
              (hash_password(new_password), user_id))
    conn.commit()
    conn.close()


def get_user_by_login(login: str):
    conn = _conn()
    c = conn.cursor()
    c.execute("SELECT id, username, role, created_at FROM users WHERE username = ?", (login,))
    row = c.fetchone()
    conn.close()
    if row:
        return {"id": row[0], "username": row[1], "role": row[2], "created_at": row[3]}
    return None


init_users_table()