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

# Dữ liệu từ vựng (Import từ file hsk2_vocabulary_full.py nếu có)
try:
    from hsk2_vocabulary_full import HSK_DATA
except ImportError:
    HSK_DATA = []  # Fallback nếu thiếu file

# --- CẤU HÌNH (ĐÃ CẬP NHẬT KEY CỦA BẠN) ---

# 1. Facebook Page Access Token
PAGE_ACCESS_TOKEN = "EAAbQQNNSmSMBQFR9N1i6RkU60RRCK6jSjeBvqp5j8iBGxEilLtkVSHr0qdPjVDy8gbyttXqoMJCRfvAFAdZAb328ZBRZBAFyN5qD9b0yzc85tUkKZBCE6k43ZCIYZBlBln970ZBGLZBoZCvYY7iqTzZBXJK7ZCDs1L6hmYhHo8uoKE1VV9ZCYZCNilOSyLkBxL7ZCRZAs9FQpPWzwZDZD"

# 2. Verify Token (Dùng để xác thực Webhook)
VERIFY_TOKEN = "hsk_mat_khau_bi_mat"

# 3. Google Gemini API Key
GEMINI_API_KEY = "AIzaSyB5V6sgqSOZO4v5DyuEZs3msgJqUk54HqQ"

# 4. Database URL (Vẫn lấy từ môi trường vì file cũ của bạn dùng os.environ.get)
# Nếu chạy local, bạn có thể thay thế dòng dưới bằng chuỗi kết nối: "postgresql://user:pass@localhost/dbname"
DATABASE_URL = os.environ.get('DATABASE_URL')

# --- SETUP LOGGING & APP ---
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
app = FastAPI()

# --- SETUP GEMINI AI ---
model = None
if GEMINI_API_KEY:
    try:
        genai.configure(api_key=GEMINI_API_KEY)
        model = genai.GenerativeModel('gemini-1.5-flash') # Dùng bản Flash cho nhanh và tiết kiệm
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
else:
    logger.warning("⚠️ DATABASE_URL chưa được thiết lập!")

USER_CACHE = {}  # Lưu trạng thái tạm thời trong RAM

# --- DATABASE HELPER FUNCTIONS ---
def get_db_conn():
    if db_pool:
        return db_pool.getconn()
    return None

def release_db_conn(conn):
    if db_pool and conn:
        db_pool.putconn(conn)

