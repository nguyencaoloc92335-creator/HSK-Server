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

# ==============================================================================
# PHẦN 1: THE BRAIN (AI QUẢN LÝ)
# ==============================================================================

def run_ai_agent(uid, text, state):
    """
    AI đóng vai trò 'Bộ não'. Nó nhận ngữ cảnh và tin nhắn,
    sau đó quyết định gọi 'Công cụ' (Action) nào của Python.
    """
    if not model: 
        send_fb(uid, "AI đang bảo trì. Vui lòng gõ đúng lệnh (Bắt đầu, Tiếp, Hiểu).")
        return

    context_str = f"""
    - Trạng thái user: {state['mode']}
    - Đang chờ xác nhận: {state['waiting']}
    - Số từ đã học phiên này: {len(state['session'])}
    """

    prompt = f"""
    Bạn là AI điều phối cho ứng dụng học HSK.
    Ngữ cảnh hệ thống:
    {context_str}
    
    Người dùng nhắn: "{text}"
    
    Hãy phân tích ý định người dùng và trả về JSON theo định dạng sau:
    {{
        "thought": "Suy nghĩ của bạn về ý định người dùng",
        "action": "TÊN_HÀNH_ĐỘNG", 
        "reply": "Câu trả lời để gửi cho người dùng (Tiếng Việt, ngắn gọn, thân thiện)"
    }}
    
    Danh sách HÀNH ĐỘNG (Action) khả dụng:
    - START: Người dùng muốn bắt đầu học hoặc chào buổi sáng.
    - CONFIRM: Người dùng xác nhận đã hiểu, đã học xong từ hiện tại.
    - SKIP: Người dùng muốn nhận từ tiếp theo ngay lập tức (bỏ qua chờ đợi).
    - STOP: Người dùng muốn dừng lại, nghỉ ngơi.
    - RESET: Người dùng muốn xóa lịch sử học lại từ đầu.
    - GUIDE: Người dùng hỏi cách dùng.
    - NONE: Chỉ là trò chuyện xã giao, không cần thực thi lệnh hệ thống.
    
    Lưu ý: Chỉ trả về JSON thuần túy, không markdown.
    """
    
    try:
        response = model.generate_content(prompt).text.strip()
        # Clean markdown json if exists
        if "```json" in response:
            response = response.split("```json")[1].split("```")[0].strip()
        elif "```" in response:
            response = response.split("```")[1].split("```")[0].strip()
            
        decision = json.loads(response)
        
        # Gửi câu trả lời của AI cho người dùng trước
        if decision.get("reply"):
            send_fb(uid, decision["reply"])
            
        # Thực thi hành động mà AI yêu cầu (The Body)
        action = decision.get("action", "NONE")
        logger.info(f"🤖 AI Decided: {action} | Thought: {decision.get('thought')}")
        
        if action == "START": cmd_start(uid, state)
        elif action == "CONFIRM": cmd_confirm(uid, state, text) # Tái sử dụng logic confirm
        elif action == "SKIP": cmd_next(uid, state)
        elif action == "STOP": cmd_stop(uid, state)
        elif action == "RESET": cmd_reset(uid, state)
        elif action == "GUIDE": send_guide_message(uid)
        # NONE thì không làm gì thêm, chỉ chat.

    except Exception as e:
        logger.error(f"AI Agent Error: {e}")
        send_fb(uid, "Mình chưa hiểu ý bạn lắm. Bạn thử gõ 'Hướng dẫn' xem sao nhé!")

def ai_generate_content_data(word):
    """AI tạo nội dung học (Ví dụ)"""
    if not model: return {"han": word['Ví dụ'], "viet": word['Dịch câu']}
    try:
        prompt = f"Tạo ví dụ HSK2 đơn giản cho từ: {word['Hán tự']} ({word['Nghĩa']}). JSON: {{\"han\": \"...\", \"pinyin\": \"...\", \"viet\": \"...\"}}"
        res = model.generate_content(prompt).text.strip()
        match = re.search(r'\{.*\}', res, re.DOTALL)
        if match: return json.loads(match.group())
    except: pass
    return {"han": word['Ví dụ'], "pinyin": word.get('Ví dụ Pinyin',''), "viet": word['Dịch câu']}

