import sys
import os
from fastapi import FastAPI, Request, HTTPException, BackgroundTasks
from starlette.responses import PlainTextResponse
import uvicorn
import random
import requests
import json
from typing import List, Dict, Any, Optional
import time
import psycopg2
import google.generativeai as genai

# --- CẤU HÌNH ---
DATABASE_URL = os.environ.get('DATABASE_URL')
DB_STATUS = "Postgres" if DATABASE_URL else None

# FACEBOOK TOKEN
PAGE_ACCESS_TOKEN = "EAAbQQNNSmSMBQKWd5qB15zFMy2KdPm6Ko1rJX6R4ZC3EtnNfvf0gT76V1Qk4l1vflxL1pDVwY8mrgbgAaFFtG6bzcrhJfQ86HdK5v8qZA9zTIge2ZBJcx9oNPOjk1DlQ8juGinZBuah0RDgbCd2vBvlNWr47GVz70BdPNzKRctCGphNJRI0Wm57UwKRmXOZAVfDP7zwZDZD"
VERIFY_TOKEN = "hsk_mat_khau_bi_mat"

# GOOGLE GEMINI API (KEY CỦA BẠN)
GEMINI_API_KEY = "AIzaSyB5V6sgqSOZO4v5DyuEZs3msgJqUk54HqQ"
genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel('gemini-1.5-flash')

WORDS_PER_SESSION = 10
REMINDER_INTERVAL_SECONDS = 3600

# --- DATABASE SETUP ---
if DB_STATUS:
    try:
        with psycopg2.connect(DATABASE_URL, sslmode='require') as conn:
            with conn.cursor() as cursor:
                cursor.execute("""
                    CREATE TABLE IF NOT EXISTS users (
                        user_id VARCHAR(50) PRIMARY KEY,
                        state JSONB,
                        last_study_time INTEGER
                    );
                """)
            conn.commit()
        print("--> Kết nối PostgreSQL thành công.")
    except Exception as e:
        print(f"--> LỖI KẾT NỐI DB: {e}")
        DB_STATUS = None

# --- LOAD DATA ---
try:
    import hsk2_vocabulary_full as hsk_data
    HSK_DATA = hsk_data.HSK_DATA
    HSK_MAP = {word["Hán tự"]: word for word in HSK_DATA}
    ALL_HANZI = list(HSK_MAP.keys())
    print(f"--> Đã nạp {len(HSK_DATA)} từ vựng.")
except ImportError:
    HSK_DATA = [{"Hán tự": "你好", "Pinyin": "nǐhǎo", "Nghĩa": "xin chào", "Ví dụ": "你好吗", "Ví dụ Pinyin": "Nǐ hǎo ma", "Dịch câu": "Bạn khỏe không"}]
    HSK_MAP = {word["Hán tự"]: word for word in HSK_DATA}
    ALL_HANZI = list(HSK_MAP.keys())

# CÁC DẠNG BÀI
BOT_MODES = [
    {"name": "hanzi_to_viet", "title": "DẠNG 1: NHÌN HÁN TỰ -> ĐOÁN NGHĨA"},
    {"name": "viet_to_hanzi", "title": "DẠNG 2: NHÌN NGHĨA -> VIẾT HÁN TỰ"},
    {"name": "example_to_hanzi", "title": "DẠNG 3: ĐIỀN TỪ VÀO CÂU"},
    {"name": "translate_sentence", "title": "DẠNG 4: DỊCH CÂU SANG TIẾNG TRUNG"}
]

app = FastAPI()

# --- HELPER: DATABASE ---
def get_user_state(user_id: str) -> Dict[str, Any]:
    default_state = {
        "session_hanzi": [], "learned_hanzi": [], "mode_index": 0,
        "task_queue": [], "backup_queue": [], "mistake_made": False,
        "current_task": None, "score": 0, "total_questions": 0,
        "current_phase": "IDLE", "preview_queue": [], "reminder_sent": False,
        "last_study_time": 0
    }
    if DB_STATUS:
        try:
            with psycopg2.connect(DATABASE_URL, sslmode='require') as conn:
                with conn.cursor() as cursor:
                    cursor.execute("SELECT state FROM users WHERE user_id = %s", (user_id,))
                    res = cursor.fetchone()
                    if res: return {**default_state, **res[0]}
                    save_user_state(user_id, default_state, False)
                    return default_state
        except: return default_state
    return default_state

def save_user_state(user_id: str, state: Dict[str, Any], update_time: bool = True):
    if DB_STATUS:
        try:
            if update_time:
                state["last_study_time"] = int(time.time())
                state["reminder_sent"] = False

            t = state.get("last_study_time", 0)

            with psycopg2.connect(DATABASE_URL, sslmode='require') as conn:
                with conn.cursor() as cursor:
                    cursor.execute("""
                        INSERT INTO users (user_id, state, last_study_time) VALUES (%s, %s, %s)
                        ON CONFLICT (user_id) DO UPDATE SET state = EXCLUDED.state, last_study_time = EXCLUDED.last_study_time
                    """, (user_id, json.dumps(state), t))
                conn.commit()
        except Exception as e: print(f"Lỗi lưu DB: {e}")

# --- AI HELPERS ---

def ai_generate_example(word_data):
    """Dùng AI tạo câu ví dụ mới phong phú hơn (nhưng vẫn phải có Pinyin/Dịch)."""
    hanzi = word_data['Hán tự']
    meaning = word_data['Nghĩa']
    
    prompt = f"""
    Hãy tạo một câu ví dụ tiếng Trung ngắn gọn, đơn giản (HSK 2) cho từ: "{hanzi}" (Nghĩa: {meaning}).
    Yêu cầu bắt buộc:
    1. Câu ví dụ phải khác với câu mẫu: "{word_data['Ví dụ']}".
    2. Phải cung cấp đủ 3 thành phần: Câu chữ Hán, Pinyin, Dịch tiếng Việt.
    3. Trả về định dạng JSON duy nhất: {{"han": "...", "pinyin": "...", "viet": "..."}}
    """
    try:
        response = model.generate_content(prompt)
        txt = response.text.strip()
        if "
