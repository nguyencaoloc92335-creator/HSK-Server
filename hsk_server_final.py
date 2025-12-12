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

# --- CẤU HÌNH ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(message)s')
logger = logging.getLogger(__name__)

# Thông tin cấu hình (Giữ nguyên của bạn)
PAGE_ACCESS_TOKEN = "EAAbQQNNSmSMBQKWd5qB15zFMy2KdPm6Ko1rJX6R4ZC3EtnNfvf0gT76V1Qk4l1vflxL1pDVwY8mrgbgAaFFtG6bzcrhJfQ86HdK5v8qZA9zTIge2ZBJcx9oNPOjk1DlQ8juGinZBuah0RDgbCd2vBvlNWr47GVz70BdPNzKRctCGphNJRI0Wm57UwKRmXOZAVfDP7zwZDZD"
VERIFY_TOKEN = "hsk_mat_khau_bi_mat"
GEMINI_API_KEY = "AIzaSyB5V6sgqSOZO4v5DyuEZs3msgJqUk54HqQ"
DATABASE_URL = os.environ.get('DATABASE_URL')

# --- DATA ---
try:
    import hsk2_vocabulary_full as hsk_data
    HSK_DATA = hsk_data.HSK_DATA
except:
    HSK_DATA = [{"Hán tự": "你好", "Pinyin": "nǐhǎo", "Nghĩa": "xin chào", "Ví dụ": "你好", "Ví dụ Pinyin": "nihao", "Dịch câu": "Chào"}]

# --- DATABASE ---
db_pool = None
if DATABASE_URL:
    try:
        db_pool = psycopg2.pool.ThreadedConnectionPool(1, 5, DATABASE_URL, sslmode='require')
        logger.info("DB Connected.")
    except Exception as e:
        logger.error(f"DB Error: {e}")

USER_CACHE = {} # Cache bộ nhớ để chạy nhanh

app = FastAPI()

# --- AI SETUP ---
try:
    genai.configure(api_key=GEMINI_API_KEY)
    model = genai.GenerativeModel('gemini-1.5-flash')
except: model = None

def ai_smart_reply(text, context):
    """AI trả lời khi người dùng chat linh tinh"""
    if not model: return "Gõ 'Bắt đầu' để học nhé."
    try:
        prompt = f"User nói: '{text}'. Ngữ cảnh: {context}. Hãy trả lời ngắn gọn tiếng Việt, thân thiện và hướng dẫn họ dùng lệnh đúng (ví dụ: 'Hiểu', 'Tiếp', 'Bắt đầu')."
        return model.generate_content(prompt).text.strip()
    except: return "Mình chưa hiểu, bạn gõ 'Hướng dẫn' nhé."

# --- HELPER ---
def get_ts(): return int(time.time())
def get_vn_time_str(ts=None):
    if ts is None: ts = time.time()
    return datetime.fromtimestamp(ts, timezone(timedelta(hours=7))).strftime("%H:%M")

def send_fb(uid, txt):
    try:
        r = requests.post("https://graph.facebook.com/v16.0/me/messages", 
            params={"access_token": PAGE_ACCESS_TOKEN},
            json={"recipient": {"id": uid}, "message": {"text": txt}},
            timeout=10)
    except Exception as e: logger.error(f"Send Err: {e}")

# --- STATE MANAGER ---
def get_state(uid):
    if uid in USER_CACHE: return USER_CACHE[uid]
    
    # State mặc định
    s = {"user_id": uid, "mode": "IDLE", "learned": [], "session": [], "next_time": 0, "waiting": False}
    
    # Đọc DB
    if db_pool:
        conn = None
        try:
            conn = db_pool.getconn()
            with conn.cursor() as cur:
                cur.execute("CREATE TABLE IF NOT EXISTS users (user_id VARCHAR(50) PRIMARY KEY, state JSONB)")
                cur.execute("SELECT state FROM users WHERE user_id = %s", (uid,))
                row = cur.fetchone()
                if row: s.update(row[0]) # Update state từ DB
        except Exception as e: logger.error(f"DB Read: {e}")
        finally: 
            if conn: db_pool.putconn(conn)
            
    USER_CACHE[uid] = s
    return s

def save_state(uid, s):
    USER_CACHE[uid] = s
    if db_pool:
        conn = None
        try:
            conn = db_pool.getconn()
            with conn.cursor() as cur:
                cur.execute("INSERT INTO users (user_id, state) VALUES (%s, %s) ON CONFLICT (user_id) DO UPDATE SET state = EXCLUDED.state", (uid, json.dumps(s)))
                conn.commit()
        except: pass
        finally: 
            if conn: db_pool.putconn(conn)

# --- CORE LOGIC ---

def send_card(uid, state):
    # Kiểm tra giờ ngủ 0h-6h
    if 0 <= datetime.now(timezone(timedelta(hours=7))).hour < 6: return

    # Kiểm tra đủ 6 từ -> Quiz
    if len(state["session"]) >= 6:
        state["mode"] = "QUIZ"
        state["q_idx"] = 0
        state["q_score"] = 0
        save_state(uid, state)
        send_fb(uid, "⏰ Đã đủ 6 từ! Kiểm tra ngay.")
        send_quiz(uid, state)
        return

    # Chọn từ chưa học
    learned = set(state["learned"])
    pool = [w for w in HSK_DATA if w['Hán tự'] not in learned]
    if not pool:
        send_fb(uid, "🎉 Học hết rồi! Reset lại từ đầu.")
        state["learned"] = []
        pool = HSK_DATA
    
    word = random.choice(pool)
    state["session"].append(word)
    state["learned"].append(word['Hán tự'])
    
    msg = (f"🔔 Từ #{len(state['session'])}\n"
           f"🇨🇳 {word['Hán tự']} ({word['Pinyin']})\n"
           f"🇻🇳 {word['Nghĩa']}\n"
           f"----------------\n"
           f"Ví dụ: {word.get('Ví dụ','')}\n👉 {word.get('Dịch câu','')}\n\n"
           f"👉 Gõ 'Hiểu' để bắt đầu tính giờ (10p).")
    send_fb(uid, msg)
    
    state["waiting"] = True # Chờ user confirm
    state["next_time"] = 0  # Chưa tính giờ vội
    save_state(uid, state)

