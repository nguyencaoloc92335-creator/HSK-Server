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
        prompt = f"Tạo ví dụ HSK2 đơn giản, ngắn gọn cho từ: {hanzi} ({meaning}). Trả về JSON: {{\"han\": \"...\", \"pinyin\": \"...\", \"viet\": \"...\"}}"
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
            "word_idx": 0,
            "level": 0,
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
                    if "current_word_char" not in db_s: db_s["current_word_char"] = ""
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
        "📚 **HƯỚNG DẪN HỌC TẬP**\n\n"
        "🔹 **Bắt đầu:** Gõ `Bắt đầu` để nhận từ vựng.\n"
        "🔹 **Học từ:** Gõ lại chính xác **từ vựng** để xác nhận.\n"
        "🔹 **Học nhanh:** Gõ `Tiếp` để nhận ngay từ mới.\n"
        "🔹 **Thi:** Đủ 6 từ sẽ có bài kiểm tra 3 cấp độ (Đúng 100% mới qua).\n"
        "🔹 **Lệnh khác:** `Chào buổi sáng`, `Học lại`, `Dừng`.\n"
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
    
    msg = (f"🔔 **TỪ VỰNG MỚI** (Bài: {session_prog} | Tổng: {total_prog})\n\n"
           f"🇨🇳 **{word['Hán tự']}** ({word['Pinyin']})\n"
           f"🇻🇳 Nghĩa: {word['Nghĩa']}\n"
           f"----------------\n"
           f"Ví dụ: {ex['han']}\n{ex['pinyin']}\n👉 {ex['viet']}\n\n"
           f"👉 Hãy gõ lại từ **{word['Hán tự']}** để xác nhận và nghe phát âm.")
    send_fb(uid, msg)
    
    # Gửi audio TỪ VỰNG trước
    threading.Thread(target=send_audio_fb, args=(uid, word['Hán tự'])).start()
    # Sau đó gửi audio VÍ DỤ
    def send_ex_audio():
        time.sleep(2)
        send_audio_fb(uid, ex['han'])
    threading.Thread(target=send_ex_audio).start()
    
    state["waiting"] = True 
    state["next_time"] = 0 
    state["last_interaction"] = get_ts()
    state["reminder_sent"] = False
    save_state(uid, state)

def send_card(uid, state):
    send_next_auto_word(uid, state)

# --- ADVANCED QUIZ LOGIC (BATCH PROCESSING - 3 LEVELS) ---

def start_advanced_quiz(uid, state):
    state["mode"] = "QUIZ"
    
    # Khởi tạo Queue cho Level 1 (toàn bộ 6 từ)
    indices = list(range(len(state["session"])))
    random.shuffle(indices)
    
    state["quiz_state"] = {
        "level": 1,
        "queue": indices, # Danh sách index cần hỏi
        "failed": [],     # Danh sách index làm sai
        "current_idx": -1, # Con trỏ hiện tại trong queue
        "current_question": None
    }
    
    state["waiting"] = False
    state["next_time"] = 0
    save_state(uid, state)
    
    send_fb(uid, "🛑 **KIỂM TRA 3 CẤP ĐỘ**\nBạn phải trả lời đúng hết tất cả các từ ở mỗi cấp độ mới được đi tiếp.\n\n🚀 **CẤP ĐỘ 1: NHÌN HÁN TỰ -> ĐOÁN NGHĨA**")
    time.sleep(2)
    send_next_batch_question(uid, state)

def send_next_batch_question(uid, state):
    qs = state["quiz_state"]
    
    # Tăng con trỏ
    qs["current_idx"] += 1
    
    # Kiểm tra xem đã hết hàng đợi chưa
    if qs["current_idx"] >= len(qs["queue"]):
        # Hết vòng. Kiểm tra xem có từ nào sai không
        if len(qs["failed"]) > 0:
            # Có từ sai -> Ôn lại những từ sai (Cùng Level)
            send_fb(uid, f"⚠️ Bạn làm sai {len(qs['failed'])} từ. Chúng ta sẽ ôn lại những từ này ngay bây giờ.")
            qs["queue"] = qs["failed"][:] 
            random.shuffle(qs["queue"])   
            qs["failed"] = []             
            qs["current_idx"] = 0         
            save_state(uid, state)
            time.sleep(1)
            send_next_batch_question_content(uid, state)
        else:
            # Đúng hết -> Qua Level tiếp theo
            next_level = qs["level"] + 1
            if next_level > 3: # Đã xong cấp 3 -> Hoàn thành
                finish_session(uid, state)
            else:
                qs["level"] = next_level
                qs["queue"] = list(range(len(state["session"]))) # Reset queue full 6 từ
                random.shuffle(qs["queue"])
                qs["failed"] = []
                qs["current_idx"] = 0
                
                level_names = {
                    2: "CẤP ĐỘ 2: NHÌN NGHĨA -> VIẾT HÁN TỰ",
                    3: "CẤP ĐỘ 3: NGHE TỪ VỰNG -> VIẾT HÁN TỰ"
                }
                send_fb(uid, f"🎉 Xuất sắc! Qua màn.\n\n🚀 **{level_names[next_level]}**")
                save_state(uid, state)
                time.sleep(2)
                send_next_batch_question_content(uid, state)
    else:
        # Vẫn còn trong hàng đợi -> Gửi câu hỏi tiếp theo
        send_next_batch_question_content(uid, state)

