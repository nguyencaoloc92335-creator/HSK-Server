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
from gtts import gTTS  # <--- THÊM THƯ VIỆN NÀY

# --- CẤU HÌNH ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(message)s')
logger = logging.getLogger(__name__)

# Thông tin cấu hình
PAGE_ACCESS_TOKEN = "EAAbQQNNSmSMBQOLS4eBsN7f8vUdGyOsxupjsjl3aJyU6w9udeAVEFRdtLkikidUowCEYxgjiZBvCZBM8ZCISVqrG7crVqMjUCYE0HNixNuQIrdgaPrTJd0w78ZAZC7lEnnyrSTlTZCc0UxZAkYQ0ZCF8hh8A6JskvPmZCNkm5ZBprIAEYQcKAWqXCBakZAOcE7Dli4be4FEeAZDZD"
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

USER_CACHE = {} 

app = FastAPI()

# --- AI SETUP ---
try:
    genai.configure(api_key=GEMINI_API_KEY)
    model = genai.GenerativeModel('gemini-1.5-flash')
except: model = None

def ai_smart_reply(text, context):
    if not model: return "Gõ 'Bắt đầu' để học nhé."
    try:
        prompt = f"User nói: '{text}'. Ngữ cảnh: {context}. Hãy trả lời ngắn gọn tiếng Việt, thân thiện và hướng dẫn họ dùng lệnh đúng."
        return model.generate_content(prompt).text.strip()
    except: return "Gõ 'Hướng dẫn' để xem menu nhé."

def ai_generate_example_smart(word_data: dict) -> dict:
    hanzi = word_data.get('Hán tự', '')
    meaning = word_data.get('Nghĩa', '')
    backup = {
        "han": word_data.get('Ví dụ', '...'),
        "pinyin": word_data.get('Ví dụ Pinyin', '...'),
        "viet": word_data.get('Dịch câu', '...')
    }
    try:
        prompt = f"""
        Tạo ví dụ HSK2 cho từ: {hanzi} ({meaning}).
        Trả về JSON: {{"han": "...", "pinyin": "...", "viet": "..."}}
        """
        response = model.generate_content(prompt)
        text = response.text.strip()
        match = re.search(r'\{.*\}', text, re.DOTALL)
        if match: return json.loads(match.group())
        return backup
    except:
        return backup

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

# --- AUDIO HELPER (MỚI) ---
def send_audio_fb(user_id, text_content):
    """Tạo file MP3 từ text và gửi sang Facebook"""
    if not text_content: return
    
    filename = f"voice_{user_id}_{int(time.time())}.mp3"
    try:
        # 1. Tạo file audio
        tts = gTTS(text=text_content, lang='zh-cn')
        tts.save(filename)
        
        # 2. Upload file lên Facebook
        url = f"https://graph.facebook.com/v16.0/me/messages?access_token={PAGE_ACCESS_TOKEN}"
        
        # Cấu trúc payload gửi file multipart
        data = {
            'recipient': json.dumps({'id': user_id}),
            'message': json.dumps({'attachment': {'type': 'audio', 'payload': {}}})
        }
        
        with open(filename, 'rb') as f:
            files = {'filedata': (filename, f, 'audio/mp3')}
            r = requests.post(url, data=data, files=files, timeout=20)
            
        if r.status_code != 200:
            logger.error(f"Audio Send Error: {r.text}")
        else:
            logger.info(f"Sent audio to {user_id}")
            
    except Exception as e:
        logger.error(f"TTS Error: {e}")
    finally:
        # 3. Dọn dẹp file tạm
        if os.path.exists(filename):
            os.remove(filename)

# --- STATE MANAGER ---
def get_state(uid):
    if uid in USER_CACHE: return USER_CACHE[uid]
    s = {"user_id": uid, "mode": "IDLE", "learned": [], "session": [], "next_time": 0, "waiting": False}
    if db_pool:
        conn = None
        try:
            conn = db_pool.getconn()
            with conn.cursor() as cur:
                cur.execute("CREATE TABLE IF NOT EXISTS users (user_id VARCHAR(50) PRIMARY KEY, state JSONB)")
                cur.execute("SELECT state FROM users WHERE user_id = %s", (uid,))
                row = cur.fetchone()
                if row: s.update(row[0])
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

