import os
import json
import time
import random
import logging
import threading
import re
import requests
from datetime import datetime, timedelta, timezone

# Web Framework
import uvicorn
from fastapi import FastAPI, Request, BackgroundTasks
from fastapi.responses import PlainTextResponse

# AI & Audio
import google.generativeai as genai
from gtts import gTTS

# Database
import psycopg2
from psycopg2 import pool

# Dữ liệu từ vựng (Fallback nếu file import bị lỗi)
try:
    from hsk2_vocabulary_full import HSK_DATA
except ImportError:
    HSK_DATA = []

# --- CẤU HÌNH (TOKEN MỚI NHẤT) ---
PAGE_ACCESS_TOKEN = "EAAbQQNNSmSMBQM5JdL7WYT15Kpz2WUip1Tte40vI75VbtRNm1O1F5mauEtTpzsTvetV9DFjEj4rRsWMUvZB8c2RvwV4FIhX0ky4bjoup8vjJrhyjiUPgUCpR0Gkg1UDxEiorU6C5LORUGwhBrRBIvRL7a8WQmtoafKpaxRkgjeZCfWQZBsqGZBNxEMoUuaFclIqWkwZDZD"
VERIFY_TOKEN = "hsk_mat_khau_bi_mat"
GEMINI_API_KEY = "AIzaSyB5V6sgqSOZO4v5DyuEZs3msgJqUk54HqQ"
DATABASE_URL = os.environ.get('DATABASE_URL')

# --- SETUP LOGGING & APP ---
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
app = FastAPI()

# --- SETUP AI ---
model = None
if GEMINI_API_KEY:
    try:
        genai.configure(api_key=GEMINI_API_KEY)
        model = genai.GenerativeModel('gemini-1.5-flash')
    except Exception as e:
        logger.error(f"Gemini Config Error: {e}")

# --- SETUP DATABASE ---
db_pool = None
if DATABASE_URL:
    try:
        db_pool = psycopg2.pool.SimpleConnectionPool(1, 20, dsn=DATABASE_URL)
        logger.info("✅ Database connected!")
    except Exception as e:
        logger.error(f"❌ Database connection failed: {e}")

USER_CACHE = {}

# --- DATABASE FUNCTIONS ---
def get_db_conn():
    if db_pool: return db_pool.getconn()
    return None

def release_db_conn(conn):
    if db_pool and conn: db_pool.putconn(conn)

