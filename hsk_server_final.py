import sys
import os
import time
import json
import random
import re
import requests
import logging
import threading
from typing import Dict, Any, List, Optional
from datetime import datetime, timezone, timedelta

# --- CÁC THƯ VIỆN CHÍNH ---
from fastapi import FastAPI, Request, BackgroundTasks
from starlette.responses import PlainTextResponse
import uvicorn
import psycopg2
from psycopg2 import pool
import google.generativeai as genai

# --- 0. CẤU HÌNH LOGGING ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# --- 1. CẤU HÌNH HỆ THỐNG ---

# Token Facebook
PAGE_ACCESS_TOKEN = "EAAbQQNNSmSMBQKWd5qB15zFMy2KdPm6Ko1rJX6R4ZC3EtnNfvf0gT76V1Qk4l1vflxL1pDVwY8mrgbgAaFFtG6bzcrhJfQ86HdK5v8qZA9zTIge2ZBJcx9oNPOjk1DlQ8juGinZBuah0RDgbCd2vBvlNWr47GVz70BdPNzKRctCGphNJRI0Wm57UwKRmXOZAVfDP7zwZDZD"
VERIFY_TOKEN = "hsk_mat_khau_bi_mat"

# Gemini API Key
GEMINI_API_KEY = "AIzaSyB5V6sgqSOZO4v5DyuEZs3msgJqUk54HqQ"
DATABASE_URL = os.environ.get('DATABASE_URL') 

# Cấu hình AI
try:
    genai.configure(api_key=GEMINI_API_KEY)
    model = genai.GenerativeModel('gemini-1.5-flash')
except Exception as e:
    logger.error(f"Lỗi cấu hình AI: {e}")

# --- 2. NẠP DỮ LIỆU ---
try:
    import hsk2_vocabulary_full as hsk_data
    HSK_DATA = hsk_data.HSK_DATA
    HSK_MAP = {word["Hán tự"]: word for word in HSK_DATA}
    logger.info(f"--> [SYSTEM] Đã nạp {len(HSK_DATA)} từ vựng.")
except ImportError:
    HSK_DATA = [{"Hán tự": "你好", "Pinyin": "nǐhǎo", "Nghĩa": "xin chào", "Ví dụ": "你好!", "Ví dụ Pinyin": "Nǐ hǎo!", "Dịch câu": "Chào bạn!"}]
    HSK_MAP = {word["Hán tự"]: word for word in HSK_DATA}

# --- 3. DATABASE POOL ---
db_pool = None
if DATABASE_URL:
    try:
        db_pool = psycopg2.pool.ThreadedConnectionPool(1, 10, DATABASE_URL, sslmode='require')
        logger.info("--> [DB] Connection Pool OK.")
    except Exception as e:
        logger.error(f"--> [DB ERROR] {e}")

USER_CACHE = {}

app = FastAPI()

# --- 4. STATE MANAGEMENT ---

def get_db_conn():
    return db_pool.getconn() if db_pool else None

def release_db_conn(conn):
    if db_pool and conn: db_pool.putconn(conn)

def get_user_state(user_id: str) -> Dict[str, Any]:
    default_state = {
        "user_id": user_id,
        "mode": "IDLE",            # IDLE, AUTO_LEARNING, QUIZ
        "session_words": [],       
        "learned_history": [],     
        "current_index": 0,        
        "quiz_score": 0,           
        "current_quiz_word": None, 
        "quiz_type": None,
        "quiz_options": {},
        "next_action_time": 0,     # QUAN TRỌNG: Thời điểm sẽ gửi tin nhắn tiếp theo
        "waiting_confirm": False,  # True: Đang đợi user nhắn "Hiểu"
        "reminder_count": 0        # Đếm số lần nhắc nếu user quên trả lời
    }

    if user_id in USER_CACHE:
        merged = default_state.copy()
        merged.update(USER_CACHE[user_id])
        return merged

    if db_pool:
        conn = get_db_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("CREATE TABLE IF NOT EXISTS users (user_id VARCHAR(50) PRIMARY KEY, state JSONB);")
                cur.execute("SELECT state FROM users WHERE user_id = %s", (user_id,))
                res = cur.fetchone()
                if res:
                    db_data = res[0]
                    final_state = default_state.copy()
                    final_state.update(db_data if isinstance(db_data, dict) else {})
                    USER_CACHE[user_id] = final_state
                    return final_state
        except Exception as e:
            logger.error(f"DB Read Error: {e}")
        finally:
            release_db_conn(conn)
    
    return default_state

