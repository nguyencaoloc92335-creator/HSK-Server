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
        prompt = f"""
        Bạn là trợ lý ảo dạy tiếng Trung HSK.
        Ngữ cảnh: {context}
        User nhắn: "{text}"
        Nhiệm vụ:
        1. Hiểu ý định user.
        2. Trả lời ngắn gọn (dưới 50 từ).
        3. Hướng dẫn họ dùng lệnh đúng (Ví dụ: 'Bắt đầu', 'Hiểu', 'Tiếp') nếu họ đang lạc đề.
        """
        return model.generate_content(prompt).text.strip()
    except: return "Gõ 'Hướng dẫn' để xem menu nhé."

def ai_generate_example_smart(word_data: dict) -> dict:
    hanzi = word_data.get('Hán tự', '')
    meaning = word_data.get('Nghĩa', '')
    backup = {"han": word_data.get('Ví dụ', ''), "pinyin": word_data.get('Ví dụ Pinyin', ''), "viet": word_data.get('Dịch câu', '')}
    try:
        prompt = f"Tạo ví dụ HSK2 đơn giản cho từ: {hanzi} ({meaning}). Trả về JSON: {{\"han\": \"...\", \"pinyin\": \"...\", \"viet\": \"...\"}}"
        res = model.generate_content(prompt).text.strip()
        match = re.search(r'\{.*\}', res, re.DOTALL)
        if match: return json.loads(match.group())
        return backup
    except: return backup

# --- HELPER ---
def get_ts(): return int(time.time())
def get_vn_time_str(ts=None):
    if ts is None: ts = time.time()
    return datetime.fromtimestamp(ts, timezone(timedelta(hours=7))).strftime("%H:%M")

def draw_progress_bar(current, total, length=8):
    if total == 0: return "[░░░░░░░░]"
    percent = current / total
    filled_length = int(length * percent)
    bar = "▓" * filled_length + "░" * (length - filled_length)
    return f"{bar}"

def send_fb(uid, txt):
    try:
        r = requests.post("https://graph.facebook.com/v16.0/me/messages", 
            params={"access_token": PAGE_ACCESS_TOKEN},
            json={"recipient": {"id": uid}, "message": {"text": txt}},
            timeout=10)
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
    except: pass
    finally:
        if os.path.exists(filename): os.remove(filename)

# --- STATE MANAGER ---
def get_state(uid):
    if uid in USER_CACHE: return USER_CACHE[uid]
    s = {
        "user_id": uid, 
        "mode": "IDLE", 
        "learned": [], 
        "session": [], 
        "next_time": 0, 
        "waiting": False,
        "last_interaction": 0,
        "reminder_sent": False,
        "quiz_state": {
            "level": 0,
            "queue": [],
            "failed": [],
            "current_idx": -1,
            "current_question": None
        },
        "current_word_char": ""
    }
    if db_pool:
        conn = None
        try:
            conn = db_pool.getconn()
            with conn.cursor() as cur:
                cur.execute("CREATE TABLE IF NOT EXISTS users (user_id VARCHAR(50) PRIMARY KEY, state JSONB)")
                cur.execute("SELECT state FROM users WHERE user_id = %s", (uid,))
                row = cur.fetchone()
                if row: 
                    db_s = row[0]
                    if "quiz_state" not in db_s: db_s["quiz_state"] = s["quiz_state"]
                    s.update(db_s)
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

def send_guide_message(user_id):
    guide = (
        "📚 **HƯỚNG DẪN**\n\n"
        "🔹 `Bắt đầu`: Học từ mới.\n"
        "🔹 `Hiểu`: Xác nhận đã học (đếm 10p).\n"
        "🔹 `Tiếp`: Bỏ qua chờ, học tiếp.\n"
        "🔹 `Thi`: Sau 6 từ sẽ thi 3 cấp độ.\n"
        "🔹 `Học lại`: Xóa dữ liệu.\n"
    )
    send_fb(user_id, guide)

# --- CORE LOGIC (LEARNING) ---