def send_next_batch_question_content(uid, state):
    qs = state["quiz_state"]
    word_idx = qs["queue"][qs["current_idx"]]
    word = state["session"][word_idx]
    level = qs["level"]
    
    msg = ""
    # Tiến độ trong bài thi (Ví dụ: Câu 1/6)
    prog = f"({qs['current_idx'] + 1}/{len(qs['queue'])})"
    
    if level == 1:
        msg = f"🔥 {prog} Nghĩa của từ **[{word['Hán tự']}]** là gì?"
        qs["current_question"] = {"type": "HAN_VIET", "answer": word["Nghĩa"]}
    elif level == 2:
        msg = f"🔥 {prog} Viết chữ Hán cho từ **'{word['Nghĩa']}'**:"
        qs["current_question"] = {"type": "VIET_HAN", "answer": word["Hán tự"]}
    elif level == 3:
        msg = f"🔥 {prog} Nghe và gõ lại từ vựng (Audio đang gửi...):"
        qs["current_question"] = {"type": "LISTEN_WRITE", "answer": word["Hán tự"]}
        # Gửi audio chỉ đọc từ vựng
        threading.Thread(target=send_audio_fb, args=(uid, word['Hán tự'])).start()

    send_fb(uid, msg)
    save_state(uid, state)

def check_quiz_answer(uid, state, user_ans):
    qs = state["quiz_state"]
    target = qs.get("current_question")
    if not target: return

    is_correct = False
    correct_ans = target["answer"]
    
    user_clean = user_ans.lower().strip().replace("?", "").replace(".", "").replace("!", "")
    ans_clean = correct_ans.lower().strip().replace("?", "").replace(".", "").replace("!", "")

    if target["type"] == "HAN_VIET":
        keywords = ans_clean.split(",")
        if any(k.strip() in user_clean for k in keywords): is_correct = True
        
    elif target["type"] == "VIET_HAN":
        if ans_clean in user_clean: is_correct = True
        
    elif target["type"] == "LISTEN_WRITE":
        if ans_clean in user_clean: is_correct = True

    if is_correct:
        send_fb(uid, "✅ Chính xác!")
    else:
        # SAI -> BÁO SAI VÀ GHI NHẬN ĐỂ THI LẠI
        word_idx = qs["queue"][qs["current_idx"]]
        
        # Thêm vào danh sách failed nếu chưa có
        if word_idx not in qs["failed"]:
            qs["failed"].append(word_idx)
            
        send_fb(uid, f"❌ Sai rồi. Đáp án đúng là: {correct_ans}\n(Bot sẽ hỏi lại từ này cuối đợt).")

    # Dù đúng hay sai cũng chuyển sang câu tiếp theo trong hàng đợi
    save_state(uid, state)
    time.sleep(1)
    send_next_batch_question(uid, state)

def finish_session(uid, state):
    send_fb(uid, "🏆 XUẤT SẮC! Bạn đã hoàn thành toàn bộ bài kiểm tra.\nĐồng hồ 10 phút bắt đầu đếm từ bây giờ. Nghỉ ngơi nhé!")
    
    state["mode"] = "AUTO"
    state["session"] = [] 
    
    now = get_ts()
    next_t = now + 540 # 9 phút (bù trừ)
    state["next_time"] = next_t
    state["waiting"] = False 
    
    time_str = get_vn_time_str(next_t)
    send_fb(uid, f"⏰ Hẹn gặp lại lúc {time_str}.")
    save_state(uid, state)

# --- MESSAGE PROCESSOR ---

def process(uid, text):
    state = get_state(uid)
    msg = text.lower().strip()
    state["last_interaction"] = get_ts()
    
    # 1. LỆNH CƠ BẢN
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
            # YÊU CẦU NHẬP LẠI TỪ
            current_char = state.get("current_word_char", "").strip()
            is_correct_char = current_char and (current_char in msg or msg in current_char)
            
            if is_correct_char or "tiếp" in msg or "ok" in msg:
                if len(state["session"]) >= 6:
                    start_advanced_quiz(uid, state)
                else:
                    now = get_ts()
                    next_t = now + 540 
                    state["next_time"] = next_t
                    state["waiting"] = False
                    state["reminder_sent"] = False
                    time_str = get_vn_time_str(next_t)
                    send_fb(uid, f"✅ Đã xác nhận! Hẹn {time_str} gửi từ tiếp.")
                    save_state(uid, state)
            else:
                send_fb(uid, f"⚠️ Vui lòng gõ lại từ **{current_char}** để xác nhận đã học (hoặc gõ 'Tiếp' để bỏ qua).")
        else:
            if "tiếp" in msg:
                send_card(uid, state)
            elif "bao lâu" in msg:
                rem = state["next_time"] - get_ts()
                if rem > 0:
                    send_fb(uid, f"⏳ Còn {rem//60} phút.")
                else:
                    send_card(uid, state)
            else:
                reply = ai_smart_reply(text, "User đang chờ timer đếm ngược.")
                send_fb(uid, reply)

    elif state["mode"] == "QUIZ":
        check_quiz_answer(uid, state, text)
        
    else:
        reply = ai_smart_reply(text, "User đang rảnh. Rủ họ học.")
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
                                send_fb(uid, "🔔 Bạn ơi, học xong chưa? Gõ lại từ vựng để tiếp tục nhé!")
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
