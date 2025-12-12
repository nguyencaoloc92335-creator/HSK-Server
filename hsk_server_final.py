import sys
import os
import time
import json
import random
import threading
import logging
import requests
import psycopg2
import re
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
        Nhiệm vụ: Trả lời ngắn gọn, thân thiện. Nếu user muốn nghỉ ngơi, hãy hướng dẫn họ gõ 'Nghỉ'.
        """
        return model.generate_content(prompt).text.strip()
    except: return "Gõ 'Hướng dẫn' để xem menu nhé."

def ai_generate_simple_sentence(word):
    if not model: return {"han": word['Ví dụ'], "viet": word['Dịch câu']}
    try:
        prompt = f"Tạo 1 câu tiếng Trung cực ngắn (3-6 chữ), dùng từ vựng HSK1 và từ '{word['Hán tự']}'. Trả về JSON: {{\"han\": \"...\", \"viet\": \"...\"}}"
        res = model.generate_content(prompt).text.strip()
        match = re.search(r'\{.*\}', res, re.DOTALL)
        if match: return json.loads(match.group())
    except: pass
    return {"han": word['Ví dụ'], "viet": word['Dịch câu']}

def ai_generate_example_smart(word_data: dict) -> dict:
    hanzi = word_data.get('Hán tự', '')
    meaning = word_data.get('Nghĩa', '')
    backup = {"han": word_data.get('Ví dụ', ''), "pinyin": word_data.get('Ví dụ Pinyin', ''), "viet": word_data.get('Dịch câu', '')}
    try:
        prompt = f"Tạo ví dụ HSK2 cho từ: {hanzi} ({meaning}). Trả về JSON: {{\"han\": \"...\", \"pinyin\": \"...\", \"viet\": \"...\"}}"
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
    return f"[{bar}] {int(percent*100)}%"

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
        "mode": "IDLE", # IDLE, AUTO, QUIZ, REST_SETUP, REST_WAIT
        "previous_mode": "IDLE", # Lưu chế độ cũ để quay lại sau khi nghỉ
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
        "current_word_char": "",
        "rest_config": { # Cấu hình nghỉ ngơi
            "type": None, # 'FIXED' hoặc 'INDEFINITE'
            "end_time": 0,
            "last_check": 0,
            "notified": False
        }
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
                    # Merge deep structure
                    if "quiz_state" not in db_s: db_s["quiz_state"] = s["quiz_state"]
                    if "rest_config" not in db_s: db_s["rest_config"] = s["rest_config"]
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
        "📚 **HƯỚNG DẪN**\n"
        "🔹 `Bắt đầu`: Học từ đầu.\n"
        "🔹 `Nghỉ`: Tạm dừng (có thời hạn hoặc không).\n"
        "🔹 `Tiếp tục`: Quay lại học ngay.\n"
        "🔹 `Hiểu` / `Tiếp`: Các lệnh học tập.\n"
    )
    send_fb(user_id, guide)

# --- CORE LOGIC (LEARNING) ---

def send_next_auto_word(uid, state):
    current_hour = datetime.now(timezone(timedelta(hours=7))).hour
    if 0 <= current_hour < 6: return

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
           f"👉 Gõ lại từ **{word['Hán tự']}** để xác nhận.")
    send_fb(uid, msg)
    
    threading.Thread(target=send_audio_fb, args=(uid, word['Hán tự'])).start()
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

# --- ADVANCED QUIZ LOGIC ---

def start_advanced_quiz(uid, state):
    state["mode"] = "QUIZ"
    indices = list(range(len(state["session"])))
    random.shuffle(indices)
    state["quiz_state"] = {"level": 1, "queue": indices, "failed": [], "current_idx": -1, "current_question": None}
    state["waiting"] = False
    state["next_time"] = 0
    save_state(uid, state)
    send_fb(uid, "🛑 **KIỂM TRA 3 CẤP ĐỘ**\nĐúng 100% mới qua màn.\n🚀 **CẤP 1: NHÌN HÁN -> ĐOÁN NGHĨA**")
    time.sleep(2)
    send_next_batch_question(uid, state)

def send_next_batch_question(uid, state):
    qs = state["quiz_state"]
    qs["current_idx"] += 1
    
    if qs["current_idx"] >= len(qs["queue"]):
        if len(qs["failed"]) > 0:
            send_fb(uid, f"⚠️ Sai {len(qs['failed'])} từ. Ôn lại ngay!")
            qs["queue"] = qs["failed"][:] 
            random.shuffle(qs["queue"])   
            qs["failed"] = []             
            qs["current_idx"] = 0         
            save_state(uid, state)
            time.sleep(1)
            send_next_batch_question_content(uid, state)
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
                names = {2: "CẤP 2: NGHĨA -> VIẾT HÁN", 3: "CẤP 3: NGHE -> VIẾT HÁN"}
                send_fb(uid, f"🎉 Xuất sắc! 🚀 **{names[next_level]}**")
                save_state(uid, state)
                time.sleep(2)
                send_next_batch_question_content(uid, state)
    else:
        send_next_batch_question_content(uid, state)

def send_next_batch_question_content(uid, state):
    qs = state["quiz_state"]
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
        msg = f"🔥 {prog} Nghe và gõ lại từ vựng (Audio...):"
        qs["current_question"] = {"type": "LISTEN_WRITE", "answer": word["Hán tự"]}
        threading.Thread(target=send_audio_fb, args=(uid, word['Hán tự'])).start()

    send_fb(uid, msg)
    save_state(uid, state)

def check_quiz_answer(uid, state, user_ans):
    qs = state["quiz_state"]
    target = qs.get("current_question")
    if not target: return

    is_correct = False
    correct_ans = target["answer"]
    user_clean = user_ans.lower().strip().replace("?", "").replace(".", "")
    ans_clean = correct_ans.lower().strip().replace("?", "").replace(".", "")

    if target["type"] == "HAN_VIET":
        keywords = ans_clean.split(",")
        if any(k.strip() in user_clean for k in keywords): is_correct = True
    elif target["type"] in ["VIET_HAN", "LISTEN_WRITE"]:
        if ans_clean in user_clean: is_correct = True

    if is_correct:
        send_fb(uid, "✅ Chính xác!")
        if qs["level"] < 4: qs["level"] += 1
        else:
            qs["level"] = 1
            qs["word_idx"] += 1
            done_s = qs["word_idx"]
            total_s = len(state["session"])
            send_fb(uid, f"📈 Tiến độ: {draw_progress_bar(done_s, total_s)}")
            time.sleep(1)
        save_state(uid, state)
        time.sleep(1)
        send_next_batch_question(uid, state)
    else:
        word_idx = qs["queue"][qs["current_idx"]]
        if word_idx not in qs["failed"]: qs["failed"].append(word_idx)
        send_fb(uid, f"❌ Sai rồi. Đáp án: {correct_ans}\n(Sẽ hỏi lại sau).")
        save_state(uid, state)
        time.sleep(1)
        send_next_batch_question(uid, state)

def finish_session(uid, state):
    send_fb(uid, "🏆 XUẤT SẮC! Hoàn thành bài thi.\nNghỉ 10 phút nhé!")
    state["mode"] = "AUTO"
    state["session"] = [] 
    now = get_ts()
    next_t = now + 540 
    state["next_time"] = next_t
    state["waiting"] = False 
    time_str = get_vn_time_str(next_t)
    send_fb(uid, f"⏰ Hẹn gặp lúc {time_str}.")
    save_state(uid, state)

# --- REST MODE LOGIC (MỚI) ---

def parse_time_duration(text):
    """Phân tích chuỗi thời gian: '15 phút', '1 tiếng', '30p'"""
    text = text.lower()
    minutes = 0
    
    # Tìm số
    nums = re.findall(r'\d+', text)
    if not nums: return 0
    val = int(nums[0])
    
    if any(u in text for u in ['tiếng', 'giờ', 'h']):
        minutes = val * 60
    else:
        minutes = val
    return minutes * 60 # Trả về giây

def process(uid, text):
    state = get_state(uid)
    msg = text.lower().strip()
    state["last_interaction"] = get_ts()
    
    # --- XỬ LÝ LỆNH NGHỈ ---
    if msg == "nghỉ" or msg == "nghi" or msg == "dừng":
        # Lưu chế độ hiện tại để sau này quay lại
        current_mode = state.get("mode", "IDLE")
        if current_mode != "REST_SETUP" and current_mode != "REST_WAIT":
            state["previous_mode"] = current_mode
        
        state["mode"] = "REST_SETUP"
        send_fb(uid, "💤 Bạn muốn nghỉ bao lâu?\n- Gõ số phút/giờ (Ví dụ: '15 phút', '1 tiếng').\n- Hoặc gõ 'Không biết' để nghỉ vô thời hạn (1 tiếng mình sẽ hỏi thăm 1 lần).")
        save_state(uid, state)
        return

    # --- XỬ LÝ LỆNH TIẾP TỤC ---
    if any(w in msg for w in ["tiếp tục", "quay lại", "học tiếp", "sẵn sàng", "ready"]):
        prev_mode = state.get("previous_mode", "AUTO")
        if prev_mode == "IDLE": prev_mode = "AUTO"
        
        state["mode"] = prev_mode
        state["rest_config"] = {"type": None} # Xóa cấu hình nghỉ
        
        send_fb(uid, "🎉 Mừng bạn quay trở lại! Tiếp tục hành trình nào.")
        
        # Logic khôi phục
        if prev_mode == "AUTO":
            # Nếu đang chờ confirm -> Nhắc confirm
            if state["waiting"]:
                char = state.get("current_word_char", "từ vựng")
                send_fb(uid, f"👉 Gõ lại từ **{char}** để xác nhận nhé.")
            # Nếu đang đếm giờ -> Gửi luôn
            else:
                send_next_auto_word(uid, state)
        elif prev_mode == "QUIZ":
            send_fb(uid, "📝 Tiếp tục bài kiểm tra...")
            time.sleep(1)
            # Gửi lại câu hỏi hiện tại
            if state["quiz_state"]["current_question"]:
                q_type = state["quiz_state"]["current_question"]["type"]
                q_text = "Câu hỏi cũ"
                # (Đơn giản hóa: gửi lại câu hỏi mới của cùng index)
                send_next_batch_question_content(uid, state)
        
        save_state(uid, state)
        return

    # --- SETUP NGHỈ ---
    if state["mode"] == "REST_SETUP":
        # Check xem user muốn nghỉ có thời hạn hay không
        if any(w in msg for w in ["không", "chưa", "lâu", "tùy", "vô", "unknown"]):
            # Nghỉ vô thời hạn
            state["mode"] = "REST_WAIT"
            state["rest_config"] = {
                "type": "INDEFINITE",
                "last_check": get_ts()
            }
            send_fb(uid, "😴 Ok, bạn cứ nghỉ ngơi thoải mái. Mỗi 1 tiếng mình sẽ nhắn hỏi thăm nhé.\nKhi nào sẵn sàng gõ 'Tiếp tục'.")
        else:
            # Nghỉ có thời hạn
            duration = parse_time_duration(msg)
            if duration > 0:
                end_time = get_ts() + duration
                state["mode"] = "REST_WAIT"
                state["rest_config"] = {
                    "type": "FIXED",
                    "end_time": end_time,
                    "notified": False
                }
                time_str = get_vn_time_str(end_time)
                send_fb(uid, f"⏱️ Ok! Mình sẽ đợi đến **{time_str}**.\nNghỉ ngơi vui vẻ nhé!")
            else:
                send_fb(uid, "Mình không hiểu thời gian. Vui lòng nhập lại (VD: '10 phút') hoặc gõ 'Tiếp tục' để hủy nghỉ.")
                return 
        save_state(uid, state)
        return

    # --- ĐANG NGHỈ (REST_WAIT) ---
    if state["mode"] == "REST_WAIT":
        if any(w in msg for w in ["chưa", "đợi", "wait", "no"]):
            send_fb(uid, "Ok, cứ thong thả nhé. 1 tiếng sau mình gọi lại.")
            # Reset timer check
            state["rest_config"]["last_check"] = get_ts()
            save_state(uid, state)
        else:
            # Nếu user nói gì đó khác (không phải lệnh tiếp tục), AI trả lời xã giao
            send_fb(uid, "Bot đang chế độ nghỉ. Gõ 'Tiếp tục' để quay lại học nhé.")
        return

    # --- CÁC MODE CHÍNH ---
    
    if msg == "reset":
        state = {"user_id": uid, "mode": "IDLE", "learned": [], "session": [], "next_time": 0, "waiting": False}
        save_state(uid, state)
        send_fb(uid, "Đã reset.")
        return

    if "bắt đầu" in msg:
        state["mode"] = "AUTO"
        state["session"] = []
        send_fb(uid, "🚀 Bắt đầu!")
        send_card(uid, state)
        return

    if state["mode"] == "AUTO":
        if state["waiting"]:
            current_char = state.get("current_word_char", "").strip()
            is_correct = current_char and (current_char in msg or msg in current_char)
            if is_correct or "tiếp" in msg or "ok" in msg:
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
                send_fb(uid, f"⚠️ Gõ lại từ **{current_char}** để xác nhận.")
        else:
            if "tiếp" in msg:
                send_card(uid, state)
            elif "bao lâu" in msg:
                rem = state["next_time"] - get_ts()
                send_fb(uid, f"⏳ Còn {rem//60} phút.")
            else:
                send_fb(uid, ai_smart_reply(text, "Chờ timer."))

    elif state["mode"] == "QUIZ":
        check_quiz_answer(uid, state, text)
    else:
        send_fb(uid, ai_smart_reply(text, "Idle"))

# --- TRIGGER SCAN (CRON) ---
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
                        
                        mode = state.get("mode")
                        
                        # 1. Logic AUTO Learning
                        if mode == "AUTO" and not state["waiting"] and state["next_time"] > 0:
                            if now >= state["next_time"]:
                                logger.info(f"Trigger send {uid}")
                                send_card(uid, state)
                        
                        # 2. Logic Nhắc nhở khi treo
                        if mode == "AUTO" and state["waiting"]:
                            last = state.get("last_interaction", 0)
                            if (now - last > 1800) and not state.get("reminder_sent", False):
                                send_fb(uid, "🔔 Học xong chưa? Gõ lại từ vựng nhé!")
                                state["reminder_sent"] = True
                                save_state(uid, state)
                                
                        # 3. LOGIC REST MODE (MỚI)
                        if mode == "REST_WAIT":
                            cfg = state.get("rest_config", {})
                            rtype = cfg.get("type")
                            
                            # Loại 1: Có thời hạn
                            if rtype == "FIXED":
                                end_t = cfg.get("end_time", 0)
                                notified = cfg.get("notified", False)
                                if now >= end_t and not notified:
                                    send_fb(uid, "⏰ Hết giờ nghỉ rồi! Bạn đã sẵn sàng chưa?\nGõ 'Tiếp tục' để quay lại guồng quay nào! 💪")
                                    state["rest_config"]["notified"] = True
                                    save_state(uid, state)
                                    
                            # Loại 2: Vô thời hạn (Nhắc mỗi 1 tiếng)
                            if rtype == "INDEFINITE":
                                last_chk = cfg.get("last_check", 0)
                                if now - last_chk >= 3600: # 1 tiếng
                                    send_fb(uid, "🔔 Bạn đã nghỉ 1 tiếng rồi. Đã nạp đủ năng lượng chưa?\n- Gõ 'Tiếp tục' để học.\n- Gõ 'Chưa' để nghỉ tiếp.")
                                    state["rest_config"]["last_check"] = now
                                    save_state(uid, state)

            finally:
                db_pool.putconn(conn)
        return PlainTextResponse("SCAN OK")
    except Exception as e:
        return PlainTextResponse(f"ERR: {e}", 500)

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