def save_user_state(user_id: str, state: Dict[str, Any]):
    USER_CACHE[user_id] = state 
    if db_pool:
        conn = get_db_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO users (user_id, state) VALUES (%s, %s)
                    ON CONFLICT (user_id) DO UPDATE SET state = EXCLUDED.state
                """, (user_id, json.dumps(state)))
                conn.commit()
        except Exception as e:
            logger.error(f"DB Save Error: {e}")
        finally:
            release_db_conn(conn)

def reset_user_state(user_id: str):
    if user_id in USER_CACHE: del USER_CACHE[user_id]
    if db_pool:
        conn = get_db_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM users WHERE user_id = %s", (user_id,))
                conn.commit()
        except Exception: pass
        finally: release_db_conn(conn)

def clear_learning_history(user_id: str, state: Dict[str, Any]):
    state["learned_history"] = []
    state["session_words"] = []
    state["mode"] = "IDLE"
    state["quiz_score"] = 0
    save_user_state(user_id, state)
    send_fb_message(user_id, "🔄 Đã xóa toàn bộ lịch sử học tập! Gõ 'Bắt đầu' để học lại từ đầu.")

# --- 5. AI & HELPERS ---

def ai_chat_chit(message: str) -> str:
    try:
        prompt = f"Bạn là trợ lý HSK. User nói: '{message}'. Trả lời ngắn gọn, nhắc họ gõ 'Bắt đầu' để vào chế độ học tự động. Nếu họ hỏi tiến độ, nhắc họ gõ 'Tiến độ'."
        response = model.generate_content(prompt)
        return response.text.strip()
    except Exception as e:
        logger.error(f"AI Error: {e}")
        return "Chào bạn! Gõ 'Bắt đầu' để học, hoặc 'Hướng dẫn' để xem cách dùng nhé! 😄"

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
    except Exception as e:
        logger.error(f"AI Gen Error: {e}")
        return backup

def send_fb_message(user_id: str, text: str):
    logger.info(f"Đang gửi tin nhắn tới {user_id}: {text[:50]}...")
    params = {"access_token": PAGE_ACCESS_TOKEN}
    headers = {"Content-Type": "application/json"}
    data = {"recipient": {"id": user_id}, "message": {"text": text}}
    try:
        r = requests.post("https://graph.facebook.com/v16.0/me/messages", params=params, headers=headers, json=data)
        if r.status_code != 200:
            logger.error(f"❌ FB GỬI LỖI (Status {r.status_code}): {r.text}")
        else:
            logger.info("✅ Gửi tin nhắn thành công.")
    except Exception as e:
        logger.error(f"❌ FB REQUEST ERROR: {e}")

def get_vietnam_time():
    return datetime.now(timezone(timedelta(hours=7)))

def send_guide_message(user_id: str):
    guide_text = (
        "🤖 **HƯỚNG DẪN** 🤖\n\n"
        "1️⃣ **Học tập:**\n"
        "   - Gõ `Bắt đầu`: Bot gửi từ vựng.\n"
        "   - Gõ `Hiểu`: Bot sẽ **đếm 10 phút** rồi gửi từ tiếp theo.\n"
        "   - Đủ 6 từ sẽ kiểm tra.\n\n"
        "2️⃣ **Tiện ích:**\n"
        "   - `Tiến độ`: Xem số từ đã học.\n"
        "   - `Bao lâu`: Xem thời gian còn lại đến từ mới.\n"
        "   - `Chào buổi sáng`: Học tiếp tiến độ cũ.\n"
        "   - `Học lại`: Xóa lịch sử.\n"
        "   - `Dừng`: Nghỉ ngơi.\n\n"
        "Bot nghỉ từ 0h-6h sáng."
    )
    send_fb_message(user_id, guide_text)

# --- 6. CORE LOGIC ---

def process_message_background(user_id: str, message_text: str):
    try:
        logger.info(f"Processing msg from {user_id}: {message_text}")
        state = get_user_state(user_id)
        msg = message_text.strip().lower()

        # --- NHÓM LỆNH HỆ THỐNG ---
        if any(cmd in msg for cmd in ['hướng dẫn', 'huong dan', 'help', 'giới thiệu', 'menu']):
            send_guide_message(user_id)
            return

        # --- KIỂM TRA TIẾN ĐỘ ---
        if any(cmd in msg for cmd in ['tiến độ', 'tien do', 'progress', 'bao nhiêu từ', 'học được bao nhiêu', 'thống kê']):
            learned_count = len(state.get("learned_history", []))
            total_count = len(HSK_DATA)
            percent = (learned_count / total_count) * 100 if total_count > 0 else 0
            
            msg_reply = (
                f"📊 **THỐNG KÊ TIẾN ĐỘ**\n"
                f"- Đã học: {learned_count} từ\n"
                f"- Tổng số: {total_count} từ\n"
                f"- Hoàn thành: {percent:.1f}%\n\n"
                f"Cố gắng lên nhé! 🚀"
            )
            send_fb_message(user_id, msg_reply)
            return

        # --- KIỂM TRA THỜI GIAN CÒN LẠI ---
        if any(cmd in msg for cmd in ['bao lâu', 'khi nào', 'mấy phút', 'thời gian', 'time', 'chờ bao lâu']):
            mode = state.get("mode", "IDLE")
            if mode != "AUTO_LEARNING":
                send_fb_message(user_id, "Bạn chưa bắt đầu chế độ học tự động. Gõ 'Bắt đầu' nhé!")
                return
                
            if state.get("waiting_confirm", False):
                send_fb_message(user_id, "Bot đang chờ bạn xác nhận 'Hiểu' để bắt đầu tính giờ nha!")
                return
                
            next_time = state.get("next_action_time", 0)
            now = int(time.time())
            remaining = next_time - now
            
            if remaining > 0:
                mins = remaining // 60
                secs = remaining % 60
                send_fb_message(user_id, f"⏳ Còn khoảng {mins} phút {secs} giây nữa là đến từ tiếp theo.\nNếu muốn học luôn, hãy gõ 'Tiếp'.")
            else:
                send_fb_message(user_id, "⏰ Đã đến giờ rồi! Bot đang chuẩn bị gửi từ ngay đây...")
            return

        if any(cmd in msg for cmd in ['học lại', 'hoc lai', 'reset history', 'xóa lịch sử']):
            clear_learning_history(user_id, state)
            return

        if msg == "reset":
            reset_user_state(user_id)
            send_fb_message(user_id, "⚙️ Đã Reset kỹ thuật. Gõ 'Bắt đầu' để học.")
            return

        if any(keyword in msg for keyword in ['chào buổi sáng', 'buổi sáng', 'good morning', 'morning', 'dậy rồi']):
            send_fb_message(user_id, "🌞 Chào buổi sáng! Tiếp tục học nào! 🚀")
            state["mode"] = "AUTO_LEARNING"
            # Reset để gửi ngay lập tức
            state["next_action_time"] = int(time.time())
            state["waiting_confirm"] = False
            save_user_state(user_id, state)
            return

        if any(cmd in msg for cmd in ['bắt đầu', 'bat dau', 'start']):
            start_auto_learning(user_id, state)
            return
        
        if any(cmd in msg for cmd in ['thoát', 'dừng', 'stop']):
            state["mode"] = "IDLE"
            save_user_state(user_id, state)
            send_fb_message(user_id, "Đã dừng. Hẹn gặp lại! 👋")
            return

        # --- XỬ LÝ THEO CHẾ ĐỘ ---
        mode = state.get("mode", "IDLE")

        if mode == "IDLE":
            reply = ai_chat_chit(message_text)
            send_fb_message(user_id, reply)

        elif mode == "AUTO_LEARNING":
            vn_now = get_vietnam_time()
            if 0 <= vn_now.hour < 6:
                send_fb_message(user_id, "🌙 Giờ đi ngủ (0h-6h). Mai học tiếp nhé!")
                return

            # LOGIC XÁC NHẬN HIỂU
            if state.get("waiting_confirm", False):
                if any(w in msg for w in ["hiểu", "ok", "rồi", "yes", "tiếp", "đã xem", "ok bot"]):
                    next_time = int(time.time()) + 600
                    state["next_action_time"] = next_time
                    state["waiting_confirm"] = False 
                    state["reminder_count"] = 0
                    
                    send_fb_message(user_id, f"Tuyệt vời! 👍 Đồng hồ đã chạy. 10 phút nữa mình sẽ gửi từ tiếp theo.")
                    save_user_state(user_id, state)
                else:
                    send_fb_message(user_id, "Bạn gõ 'Hiểu' hoặc 'OK' để mình bắt đầu tính giờ 10 phút nhé!")
            else:
                if "tiếp" in msg:
                    state["next_action_time"] = int(time.time()) 
                    save_user_state(user_id, state)
                else:
                    # Nếu hỏi linh tinh khi đang chờ giờ
                    remain = state.get("next_action_time", 0) - int(time.time())
                    if remain > 0:
                        minutes = remain // 60
                        send_fb_message(user_id, f"Còn {minutes} phút nữa. Gõ 'Tiếp' để học luôn, hoặc 'Tiến độ' để xem thống kê.")

        elif mode == "QUIZ":
            check_quiz_answer(user_id, state, message_text)
            
    except Exception as e:
        logger.error(f"FATAL ERROR in logic: {e}")

def start_auto_learning(user_id, state):
    state["mode"] = "AUTO_LEARNING"
    state["session_words"] = [] 
    
    learned_count = len(state.get("learned_history", []))
    total_count = len(HSK_DATA)
    
    send_fb_message(user_id, f"🚀 Bắt đầu!\nTiến độ: {learned_count}/{total_count}.\nGửi ngay từ đầu tiên...")
    
    state["next_action_time"] = int(time.time())
    state["waiting_confirm"] = False 
    save_user_state(user_id, state)

def send_next_auto_word(user_id, state):
    vn_now = get_vietnam_time()
    if 0 <= vn_now.hour < 6: return 

    if len(state["session_words"]) >= 6:
        start_quiz_session(user_id, state)
        return

    learned_history = set(state.get("learned_history", []))
    available_words = [w for w in HSK_DATA if w['Hán tự'] not in learned_history]
    
    if not available_words:
        send_fb_message(user_id, "🎉 Đã học hết thư viện từ! Reset lại nhé.")
        state["learned_history"] = [] 
        available_words = HSK_DATA 
        learned_history = set()

    new_word = random.choice(available_words)
    state["session_words"].append(new_word)
    
    current_history = state.get("learned_history", [])
    if new_word['Hán tự'] not in current_history:
        current_history.append(new_word['Hán tự'])
        state["learned_history"] = current_history

    ex = ai_generate_example_smart(new_word)
    progress_str = f"{len(current_history)}/{len(HSK_DATA)}"
    
    content = (
        f"🔔 [Từ #{len(state['session_words'])} - Tổng {progress_str}]\n"
        f"📖 {new_word['Hán tự']} ({new_word['Pinyin']})\n"
        f"Nghĩa: {new_word['Nghĩa']}\n"
        f"----------------\n"
        f"Ví dụ: {ex['han']}\n{ex['pinyin']}\n👉 {ex['viet']}\n\n"
        f"👉 Gõ 'Hiểu' để bắt đầu đếm ngược 10 phút cho từ tiếp theo."
    )
    send_fb_message(user_id, content)
    
    state["waiting_confirm"] = True
    state["next_action_time"] = int(time.time()) + 999999 
    state["last_msg_time"] = int(time.time()) 
    save_user_state(user_id, state)

def start_quiz_session(user_id, state):
    state["mode"] = "QUIZ"
    state["current_index"] = 0
    state["quiz_score"] = 0
    state["waiting_confirm"] = False
    save_user_state(user_id, state)
    
    send_fb_message(user_id, "⏰ Đã đủ 6 từ! Kiểm tra ngay nào...")
    time.sleep(2)
    send_quiz_question(user_id, state)

# --- 7. LOGIC QUIZ ---

def send_quiz_question(user_id, state):
    if state["current_index"] >= len(state["session_words"]):
        finish_session(user_id, state)
        return

    word = state["session_words"][state["current_index"]]
    state["current_quiz_word"] = word
    
    mode_idx = state["current_index"] % 5
    MODES = ["HAN_VIET", "VIET_HAN", "SENT_HAN_VIET", "SENT_VIET_HAN", "FILL_BLANK"]
    q_type = MODES[mode_idx]
    state["quiz_type"] = q_type
    state["quiz_options"] = {} 

    if q_type == "HAN_VIET":
        q = f"❓ Câu {state['current_index']+1}: Chữ [{word['Hán tự']}] nghĩa là gì?"
    elif q_type == "VIET_HAN":
        q = f"❓ Câu {state['current_index']+1}: Chữ Hán của từ '{word['Nghĩa']}' viết thế nào?"
    elif q_type == "SENT_HAN_VIET":
        q = f"❓ Câu {state['current_index']+1} (Dịch câu):\n🇨🇳 {word.get('Ví dụ', '')}\n👉 Hãy dịch sang tiếng Việt."
    elif q_type == "SENT_VIET_HAN":
        q = f"❓ Câu {state['current_index']+1} (Dịch câu):\n🇻🇳 {word.get('Dịch câu', '')}\n👉 Hãy viết lại câu bằng chữ Hán."
    elif q_type == "FILL_BLANK":
        origin_sent = word.get('Ví dụ', '')
        hanzi = word['Hán tự']
        question_text = origin_sent.replace(hanzi, "_____")
        distractors = random.sample([w for w in HSK_DATA if w['Hán tự'] != hanzi], 3)
        options = [word] + distractors
        random.shuffle(options)
        option_map = {}
        opt_text = ""
        for i, w in enumerate(options):
            key = chr(65 + i) 
            option_map[key] = w['Hán tự']
            opt_text += f"{key}. {w['Hán tự']}\n"
        state["quiz_options"] = option_map
        q = f"❓ Câu {state['current_index']+1} (Điền từ):\n{question_text}\n\nChọn đáp án:\n{opt_text}\n👉 Gõ A, B, C hoặc D."

    save_user_state(user_id, state)
    send_fb_message(user_id, q)

def check_quiz_answer(user_id, state, user_ans):
    target = state.get("current_quiz_word", {})
    if not target: return

    user_ans = user_ans.lower().strip()
    is_correct = False
    
    pinyin = target.get('Pinyin', '')
    meaning = target.get('Nghĩa', '')
    explanation = f"Đáp án: {target['Hán tự']} ({pinyin}) - {meaning}"
    q_type = state.get("quiz_type", "HAN_VIET")

    if q_type == "HAN_VIET":
        if any(kw in user_ans for kw in meaning.lower().replace(",", " ").split() if len(kw) > 1): is_correct = True
    elif q_type == "VIET_HAN":
        if target['Hán tự'] in user_ans: is_correct = True
    elif q_type == "SENT_HAN_VIET":
        if len(user_ans) > 5: is_correct = True
        explanation = f"Dịch: {target.get('Dịch câu', '')}"
    elif q_type == "SENT_VIET_HAN":
        if target['Hán tự'] in user_ans: is_correct = True
        explanation = f"Câu mẫu: {target.get('Ví dụ', '')}"
    elif q_type == "FILL_BLANK":
        correct_char = [k for k, v in state["quiz_options"].items() if v == target['Hán tự']]
        if (correct_char and user_ans.upper() == correct_char[0]) or target['Hán tự'] in user_ans:
            is_correct = True
        explanation = f"Đáp án: {target['Hán tự']}. Câu: {target.get('Ví dụ', '')}"

    msg = f"✅ Chính xác!\n{explanation}" if is_correct else f"❌ Sai rồi.\n{explanation}"
    state["quiz_score"] += 1 if is_correct else 0
    send_fb_message(user_id, msg)
    
    state["current_index"] += 1
    save_user_state(user_id, state)
    time.sleep(1.5)
    send_quiz_question(user_id, state)

def finish_session(user_id, state):
    score = state["quiz_score"]
    total = len(state["session_words"])
    msg = f"🏆 KẾT QUẢ: {score}/{total}.\nChuẩn bị từ tiếp theo..."
    send_fb_message(user_id, msg)
    
    state["mode"] = "AUTO_LEARNING"
    state["session_words"] = [] 
    state["next_action_time"] = int(time.time())
    state["waiting_confirm"] = False
    save_user_state(user_id, state)

# --- 8. LUỒNG CHẠY NGẦM ---

def auto_learning_loop():
    logger.info("--> Auto Learning Loop started.")
    while True:
        try:
            time.sleep(30) 
            
            vn_now = get_vietnam_time()
            if 0 <= vn_now.hour < 6: continue

            now_ts = int(time.time())
            active_users = list(USER_CACHE.items())
            
            for user_id, state in active_users:
                mode = state.get("mode", "IDLE")
                
                if mode != "AUTO_LEARNING": continue

                if not state.get("waiting_confirm", False):
                    next_time = state.get("next_action_time", 0)
                    if now_ts >= next_time:
                        logger.info(f"Time reached. Sending word to {user_id}")
                        send_next_auto_word(user_id, state)

                else:
                    last_msg = state.get("last_msg_time", 0)
                    if now_ts - last_msg > 900: 
                        reminder_count = state.get("reminder_count", 0)
                        if reminder_count < 1: 
                            send_fb_message(user_id, "🔔 Bạn ơi, bạn đã hiểu từ vừa rồi chưa? Gõ 'Hiểu' để mình đếm giờ gửi từ tiếp theo nhé!")
                            state["reminder_count"] = 1
                            state["last_msg_time"] = int(time.time()) 
                            save_user_state(user_id, state, )

        except Exception as e:
            logger.error(f"Loop Error: {e}")

# --- 9. ROUTES ---

@app.get("/")
def home(): return PlainTextResponse("HSK Server Running.")

@app.get("/webhook")
def verify(request: Request):
    if request.query_params.get("hub.verify_token") == VERIFY_TOKEN:
        return PlainTextResponse(request.query_params.get("hub.challenge"))
    return PlainTextResponse("Error", 403)

@app.post("/webhook")
async def webhook(request: Request, bg_tasks: BackgroundTasks):
    try:
        data = await request.json()
        
        # LOG CHI TIẾT GÓI TIN NHẬN ĐƯỢC
        logger.info(f"RECEIVED PAYLOAD: {json.dumps(data)}")
        
        if 'entry' in data:
            for e in data['entry']:
                for m in e.get('messaging', []):
                    if 'message' in m:
                        sender_id = m['sender']['id']
                        text = m['message'].get('text', '')
                        if text:
                            bg_tasks.add_task(process_message_background, sender_id, text)
        return PlainTextResponse("EVENT_RECEIVED")
    except Exception as e:
        logger.error(f"WEBHOOK ERROR: {e}")
        return PlainTextResponse("ERROR", 500)

if __name__ == "__main__":
    t = threading.Thread(target=auto_learning_loop, daemon=True)
    t.start()
    uvicorn.run(app, host="0.0.0.0", port=8000)
