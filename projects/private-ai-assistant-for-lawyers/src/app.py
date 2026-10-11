import streamlit as st
import ollama
import os
import re
import time
import threading
import base64
from pathlib import Path
from qdrant_client import QdrantClient
from qdrant_client.models import Filter, FieldCondition, MatchValue, FilterSelector
from history import save_query, get_history, count_history, clear_history, get_history_paginated
from auth import (
    verify_credentials, create_user, list_users, delete_user,
    change_password, get_user_by_login,
)
from ingest import ingest_folder, get_embedder, get_status, remove_status, set_file_status
from reranker import rerank
import hmac
import hashlib
import extra_streamlit_components as stx
from datetime import datetime, timedelta

QDRANT_URL = "http://localhost:6333"
COLLECTION_NAME = "legal_docs"
FILES_HASH_COLLECTION = "file_hashes"
LLM_MODEL = 'qwen2.5:14b'
SEARCH_LIMIT = 5
TEMPERATURE = 0.0
MAX_LENGTH = 512

USER_DOCS_ROOT = os.path.join(os.path.dirname(__file__), "user_documents")
os.makedirs(USER_DOCS_ROOT, exist_ok=True)

STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")
ICONS_DIR = os.path.join(STATIC_DIR, "icons")


# ============ ЗАГРУЗКА ИКОНОК ============
@st.cache_data
def load_icon(name: str) -> str:
    for ext, mime in (("png", "image/png"), ("svg", "image/svg+xml")):
        p = Path(ICONS_DIR) / f"{name}.{ext}"
        if p.exists():
            b64 = base64.b64encode(p.read_bytes()).decode()
            return f"data:{mime};base64,{b64}"
    return ""


@st.cache_data
def load_logo() -> str:
    p = Path(STATIC_DIR) / "logo.png"
    if p.exists():
        b64 = base64.b64encode(p.read_bytes()).decode()
        return f"data:image/png;base64,{b64}"
    return ""


LOGO = load_logo()
ICON_FOLDER = load_icon("folder")
ICON_CHART = load_icon("chart")
ICON_TARGET = load_icon("target")
ICON_LIGHTBULB = load_icon("lightbulb")
ICON_UPLOAD = load_icon("upload")
ICON_INDEX = load_icon("index")
ICON_SEARCH = load_icon("search")
ICON_NOCLOUD = load_icon("no-cloud")

st.set_page_config(
    page_title="ИИ-ассистент для юристов и бухгалтеров",
    page_icon=LOGO if LOGO else "📚",
    layout="wide",
)

try:
    host_header = st.context.headers.get("Host", "") or ""
except Exception:
    host_header = ""

qp = st.query_params
_admin_qp = str(qp.get("admin", "")).lower() in ("1", "true", "yes")
is_admin_domain = ("admin" in host_header.lower()) or _admin_qp


def avatar_color(name: str) -> str:
    palette = [
        "#3b82f6", "#10b981", "#f59e0b", "#ef4444",
        "#8b5cf6", "#06b6d4", "#ec4899", "#84cc16",
    ]
    h = sum(ord(c) for c in name)
    return palette[h % len(palette)]


def _secret():
    return os.getenv("SECRET_KEY", "rag-default-secret-change-me")


def make_auth_token(user_id: int, ttl_days: int = 30) -> str:
    import time as _t
    exp = int(_t.time()) + ttl_days * 86400
    payload = f"{user_id}:{exp}"
    sig = hmac.new(_secret().encode(), payload.encode(), hashlib.sha256).hexdigest()[:24]
    return f"{payload}.{sig}"


def verify_auth_token(token: str):
    try:
        import time as _t
        payload, sig = token.rsplit(".", 1)
        expected = hmac.new(_secret().encode(), payload.encode(), hashlib.sha256).hexdigest()[:24]
        if not hmac.compare_digest(sig, expected):
            return None
        uid_s, exp_s = payload.split(":")
        if int(exp_s) < int(_t.time()):
            return None
        return int(uid_s)
    except Exception:
        return None
    return None


cookie_manager = stx.CookieManager(key="rag_cookie_manager")


def set_auth_cookie(user_id: int):
    token = make_auth_token(user_id)
    try:
        cookie_manager.set(
            "rag_auth",
            token,
            expires_at=datetime.now() + timedelta(days=30),
            key="set_rag_auth",
        )
    except Exception as e:
        print(f"cookie set error: {e}")


def clear_auth_cookie():
    try:
        cookie_manager.delete("rag_auth", key="del_rag_auth")
    except Exception as e:
        print(f"cookie del error: {e}")


def read_auth_cookie():
    try:
        token = cookie_manager.get("rag_auth")
        if token:
            return verify_auth_token(token)
    except Exception:
        pass
    return None


def md_to_html(text: str) -> str:
    """Конвертирует базовый markdown в HTML для отображения внутри div."""
    import re as _re
    import html as _html
    # Экранируем HTML-спецсимволы
    text = _html.escape(text)
    # Bold
    text = _re.sub(r'\*\*(.+?)\*\*', r'<strong>\1</strong>', text)
    # Списки "– xxx" в начале строки
    lines = text.split('\n')
    out = []
    for line in lines:
        if line.strip().startswith('–') or line.strip().startswith('- '):
            out.append('&nbsp;&nbsp;' + line)
        else:
            out.append(line)
    text = '<br>'.join(out)
    return text


def icon_img(data_uri: str, size: int = 22, margin: int = 8) -> str:
    if not data_uri:
        return ""
    return f'<img src="{data_uri}" style="width:{size}px;height:{size}px;vertical-align:middle;margin-right:{margin}px;" alt=""/>'


