import sys
import os
import time
import json
import random
import threading
import logging
import requests
import psycopg2
from psycopg2 import pool
from datetime import datetime, timezone, timedelta
from fastapi import FastAPI, Request, BackgroundTasks
from starlette.responses import PlainTextResponse
import uvicorn
import google.generativeai as genai
from gtts import gTTS
import difflib

# --- CẤU HÌNH ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(message)s')
logger = logging.getLogger(__name__)

# Thông tin cấu hình
PAGE_ACCESS_TOKEN = "EAAbQQNNSmSMBQOLS4eBsN7f8vUdGyOsxupjsjl3aJyU6w9udeAVEFRdtLkikidUowCEYxgjiZBvCZBM8ZCISVqrG7crVqMjUCYE0HNixNuQIrdgaPrTJd0w78ZAZC7lEnnyrSTlTZCc0UxZAkYQ0ZCF8hh8A6JskvPmZCNkm5ZBprIAEYQcKAWqXCBakZAOcE7Dli4be4FEeAZDZD"
VERIFY_TOKEN = "hsk_mat_khau_bi_mat"
GEMINI_API_KEY = "AIzaSyB5V6sgqSOZO4v5DyuEZs3msgJqUk54HqQ"
DATABASE_URL = os.environ.get('DATABASE_URL')

# --- DATA ---
# (Phần này sẽ được thay thế bằng DB load, nhưng giữ lại fallback)
try:
    import hsk2_vocabulary_full as hsk_data
    HSK_DATA = hsk_data.HSK_DATA
except:
    HSK_DATA = [{"Hán tự": "你好", "Pinyin": "nǐhǎo", "Nghĩa": "xin chào"}]

# --- DATABASE & SEEDING ---
db_pool = None
if DATABASE_URL:
    try:
        db_pool = psycopg2.pool.ThreadedConnectionPool(1, 5, DATABASE_URL, sslmode='require')
        logger.info("DB Connected.")
    except Exception as e:
        logger.error(f"DB Error: {e}")

def init_database():
    """Khởi tạo bảng và nạp dữ liệu nếu rỗng"""
    if not db_pool: return
    conn = None
    try:
        conn = db_pool.getconn()
        with conn.cursor() as cur:
            # 1. Tạo bảng Users
            cur.execute("CREATE TABLE IF NOT EXISTS users (user_id VARCHAR(50) PRIMARY KEY, state JSONB)")
            # 2. Tạo bảng Vocabulary
            cur.execute("""
                CREATE TABLE IF NOT EXISTS vocabulary (
                    id SERIAL PRIMARY KEY,
                    hanzi VARCHAR(50) UNIQUE NOT NULL,
                    pinyin VARCHAR(100),
                    meaning TEXT
                )
            """)
            conn.commit()
            
            # 3. Kiểm tra dữ liệu và Seed
            cur.execute("SELECT COUNT(*) FROM vocabulary")
            count = cur.fetchone()[0]
            if count == 0:
                logger.info("Seeding database from file...")
                try:
                    import hsk2_vocabulary_full as seed_data
                    data = seed_data.HSK_DATA
                    for item in data:
                        cur.execute(
                            "INSERT INTO vocabulary (hanzi, pinyin, meaning) VALUES (%s, %s, %s) ON CONFLICT DO NOTHING",
                            (item['Hán tự'], item['Pinyin'], item['Nghĩa'])
                        )
                    conn.commit()
                    logger.info(f"Seeded {len(data)} words.")
                except ImportError:
                    logger.warning("No seed file found.")
    except Exception as e:
        logger.error(f"Init DB Error: {e}")
    finally:
        if conn: db_pool.putconn(conn)

# Gọi hàm init ngay khi start
init_database()

USER_CACHE = {} 
app = FastAPI()

# --- AI SETUP ---
try:
    genai.configure(api_key=GEMINI_API_KEY)
    model = genai.GenerativeModel('gemini-1.5-flash')
except: model = None

# --- DB HELPERS ---
def get_db_conn():
    try: return db_pool.getconn()
    except: return None

def release_db_conn(conn):
    if db_pool and conn: db_pool.putconn(conn)

def get_random_words_from_db(exclude_list, limit=1):
    conn = get_db_conn()
    if not conn: return []
    try:
        with conn.cursor() as cur:
            if exclude_list:
                query = "SELECT hanzi, pinyin, meaning FROM vocabulary WHERE hanzi NOT IN %s ORDER BY RANDOM() LIMIT %s"
                cur.execute(query, (tuple(exclude_list), limit))
            else:
                query = "SELECT hanzi, pinyin, meaning FROM vocabulary ORDER BY RANDOM() LIMIT %s"
                cur.execute(query, (limit,))
            rows = cur.fetchall()
            return [{"Hán tự": r[0], "Pinyin": r[1], "Nghĩa": r[2]} for r in rows]
    finally: release_db_conn(conn)

def get_total_words_count():
    conn = get_db_conn()
    if not conn: return 0
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM vocabulary")
            return cur.fetchone()[0]
    finally: release_db_conn(conn)

def add_word_to_db(hanzi, pinyin, meaning):
    conn = get_db_conn()
    if not conn: return False
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO vocabulary (hanzi, pinyin, meaning) VALUES (%s, %s, %s) ON CONFLICT (hanzi) DO UPDATE SET meaning = EXCLUDED.meaning",
                (hanzi, pinyin, meaning)
            )
            conn.commit()
            return True
    except: return False
    finally: release_db_conn(conn)

def delete_word_from_db(hanzi):
    conn = get_db_conn()
    if not conn: return False
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM vocabulary WHERE hanzi = %s", (hanzi,))
            conn.commit()
            return True
    finally: release_db_conn(conn)

# --- AI FUNCTIONS ---

def ai_parse_command(text):
    if not model: return None
    try:
        prompt = f"""
        Phân tích lệnh quản lý từ vựng: "{text}"
        - Nếu thêm: {{"action": "ADD", "hanzi": "...", "pinyin": "...", "meaning": "..."}}
        - Nếu xóa: {{"action": "DELETE", "hanzi": "..."}}
        - Khác: {{"action": "NONE"}}
        Lưu ý: Tự suy luận Pinyin/Hán tự nếu thiếu. Trả về JSON thuần.
        """
        res = model.generate_content(prompt).text.strip()
        if "