def send_next_auto_word(uid, state):
    # Kiểm tra giờ ngủ 0h-6h sáng VN
    current_hour = datetime.now(timezone(timedelta(hours=7))).hour
    if 0 <= current_hour < 6: return

    if len(state["session"]) >= 6:
        state["mode"] = "QUIZ"
        state["q_idx"] = 0
        state["q_score"] = 0
        save_state(uid, state)
        send_fb(uid, "⏰ Đã đủ 6 từ! Kiểm tra ngay.")
        send_quiz(uid, state)
        return

    learned = set(state["learned"])
    pool = [w for w in HSK_DATA if w['Hán tự'] not in learned]
    if not pool:
        send_fb(uid, "🎉 Học hết rồi! Reset lại từ đầu.")
        state["learned"] = []
        pool = HSK_DATA
    
    word = random.choice(pool)
    state["session"].append(word)
    state["learned"].append(word['Hán tự'])
    
    # Tạo ví dụ mới bằng AI hoặc lấy sẵn
    ex = ai_generate_example_smart(word)
    
    # Gửi tin nhắn TEXT
    msg = (f"🔔 Từ #{len(state['session'])}\n"
           f"🇨🇳 {word['Hán tự']} ({word['Pinyin']})\n"
           f"🇻🇳 {word['Nghĩa']}\n"
           f"----------------\n"
           f"Ví dụ: {ex['han']}\n{ex['pinyin']}\n👉 {ex['viet']}\n\n"
           f"👉 Gõ 'Hiểu' để bắt đầu tính giờ.")
    send_fb(uid, msg)
    
    # Gửi tin nhắn AUDIO (Chỉ đọc câu ví dụ tiếng Trung)
    # Chạy trên thread riêng để không chặn flow chính
    threading.Thread(target=send_audio_fb, args=(uid, ex['han'])).start()
    
    state["waiting"] = True 
    state["next_time"] = 0 
    save_state(uid, state)

def send_card(uid, state):
    # Wrapper hàm cũ để tương thích
    send_next_auto_word(uid, state)

def send_quiz(uid, state):
    idx = state.get("q_idx", 0)
    if idx >= len(state["session"]):
        send_fb(uid, f"🏆 Kết quả: {state['q_score']}/{len(state['session'])}.\nTiếp tục học từ mới!")
        state["mode"] = "AUTO"
        state["session"] = [] 
        send_card(uid, state) 
        return
    
    w = state["session"][idx]
    send_fb(uid, f"❓ Câu {idx+1}: '{w['Nghĩa']}' là chữ gì?")

def process(uid, text):
    state = get_state(uid)
    msg = text.lower().strip()
    
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

    if state["mode"] == "AUTO":
        if state["waiting"]:
            if any(w in msg for w in ["hiểu", "ok", "rồi", "tiếp", "yes"]):
                now = get_ts()
                next_t = now + 540 # 9 phút
                state["next_time"] = next_t
                state["waiting"] = False
                time_str = get_vn_time_str(next_t)
                send_fb(uid, f"✅ Ok! Từ tiếp theo sẽ đến lúc {time_str} (khoảng 9-10p nữa).")
                save_state(uid, state)
            else:
                send_fb(uid, ai_smart_reply(text, "Đang chờ user gõ 'Hiểu'"))
        else:
            if "tiếp" in msg:
                send_card(uid, state)
            elif "bao lâu" in msg:
                rem = state["next_time"] - get_ts()
                if rem > 0:
                    mins = rem // 60
                    send_fb(uid, f"⏳ Còn {mins} phút.")
                else:
                    send_card(uid, state)
            else:
                send_fb(uid, ai_smart_reply(text, "User đang chờ timer"))

    elif state["mode"] == "QUIZ":
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

# --- CRON JOB TRIGGER ---
@app.get("/trigger_scan")
def trigger_scan():
    try:
        now = get_ts()
        if db_pool:
            conn = db_pool.getconn()
            try:
                with conn.cursor() as cur:
                    cur.execute("CREATE TABLE IF NOT EXISTS users (user_id VARCHAR(50) PRIMARY KEY, state JSONB)")
                    cur.execute("SELECT state FROM users")
                    rows = cur.fetchall()
                    
                    for row in rows:
                        state = row[0]
                        uid = state["user_id"]
                        USER_CACHE[uid] = state
                        
                        if state["mode"] == "AUTO" and not state["waiting"] and state["next_time"] > 0:
                            if now >= state["next_time"]:
                                logger.info(f"CRON: Triggering send for {uid}")
                                send_card(uid, state)
            finally:
                db_pool.putconn(conn)
        return PlainTextResponse("SCAN COMPLETED")
    except Exception as e:
        logger.error(f"Scan Error: {e}")
        return PlainTextResponse(f"ERROR: {e}", status_code=500)

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
def home(): return PlainTextResponse("Server OK")

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)