# ============ СТИЛИ ============
st.markdown("""
<style>
[data-testid="stSidebarNav"] {display: none !important;}
section[data-testid="stSidebarNav"] {display: none !important;}
.stApp { background: #f5f8fb; }

.hero {
    background: linear-gradient(135deg, #e8f2fc 0%, #dbeafe 100%);
    color: #1e3a5f;
    padding: 36px 32px;
    border-radius: 18px;
    margin-bottom: 32px;
    border: 1px solid #cfe2f3;
    text-align: center;
}
.hero h1 {
    font-size: 30px; font-weight: 800; margin: 0 0 12px 0;
    color: #1e3a5f; text-align: center;
    display: flex; align-items: center; justify-content: center; gap: 14px;
}
.hero h1 img {
    width: 56px; height: 56px; border-radius: 14px;
    box-shadow: 0 4px 12px rgba(30,58,95,0.12);
}
.hero p {
    font-size: 16px; line-height: 1.6; margin: 0 auto;
    color: #475569; max-width: 720px; text-align: center;
}
.hero .badges {
    margin-top: 24px;
    display: flex; gap: 14px; flex-wrap: wrap;
    justify-content: center;
}
.hero .badge {
    background: white;
    padding: 12px 22px;
    border-radius: 24px;
    font-size: 15px; font-weight: 600;
    color: #1e3a5f;
    border: 1.5px solid #cfe2f3;
    box-shadow: 0 2px 6px rgba(30,58,95,0.06);
    display: inline-flex; align-items: center; gap: 6px;
}
.hero .badge img {
    width: 22px; height: 22px;
}

.section-title {
    display: flex; align-items: center; gap: 10px;
    color: #1e3a5f; font-weight: 700;
    font-size: 20px; margin: 12px 0;
}
.section-title img {
    width: 26px; height: 26px;
}

.sidebar-section {
    display: flex; align-items: center; gap: 8px;
    color: #1e3a5f; font-weight: 700;
    font-size: 15px; margin: 10px 0 8px 0;
}
.sidebar-section img { width: 22px; height: 22px; }

.login-card {
    max-width: 460px; margin: 40px auto 0; background: white;
    padding: 36px 32px 16px 32px; border-radius: 18px;
    box-shadow: 0 8px 32px rgba(30,58,95,0.08); text-align: center;
}
.login-card h1 { color: #1e3a5f; font-size: 24px; margin-bottom: 8px; }
.login-card p { color: #64748b; margin-bottom: 0; }

/* ============ ВЫРАВНИВАНИЕ INPUT И КНОПОК ============ */



[data-testid="stMain"] .stButton > button {
    width: 100%;
    height: 52px !important;
    min-height: 52px !important;
    max-height: 52px !important;
    padding: 0 !important;
    line-height: 52px !important;
    background-color: #ffffff; color: #1e3a5f;
    border: 1.5px solid #d6e4f0; border-radius: 12px;
    font-weight: 700; font-size: 20px;
    transition: all 0.15s;
    box-shadow: 0 1px 4px rgba(30,58,95,0.04);
}
[data-testid="stMain"] .stButton > button:hover {
    background-color: #e8f2fc; border-color: #93c5fd;
    box-shadow: 0 6px 16px rgba(30,58,95,0.1);
}



h3 { color: #1e3a5f !important; font-weight: 700 !important; }

/* ============ КНОПКИ В САЙДБАРЕ ============ */

/* Убираем лишний padding у обёртки кнопок */
[data-testid="stSidebar"] [data-testid="stHorizontalBlock"] [data-testid="stButton"] {
    padding: 0 !important;
    margin: 0 !important;
}

/* ВСЕ кнопки в сайдбаре — 40px высоты */
[data-testid="stSidebar"] [data-testid="stHorizontalBlock"] button {
    height: 40px !important;
    min-height: 40px !important;
    max-height: 40px !important;
    padding: 0 !important;
    margin: 0 !important;
    border-radius: 10px !important;
    box-sizing: border-box !important;
    overflow: hidden !important;
    line-height: 1 !important;
}
[data-testid="stSidebar"] [data-testid="stHorizontalBlock"] button > div {
    height: 40px !important;
    min-height: 40px !important;
    max-height: 40px !important;
    padding: 0 !important;
    margin: 0 !important;
    display: flex !important;
    align-items: center !important;
    justify-content: center !important;
    line-height: 1 !important;
}
[data-testid="stSidebar"] [data-testid="stHorizontalBlock"] button p,
[data-testid="stSidebar"] [data-testid="stHorizontalBlock"] button span {
    font-size: 20px !important;
    line-height: 1 !important;
    padding: 0 !important;
    margin: 0 !important;
}

/* КНОПКА ▶ — жёлтая, 40x40 */




/* КНОПКА ⏹ — красная */



/* ============ КНОПКИ ПО ARIA-LABEL (НАДЁЖНО) ============ */

/* Кнопка "Индексировать" */




/* Кнопка "Остановить индексацию" — красная */





/* ============ КНОПКА "ИНДЕКСИРОВАТЬ" ============ */
[data-testid="stSidebar"] .st-key-idx_button_wrap button {
    background: #ffffff !important;
    background-color: #ffffff !important;
    background-image: none !important;
    color: #1e3a5f !important;
    border: 1.5px solid #d6e4f0 !important;
    border-radius: 12px !important;
    height: 52px !important;
    min-height: 52px !important;
    max-height: 52px !important;
    padding: 0 !important;
    margin: 0 !important;
    font-size: 16px !important;
    font-weight: 600 !important;
    box-shadow: 0 1px 4px rgba(30,58,95,0.04) !important;
    transition: all 0.15s !important;
}
[data-testid="stSidebar"] .st-key-idx_button_wrap button p {
    font-size: 16px !important;
    font-weight: 600 !important;
    color: #1e3a5f !important;
    margin: 0 !important;
    padding: 0 !important;
}
[data-testid="stSidebar"] .st-key-idx_button_wrap button:hover {
    background: #e8f2fc !important;
    background-color: #e8f2fc !important;
    border-color: #93c5fd !important;
    box-shadow: 0 6px 16px rgba(30,58,95,0.1) !important;
}

/* ============ КНОПКА "ОСТАНОВИТЬ ИНДЕКСАЦИЮ" — красная ============ */
[data-testid="stSidebar"] .st-key-stop_index_wrap button {
    background: #dc2626 !important;
    background-color: #dc2626 !important;
    background-image: none !important;
    color: #ffffff !important;
    border: none !important;
    border-radius: 12px !important;
    height: 48px !important;
    min-height: 48px !important;
    max-height: 48px !important;
    padding: 0 !important;
    margin: 0 !important;
    font-size: 15px !important;
    font-weight: 700 !important;
    box-shadow: 0 4px 12px rgba(220,38,38,0.45) !important;
    transition: all 0.15s !important;
}
[data-testid="stSidebar"] .st-key-stop_index_wrap button p {
    font-size: 15px !important;
    font-weight: 700 !important;
    color: #ffffff !important;
    margin: 0 !important;
    padding: 0 !important;
}
[data-testid="stSidebar"] .st-key-stop_index_wrap button:hover {
    background: #b91c1c !important;
    background-color: #b91c1c !important;
    box-shadow: 0 6px 16px rgba(220,38,38,0.65) !important;
}



/* ============ ТОЧНЫЕ СЕЛЕКТОРЫ (по key контейнера) ============ */

/* Кнопка "Индексировать" — белая */
[data-testid="stSidebar"] [class*="st-key-idx_button_wrap"] button {
    background: #ffffff !important;
    background-color: #ffffff !important;
    background-image: none !important;
    color: #1e3a5f !important;
    border: 1.5px solid #d6e4f0 !important;
    border-radius: 12px !important;
    height: 52px !important;
    min-height: 52px !important;
    max-height: 52px !important;
    padding: 0 !important;
    font-size: 16px !important;
    font-weight: 600 !important;
    box-shadow: 0 1px 4px rgba(30,58,95,0.04) !important;
}
[data-testid="stSidebar"] [class*="st-key-idx_button_wrap"] button p {
    font-size: 16px !important;
    font-weight: 600 !important;
    color: #1e3a5f !important;
    margin: 0 !important;
}
[data-testid="stSidebar"] [class*="st-key-idx_button_wrap"] button:hover {
    background: #e8f2fc !important;
    background-color: #e8f2fc !important;
    border-color: #93c5fd !important;
}

/* Кнопка "Остановить индексацию" — КРАСНАЯ */
[data-testid="stSidebar"] [class*="st-key-stop_index_wrap"] button {
    background: #dc2626 !important;
    background-color: #dc2626 !important;
    background-image: none !important;
    color: #ffffff !important;
    border: none !important;
    border-radius: 12px !important;
    height: 48px !important;
    min-height: 48px !important;
    max-height: 48px !important;
    padding: 0 !important;
    font-size: 15px !important;
    font-weight: 700 !important;
    box-shadow: 0 4px 12px rgba(220,38,38,0.5) !important;
}
[data-testid="stSidebar"] [class*="st-key-stop_index_wrap"] button p {
    color: #ffffff !important;
    font-size: 15px !important;
    font-weight: 700 !important;
    margin: 0 !important;
}
[data-testid="stSidebar"] [class*="st-key-stop_index_wrap"] button:hover {
    background: #b91c1c !important;
    background-color: #b91c1c !important;
}

/* Кнопка ▶ Продолжить — ЖЁЛТАЯ */
[data-testid="stSidebar"] [class*="st-key-resume_"] button {
    background: #f59e0b !important;
    background-color: #f59e0b !important;
    background-image: none !important;
    color: #ffffff !important;
    border: none !important;
    width: 40px !important;
    min-width: 40px !important;
    max-width: 40px !important;
    height: 40px !important;
    min-height: 40px !important;
    max-height: 40px !important;
    padding: 0 !important;
    border-radius: 10px !important;
    box-shadow: 0 3px 8px rgba(245,158,11,0.4) !important;
}
[data-testid="stSidebar"] [class*="st-key-resume_"] button p {
    color: #ffffff !important;
    font-size: 20px !important;
    font-weight: 900 !important;
}
[data-testid="stSidebar"] [class*="st-key-resume_"] button:hover {
    background: #d97706 !important;
    background-color: #d97706 !important;
}


/* ============ КНОПКИ ИСТОРИИ (под st-key-history_btns_wrap) ============ */

/* Левая кнопка "Вся история" — как "Индексировать" */




/* Правая кнопка-корзина — квадратная, 52px */




/* ============ КНОПКИ ИСТОРИИ (обе БЕЛЫЕ, как "Индексировать") ============ */

/* Кнопка "Вся история" — БЕЛАЯ */



/* Кнопка "Очистить историю" — тоже БЕЛАЯ (не красная) */




/* ============ КНОПКИ ИСТОРИИ (по реальным классам) ============ */

/* Кнопка "Вся история" — БЕЛАЯ, шрифт как у Индексировать */




/* Кнопка "Очистить историю" — БЕЛАЯ */





/* ============ КНОПКИ ИСТОРИИ — как "Индексировать" ============ */

/* Кнопка "Вся история" */




/* Кнопка "Очистить историю" */





/* ============ КНОПКИ ИСТОРИИ С ИКОНКАМИ ============ */

/* Кнопка "Вся история" */
[data-testid="stSidebar"] .st-key-btn_show_all_history button {
    background: #ffffff !important;
    background-color: #ffffff !important;
    background-image: none !important;
    color: #1e3a5f !important;
    border: 1.5px solid #d6e4f0 !important;
    border-radius: 12px !important;
    height: 52px !important;
    min-height: 52px !important;
    max-height: 52px !important;
    padding: 0 12px !important;
    box-shadow: 0 1px 4px rgba(30,58,95,0.04) !important;
    position: relative !important;
}
[data-testid="stSidebar"] .st-key-btn_show_all_history button > div {
    font-size: 16px !important;
    font-weight: 600 !important;
    color: #1e3a5f !important;
    line-height: 1 !important;
    margin: 0 !important;
    padding-left: 26px !important;
    position: relative !important;
}
[data-testid="stSidebar"] .st-key-btn_show_all_history button > div::before {
    content: "📜";
    position: absolute;
    left: 0;
    top: 50%;
    transform: translateY(-50%);
    font-size: 18px;
    line-height: 1;
}
[data-testid="stSidebar"] .st-key-btn_show_all_history button:hover {
    background: #e8f2fc !important;
    border-color: #93c5fd !important;
}

/* Кнопка "Очистить историю" */
[data-testid="stSidebar"] .st-key-btn_clear_history_short button {
    background: #ffffff !important;
    background-color: #ffffff !important;
    background-image: none !important;
    color: #1e3a5f !important;
    border: 1.5px solid #d6e4f0 !important;
    border-radius: 12px !important;
    height: 52px !important;
    min-height: 52px !important;
    max-height: 52px !important;
    padding: 0 12px !important;
    box-shadow: 0 1px 4px rgba(30,58,95,0.04) !important;
    position: relative !important;
}
[data-testid="stSidebar"] .st-key-btn_clear_history_short button > div {
    font-size: 16px !important;
    font-weight: 600 !important;
    color: #1e3a5f !important;
    line-height: 1 !important;
    margin: 0 !important;
    padding-left: 26px !important;
    position: relative !important;
}
[data-testid="stSidebar"] .st-key-btn_clear_history_short button > div::before {
    content: "🗑️";
    position: absolute;
    left: 0;
    top: 50%;
    transform: translateY(-50%);
    font-size: 18px;
    line-height: 1;
}
[data-testid="stSidebar"] .st-key-btn_clear_history_short button:hover {
    background: #e8f2fc !important;
    border-color: #93c5fd !important;
}


/* ============ ЗЕЛЁНАЯ КНОПКА ЗАПУСКА ПОИСКА ============ */





/* Кнопка ✖ остановки поиска — КРАСНАЯ */





/* ============ ВЫРАВНИВАНИЕ INPUT И КНОПКИ ▶ ============ */

/* Поле ввода — 52px */



/* Кнопка ▶ — тоже 52px, точно */
.st-key-btn_go button,
.st-key-btn_stop button {
    background: #10b981 !important;
    background-color: #10b981 !important;
    background-image: linear-gradient(135deg, #10b981 0%, #059669 100%) !important;
    color: #ffffff !important;
    border: none !important;
    border-radius: 12px !important;
    height: 52px !important;
    min-height: 52px !important;
    max-height: 52px !important;
    padding: 0 !important;
    margin: 0 !important;
    box-sizing: border-box !important;
    box-shadow: 0 6px 16px rgba(16,185,129,0.5) !important;
    display: flex !important;
    align-items: center !important;
    justify-content: center !important;
}
.st-key-btn_go button p,
.st-key-btn_go button > div,
.st-key-btn_stop button p,
.st-key-btn_stop button > div {
    color: #ffffff !important;
    font-size: 24px !important;
    font-weight: 900 !important;
    line-height: 1 !important;
    margin: 0 !important;
    padding: 0 !important;
}
.st-key-btn_go button:hover {
    background: #059669 !important;
    background-color: #059669 !important;
    background-image: linear-gradient(135deg, #059669 0%, #047857 100%) !important;
    box-shadow: 0 8px 22px rgba(16,185,129,0.7) !important;
}

/* Кнопка ✖ — красная, тоже 52px */
.st-key-btn_stop button {
    background: #ef4444 !important;
    background-color: #ef4444 !important;
    background-image: linear-gradient(135deg, #ef4444 0%, #dc2626 100%) !important;
    box-shadow: 0 6px 16px rgba(239,68,68,0.5) !important;
}
.st-key-btn_stop button:hover {
    background: #dc2626 !important;
    background-color: #dc2626 !important;
    box-shadow: 0 8px 22px rgba(239,68,68,0.7) !important;
}

/* Убираем лишние отступы у колонки с кнопкой, чтобы выравнивание работало */
[data-testid="stMain"] [data-testid="stHorizontalBlock"] > [data-testid="column"] > div[data-testid="stVerticalBlock"] > div[data-testid="element-container"] {
    padding: 0 !important;
    margin: 0 !important;
}


/* ============ ПОЛЕ ВВОДА ВОПРОСА (безопасный CSS) ============ */

/* Только сам input — не трогаем внутренние div'ы BaseWeb */


/* Внешняя рамка — только border и radius, без height */




/* ============ ПОЛЕ ВВОДА (безопасный line-height) ============ */

/* line-height НЕ ломает клик — padding тоже ок */
[data-testid="stMain"] .stTextInput input {
    line-height: 52px !important;
    height: 52px !important;
    padding: 0 16px !important;
    font-size: 16px !important;
    box-sizing: border-box !important;
    border: none !important;
    background: transparent !important;
}

/* Обёртка BaseWeb — только border + radius, БЕЗ height, БЕЗ position */
[data-testid="stMain"] .stTextInput [data-baseweb="input"],
[data-testid="stMain"] .stTextInput [data-baseweb="base-input"] {
    border-radius: 12px !important;
    border: 1.5px solid #d6e4f0 !important;
    background: white !important;
    min-height: 52px !important;
    display: flex !important;
    align-items: center !important;
}
[data-testid="stMain"] .stTextInput [data-baseweb="input"]:focus-within,
[data-testid="stMain"] .stTextInput [data-baseweb="base-input"]:focus-within {
    border-color: #60a5fa !important;
    box-shadow: 0 0 0 3px rgba(96,165,250,0.15) !important;
}

/* Если input в контейнере с .stTextInputRootElement — тот же стиль */
[data-testid="stMain"] .stTextInputRootElement,
[data-testid="stMain"] div[data-testid="stTextInputRootElement"] {
    min-height: 52px !important;
    display: flex !important;
    align-items: center !important;
    border-radius: 12px !important;
}


/* Кнопка ✖ Остановить поиск в wrap-контейнере */
[data-testid="stMain"] .st-key-btn_stop_wrap button {
    background: linear-gradient(135deg, #ef4444 0%, #dc2626 100%) !important;
    background-color: #ef4444 !important;
    color: #ffffff !important;
    border: none !important;
    border-radius: 12px !important;
    height: 52px !important;
    min-height: 52px !important;
    max-height: 52px !important;
    padding: 0 !important;
    box-shadow: 0 6px 16px rgba(239,68,68,0.5) !important;
    font-size: 24px !important;
    font-weight: 900 !important;
}
[data-testid="stMain"] .st-key-btn_stop_wrap button p,
[data-testid="stMain"] .st-key-btn_stop_wrap button > div {
    color: #ffffff !important;
    font-size: 24px !important;
    font-weight: 900 !important;
    margin: 0 !important;
}
[data-testid="stMain"] .st-key-btn_stop_wrap button:hover {
    background: #dc2626 !important;
    background-color: #dc2626 !important;
}


/* Зелёная кнопка ▶ внутри формы поиска */
[data-testid="stForm"] button[type="submit"],
[data-testid="stForm"] button[kind="secondaryFormSubmit"],
[data-testid="stForm"] button[kind="primaryFormSubmit"],
[data-testid="stForm"] button[data-testid="stBaseButton-secondaryFormSubmit"],
[data-testid="stForm"] button[data-testid="stBaseButton-primaryFormSubmit"] {
    background: #10b981 !important;
    background-color: #10b981 !important;
    background-image: linear-gradient(135deg, #10b981 0%, #059669 100%) !important;
    color: #ffffff !important;
    border: none !important;
    border-radius: 12px !important;
    height: 52px !important;
    min-height: 52px !important;
    max-height: 52px !important;
    padding: 0 !important;
    margin: 0 !important;
    box-shadow: 0 6px 16px rgba(16,185,129,0.5) !important;
    display: flex !important;
    align-items: center !important;
    justify-content: center !important;
}
[data-testid="stForm"] button[type="submit"] p,
[data-testid="stForm"] button[type="submit"] > div,
[data-testid="stForm"] button[kind="secondaryFormSubmit"] p,
[data-testid="stForm"] button[kind="secondaryFormSubmit"] > div {
    color: #ffffff !important;
    font-size: 24px !important;
    font-weight: 900 !important;
    line-height: 1 !important;
    margin: 0 !important;
    padding: 0 !important;
}
[data-testid="stForm"] button[type="submit"]:hover,
[data-testid="stForm"] button[kind="secondaryFormSubmit"]:hover {
    background: #059669 !important;
    background-color: #059669 !important;
    background-image: linear-gradient(135deg, #059669 0%, #047857 100%) !important;
    box-shadow: 0 8px 22px rgba(16,185,129,0.7) !important;
}

</style>
""", unsafe_allow_html=True)