def send_quiz(uid, state):
    idx = state.get("q_idx", 0)
    if idx >= len(state["session"]):
        send_fb(uid, f"🏆 Kết quả: {state['q_score']}/{len(state['session'])}.\nTiếp tục học từ mới!")
        state["mode"] = "AUTO"
        state["session"] = [] # Reset session
        send_card(uid, state) # Gửi tiếp luôn
        return
    
    w = state["session"][idx]
    send_fb(uid, f"❓ Câu {idx+1}: '{w['Nghĩa']}' là chữ gì?")

def process(uid, text):
    state = get_state(uid)
    msg = text.lower().strip()
    
    # 1. LỆNH CƠ BẢN
    if msg == "reset":
        state = {"user_id": uid, "mode": "IDLE", "learned": [], "session": [], "next_time": 0, "waiting": False}
        save_state(uid, state)
        send_fb(uid, "Đã reset.")
        return

    if "bắt đầu" in msg or "start" in msg or "chào buổi sáng" in msg:
        state["mode"] = "AUTO"
        state["session"] = []
        send_fb(uid, "🚀 Bắt đầu chế độ 10p/từ.")
        send_card(uid, state)
        return

    if "dừng" in msg or "stop" in msg:
        state["mode"] = "IDLE"
        save_state(uid, state)
        send_fb(uid, "Đã dừng.")
        return

    # 2. XỬ LÝ THEO CHẾ ĐỘ
    if state["mode"] == "AUTO":
        # A. Đang chờ xác nhận "Hiểu"
        if state["waiting"]:
            if any(w in msg for w in ["hiểu", "ok", "rồi", "tiếp", "yes"]):
                # Bắt đầu tính giờ TỪ LÚC NÀY
                now = get_ts()
                next_t = now + 600 # +10 phút
                state["next_time"] = next_t
                state["waiting"] = False
                
                time_str = get_vn_time_str(next_t)
                send_fb(uid, f"✅ Ok! Từ tiếp theo sẽ đến lúc {time_str}.")
                save_state(uid, state)
            else:
                # Chat linh tinh -> AI
                send_fb(uid, ai_smart_reply(text, "Đang chờ user gõ 'Hiểu' để đếm giờ"))
        
        # B. Đang đếm ngược
        else:
            if "tiếp" in msg:
                # User muốn học luôn
                send_card(uid, state)
            elif "bao lâu" in msg or "khi nào" in msg:
                rem = state["next_time"] - get_ts()
                if rem > 0:
                    mins = rem // 60
                    secs = rem % 60
                    send_fb(uid, f"⏳ Còn {mins} phút {secs} giây. Gõ 'Tiếp' để học luôn.")
                else:
                    # Hết giờ mà chưa gửi -> Gửi ngay (Fix lỗi user report)
                    send_fb(uid, "⏰ Đã đến giờ! Gửi ngay đây...")
                    send_card(uid, state)
            else:
                send_fb(uid, ai_smart_reply(text, "User đang chờ timer. Có thể gõ 'Tiếp'"))

    elif state["mode"] == "QUIZ":
        # Check đáp án
        target = state["session"][state["q_idx"]]
        if target['Hán tự'] in text:
            state["q_score"] += 1
            send_fb(uid, "✅ Đúng!")
        else:
            send_fb(uid, f"❌ Sai. Là: {target['Hán tự']}")
        state["q_idx"] += 1
        save_state(uid, state)
        time.sleep(1)
        send_quiz(uid, state)
        
    else:
        send_fb(uid, "Gõ 'Bắt đầu' để học nhé.")

# --- LOOP CHẠY NGẦM ---
def loop():
    logger.info("Loop Running...")
    while True:
        time.sleep(30) # Quét mỗi 30s
        try:
            now = get_ts()
            for uid, s in list(USER_CACHE.items()):
                # Logic: Mode AUTO + Không chờ confirm + Đã quá giờ hẹn
                if s["mode"] == "AUTO" and not s["waiting"] and s["next_time"] > 0:
                    if now >= s["next_time"]:
                        logger.info(f"Auto sending to {uid}")
                        send_card(uid, s)
        except Exception as e: logger.error(f"Loop Err: {e}")

# --- WEBHOOK ---
@app.post("/webhook")
async def wh(req: Request, bg: BackgroundTasks):
    try:
        d = await req.json()
        if 'entry' in d:
            for e in d['entry']:
                for m in e.get('messaging', []):
                    if 'message' in m:
                        bg.add_task(process, m['sender']['id'], m['message'].get('text', ''))
        return PlainTextResponse("EVENT_RECEIVED")
    except: return PlainTextResponse("ERROR")

@app.get("/webhook")
def verify(request: Request):
    if request.query_params.get("hub.verify_token") == VERIFY_TOKEN:
        return PlainTextResponse(request.query_params.get("hub.challenge"))
    return PlainTextResponse("Error", 403)

@app.get("/")
def home(): return PlainTextResponse("OK")

if __name__ == "__main__":
    threading.Thread(target=loop, daemon=True).start()
    uvicorn.run(app, host="0.0.0.0", port=8000)