def init_db():
    """Tạo bảng và nạp dữ liệu mẫu nếu chưa có"""
    conn = get_db_conn()
    if not conn: return
    try:
        with conn.cursor() as cur:
            # Bảng Users
            cur.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    user_id VARCHAR(50) PRIMARY KEY,
                    state JSONB,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            """)
            # Bảng Words
            cur.execute("""
                CREATE TABLE IF NOT EXISTS words (
                    id SERIAL PRIMARY KEY,
                    hanzi VARCHAR(50) UNIQUE NOT NULL,
                    pinyin VARCHAR(100),
                    meaning TEXT,
                    level INT DEFAULT 2
                );
            """)
            # Kiểm tra xem có từ chưa, nếu chưa thì nạp từ HSK_DATA
            cur.execute("SELECT COUNT(*) FROM words")
            if cur.fetchone()[0] == 0 and HSK_DATA:
                logger.info("Seed data to DB...")
                # Xử lý an toàn dữ liệu đầu vào
                valid_data = [x for x in HSK_DATA if 'Hán tự' in x and 'Pinyin' in x and 'Nghĩa' in x]
                if valid_data:
                    args_str = ','.join(cur.mogrify("(%s,%s,%s)", (x['Hán tự'], x['Pinyin'], x['Nghĩa'])).decode('utf-8') for x in valid_data)
                    cur.execute("INSERT INTO words (hanzi, pinyin, meaning) VALUES " + args_str)
        conn.commit()
    except Exception as e:
        logger.error(f"Init DB Error: {e}")
        conn.rollback()
    finally:
        release_db_conn(conn)

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
    except Exception as e:
        logger.error(f"Get Words Err: {e}")
        return []
    finally:
        release_db_conn(conn)

def get_total_words_count():
    conn = get_db_conn()
    if not conn: return 0
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM words")
            return cur.fetchone()[0]
    finally:
        release_db_conn(conn)

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

def delete_word_from_db(hanzi):
    conn = get_db_conn()
    if not conn: return False
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM words WHERE hanzi = %s", (hanzi,))
        conn.commit()
        return True
    except: return False
    finally: release_db_conn(conn)

# --- AI HELPER FUNCTIONS ---

def ai_parse_command(text):
    if not model: return None
    try:
        prompt = f"""
        Phân tích câu lệnh: "{text}".
        Nhiệm vụ: Trích xuất hành động (ADD/DELETE), Hán tự, Pinyin (nếu có), Nghĩa (nếu có).
        Trả về JSON thuần túy (không markdown).
        Ví dụ: "Thêm từ 猫 pinyin māo nghĩa con mèo" -> {{"action": "ADD", "hanzi": "猫", "pinyin": "māo", "meaning": "con mèo"}}
        """
        res = model.generate_content(prompt).text.strip()
        res = res.replace('```json', '').replace('```', '')
        return json.loads(res)
    except Exception as e:
        logger.error(f"AI Parse Err: {e}")
        return None

def ai_smart_reply(text, context):
    if not model: return "Gõ 'Bắt đầu' để học nhé."
    try:
        prompt = f"""
        Bạn là trợ lý HSK. Ngữ cảnh: {context}. User: "{text}".
        Trả lời ngắn gọn tiếng Việt.
        """
        return model.generate_content(prompt).text.strip()
    except: return "Gõ 'Hướng dẫn' để xem menu."

def ai_generate_example_smart(word_data):
    hanzi = word_data.get('Hán tự', '')
    meaning = word_data.get('Nghĩa', '')
    backup = {"han": f"{hanzi}", "pinyin": "...", "viet": f"{meaning}"}
    if not model: return backup
    try:
        prompt = f"Tạo ví dụ HSK2 đơn giản cho từ: {hanzi} ({meaning}). Trả về JSON: {{\"han\": \"...\", \"pinyin\": \"...\", \"viet\": \"...\"}} (Không markdown)"
        res = model.generate_content(prompt).text.strip()
        match = re.search(r'\{.*\}', res, re.DOTALL)
        if match: return json.loads(match.group())
        return backup
    except: return backup

# --- UTILS ---
def get_ts(): return int(time.time())
def get_vn_time_str(ts=None):
    if ts is None: ts = time.time()
    return datetime.fromtimestamp(ts, timezone(timedelta(hours=7))).strftime("%H:%M")

def send_fb(uid, txt):
    try:
        r = requests.post("https://graph.facebook.com/v16.0/me/messages", 
            params={"access_token": PAGE_ACCESS_TOKEN},
            json={"recipient": {"id": uid}, "message": {"text": txt}}, timeout=10)
        
        # --- ĐOẠN MỚI THÊM ĐỂ CHECK LỖI ---
        if r.status_code != 200:
            logger.error(f"❌ LỖI FACEBOOK: {r.text}") # In ra lỗi cụ thể
        else:
            logger.info(f"✅ Đã gửi tin nhắn cho: {uid}")
        # ----------------------------------
            
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
    except Exception as e:
        logger.error(f"Audio Err: {e}")
    finally:
        if os.path.exists(filename): os.remove(filename)

# --- STATE MANAGER ---
def get_state(uid):
    if uid in USER_CACHE: return USER_CACHE[uid]
    s = {"user_id": uid, "mode": "IDLE", "learned": [], "session": [], "next_time": 0, "waiting": False, "last_interaction": 0, "reminder_sent": False, "quiz_state": {"word_idx": 0, "level": 0, "current_question": None}, "current_word_char": ""}
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
            except Exception as e: logger.error(f"Get State Err: {e}")
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
            except Exception as e: logger.error(f"Save State Err: {e}")
            finally: release_db_conn(conn)

def send_guide_message(user_id):
    guide = (
        "📚 **HƯỚNG DẪN**\n"
        "🔹 `Bắt đầu`: Học từ mới.\n"
        "🔹 `Hiểu`: Xác nhận đã học (đếm 9p).\n"
        "🔹 `Tiếp`: Bỏ qua chờ, học tiếp.\n"
        "🔹 `Học lại`: Xóa dữ liệu cũ.\n"
    )
    send_fb(user_id, guide)

# --- CORE LOGIC ---

def send_next_auto_word(uid, state):
    # Không gửi tin nhắn từ 0h - 6h sáng
    if 0 <= datetime.now(timezone(timedelta(hours=7))).hour < 6: return
    
    if len(state["session"]) >= 6:
        start_advanced_quiz(uid, state)
        return

    # LẤY TỪ DB
    learned = state.get("learned", [])
    new_words = get_random_words_from_db(learned, 1)
    
    if not new_words:
        send_fb(uid, "🎉 Đã học hết từ vựng trong kho! Reset lại nhé.")
        state["learned"] = []
        new_words = get_random_words_from_db([], 1)
        if not new_words:
            send_fb(uid, "⚠️ Kho từ vựng trống. Vui lòng thêm từ.")
            return
    
    word = new_words[0]
    state["session"].append(word)
    state["learned"].append(word['Hán tự'])
    state["current_word_char"] = word['Hán tự']
    
    ex = ai_generate_example_smart(word)
    total_count = get_total_words_count()
    
    msg = (f"🔔 **TỪ MỚI** ({len(state['session'])}/6 | Tổng: {len(state['learned'])}/{total_count})\n\n"
           f"🇨🇳 **{word['Hán tự']}** ({word['Pinyin']})\n"
           f"🇻🇳 Nghĩa: {word['Nghĩa']}\n"
           f"----------------\n"
           f"Ví dụ: {ex['han']}\n{ex['pinyin']}\n👉 {ex['viet']}\n\n"
           f"👉 Gõ lại từ **{word['Hán tự']}** để xác nhận.")
    send_fb(uid, msg)
    
    threading.Thread(target=send_audio_fb, args=(uid, word['Hán tự'])).start()
    def send_ex(): time.sleep(2); send_audio_fb(uid, ex['han'])
    threading.Thread(target=send_ex).start()
    
    state["waiting"] = True; state["next_time"] = 0; state["last_interaction"] = get_ts()
    save_state(uid, state)

def send_card(uid, state):
    send_next_auto_word(uid, state)

def cmd_confirm(uid, state, text_msg):
    current_char = state.get("current_word_char", "").strip()
    # Chấp nhận gõ đúng từ hoặc các lệnh xác nhận
    is_correct = (current_char and current_char in text_msg) or any(w in text_msg.lower() for w in ["hiểu", "ok", "tiếp", "yes"])
    
    if is_correct:
        if len(state["session"]) >= 6:
            start_advanced_quiz(uid, state)
        else:
            now = get_ts()
            next_t = now + 540 # 9 phút
            state["next_time"] = next_t
            state["waiting"] = False
            state["reminder_sent"] = False
            send_fb(uid, f"✅ Đã xác nhận. Hẹn {get_vn_time_str(next_t)} gửi tiếp.")
            save_state(uid, state)
    else:
        send_fb(uid, f"⚠️ Hãy gõ lại từ **{current_char}** để ghi nhớ mặt chữ nhé!")

# --- QUIZ LOGIC (BATCH MODE - 3 LEVELS) ---

def start_advanced_quiz(uid, state):
    state["mode"] = "QUIZ"
    indices = list(range(len(state["session"])))
    random.shuffle(indices)
    state["quiz_state"] = {
        "level": 1,
        "queue": indices, 
        "failed": [],     
        "current_idx": -1, 
        "current_question": None
    }
    state["waiting"] = False
    state["next_time"] = 0
    save_state(uid, state)
    send_fb(uid, "🛑 **KIỂM TRA 3 CẤP ĐỘ**\nQuy tắc: Đúng 100% mới qua màn.\n\n🚀 **CẤP 1: NHÌN HÁN TỰ -> ĐOÁN NGHĨA**")
    time.sleep(1)
    send_next_batch_question(uid, state)

def send_next_batch_question(uid, state):
    qs = state["quiz_state"]
    qs["current_idx"] += 1
    
    if qs["current_idx"] >= len(qs["queue"]):
        if len(qs["failed"]) > 0:
            send_fb(uid, f"⚠️ Sai {len(qs['failed'])} từ. Ôn lại ngay.")
            qs["queue"] = qs["failed"][:] 
            random.shuffle(qs["queue"])   
            qs["failed"] = []             
            qs["current_idx"] = 0         
            save_state(uid, state)
            time.sleep(1)
            send_batch_question_content(uid, state)
        else:
            next_level = qs["level"] + 1
            if next_level > 3:
                finish_session(uid, state)
            else:
                qs["level"] = next_level
                qs["queue"] = list(range(len(state["session"]))) 
                random.shuffle(qs["queue"])
                qs["failed"] = []
                qs["current_idx"] = 0
                level_names = {2: "CẤP 2: NHÌN NGHĨA -> VIẾT HÁN TỰ", 3: "CẤP 3: NGHE TỪ VỰNG -> VIẾT HÁN TỰ"}
                send_fb(uid, f"🎉 Xuất sắc! Qua màn.\n\n🚀 **{level_names.get(next_level, '')}**")
                save_state(uid, state)
                time.sleep(2)
                send_batch_question_content(uid, state)
    else:
        send_batch_question_content(uid, state)

def send_batch_question_content(uid, state):
    qs = state["quiz_state"]
    word_idx = qs["queue"][qs["current_idx"]]
    if word_idx >= len(state["session"]):
        qs["current_idx"] += 1
        send_next_batch_question(uid, state)
        return

    word = state["session"][word_idx]
    level = qs["level"]
    prog = f"({qs['current_idx'] + 1}/{len(qs['queue'])})"
    
    if level == 1:
        msg = f"🔥 {prog} Nghĩa của từ **[{word['Hán tự']}]** là gì?"
        qs["current_question"] = {"type": "HAN_VIET", "answer": word["Nghĩa"]}
    elif level == 2:
        msg = f"🔥 {prog} Viết chữ Hán cho từ **'{word['Nghĩa']}'**:"
        qs["current_question"] = {"type": "VIET_HAN", "answer": word["Hán tự"]}
    elif level == 3:
        msg = f"🔥 {prog} Nghe và gõ lại từ (Audio đang gửi...):"
        qs["current_question"] = {"type": "LISTEN_WRITE", "answer": word["Hán tự"]}
        threading.Thread(target=send_audio_fb, args=(uid, word['Hán tự'])).start()

    send_fb(uid, msg)
    save_state(uid, state)

def check_quiz_answer(uid, state, text):
    qs = state["quiz_state"]
    target = qs.get("current_question")
    if not target: return

    is_correct = False
    ans = target["answer"].lower().strip()
    usr = text.lower().strip().replace(".", "").replace("!", "")
    
    if target["type"] == "HAN_VIET":
        if any(k.strip() in usr for k in ans.split(",")): is_correct = True
    elif target["type"] in ["VIET_HAN", "LISTEN_WRITE"]:
        if ans in usr: is_correct = True
        
    if is_correct:
        send_fb(uid, "✅ Chính xác!")
    else:
        word_idx = qs["queue"][qs["current_idx"]]
        if word_idx not in qs["failed"]: qs["failed"].append(word_idx)
        send_fb(uid, f"❌ Sai rồi. Đáp án: {target['answer']}")
        
    save_state(uid, state)
    time.sleep(1)
    send_next_batch_question(uid, state)

def finish_session(uid, state):
    send_fb(uid, "🏆 Hoàn thành bài thi! Nghỉ 10 phút nhé.")
    state["mode"] = "AUTO"
    state["session"] = [] 
    state["next_time"] = get_ts() + 600 # 10 phút
    state["waiting"] = False 
    send_fb(uid, f"⏰ Hẹn {get_vn_time_str(state['next_time'])}.")
    save_state(uid, state)

# --- MESSAGE ROUTER ---
def process(uid, text):
    state = get_state(uid)
    msg = text.lower().strip()
    state["last_interaction"] = get_ts()

    # 1. QUẢN LÝ TỪ VỰNG (AI Parsing)
    if "thêm từ" in msg or "xóa từ" in msg:
        parsed = ai_parse_command(text)
        if parsed:
            if parsed.get('action') == 'ADD':
                if add_word_to_db(parsed.get('hanzi'), parsed.get('pinyin',''), parsed.get('meaning','')):
                    send_fb(uid, f"✅ Đã thêm: {parsed.get('hanzi')}")
                else: send_fb(uid, "❌ Lỗi: Từ đã có hoặc lỗi DB.")
            elif parsed.get('action') == 'DELETE':
                if delete_word_from_db(parsed.get('hanzi')):
                    send_fb(uid, f"🗑️ Đã xóa: {parsed.get('hanzi')}")
                else: send_fb(uid, "❌ Lỗi xóa.")
        else:
            send_fb(uid, "⚠️ Lỗi AI. Ví dụ: 'Thêm từ Mèo nghĩa là con mèo'")
        return

    # 2. LOGIC HỌC
    if any(c in msg for c in ['bắt đầu', 'start']):
        state["mode"] = "AUTO"; state["session"] = []
        send_card(uid, state)
        return
        
    if "reset" in msg or "học lại" in msg:
        state = {"user_id": uid, "mode": "IDLE", "learned": [], "session": [], "next_time": 0, "waiting": False, "last_interaction": 0, "reminder_sent": False, "quiz_state": {"word_idx": 0, "level": 0, "current_question": None}, "current_word_char": ""}
        save_state(uid, state)
        send_fb(uid, "🔄 Đã reset.")
        return
        
    if "hướng dẫn" in msg or "menu" in msg:
        send_guide_message(uid)
        return

    if state["mode"] == "AUTO":
        if state["waiting"]:
            cmd_confirm(uid, state, text)
        else:
            if "tiếp" in msg:
                send_card(uid, state)
            elif "bao lâu" in msg:
                rem = state["next_time"] - get_ts()
                if rem > 0: send_fb(uid, f"⏳ Còn {rem//60} phút.")
                else: send_card(uid, state)
            else:
                send_fb(uid, ai_smart_reply(text, "User chờ timer"))

    elif state["mode"] == "QUIZ":
        check_quiz_answer(uid, state, text)
        
    else:
        send_fb(uid, ai_smart_reply(text, "User rảnh"))

# --- CRON & WEBHOOK ---
@app.on_event("startup")
def startup_event():
    init_db()

@app.get("/trigger_scan")
def trigger_scan():
    try:
        now = get_ts()
        if db_pool:
            conn = db_pool.getconn()
            try:
                with conn.cursor() as cur:
                    cur.execute("SELECT state FROM users")
                    rows = cur.fetchall()
                    for row in rows:
                        state = row[0]
                        if isinstance(state, str): state = json.loads(state)
                        uid = state["user_id"]
                        USER_CACHE[uid] = state 
                        
                        if state["mode"] == "AUTO" and not state["waiting"] and state["next_time"] > 0:
                            if now >= state["next_time"]:
                                send_card(uid, state)
            finally:
                db_pool.putconn(conn)
        return PlainTextResponse("SCAN COMPLETED")
    except Exception as e:
        return PlainTextResponse(f"ERROR: {e}", status_code=500)

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