def send_next_auto_word(uid, state):
    current_hour = datetime.now(timezone(timedelta(hours=7))).hour
    if 0 <= current_hour < 6: return

    # Đủ 6 từ -> Vào Quiz
    if len(state["session"]) >= 6:
        start_advanced_quiz(uid, state)
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
    state["current_word_char"] = word['Hán tự']
    
    ex = ai_generate_example_smart(word)
    
    session_prog = f"{len(state['session'])}/6"
    total_prog = f"{len(state['learned'])}/{len(HSK_DATA)}"
    
    msg = (f"🔔 **TỪ MỚI** ({session_prog} - Tổng: {total_prog})\n\n"
           f"🇨🇳 **{word['Hán tự']}** ({word['Pinyin']})\n"
           f"🇻🇳 Nghĩa: {word['Nghĩa']}\n"
           f"----------------\n"
           f"Ví dụ: {ex['han']}\n{ex['pinyin']}\n👉 {ex['viet']}\n\n"
           f"👉 Gõ lại từ **{word['Hán tự']}** để xác nhận.")
    send_fb(uid, msg)
    
    threading.Thread(target=send_audio_fb, args=(uid, word['Hán tự'])).start()
    def send_ex_audio(): time.sleep(2); send_audio_fb(uid, ex['han'])
    threading.Thread(target=send_ex_audio).start()
    
    state["waiting"] = True 
    state["next_time"] = 0 
    state["last_interaction"] = get_ts()
    state["reminder_sent"] = False
    save_state(uid, state)

def send_card(uid, state):
    send_next_auto_word(uid, state)

def cmd_confirm(uid, state, text_msg):
    # Logic xác nhận đã hiểu
    current_char = state.get("current_word_char", "").strip()
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

# --- ADVANCED QUIZ LOGIC (BATCH MODE - FIXED) ---

def start_advanced_quiz(uid, state):
    state["mode"] = "QUIZ"
    
    # Tạo danh sách thứ tự thi (0 đến 5)
    indices = list(range(len(state["session"])))
    random.shuffle(indices)
    
    state["quiz_state"] = {
        "level": 1,
        "queue": indices, # Danh sách cần hỏi
        "failed": [],     # Danh sách làm sai
        "current_idx": -1, # Con trỏ (chưa bắt đầu)
        "current_question": None
    }
    
    state["waiting"] = False
    state["next_time"] = 0
    save_state(uid, state)
    
    send_fb(uid, "🛑 **BẮT ĐẦU KIỂM TRA (3 CẤP ĐỘ)**\nQuy tắc: Phải trả lời đúng hết 6 từ của cấp này mới được qua cấp sau.\n\n🚀 **CẤP 1: NHÌN HÁN TỰ -> ĐOÁN NGHĨA**")
    time.sleep(1)
    # Kích hoạt câu hỏi đầu tiên
    send_next_batch_question(uid, state)

def send_next_batch_question(uid, state):
    qs = state["quiz_state"]
    
    # Tăng con trỏ lên
    qs["current_idx"] += 1
    
    # Kiểm tra xem đã đi hết hàng đợi chưa
    if qs["current_idx"] >= len(qs["queue"]):
        # Đã hết hàng đợi. Kiểm tra xem có từ nào làm sai không?
        if len(qs["failed"]) > 0:
            send_fb(uid, f"⚠️ Bạn làm sai {len(qs['failed'])} từ. Chúng ta sẽ ôn lại ngay bây giờ.")
            
            # Đưa danh sách sai vào làm hàng đợi mới
            qs["queue"] = qs["failed"][:]
            random.shuffle(qs["queue"])
            
            # Reset trạng thái cho vòng lặp lại
            qs["failed"] = []
            qs["current_idx"] = 0
            
            save_state(uid, state)
            time.sleep(1)
            send_batch_question_content(uid, state)
        else:
            # Đúng hết -> Qua Level
            next_level = qs["level"] + 1
            if next_level > 3:
                finish_session(uid, state)
            else:
                qs["level"] = next_level
                # Reset hàng đợi full 6 từ cho level mới
                qs["queue"] = list(range(len(state["session"])))
                random.shuffle(qs["queue"])
                qs["failed"] = []
                qs["current_idx"] = 0
                
                level_names = {
                    2: "CẤP 2: NHÌN NGHĨA -> VIẾT HÁN TỰ",
                    3: "CẤP 3: NGHE -> VIẾT HÁN TỰ"
                }
                send_fb(uid, f"🎉 Tuyệt vời! Qua màn.\n\n🚀 **{level_names.get(next_level, '')}**")
                save_state(uid, state)
                time.sleep(2)
                send_batch_question_content(uid, state)
    else:
        # Vẫn còn câu hỏi trong hàng đợi -> Gửi tiếp
        send_batch_question_content(uid, state)