@st.cache_resource
def load_embedder_cached():
    return get_embedder()


embedder = load_embedder_cached()
qdrant = QdrantClient(url=QDRANT_URL)

if 'user' not in st.session_state:
    st.session_state.user = None

# ============ ФЛАГИ ИНДЕКСАЦИИ (должны быть инициализированы ДО сайдбара) ============
if 'ingest_stop_event' not in st.session_state:
    st.session_state.ingest_stop_event = threading.Event()
if 'ingest_running' not in st.session_state:
    st.session_state.ingest_running = False
if 'ingest_thread' not in st.session_state:
    st.session_state.ingest_thread = None

# ============ АВТОСБРОС ВИСЯЩИХ "indexing" — ТОЛЬКО ОДИН РАЗ ЗА ПРОЦЕСС ============
@st.cache_resource
def _startup_reset_indexing():
    """Выполняется один раз при старте процесса Streamlit.
    Сбрасывает все зависшие indexing в stopped (ингест обрублен рестартом)."""
    try:
        status = get_status()
        cnt = 0
        for p, s in list(status.items()):
            if s.get("status") == "indexing":
                last = int(s.get("last_chunk_index", 0) or 0)
                total = int(s.get("total_chunks", 0) or 0)
                set_file_status(
                    p,
                    status="stopped",
                    progress=(last / total) if total else 0.0,
                    stage=f"остановлено на {last}/{total}" if total else "остановлено",
                    chunks=last,
                    last_chunk_index=last,
                    total_chunks=total,
                    note="сессия прервана"
                )
                cnt += 1
        if cnt:
            print(f"[STARTUP-RESET] Сброшено висящих: {cnt}", flush=True)
    except Exception as e:
        print(f"[STARTUP-RESET] Ошибка: {e}", flush=True)
    return True