def init_db():
    conn = get_db_conn()
    if not conn: return
    try:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    user_id VARCHAR(50) PRIMARY KEY,
                    state JSONB,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS words (
                    id SERIAL PRIMARY KEY,
                    hanzi VARCHAR(50) UNIQUE NOT NULL,
                    pinyin VARCHAR(100),
                    meaning TEXT,
                    level INT DEFAULT 2
                );
            """)
            # Seed data nếu DB trống
            cur.execute("SELECT COUNT(*) FROM words")
            if cur.fetchone()[0] == 0 and HSK_DATA:
                valid_data = [x for x in HSK_DATA if 'Hán tự' in x]
                if valid_data:
                    args_str = ','.join(cur.mogrify("(%s,%s,%s)", (x['Hán tự'], x['Pinyin'], x['Nghĩa'])).decode('utf-8') for x in valid_data)
                    cur.execute("INSERT INTO words (hanzi, pinyin, meaning) VALUES " + args_str)
        conn.commit()
    except Exception as e:
        logger.error(f"Init DB Error: {e}")
        conn.rollback()
    finally: release_db_conn(conn)

def get_random_words_from_db(exclude_list, count=1):
    conn = get_db_conn()
    if not conn: return []
    try:
        with conn.cursor() as cur:
            if exclude_list:
                query = "SELECT hanzi, pinyin, meaning FROM words WHERE hanzi NOT IN %s ORDER BY RANDOM() LIMIT %s"
                cur.execute(query, (tuple(exclude_list), count))
            else:
                query = "SELECT hanzi, pinyin, meaning FROM words ORDER BY RANDOM() LIMIT %s"
                cur.execute(query, (count,))
            rows = cur.fetchall()
            return [{"Hán tự": r[0], "Pinyin": r[1], "Nghĩa": r[2]} for r in rows]
    except: return []
    finally: release_db_conn(conn)

def get_total_words_count():
    conn = get_db_conn()
    if not conn: return 0
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM words")
            return cur.fetchone()[0]
    finally: release_db_conn(conn)

def add_word_to_db(hanzi, pinyin, meaning):
    conn = get_db_conn()
    if not conn: return False
    try:
        with conn.cursor() as cur:
            cur.execute("INSERT INTO words (hanzi, pinyin, meaning) VALUES (%s, %s, %s) ON CONFLICT (hanzi) DO NOTHING", (hanzi, pinyin, meaning))
        conn.commit()
        return True
    except: return False
    finally: release_db_conn(conn)

# --- AI LOGIC (TRA TỪ & VÍ DỤ) ---

def ai_generate_example_smart(word_data):
    """Tạo câu ví dụ siêu đơn giản."""
    hanzi = word_data.get('Hán tự', '')
    meaning = word_data.get('Nghĩa', '')
    backup = {"han": f"{hanzi}", "pinyin": "...", "viet": f"{meaning}"}
    if not model: return backup
    try:
        prompt = f"""
        Đặt 1 câu tiếng Trung CỰC KỲ ĐƠN GIẢN (HSK 1, <10 từ) dùng từ: {hanzi} ({meaning}).
        Trả JSON: {{"han": "...", "pinyin": "...", "viet": "..."}}
        """
        res = model.generate_content(prompt).text.strip()
        match = re.search(r'\{.*\}', res, re.DOTALL)
        if match: return json.loads(match.group())
        return backup
    except: return backup

def ai_lookup_word(hanzi_input):
    """
    AI đóng vai trò từ điển:
    User đưa "猫" -> AI trả về Pinyin + Nghĩa.
    """
    if not model: return None
    try:
        prompt = f"""
        User muốn thêm từ Hán tự này vào từ điển: "{hanzi_input}".
        Hãy cung cấp Pinyin chuẩn và Nghĩa tiếng Việt thông dụng nhất của nó.
        Trả về JSON duy nhất (không markdown):
        {{"hanzi": "{hanzi_input}", "pinyin": "...", "meaning": "..."}}
        Nếu input không phải tiếng Trung, trả về null.
        """
        res = model.generate_content(prompt).text.strip()
        res = res.replace('```json', '').replace('```', '')
        return json.loads(res)
    except: return None

def ai_smart_reply(text):
    if not model: return "Gõ 'Menu' để xem hướng dẫn."
    try:
        return model.generate_content(f"Bạn là bot học tiếng Trung. User nói: '{text}'. Trả lời ngắn gọn tiếng Việt.").text.strip()
    except: return "Hệ thống đang bận."

# --- UTILS & MESSAGING ---
def get_ts(): return int(time.time())
def get_vn_time_str(ts=None):
    if ts is None: ts = time.time()
    return datetime.fromtimestamp(ts, timezone(timedelta(hours=7))).strftime("%H:%M")

def send_fb(uid, txt):
    try:
        r = requests.post("https://graph.facebook.com/v16.0/me/messages", 
            params={"access_token": PAGE_ACCESS_TOKEN},
            json={"recipient": {"id": uid}, "message": {"text": txt}}, timeout=10)
        if r.status_code != 200: logger.error(f"❌ FB Error: {r.text}")
        else: logger.info(f"✅ Sent msg to {uid}")
    except Exception as e: logger.error(f"Send Err: {e}")

def send_audio_fb(user_id, text_content):
    if not text_content: return
    filename = f"voice_{user_id}_{int(time.time())}.mp3"
    try:
        tts = gTTS(text=text_content, lang='zh-cn')
        tts.save(filename)
        url = f"https://graph.facebook.com/v16.0/me/messages?access_token={PAGE_ACCESS_TOKEN}"
        data = {'recipient': json.dumps({'id': user_id}), 'message': json.dumps({'attachment': {'type': 'audio', 'payload': {}}})}
        with open(filename, 'rb') as f:
            files = {'filedata': (filename, f, 'audio/mp3')}
            requests.post(url, data=data, files=files, timeout=20)
    except Exception as e: logger.error(f"Audio Err: {e}")
    finally:
        if os.path.exists(filename): os.remove(filename)

# --- STATE MANAGER ---
def get_state(uid):
    if uid in USER_CACHE: return USER_CACHE[uid]
    s = {"user_id": uid, "mode": "IDLE", "learned": [], "session": [], "next_time": 0, "waiting": False, "temp_word": None}
    if db_pool:
        conn = get_db_conn()
        if conn:
            try:
                with conn.cursor() as cur:
                    cur.execute("SELECT state FROM users WHERE user_id = %s", (uid,))
                    row = cur.fetchone()
                    if row: 
                        db_s = row[0]
                        if isinstance(db_s, str): db_s = json.loads(db_s)
                        s.update(db_s)
            except: pass
            finally: release_db_conn(conn)
    USER_CACHE[uid] = s
    return s

def save_state(uid, s):
    USER_CACHE[uid] = s
    if db_pool:
        conn = get_db_conn()
        if conn:
            try:
                with conn.cursor() as cur:
                    cur.execute("INSERT INTO users (user_id, state) VALUES (%s, %s) ON CONFLICT (user_id) DO UPDATE SET state = EXCLUDED.state", (uid, json.dumps(s)))
                    conn.commit()
            except: pass
            finally: release_db_conn(conn)

# --- CORE LOGIC ---

def send_next_auto_word(uid, state):
    if 0 <= datetime.now(timezone(timedelta(hours=7))).hour < 6: return
    if len(state["session"]) >= 6:
        start_quiz(uid, state); return

    learned = state.get("learned", [])
    new_words = get_random_words_from_db(learned, 1)
    
    if not new_words:
        send_fb(uid, "🎉 Đã học hết! Reset hoặc thêm từ mới.")
        return
    
    word = new_words[0]
    state["session"].append(word)
    state["learned"].append(word['Hán tự'])
    state["current_word_char"] = word['Hán tự']
    
    ex = ai_generate_example_smart(word)
    total = get_total_words_count()
    
    msg = (f"🔔 **TỪ MỚI** ({len(state['session'])}/6 | Kho: {total})\n\n"
           f"🇨🇳 **{word['Hán tự']}** ({word['Pinyin']})\n"
           f"🇻🇳 Nghĩa: {word['Nghĩa']}\n"
           f"----------------\n"
           f"Ví dụ: {ex['han']}\n{ex['pinyin']}\n👉 {ex['viet']}\n\n"
           f"👉 Gõ lại từ **{word['Hán tự']}** để học.")
    send_fb(uid, msg)
    
    threading.Thread(target=send_audio_fb, args=(uid, word['Hán tự'])).start()
    threading.Thread(target=lambda: (time.sleep(2), send_audio_fb(uid, ex['han']))).start()
    
    state["waiting"] = True; state["next_time"] = 0
    save_state(uid, state)

def start_quiz(uid, state):
    state["mode"] = "QUIZ"
    send_fb(uid, "🛑 **KIỂM TRA**\nDịch từ này sang tiếng Việt:")
    state["quiz_idx"] = 0
    w = state["session"][0]
    send_fb(uid, f"🇨🇳 {w['Hán tự']}")
    save_state(uid, state)

# --- PROCESS MESSAGE (CHỨC NĂNG THÊM TỪ MỚI) ---

def process(uid, text):
    state = get_state(uid)
    msg = text.lower().strip()
    
    # 1. LOGIC THÊM TỪ (Bước 1: Kích hoạt)
    if msg == "thêm từ":
        state["mode"] = "ADD_STEP_1"
        send_fb(uid, "📝 **Thêm từ mới:**\nHãy nhập **Hán tự** bạn muốn thêm (Ví dụ: 猫).")
        save_state(uid, state)
        return

    # 2. LOGIC THÊM TỪ (Bước 2: AI Tra cứu & Hỏi xác nhận)
    if state["mode"] == "ADD_STEP_1":
        # Nếu user muốn hủy
        if msg in ["hủy", "không", "thôi", "cancel", "dừng"]:
            state["mode"] = "IDLE"
            send_fb(uid, "❌ Đã hủy bỏ quá trình thêm từ.")
            save_state(uid, state)
            return

        send_fb(uid, "⏳ Đang tra cứu, đợi chút...")
        analyzed = ai_lookup_word(text) # AI tìm nghĩa và pinyin
        
        if analyzed and analyzed.get('pinyin'):
            state["temp_word"] = analyzed
            state["mode"] = "ADD_STEP_2"
            
            confirm_msg = (
                f"📖 **Kết quả:**\n"
                f"🇨🇳 Hán tự: **{analyzed['hanzi']}**\n"
                f"🔤 Pinyin: {analyzed['pinyin']}\n"
                f"🇻🇳 Nghĩa: {analyzed['meaning']}\n\n"
                f"❓ Bạn có muốn thêm từ này không?\n(Gõ **OK** để lưu, gõ **Không** để hủy)"
            )
            send_fb(uid, confirm_msg)
        else:
            send_fb(uid, "⚠️ AI không nhận diện được từ này. Vui lòng nhập lại hoặc gõ 'Hủy'.")
        
        save_state(uid, state)
        return

    # 3. LOGIC THÊM TỪ (Bước 3: Lưu hoặc Hủy)
    if state["mode"] == "ADD_STEP_2":
        if msg in ["ok", "có", "yes", "lưu", "oke", "uh", "ừ"]:
            data = state.get("temp_word")
            if data:
                success = add_word_to_db(data['hanzi'], data['pinyin'], data['meaning'])
                if success:
                    send_fb(uid, f"✅ Đã thêm **{data['hanzi']}** vào kho!")
                else:
                    send_fb(uid, "⚠️ Từ này đã tồn tại rồi.")
            # Xong việc -> Quay về IDLE
            state["mode"] = "IDLE"
            state["temp_word"] = None
        else:
            # Nếu user gõ bất cứ thứ gì không phải đồng ý -> Coi như hủy
            send_fb(uid, "❌ Đã ngắt bỏ (Hủy thêm từ). Bot hoạt động bình thường.")
            state["mode"] = "IDLE"
            state["temp_word"] = None
            
        save_state(uid, state)
        return

    # 4. CÁC LỆNH KHÁC
    if msg in ["bắt đầu", "start"]:
        state["mode"] = "AUTO"; state["session"] = []
        send_next_auto_word(uid, state)
        return

    if msg in ["reset", "học lại"]:
        state = {"user_id": uid, "mode": "IDLE", "learned": [], "session": [], "next_time": 0, "waiting": False}
        save_state(uid, state)
        send_fb(uid, "🔄 Đã reset.")
        return

    # 5. AUTO MODE
    if state["mode"] == "AUTO":
        if state["waiting"]:
            target = state.get("current_word_char", "")
            if (target in text) or (msg in ["hiểu", "ok", "tiếp"]):
                now = get_ts()
                state["next_time"] = now + 540
                state["waiting"] = False
                send_fb(uid, f"✅ Đã thuộc. Hẹn 9 phút nữa.")
                save_state(uid, state)
            else:
                send_fb(uid, f"⚠️ Gõ lại từ **{target}** để nhớ nhé.")
        else:
            if "tiếp" in msg: send_next_auto_word(uid, state)
            else: send_fb(uid, ai_smart_reply(text))

    # 6. QUIZ MODE
    elif state["mode"] == "QUIZ":
        idx = state.get("quiz_idx", 0)
        w = state["session"][idx]
        if w['Nghĩa'].lower() in msg:
            send_fb(uid, "✅ Đúng! Hết bài. Gõ 'Bắt đầu' để học tiếp.")
            state["mode"] = "IDLE"
            state["session"] = []
        else:
            send_fb(uid, f"❌ Sai. Đáp án: {w['Nghĩa']}")
        save_state(uid, state)
        
    else:
        # Chat tự do
        send_fb(uid, ai_smart_reply(text))

# --- WEBHOOK & TRIGGER ---
@app.on_event("startup")
def startup(): init_db()

@app.get("/trigger_scan")
def trigger_scan():
    now = get_ts()
    if db_pool:
        conn = get_db_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT state FROM users")
                rows = cur.fetchall()
                for row in rows:
                    state = row[0]
                    if isinstance(state, str): state = json.loads(state)
                    uid = state["user_id"]
                    if state["mode"] == "AUTO" and not state["waiting"] and state["next_time"] > 0:
                        if now >= state["next_time"]:
                            USER_CACHE[uid] = state
                            send_next_auto_word(uid, state)
        finally: release_db_conn(conn)
    return PlainTextResponse("OK")

@app.post("/webhook")
async def webhook(req: Request, bg: BackgroundTasks):
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
def verify(req: Request):
    if req.query_params.get("hub.verify_token") == VERIFY_TOKEN:
        return PlainTextResponse(req.query_params.get("hub.challenge"))
    return PlainTextResponse("Error", 403)

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