def send_batch_question_content(uid, state):
    qs = state["quiz_state"]
    
    # Lấy từ vựng dựa trên con trỏ hiện tại
    word_idx = qs["queue"][qs["current_idx"]]
    word = state["session"][word_idx]
    level = qs["level"]
    
    prog = f"({qs['current_idx'] + 1}/{len(qs['queue'])})"
    msg = ""
    
    if level == 1:
        msg = f"🔥 {prog} Nghĩa của từ **[{word['Hán tự']}]** là gì?"
        qs["current_question"] = {"type": "HAN_VIET", "answer": word["Nghĩa"]}
    elif level == 2:
        msg = f"🔥 {prog} Viết chữ Hán cho từ **'{word['Nghĩa']}'**:"
        qs["current_question"] = {"type": "VIET_HAN", "answer": word["Hán tự"]}
    elif level == 3:
        msg = f"🔥 {prog} Nghe và gõ lại từ (Audio đang gửi...):"
        qs["current_question"] = {"type": "LISTEN", "answer": word["Hán tự"]}
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
        # Chấp nhận đúng 1 từ khóa trong nghĩa
        if any(k.strip() in usr for k in ans.split(",")): is_correct = True
    elif target["type"] in ["VIET_HAN", "LISTEN"]:
        # Phải đúng chữ Hán
        if ans in usr: is_correct = True
        
    if is_correct:
        send_fb(uid, "✅ Chính xác!")
    else:
        # Nếu sai -> Ghi vào sổ nợ (failed list)
        word_idx = qs["queue"][qs["current_idx"]]
        if word_idx not in qs["failed"]:
            qs["failed"].append(word_idx)
        send_fb(uid, f"❌ Sai rồi. Đáp án: {target['answer']}")
        
    save_state(uid, state)
    time.sleep(1)
    
    # Gọi hàm chuyển tiếp
    send_next_batch_question(uid, state)

def finish_session(uid, state):
    send_fb(uid, "🏆 XUẤT SẮC! Hoàn thành bài thi.\nNghỉ 10 phút nhé.")
    
    state["mode"] = "AUTO"
    state["session"] = [] 
    state["next_time"] = get_ts() + 540
    state["waiting"] = False
    send_fb(uid, f"⏰ Hẹn {get_vn_time_str(state['next_time'])}.")
    save_state(uid, state)

# --- MESSAGE PROCESSOR ---

def process(uid, text):
    state = get_state(uid)
    msg = text.lower().strip()
    state["last_interaction"] = get_ts()
    
    # 1. LỆNH CƠ BẢN (Ưu tiên cao nhất)
    if msg == "reset":
        state = {"user_id": uid, "mode": "IDLE", "learned": [], "session": [], "next_time": 0, "waiting": False}
        save_state(uid, state)
        send_fb(uid, "Đã reset.")
        return

    if any(c in msg for c in ["hướng dẫn", "menu", "help"]):
        send_guide_message(uid)
        return

    if any(c in msg for c in ['bắt đầu', 'start', 'chào buổi sáng']):
        state["mode"] = "AUTO"
        state["session"] = []
        send_fb(uid, "🚀 Bắt đầu!")
        send_card(uid, state)
        return

    if "dừng" in msg or "stop" in msg:
        state["mode"] = "IDLE"
        save_state(uid, state)
        send_fb(uid, "Đã dừng.")
        return

    # 2. XỬ LÝ THEO CHẾ ĐỘ
    if state["mode"] == "AUTO":
        if state["waiting"]:
            # Đang đợi confirm từ mới
            cmd_confirm(uid, state, text)
        else:
            # Đang đếm ngược
            if "tiếp" in msg:
                send_card(uid, state)
            elif "bao lâu" in msg:
                rem = state["next_time"] - get_ts()
                if rem > 0:
                    send_fb(uid, f"⏳ Còn {rem//60} phút.")
                else:
                    send_card(uid, state)
            else:
                reply = ai_smart_reply(text, "User đang chờ timer")
                send_fb(uid, reply)

    elif state["mode"] == "QUIZ":
        check_quiz_answer(uid, state, text)
        
    else:
        reply = ai_smart_reply(text, "User đang rảnh")
        send_fb(uid, reply)

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
                        
                        if state["mode"] == "AUTO" and state["waiting"]:
                            last_act = state.get("last_interaction", 0)
                            if (now - last_act > 1800) and not state.get("reminder_sent", False):
                                send_fb(uid, "🔔 Học xong chưa? Gõ lại từ để tiếp tục nhé!")
                                state["reminder_sent"] = True
                                save_state(uid, state)
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