_startup_reset_indexing()

# ============ ЭКРАН ВХОДА ============
if st.session_state.user is None:
    _auth_token = None
    try:
        _auth_token = st.query_params.get("auth", "")
    except Exception:
        pass

    _auth_uid = None
    if _auth_token:
        _auth_uid = verify_auth_token(_auth_token)
    if not _auth_uid:
        _auth_uid = read_auth_cookie()

    if _auth_uid:
        _users = list_users()
        _u = next((x for x in _users if x["id"] == _auth_uid), None)
        if _u:
            st.session_state.user = _u
            st.rerun()

if st.session_state.user is None:
    mode_label = "Админ-панель" if is_admin_domain else "ИИ-ассистент"
    _logo_html = f'<img src="{LOGO}" style="width:56px;height:56px;vertical-align:middle;margin-right:12px;border-radius:14px;box-shadow:0 4px 12px rgba(30,58,95,0.15);"/>' if LOGO else ""
    st.markdown(f"""
    <div class="login-card">
        <h1 style="display:flex;align-items:center;justify-content:center;">{_logo_html}<span>{mode_label}</span></h1>
        <p>Войдите или зарегистрируйтесь, чтобы продолжить</p>
    </div>
    """, unsafe_allow_html=True)

    tab_login, tab_register = st.tabs(["Вход", "Регистрация"])

    with tab_login:
        with st.form("login_form", clear_on_submit=False):
            login = st.text_input("Логин")
            password = st.text_input("Пароль", type="password")
            submitted = st.form_submit_button("Войти")
            if submitted:
                u = verify_credentials(login, password)
                if not u:
                    st.error("Неверный логин или пароль")
                elif is_admin_domain and u['role'] != 'admin':
                    st.error("На этом домене доступ разрешён только администратору. Обычным пользователям — через основной адрес.")
                elif (not is_admin_domain) and u['role'] == 'admin':
                    st.error("Администратор входит через admin-домен. Используйте адрес admin.ceny24.by")
                else:
                    st.session_state.user = u
                    try:
                        st.query_params["auth"] = make_auth_token(u["id"])
                    except Exception as e:
                        print(f"query_params set error: {e}")
                    set_auth_cookie(u["id"])
                    st.rerun()

        with st.expander("Забыли пароль?"):
            st.info("Для восстановления доступа обратитесь к администратору сервиса.")

    with tab_register:
        with st.form("register_form", clear_on_submit=False):
            reg_login = st.text_input("Имя пользователя")
            reg_pass = st.text_input("Пароль", type="password")
            reg_pass2 = st.text_input("Повторите пароль", type="password")
            reg_submit = st.form_submit_button("Зарегистрироваться")
            if reg_submit:
                if not reg_login or len(reg_login) < 3:
                    st.error("Логин должен быть не короче 3 символов")
                elif not reg_pass or len(reg_pass) < 4:
                    st.error("Пароль должен быть не короче 4 символов")
                elif reg_pass != reg_pass2:
                    st.error("Пароли не совпадают")
                else:
                    ok, err = create_user(reg_login, reg_pass, role='user')
                    if ok:
                        st.success("Регистрация успешна. Теперь войдите на вкладке «Вход».")
                    else:
                        st.error(err)

    st.caption("Регистрируясь, вы соглашаетесь с правилами использования сервиса.")
    st.stop()

user = st.session_state.user
USER_DOCS_DIR = os.path.join(USER_DOCS_ROOT, str(user['id']))
os.makedirs(USER_DOCS_DIR, exist_ok=True)


@st.fragment(run_every=2)
def render_status_panel(docs_dir):
    st.markdown(
        f'<div class="sidebar-section">{icon_img(ICON_CHART)} Статус индексации</div>',
        unsafe_allow_html=True
    )
    status_all = get_status()
    prefix = docs_dir + os.sep
    user_status = {p: s for p, s in status_all.items() if p.startswith(prefix)}

    if not user_status:
        st.caption("Нет данных об индексации.")
        return

    sorted_items = sorted(user_status.items(), key=lambda x: x[1].get("updated_at", ""), reverse=True)

    for path, s in sorted_items:
        name = os.path.basename(path)
        st_val = s.get("status", "?")
        prog = float(s.get("progress", 0.0) or 0.0)
        stage = s.get("stage", "")
        size_mb = (s.get("size_bytes", 0) or 0) / (1024 * 1024)
        upd = s.get("updated_at", "")
        upd_short = upd[11:19] if len(upd) >= 19 else ""

        if st_val == "indexing":
            pct = int(min(max(prog, 0.0), 1.0) * 100)
            st.markdown(f"⏳ **{name}** — {pct}%")
            st.progress(min(max(prog, 0.0), 1.0), text=stage or "индексация")
            if size_mb:
                st.caption(f"{size_mb:.1f} МБ · {upd_short}")
        elif st_val == "done":
            chunks = s.get("chunks", 0)
            st.markdown(f"✅ **{name}**")
            meta = f"{chunks} чанков"
            if size_mb:
                meta += f" · {size_mb:.1f} МБ"
            if upd_short:
                meta += f" · {upd_short}"
            if s.get("note"):
                meta += f" · {s['note']}"
            st.caption(meta)
        elif st_val == "error":
            st.markdown(f"❌ **{name}**")
            st.caption((s.get("error") or "")[:200])