def ai_generate_quiz_sentence(word):
    """AI tạo câu hỏi thi"""
    if not model: return {"han": word['Ví dụ'], "viet": word['Dịch câu']}
    try:
        prompt = f"Tạo 1 câu ngắn (HSK1-2) chứa từ '{word['Hán tự']}'. JSON: {{\"han\": \"...\", \"viet\": \"...\"}}"
        res = model.generate_content(prompt).text.strip()
        match = re.search(r'\{.*\}', res, re.DOTALL)
        if match: return json.loads(match.group())
    except: pass
    return {"han": word['Ví dụ'], "viet": word['Dịch câu']}

# ==============================================================================
# PHẦN 2: THE BODY (CÔNG CỤ THỰC THI & LOGIC CỨNG)
# ==============================================================================

# --- DATABASE & STATE HELPERS ---
def get_state(uid):
    if uid in USER_CACHE: return USER_CACHE[uid]
    s = {
        "user_id": uid, "mode": "IDLE", "learned": [], "session": [], 
        "next_time": 0, "waiting": False, "last_interaction": 0, "reminder_sent": False,
        "quiz_state": {"word_idx": 0, "level": 0, "current_question": None},
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
                if row: s.update(row[0])
        except: pass
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

def send_fb(uid, txt):
    try: requests.post("https://graph.facebook.com/v16.0/me/messages", params={"access_token": PAGE_ACCESS_TOKEN}, json={"recipient": {"id": uid}, "message": {"text": txt}}, timeout=10)
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

def get_ts(): return int(time.time())
def get_vn_time_str(ts): return datetime.fromtimestamp(ts, timezone(timedelta(hours=7))).strftime("%H:%M")
def draw_bar(c, t): return f"[{'▓'*int(8*c/t)}{'░'*(8-int(8*c/t))}]" if t>0 else ""

# --- ACTION FUNCTIONS (CÁC HÀM HOẠT ĐỘNG) ---

def cmd_start(uid, state):
    state["mode"] = "AUTO"
    state["session"] = []
    # send_fb(uid, "🚀 Bắt đầu!") # AI đã chào rồi thì thôi, hoặc giữ lại tùy bạn
    cmd_next(uid, state)

def cmd_stop(uid, state):
    state["mode"] = "IDLE"
    save_state(uid, state)
    # send_fb(uid, "Đã dừng.") # Để AI nói

def cmd_reset(uid, state):
    state.update({"mode": "IDLE", "learned": [], "session": [], "next_time": 0, "waiting": False})
    save_state(uid, state)
    # send_fb(uid, "Đã reset.") # Để AI nói

def send_guide_message(uid):
    guide = "📚 **HƯỚNG DẪN:** `Bắt đầu`, `Hiểu` (để đếm giờ), `Tiếp` (học luôn), `Học lại`, `Dừng`."
    send_fb(uid, guide)

def cmd_next(uid, state):
    # Logic gửi từ mới
    if 0 <= datetime.now(timezone(timedelta(hours=7))).hour < 6: return
    
    if len(state["session"]) >= 6:
        cmd_start_quiz(uid, state)
        return

    learned = set(state["learned"])
    pool = [w for w in HSK_DATA if w['Hán tự'] not in learned]
    if not pool:
        pool = HSK_DATA; state["learned"] = []
    
    word = random.choice(pool)
    state["session"].append(word)
    state["learned"].append(word['Hán tự'])
    state["current_word_char"] = word['Hán tự']
    
    ex = ai_generate_content_data(word)
    
    prog = f"{len(state['session'])}/6"
    msg = (f"🔔 **TỪ MỚI** ({prog})\n\n"
           f"🇨🇳 **{word['Hán tự']}** ({word['Pinyin']})\n"
           f"🇻🇳 {word['Nghĩa']}\n"
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

def cmd_confirm(uid, state, text_msg):
    # Logic xác nhận đã hiểu
    current_char = state.get("current_word_char", "").strip()
    # Kiểm tra lỏng lẻo hơn 1 chút: Đúng từ hoặc lệnh "Hiểu/OK/Tiếp"
    is_correct = (current_char and current_char in text_msg) or any(w in text_msg.lower() for w in ["hiểu", "ok", "tiếp", "yes"])
    
    if is_correct:
        if len(state["session"]) >= 6:
            cmd_start_quiz(uid, state)
        else:
            now = get_ts()
            next_t = now + 540 # 9 phút
            state["next_time"] = next_t
            state["waiting"] = False
            state["reminder_sent"] = False
            send_fb(uid, f"✅ Đã xác nhận. Hẹn {get_vn_time_str(next_t)} gửi tiếp.")
            save_state(uid, state)
    else:
        send_fb(uid, f"⚠️ Hãy gõ lại từ **{current_char}** để mình biết bạn đã nhớ mặt chữ nhé!")

# --- QUIZ LOGIC (Giữ nguyên logic cứng để đảm bảo tính đúng đắn) ---

def cmd_start_quiz(uid, state):
    state["mode"] = "QUIZ"
    indices = list(range(len(state["session"])))
    random.shuffle(indices)
    state["quiz_state"] = {"level": 1, "queue": indices, "failed": [], "current_idx": -1, "current_question": None}
    state["waiting"] = False
    state["next_time"] = 0
    save_state(uid, state)
    send_fb(uid, "🛑 **KIỂM TRA 3 CẤP ĐỘ (HARD)**\nSai làm lại!")
    time.sleep(2)
    send_quiz_question(uid, state)

def send_quiz_question(uid, state):
    qs = state["quiz_state"]
    qs["current_idx"] += 1
    
    if qs["current_idx"] >= len(qs["queue"]):
        # Hết hàng đợi
        if len(qs["failed"]) > 0:
            send_fb(uid, f"⚠️ Có {len(qs['failed'])} từ sai. Ôn lại ngay.")
            qs["queue"] = qs["failed"][:]; random.shuffle(qs["queue"])
            qs["failed"] = []; qs["current_idx"] = 0
            save_state(uid, state); time.sleep(1)
            send_quiz_content(uid, state)
        else:
            # Qua level
            nxt = qs["level"] + 1
            if nxt > 3:
                finish_quiz(uid, state)
            else:
                qs["level"] = nxt; qs["queue"] = list(range(len(state["session"]))); random.shuffle(qs["queue"])
                qs["failed"] = []; qs["current_idx"] = 0
                lvl_name = {2: "NHÌN NGHĨA VIẾT HÁN", 3: "NGHE VIẾT HÁN"}
                send_fb(uid, f"🎉 Qua màn! 🚀 **{lvl_name[nxt]}**")
                save_state(uid, state); time.sleep(2)
                send_quiz_content(uid, state)
    else:
        send_quiz_content(uid, state)

def send_quiz_content(uid, state):
    qs = state["quiz_state"]
    w_idx = qs["queue"][qs["current_idx"]]
    word = state["session"][w_idx]
    lvl = qs["level"]
    
    prog = f"({qs['current_idx']+1}/{len(qs['queue'])})"
    msg = ""
    
    if lvl == 1:
        msg = f"🔥 {prog} Nghĩa của **[{word['Hán tự']}]** là gì?"
        qs["current_question"] = {"type": "HAN_VIET", "answer": word["Nghĩa"]}
    elif lvl == 2:
        msg = f"🔥 {prog} Viết Hán tự cho **'{word['Nghĩa']}'**:"
        qs["current_question"] = {"type": "VIET_HAN", "answer": word["Hán tự"]}
    elif lvl == 3:
        msg = f"🔥 {prog} Nghe và gõ lại từ (Audio đang gửi...):"
        qs["current_question"] = {"type": "LISTEN", "answer": word["Hán tự"]}
        threading.Thread(target=send_audio_fb, args=(uid, word['Hán tự'])).start()
        
    send_fb(uid, msg)
    save_state(uid, state)

def check_quiz_answer(uid, state, text):
    qs = state["quiz_state"]
    target = qs.get("current_question")
    if not target: return

    correct = False
    ans = target["answer"].lower()
    usr = text.lower().strip().replace(".", "")
    
    if target["type"] == "HAN_VIET":
        if any(k.strip() in usr for k in ans.split(",")): correct = True
    elif target["type"] in ["VIET_HAN", "LISTEN"]:
        if ans in usr: correct = True
        
    if correct:
        send_fb(uid, "✅")
    else:
        w_idx = qs["queue"][qs["current_idx"]]
        if w_idx not in qs["failed"]: qs["failed"].append(w_idx)
        send_fb(uid, f"❌ Sai rồi. Đáp án: {target['answer']}")
        
    save_state(uid, state)
    time.sleep(1)
    send_quiz_question(uid, state)

def finish_quiz(uid, state):
    send_fb(uid, "🏆 Hoàn thành! Nghỉ 10 phút nhé.")
    state["mode"] = "AUTO"
    state["session"] = []
    state["next_time"] = get_ts() + 540
    state["waiting"] = False
    send_fb(uid, f"⏰ Hẹn {get_vn_time_str(state['next_time'])}.")
    save_state(uid, state)

# ==============================================================================
# PHẦN 3: ROUTER & TRIGGERS (QUẢN LÝ LUỒNG)
# ==============================================================================

def process_router(uid, text):
    state = get_state(uid)
    msg = text.lower().strip()
    state["last_interaction"] = get_ts()
    
    # 1. ƯU TIÊN LỆNH HỆ THỐNG CỨNG (Fast Layer)
    if msg == "reset": 
        cmd_reset(uid, state)
        send_fb(uid, "Đã reset.") # Phản hồi nhanh
        return
    if "bắt đầu" in msg or "start" in msg: 
        cmd_start(uid, state)
        return
    if "dừng" in msg: 
        cmd_stop(uid, state)
        return
    if msg in ["tiếp", "next"]: 
        if state["mode"] == "AUTO": cmd_next(uid, state)
        return
    # Nếu đang chờ gõ lại từ, mà user gõ đúng từ Hán tự -> Xử lý luôn không cần qua AI
    if state["mode"] == "AUTO" and state["waiting"]:
        curr_char = state.get("current_word_char", "")
        if curr_char and curr_char in msg:
            cmd_confirm(uid, state, text)
            return

    # 2. XỬ LÝ THEO MODE
    if state["mode"] == "QUIZ":
        # Quiz cần độ chính xác cao, không qua AI Agent để tránh ảo giác
        check_quiz_answer(uid, state, text)
        return
        
    # 3. CÁC TRƯỜNG HỢP CÒN LẠI -> GỬI CHO AI (Brain Layer)
    # (Chat linh tinh, hỏi thăm, hoặc gõ lệnh sai chính tả...)
    run_ai_agent(uid, text, state)

# --- CRON JOB (Giữ server sống & check timer) ---
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
                    for row in cur.fetchall():
                        state = row[0]
                        uid = state["user_id"]
                        USER_CACHE[uid] = state
                        
                        # Check Auto Send
                        if state["mode"] == "AUTO" and not state["waiting"] and state["next_time"] > 0:
                            if now >= state["next_time"]:
                                logger.info(f"Trigger send {uid}")
                                cmd_next(uid, state)
                        
                        # Check Reminder (30p)
                        if state["mode"] == "AUTO" and state["waiting"]:
                            if (now - state["last_interaction"] > 1800) and not state["reminder_sent"]:
                                send_fb(uid, "🔔 Quên mình rồi hả? Gõ lại từ vựng để học tiếp nào!")
                                state["reminder_sent"] = True
                                save_state(uid, state)
            finally: db_pool.putconn(conn)
        return PlainTextResponse("OK")
    except Exception as e: return PlainTextResponse(f"Err: {e}", 500)

@app.post("/webhook")
async def wh(req: Request, bg: BackgroundTasks):
    try:
        d = await req.json()
        if 'entry' in d:
            for e in d['entry']:
                for m in e.get('messaging', []):
                    if 'message' in m:
                        bg.add_task(process_router, m['sender']['id'], m['message'].get('text', ''))
        return PlainTextResponse("EVENT_RECEIVED")
    except: return PlainTextResponse("ERROR")

@app.get("/webhook")
def verify(request: Request):
    if request.query_params.get("hub.verify_token") == VERIFY_TOKEN:
        return PlainTextResponse(request.query_params.get("hub.challenge"))
    return PlainTextResponse("Error", 403)

@app.get("/")
def home(): return PlainTextResponse("HSK Bot Running")

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