with st.sidebar:
    _emoji = "🧑‍💼" if user['role'] == 'admin' else "🧑"
    _role_label = "Администратор" if user['role'] == 'admin' else "Пользователь"
    _badge_color = "#dc2626" if user['role'] == 'admin' else "#64748b"
    _avc = avatar_color(user['username'])

    st.markdown(f"""
    <div style="display:flex;align-items:center;gap:12px;padding:6px 0 12px 0;">
        <div style="width:54px;height:54px;border-radius:50%;background:{_avc};display:flex;align-items:center;justify-content:center;font-size:26px;line-height:1;box-shadow:0 3px 10px rgba(0,0,0,0.12);">{_emoji}</div>
        <div style="line-height:1.2;">
            <div style="font-weight:700;color:#1e3a5f;font-size:16px;">{user['username']}</div>
            <div style="display:inline-block;margin-top:3px;background:{_badge_color};color:white;font-size:10px;font-weight:600;padding:2px 8px;border-radius:10px;letter-spacing:0.5px;text-transform:uppercase;">{_role_label}</div>
        </div>
    </div>
    """, unsafe_allow_html=True)

    if user['role'] == 'admin':
        with st.expander("👥 Управление пользователями"):
            users_list = list_users()
            st.caption(f"Всего пользователей: {len(users_list)}")
            for u in users_list:
                st.markdown(f"**{u['username']}** · _{u['role']}_ · {u['created_at'][:10] if u['created_at'] else ''}")
            st.markdown("---")
            st.markdown("**🔑 Сменить пароль**")
            pwd_targets = [u['username'] for u in users_list]
            pwd_target = st.selectbox("Пользователь", pwd_targets, key="admin_pwd_target")
            new_pwd = st.text_input("Новый пароль", type="password", key="admin_new_pwd")
            if st.button("Сменить пароль", key="admin_pwd_btn", use_container_width=True):
                if not new_pwd or len(new_pwd) < 4:
                    st.error("Пароль должен быть не короче 4 символов")
                else:
                    t = next((u for u in users_list if u['username'] == pwd_target), None)
                    if t:
                        change_password(t['id'], new_pwd)
                        st.success(f"Пароль для «{pwd_target}» изменён")

            st.markdown("---")
            st.markdown("**🗑️ Удалить пользователя**")
            deletable = [u['username'] for u in users_list if u['role'] != 'admin']
            if deletable:
                del_target = st.selectbox("Кого удалить", deletable, key="admin_del_target")
                if st.button("Удалить пользователя", key="admin_del_btn", use_container_width=True):
                    t = next((u for u in users_list if u['username'] == del_target), None)
                    if t:
                        delete_user(t['id'])
                        st.success(f"Пользователь «{del_target}» удалён")
                        st.rerun()
            else:
                st.caption("Нет пользователей для удаления")

        with st.expander("⚙️ Диагностика"):
            st.caption(f"Host: `{host_header or '—'}`")
            st.caption(f"Admin-режим: `{is_admin_domain}`")

    st.markdown("---")

    # Проверка: идёт ли индексация — по статусу (глобально, не через session_state)
    # Кнопка ⏹ показывается если:
    #   1. Поток индексации жив (session_state.ingest_running)
    #   2. ИЛИ есть файл в статусе "indexing" в JSON
    _thread_now = st.session_state.get("ingest_thread")
    _thread_alive = _thread_now is not None and _thread_now.is_alive()
    _status_snapshot = get_status()
    _has_indexing_status = any(
        s.get("status") == "indexing"
        for s in _status_snapshot.values()
    )
    _any_indexing = _thread_alive or _has_indexing_status or st.session_state.get("ingest_running", False)

    if _any_indexing:
        with st.container(key="stop_index_wrap"):
            if st.button("⏹  Остановить индексацию", use_container_width=True, type="primary"):
                Path("/tmp/rag_stop_requested").touch()
                if "ingest_stop_event" in st.session_state:
                    st.session_state.ingest_stop_event.set()
                st.rerun()
        st.caption("⏳ Идёт индексация...")

    st.markdown("---")
    st.markdown(f'<div class="sidebar-section">{icon_img(ICON_UPLOAD)} Загрузить документы</div>', unsafe_allow_html=True)

    uploaded_files = st.file_uploader(
        "Файлы",
        type=["pdf", "docx", "txt", "jpg", "jpeg", "png"],
        accept_multiple_files=True,
        key="user_upload",
        label_visibility="collapsed",
    )

    # ---- Кнопка "Индексировать" (в контейнере для CSS) ----
    _status_now_idx = get_status()
    _any_idx_now = any(s.get("status") == "indexing" for s in _status_now_idx.values())
    with st.container(key="idx_button_wrap"):
        _index_clicked = st.button(
            "Индексировать",
            use_container_width=True,
            disabled=_any_idx_now,
        )

    if _index_clicked:
        if uploaded_files:
            for uf in uploaded_files:
                path = os.path.join(USER_DOCS_DIR, uf.name)
                with open(path, "wb") as f:
                    f.write(uf.getbuffer())

            def run_ingest():
                try:
                    ingest_folder(USER_DOCS_DIR, uploaded_by=user['id'])
                except Exception as e:
                    print(f"Ошибка ingest: {e}")

            t = threading.Thread(target=run_ingest, daemon=True)
            t.start()
            st.success("Индексация запущена — смотрите статус ниже")
        else:
            st.warning("Выберите файлы")

    st.markdown("---")

    # ============ ОБРАБОТКА КЛИКА ▶ (ДО отрисовки списка файлов) ============
    _pending_resume = st.session_state.pop("_resume_click", None)
    if _pending_resume:
        _rp = os.path.join(USER_DOCS_DIR, _pending_resume)
        print(f"[RESUME] Запуск для: {_rp}", flush=True)
        # Мгновенно ставим indexing, чтобы следующий rerun уже не рисовал ▶
        try:
            _old_st = get_status().get(_rp, {})
            set_file_status(
                _rp,
                status="indexing",
                progress=float(_old_st.get("progress", 0.0) or 0.0),
                stage="запуск потока...",
                chunks=int(_old_st.get("chunks", 0) or 0),
                last_chunk_index=int(_old_st.get("last_chunk_index", 0) or 0),
                total_chunks=int(_old_st.get("total_chunks", 0) or 0),
            )
        except Exception as _e:
            print(f"[RESUME] set_status err: {_e}", flush=True)

        _stop_flag = Path("/tmp/rag_stop_requested")
        if _stop_flag.exists():
            try: _stop_flag.unlink()
            except Exception: pass

        if "ingest_stop_event" not in st.session_state:
            st.session_state.ingest_stop_event = threading.Event()
        else:
            st.session_state.ingest_stop_event.clear()

        def _run_resume(_fp=_rp, _evt=st.session_state.ingest_stop_event):
            try:
                print(f"[RESUME-THREAD] Старт: {_fp}", flush=True)
                ingest_folder(
                    USER_DOCS_DIR,
                    uploaded_by=user['id'],
                    stop_event=_evt,
                    resume_file=_fp,
                )
                print(f"[RESUME-THREAD] Готово: {_fp}", flush=True)
            except Exception as e:
                print(f"[RESUME-THREAD] Ошибка: {e}", flush=True)

        threading.Thread(target=_run_resume, daemon=True).start()
        st.toast(f"▶ Продолжаю: {_pending_resume}", icon="▶")
        time.sleep(0.3)
        st.rerun()

    st.markdown(f'<div class="sidebar-section">{icon_img(ICON_FOLDER)} Мои документы</div>', unsafe_allow_html=True)
    my_files = sorted(os.listdir(USER_DOCS_DIR)) if os.path.exists(USER_DOCS_DIR) else []
    _status_all_docs = get_status()

    if my_files:
        for f in my_files:
            fp = os.path.join(USER_DOCS_DIR, f)
            _st = _status_all_docs.get(fp, {})
            _status = _st.get("status", "?")
            _lp = int(_st.get("last_chunk_index", 0) or 0)
            _tp = int(_st.get("total_chunks", 0) or 0)

            c1, c2, c3 = st.columns([3, 1, 1])
            c1.write(f"📄 {f}")

            if _status == "indexing":
                c1.caption(f"⏳ {_lp}/{_tp}" if _tp else "⏳ индексация...")
                c2.write("")
            elif _status == "stopped":
                c1.caption(f"⏹ {_lp}/{_tp}" if _tp else "⏹ остановлено")
                _btn_key = "resume_" + hashlib.md5(f.encode("utf-8")).hexdigest()[:8]
                if c2.button("▶", key=_btn_key, help="Продолжить", type="primary"):
                    print(f"[CLICK] Кнопка ▶ нажата для: {f}", flush=True)
                    st.session_state["_resume_click"] = f
                    st.rerun()
            elif _status == "done":
                c1.caption(f"✅ {_tp} чанков" if _tp else "✅ готово")
                c2.write("")
            elif _status == "error":
                c1.caption("❌ ошибка")
                c2.write("")
            else:
                c2.write("")

            if c3.button("🗑️", key=f"userdel_{f}", help="Удалить"):
                try:
                    qdrant.delete(
                        collection_name=COLLECTION_NAME,
                        points_selector=FilterSelector(
                            filter=Filter(must=[FieldCondition(key="source", match=MatchValue(value=fp))])
                        )
                    )
                    qdrant.delete(
                        collection_name=FILES_HASH_COLLECTION,
                        points_selector=FilterSelector(
                            filter=Filter(must=[FieldCondition(key="path", match=MatchValue(value=fp))])
                        )
                    )
                except Exception:
                    pass
                if os.path.exists(fp):
                    os.remove(fp)
                remove_status(fp)
                if 'selected_files' in st.session_state and f in st.session_state.selected_files:
                    st.session_state.selected_files.remove(f)
                st.success(f"Удалён: {f}")
                st.rerun()
    else:
        st.write("Нет документов")



    st.markdown("---")
    render_status_panel(USER_DOCS_DIR)

    st.markdown("---")
    _hist_total = count_history(user['id'])
    st.markdown(f"**📜 Моя история ({_hist_total})**")
    my_history = get_history(user_id=user['id'], limit=10)
    if my_history:
        for item in my_history:
            with st.expander(f"❓ {item['question'][:60]}"):
                st.markdown(f"**Ответ:** {item['answer']}")
    else:
        st.write("Пока нет вопросов.")

    if _hist_total > 0:
        if st.button("Вся история", key="btn_show_all_history",
                     use_container_width=True):
            st.session_state.view = "history"
            st.session_state.history_page = 1
            st.rerun()
        if st.button("Очистить историю", key="btn_clear_history_short",
                     use_container_width=True):
            clear_history(user_id=user['id'])
            st.toast("История очищена", icon="✅")
            st.rerun()
    st.markdown("---")
    if st.button("Выйти", use_container_width=True):
        st.session_state.user = None
        try:
            st.query_params.clear()
        except Exception:
            pass
        clear_auth_cookie()
        st.rerun()



# ============ СТРАНИЦА ИСТОРИИ (полный список) ============
if st.session_state.get("view") == "history":
    _nav_col1, _nav_col2 = st.columns([1, 3])
    with _nav_col1:
        if st.button("← Назад", key="btn_back_from_history", use_container_width=True):
            st.session_state.view = "main"
            st.rerun()
    with _nav_col2:
        pass

    st.markdown("## 📜 Вся история запросов")

    _hist_total_full = count_history(user_id=user['id'])
    if _hist_total_full == 0:
        st.info("История пуста.")
        st.stop()

    PAGE_SIZE = 50
    _total_pages = max(1, (_hist_total_full + PAGE_SIZE - 1) // PAGE_SIZE)
    _page = st.session_state.get("history_page", 1)
    if _page < 1:
        _page = 1
    if _page > _total_pages:
        _page = _total_pages
    st.session_state.history_page = _page

    # Навигация сверху
    _p1, _p2, _p3 = st.columns([1, 3, 1])
    with _p1:
        if _page > 1:
            if st.button("← Пред", key="hist_prev_top", use_container_width=True):
                st.session_state.history_page = _page - 1
                st.rerun()
    with _p2:
        st.markdown(
            f"<div style='text-align:center; padding-top:8px; color:#64748b;'>"
            f"Страница <b>{_page}</b> из <b>{_total_pages}</b> · всего записей: <b>{_hist_total_full}</b>"
            f"</div>",
            unsafe_allow_html=True,
        )
    with _p3:
        if _page < _total_pages:
            if st.button("След →", key="hist_next_top", use_container_width=True):
                st.session_state.history_page = _page + 1
                st.rerun()

    st.markdown("---")

    # Записи страницы
    _page_records = get_history_paginated(user['id'], page=_page, page_size=PAGE_SIZE)
    for _item in _page_records:
        _q = _item.get("question", "")
        with st.expander(f"❓ {_q[:100]}"):
            st.markdown(f"**Ответ:** {_item.get('answer', '')}")
            if _item.get("timestamp"):
                st.caption(f"🕐 {_item['timestamp']}")
            _sources = _item.get("sources") or []
            if _sources:
                st.markdown("**Источники:**")
                for _s in _sources:
                    st.caption(f"• {_s}")

    # Навигация снизу
    st.markdown("---")
    _b1, _b2, _b3 = st.columns([1, 3, 1])
    with _b1:
        if _page > 1:
            if st.button("← Пред", key="hist_prev_bot", use_container_width=True):
                st.session_state.history_page = _page - 1
                st.rerun()
    with _b2:
        st.markdown(
            f"<div style='text-align:center; padding-top:8px; color:#64748b;'>"
            f"Стр. {_page}/{_total_pages}"
            f"</div>",
            unsafe_allow_html=True,
        )
    with _b3:
        if _page < _total_pages:
            if st.button("След →", key="hist_next_bot", use_container_width=True):
                st.session_state.history_page = _page + 1
                st.rerun()

    st.stop()


# ============ ГЛАВНЫЙ КОНТЕНТ ============
st.markdown(f"""
<div class="hero">
    <h1>
        <img src="{LOGO}" alt="logo"/>
        ИИ-ассистент для юристов и бухгалтеров
    </h1>
    <p>Загружайте свои документы в левой панели и задавайте вопросы. Вы можете искать по всей базе или выбрать конкретные документы.</p>
    <div class="badges">
        <span class="badge"><img src="{ICON_NOCLOUD}" alt=""/> Без облаков</span>
        <span class="badge">📄 Любые форматы</span>
        <span class="badge"><img src="{ICON_SEARCH}" alt=""/> Ссылка на источник</span>
    </div>
</div>
""", unsafe_allow_html=True)

st.markdown(f'<div class="section-title">{icon_img(ICON_TARGET, 26)} Где искать</div>', unsafe_allow_html=True)
search_scope = st.radio(
    "Область поиска:",
    ["Все документы", "Только мои документы", "Выбрать вручную"],
    horizontal=True,
    label_visibility="collapsed"
)

selected_files = []

if search_scope == "Выбрать вручную":
    my_files = sorted(os.listdir(USER_DOCS_DIR)) if os.path.exists(USER_DOCS_DIR) else []
    if not my_files:
        st.warning("У вас нет загруженных документов. Загрузите файлы в левой панели.")
    else:
        if 'selected_files' not in st.session_state:
            st.session_state.selected_files = []

        with st.container():
            col_a, col_b, col_c = st.columns([3, 1, 1])
            with col_a:
                filter_text = st.text_input("🔎 Фильтр по названию:", placeholder="Введите часть имени файла...", key="file_filter")
            with col_b:
                st.write(""); st.write("")
                if st.button("✅ Все", use_container_width=True):
                    st.session_state.selected_files = list(my_files)
                    st.rerun()
            with col_c:
                st.write(""); st.write("")
                if st.button("❌ Ничего", use_container_width=True):
                    st.session_state.selected_files = []
                    st.rerun()

            filtered = [f for f in my_files if filter_text.lower() in f.lower()] if filter_text else my_files
            st.caption(f"Выбрано: {len(st.session_state.selected_files)} из {len(my_files)}")

            for f in filtered:
                checked = f in st.session_state.selected_files
                new_val = st.checkbox(f, value=checked, key=f"cb_{f}")
                if new_val and f not in st.session_state.selected_files:
                    st.session_state.selected_files.append(f)
                elif not new_val and f in st.session_state.selected_files:
                    st.session_state.selected_files.remove(f)

        selected_files = st.session_state.selected_files
        if selected_files:
            st.success(f"Искать по: {', '.join(selected_files[:3])}" + (" и др." if len(selected_files) > 3 else ""))
        else:
            st.info("Ничего не выбрано — поиск будет по всей базе.")

st.markdown(f'<div class="section-title">{icon_img(ICON_LIGHTBULB, 26)} Примеры вопросов</div>', unsafe_allow_html=True)
example_cols = st.columns(3)
examples = [
    "Что такое преступление?",
    "Какие условия договора аренды?",
    "Как рассчитать налог на прибыль?",
]
for idx, col in enumerate(example_cols):
    with col:
        if st.button(examples[idx], key=f"example_{idx}"):
            st.session_state.query = examples[idx]


# ============ ПОИСК С КНОПКАМИ ============
class _SearchTask:
    def __init__(self):
        self.running = False
        self.done = False
        self.stop_event = threading.Event()
        self.answer = ""
        self.points = None
        self.error = None
        self.query = ""


if '_search_task' not in st.session_state:
    st.session_state._search_task = _SearchTask()
_task = st.session_state._search_task


def _run_search(q, user_id, docs_dir, scope, sel_files, tsk):
    try:
        if tsk.stop_event.is_set():
            tsk.error = "stopped"
            return

        qv = embedder.encode([q], max_length=MAX_LENGTH)['dense_vecs'][0].tolist()

        if tsk.stop_event.is_set():
            tsk.error = "stopped"
            return

        qf = None
        if scope == "Только мои документы":
            qf = Filter(must=[FieldCondition(key="uploaded_by", match=MatchValue(value=user_id))])
        elif scope == "Выбрать вручную" and sel_files:
            conds = [
                FieldCondition(key="source", match=MatchValue(value=os.path.join(docs_dir, f)))
                for f in sel_files
            ]
            qf = Filter(should=conds)

        # 1. Берём больше кандидатов (20) для реранкинга
        CANDIDATES_LIMIT = 20
        sr = qdrant.query_points(
            collection_name=COLLECTION_NAME,
            query=qv,
            limit=CANDIDATES_LIMIT,
            query_filter=qf,
        )

        candidates_list = list(sr.points) if sr.points else []

        # 1b. Если в запросе есть "статья N" — брать ТОЛЬКО чанки с этой статьёй
        try:
            import re as _re_num
            _art_match = _re_num.search(r'стать[яию]\s+(\d{1,4})', q, _re_num.IGNORECASE)
            if _art_match:
                _art_num = int(_art_match.group(1))
                print(f"[ARTICLE-FILTER] Ищу ТОЛЬКО статью {_art_num}", flush=True)

                from qdrant_client.models import FieldCondition, MatchValue
                _art_filter = Filter(
                    must=[FieldCondition(key="article_nums", match=MatchValue(value=_art_num))]
                )

                # Объединяем с основным фильтром (если был)
                if qf is not None:
                    _combined = Filter(
                        must=list(qf.must or []) + [_art_filter.must[0]]
                    )
                else:
                    _combined = _art_filter

                # Ищем ВСЕ чанки с этой статьёй
                _art_points = qdrant.scroll(
                    collection_name=COLLECTION_NAME,
                    scroll_filter=_combined,
                    limit=5,
                    with_payload=True,
                )[0]

                if _art_points:
                    # ВАЖНО: заменяем кандидатов ТОЛЬКО на эти чанки
                    candidates_list = list(_art_points)
                    print(f"[ARTICLE-FILTER] Заменяю контекст на {len(_art_points)} чанков статьи {_art_num}", flush=True)
                else:
                    print(f"[ARTICLE-FILTER] Статья {_art_num} не найдена, fallback на вектор", flush=True)
        except Exception as _e_art:
            print(f"[ARTICLE-FILTER] Ошибка: {_e_art}", flush=True)

        sr = type('obj', (object,), {'points': candidates_list})()

        if not sr.points:
            tsk.error = "not_found"
            return

        # 2. Реранкинг — пересортировка через BGE-reranker-v2-m3
        try:
            _docs = [{"text": p.payload["text"], "original": p} for p in sr.points]
            _reranked = rerank(q, _docs, top_k=SEARCH_LIMIT)
            tsk.points = [d["original"] for d in _reranked]
            print(f"[RERANK] {len(sr.points)} → {len(tsk.points)}", flush=True)
        except Exception as _e:
            print(f"[RERANK ERROR] {_e} — fallback к векторному поиску", flush=True)
            tsk.points = sr.points[:SEARCH_LIMIT]

        if tsk.stop_event.is_set():
            tsk.error = "stopped"
            return

        ctx = "\n\n".join([p.payload['text'] for p in sr.points])
        prompt = f"""Ты — юридический ассистент. Отвечай СТРОГО по контексту.

ЖЁСТКИЕ ПРАВИЛА:
1. Используй ТОЛЬКО информацию из контекста.
2. ВСЕ числа бери ТОЛЬКО из того фрагмента, который ПРЯМО отвечает на вопрос.
3. ЗАПРЕЩЕНО смешивать числа из разных статей.
3a. ЗАПРЕЩЕНО указывать номера статей, которых нет в контексте. Если в контексте «Статья 155» — пиши «статья 155», НЕ «статья 125».
3b. ЗАПРЕЩЕНО ПРОИЗВОДИТЬ ЛЮБЫЕ ВЫЧИСЛЕНИЯ (сложение, вычитание, проценты). Если в документе нет готового числа — не считай. Не пиши формулы, не вычисляй, не выводи одно число из другого.
4. Цитируй точные формулировки из документа. Не пересказывай по-своему.
5. Если в контексте нет прямого ответа — напиши: «В загруженных документах нет точной информации.»

ТИП ОТВЕТА ПО ТИПУ ВОПРОСА:
• Вопрос «что относится к...» / «какие бывают...» / «что входит в...» → ОБЯЗАТЕЛЬНО приведи ПОЛНЫЙ ПЕРЕЧЕНЬ из документа, каждый пункт отдельной строкой через тире. НЕ ограничивайся определением!
• Вопрос «какова продолжительность...» / «сколько дней...» → назови КОНКРЕТНОЕ число из документа.
• Вопрос «что такое...» / «дай определение...» → процитируй определение.
• Вопрос «какая ответственность...» → перечисли виды наказаний из статьи.

ФОРМАТ:
1. Прямой ответ (перечень / число / определение)
2. Цитата из закона в кавычках
3. Номер статьи

ПРИМЕР правильного ответа на «что относится к X»:
К X относятся:
– пункт 1
– пункт 2
– пункт 3
(Цитата: «...»)
Статья N.

Контекст:
{ctx}

Вопрос: {q}

Ответ:"""

        stream = ollama.generate(
            model=LLM_MODEL,
            prompt=prompt,
            stream=True,
            options={"temperature": TEMPERATURE},
        )
        for chunk in stream:
            if tsk.stop_event.is_set():
                tsk.error = "stopped"
                try:
                    stream.close()
                except Exception:
                    pass
                return
            tsk.answer += chunk.get('response', '')

        # ============ ПОСТПРОВЕРКА (умная) ============
        try:
            import re as _re_verify

            def extract_meaningful_numbers(text):
                """Извлекает числа, которые НЕ являются номерами статей/пунктов."""
                # Убираем "статья 125", "ст. 155", "№ 12", "пункт 4", "п. 3", "ч. 2", "раздел V"
                cleaned = _re_verify.sub(
                    r'(?:стать[яию]|ст\.|№|пункт|п\.|ч\.|часть|раздел|глава|гл\.)\s*\d+',
                    ' ', text, flags=_re_verify.IGNORECASE
                )
                # Убираем римские цифры и ссылки типа "V.1"
                cleaned = _re_verify.sub(r'\b[IVХ]+\.', ' ', cleaned)
                return set(_re_verify.findall(r'\b\d{1,5}\b', cleaned))

            # --- Проверка 1: значимые числа из ответа должны быть в контексте ---
            ctx_numbers = extract_meaningful_numbers(ctx)
            ans_numbers = extract_meaningful_numbers(tsk.answer)
            # Берём только числа > 5 (мелкие — предлоги, 1-2-3)
            fake_numbers = {n for n in (ans_numbers - ctx_numbers) if int(n) > 5}

            # Дополнительная фильтрация: игнорируем числа, если они редко упоминаются с единицами
            # Оставляем только те, что могут быть важными (дни, проценты, суммы)
            important_numbers = set()
            for n in fake_numbers:
                # Проверяем, стоит ли рядом с числом единица измерения в ответе
                if _re_verify.search(
                    rf'\b{n}\s*(?:календарн|дн|день|дней|%|процент|руб|бел|год|лет|месяц|час)',
                    tsk.answer, _re_verify.IGNORECASE
                ):
                    important_numbers.add(n)
            fake_numbers = important_numbers

            # --- Проверка 2: слова-существительные из вопроса должны быть в контексте ---
            q_words = set(w.lower() for w in _re_verify.findall(r'\b[А-Яа-яЁё]{7,}\b', q))
            ctx_lower = ctx.lower()
            missing_subjects = set()

            # Игнор-лист: глаголы и служебные слова (проверяем ПО ОКОНЧАНИЮ)
            VERB_ENDINGS = (
                'ется', 'ются', 'атся', 'ятся', 'ешься', 'ишься',
                'ать', 'ять', 'еть', 'ить', 'ыть', 'уть',
                'ает', 'яет', 'ует', 'иет',
                'ают', 'яют', 'уют',
                'жит', 'шит', 'чит', 'дит', 'тит', 'нит', 'рит',
                'жет', 'шет', 'чет', 'дет',
                'сти', 'сть', 'зти',
                'его', 'ого', 'ему', 'ому', 'ыми', 'ими',
            )
            # Существительные-термины, которые нас не интересуют
            IGNORE_TERMS = {
                'который', 'которая', 'которое', 'которые', 'сколько',
                'какова', 'каковы', 'какой', 'какие', 'бывает', 'бывают',
                'скажите', 'подскажи', 'объясни', 'продолжит',
                'ответствен', 'наказание', 'положено', 'предусмотр',
                'относится', 'относятся', 'входит', 'входят',
                'является', 'являются', 'считается', 'считаются',
                'называется', 'называются', 'применяется', 'применяются',
                'устанавлив', 'определя', 'регулиру', 'использу',
                'рассчитыв', 'учитыва', 'включает', 'включают',
            }

            for w in q_words:
                # Пропускаем по игнор-списку
                if w in IGNORE_TERMS:
                    continue
                if any(w.startswith(prefix) for prefix in IGNORE_TERMS):
                    continue
                # Пропускаем глаголы по окончанию
                if w.endswith(VERB_ENDINGS):
                    continue
                # Проверяем основу слова (первые 6 символов)
                base = w[:6]
                if base not in ctx_lower:
                    missing_subjects.add(w)

            has_problem = bool(fake_numbers) or bool(missing_subjects)

            if has_problem:
                print(f"[VERIFY] Галлюцинация! Числа: {fake_numbers}, Субъекты: {missing_subjects}", flush=True)
                parts = ["⚠️ **Модель не смогла сформировать ответ строго по контексту.**\n"]
                if fake_numbers:
                    parts.append(f"Обнаружены значимые числа, которых нет в источниках: `{sorted(fake_numbers)}`")
                if missing_subjects:
                    parts.append(f"Вопрос содержит слова, которых нет в найденных документах: `{sorted(missing_subjects)}`")
                parts.append("")
                parts.append("**Скорее всего, в загруженных документах нет точной информации по этому вопросу.**")
                parts.append("")
                parts.append("Попробуйте:")
                parts.append("1. Переформулировать вопрос более конкретно")
                parts.append("2. Убедиться, что нужный документ загружен и проиндексирован")
                parts.append("3. Задать вопрос по статье/разделу")
                tsk.answer = "\n\n".join(parts)
        except Exception as _e_verify:
            print(f"[VERIFY] Ошибка проверки: {_e_verify}", flush=True)

        # ============ EXTRACT-LIST ============
        try:
            import re as _re_list

            q_low = q.lower()
            is_list_question = any(p in q_low for p in [
                'что относится', 'какие относятся', 'что входит',
                'какие входят', 'перечисли', 'какие бывают'
            ])

            if is_list_question:
                # Ищем ВСЁ предложение "К X относятся: a; b; c."
                sent_match = _re_list.search(
                    r'([А-ЯЁ][^.:\n]{5,200}?)\s*(?:относят(?:ся|ся)|входят|включа(?:ют|ет))\s*:\s*([^.]{10,600})\.',
                    ctx
                )

                if sent_match:
                    intro = sent_match.group(1).strip()
                    raw_list = sent_match.group(2).strip()

                    # Обрезаем intro по последней заглавной букве — убираем склеенный заголовок
                    # Например: "Местные налоги и сборы К местным налогам и сборам" → "К местным налогам и сборам"
                    _caps = [i for i, ch in enumerate(intro) if ch.isupper() and i > 0]
                    if len(_caps) >= 1:
                        _last_cap = _caps[-1]
                        # Если перед заглавной есть пробел — режем
                        if intro[_last_cap - 1] == ' ':
                            intro = intro[_last_cap:].strip()

                    # Режем только по ';'
                    items = [it.strip().rstrip(',').rstrip('.') for it in raw_list.split(';')]
                    items = [it for it in items if len(it) > 3]

                    if len(items) >= 2:
                        # Формируем markdown-список (будет конвертирован в HTML)
                        list_md = "\n".join(["– " + it for it in items])
                        quote = f"{intro} относятся: {raw_list}."

                        prefix = (
                            f"**Прямой перечень из документа:**\n\n"
                            f"{list_md}\n\n"
                            f"**Цитата:** «{quote}»\n\n"
                            f"---\n\n"
                        )

                        if not any(it[:15] in tsk.answer for it in items[:2]):
                            tsk.answer = prefix + tsk.answer
                            print(f"[EXTRACT-LIST] Добавлен перечень из {len(items)} пунктов", flush=True)

        except Exception as _e_list:
            print(f"[EXTRACT-LIST] Ошибка: {_e_list}", flush=True)



        # ============ ANTI-MATH: ловим вычисления ============
        # Если модель считает (a - b = c, a + b = c, a / b = c) — значит галлюцинация
        try:
            import re as _re_math
            # Ищем паттерны: "146 - 118 = 28", "146-118=28", "146 + 118 = 264"
            math_patterns = [
                r'\d+\s*[-+*/]\s*\d+\s*=\s*\d+',   # 5 - 3 = 2
                r'\d+\s*[-+*/]\s*\d+',                # 5 - 3 (без равно)
                r'равно\s+\d+',                         # "равно 28"
                r'составляет\s+\d+\s*[-+*/]',          # "составляет 146 -"
                r'\d+\s*минус\s*\d+',                 # "146 минус 118"
                r'\d+\s*плюс\s*\d+',                  # "146 плюс 118"
            ]
            has_math = False
            for p in math_patterns:
                if _re_math.search(p, tsk.answer):
                    has_math = True
                    print(f"[ANTI-MATH] Обнаружена математика в ответе: {p}", flush=True)
                    break

            if has_math:
                tsk.answer = (
                    "⚠️ **Модель попыталась выполнить вычисления, которых нет в документе.**\n\n"
                    "Это признак того, что точного ответа в источниках нет.\n\n"
                    "**В загруженных документах нет готового числа, отвечающего на ваш вопрос.**\n\n"
                    "Попробуйте:\n"
                    "1. Уточнить вопрос\n"
                    "2. Проверить, что нужный документ загружен\n"
                    "3. Задать вопрос про конкретную статью"
                )
        except Exception as _e_math:
            print(f"[ANTI-MATH] Ошибка: {_e_math}", flush=True)

        # ============ ANTI-FAKE-ARTICLE v3: полный regex + замена ============
        try:
            import re as _re_art3

            # ВАЖНО: ловим ВСЕ формы, включая "статьей"
            _art_re = r'(?:[Сс]тать[яиюей]{1,3}|[Сс]т\.)\s*(\d{1,4})'

            _ans_arts = set(_re_art3.findall(_art_re, tsk.answer))
            _ctx_arts = set(_re_art3.findall(_art_re, ctx))

            # Выдуманные номера (в ответе есть, в контексте нет)
            _fake = {n for n in (_ans_arts - _ctx_arts) if len(n) >= 2}

            if _fake and _ctx_arts:
                print(f"[ANTI-FAKE-ARTICLE v3] Выдумано: {_fake}, есть в контексте: {_ctx_arts}", flush=True)

                # Берём первую статью из контекста (обычно это главная)
                _first_ctx_art = None
                _m = _re_art3.search(_art_re, ctx)
                if _m:
                    _first_ctx_art = _m.group(1)

                if _first_ctx_art:
                    for _fake_n in _fake:
                        # Заменяем "статья 127" → "статья 155" (сохраняя форму слова)
                        _pattern = rf'((?:[Сс]тать[яиюей]{{1,3}}|[Сс]т\.)\s*){_fake_n}\b'
                        tsk.answer = _re_art3.sub(
                            _pattern,
                            lambda m: m.group(1) + _first_ctx_art,
                            tsk.answer
                        )
                    print(f"[ANTI-FAKE-ARTICLE v3] Заменено {_fake} → {_first_ctx_art}", flush=True)
        except Exception as _e_art3:
            print(f"[ANTI-FAKE-ARTICLE v3] Ошибка: {_e_art3}", flush=True)





        if tsk.answer and tsk.points:
            src_list = []
            for p in tsk.points:
                t = p.payload['text']
                m = re.search(r'(Статья\s+\d+[^.]*\.)', t)
                title = m.group(1) if m else t[:80].replace('\n', ' ') + '...'
                src_list.append(f"{title} (файл: {p.payload['source']})")
            save_query(user_id, q, tsk.answer, src_list)

    except Exception as e:
        tsk.error = str(e)
    finally:
        tsk.done = True


# ---- Строка ввода + кнопка (только одна: ▶ или ✖) ----
_is_running = _task.running and not _task.done and not _task.stop_event.is_set()

if _is_running:
    # Идёт поиск — поле только для чтения + красная кнопка ✖
    _q_col1, _q_col2 = st.columns([15, 1], vertical_alignment="bottom")
    with _q_col1:
        _ro_query = st.session_state.get("query", "")
        st.text_input(
            "Ваш вопрос:",
            value=_ro_query,
            disabled=True,
            key="query_input_readonly",
        )
        query = _ro_query
    with _q_col2:
        with st.container(key="btn_stop_wrap"):
            if st.button("✖", key="btn_stop", help="Остановить поиск", use_container_width=True):
                stop_clicked = True
            else:
                stop_clicked = False
    go_clicked = False
else:
    # Обычный режим — форма с полем ввода + зелёная кнопка ▶
    # Enter в поле = submit формы (запускает поиск)
    stop_clicked = False
    with st.form(key="search_form", clear_on_submit=False):
        _q_col1, _q_col2 = st.columns([15, 1], vertical_alignment="bottom")
        with _q_col1:
            query = st.text_input(
                "Ваш вопрос:",
                value=st.session_state.get("query", ""),
                key="query_input",
                placeholder="Введите вопрос и нажмите Enter...",
            )
        with _q_col2:
            with st.container(key="btn_go_wrap"):
                go_clicked = st.form_submit_button("▶", use_container_width=True)


# ---- Обработка кликов ----
if stop_clicked:
    _task.stop_event.set()
    _task.running = False
    _task.done = True
    _task.error = "stopped"
    st.rerun()

if go_clicked and query:
    new_task = _SearchTask()
    new_task.running = True
    new_task.query = query
    st.session_state._search_task = new_task
    _task = new_task
    st.session_state.query = query

    _th = threading.Thread(
        target=_run_search,
        args=(query, user['id'], USER_DOCS_DIR, search_scope, list(selected_files), new_task),
        daemon=True,
    )
    _th.start()
    st.rerun()


# ---- Ожидание ----
if _task.running and not _task.done and not _task.stop_event.is_set():
    st.info("⏳ Идёт поиск... Нажмите ✖, чтобы остановить.")
    time.sleep(0.5)
    st.rerun()


# ---- Переходные состояния (stop мгновенный) ----


# ---- Обработка результата ----
if _task.running and _task.done:
    _task.running = False
    st.rerun()


if _task.error == "stopped":
    st.warning("⏹ Поиск остановлен.")
    _task.error = None
    _task.answer = ""
    _task.points = None
    _task.stop_event.clear()
elif _task.error == "not_found":
    st.warning("По вашему запросу ничего не найдено. Попробуйте изменить область поиска.")
    _task.error = None
    _task.points = None
elif _task.error:
    st.error(f"Ошибка: {_task.error}")
    _task.error = None
    _task.points = None


# ---- Стриминг (пока идёт) ----
if _task.running and _task.answer and not _task.done:
    st.markdown("### 📝 Ответ")
    st.markdown(f"""
    <div style="background:white; border-left:4px solid #3b82f6; padding:20px; border-radius:10px; margin-bottom:24px; box-shadow:0 2px 8px rgba(30,58,95,0.05); color:#1e293b;">
        {md_to_html(_task.answer)}▌
    </div>
    """, unsafe_allow_html=True)


# ---- Финальный ответ ----
if _task.done and _task.points and _task.answer and not _task.stop_event.is_set():
    st.markdown("### 📝 Ответ")
    if "недостаточно информации" not in _task.answer.lower():
        st.markdown(f"""
        <div style="background:white; border-left:4px solid #3b82f6; padding:20px; border-radius:10px; margin-bottom:24px; box-shadow:0 2px 8px rgba(30,58,95,0.05); color:#1e293b;">
            {md_to_html(_task.answer)}
        </div>
        """, unsafe_allow_html=True)
    else:
        st.warning("Модель не смогла сформировать уверенный ответ. Ниже — наиболее релевантные фрагменты.")

    st.markdown("### 📚 Источники")
    for p in _task.points:
        text = p.payload['text']
        match = re.search(r'(Статья\s+\d+[^.]*\.)', text)
        title = match.group(1) if match else text[:80].replace('\n', ' ') + ' ...'
        with st.expander(f"📄 {title}"):
            st.caption(f"Файл: {p.payload['source']}")
            st.text(text)


# ============ AUTO-REFRESH-WHILE-INDEXING ============
# Если есть файлы со статусом "indexing" — перерисовываем страницу каждые 1.5 сек,
# чтобы пользователь видел прогресс в реальном времени
try:
    _status_final = get_status()
    _any_idx_final = any(
        s.get("status") == "indexing"
        for s in _status_final.values()
    )
    if _any_idx_final:
        time.sleep(1.5)
        st.rerun()
except Exception:
    pass
