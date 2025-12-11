import sys
import os
import time
import json
import random
import re
import requests
import logging
from typing import Dict, Any, List, Optional

# --- CÁC THƯ VIỆN CHÍNH ---
from fastapi import FastAPI, Request, BackgroundTasks
from starlette.responses import PlainTextResponse
import uvicorn
import psycopg2
from psycopg2 import pool
import google.generativeai as genai

# --- 0. CẤU HÌNH LOGGING (ĐỂ DỄ DEBUG) ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# --- 1. CẤU HÌNH HỆ THỐNG ---

# Token Facebook (Lưu ý: Giữ bí mật trong môi trường Production)
PAGE_ACCESS_TOKEN = "EAAbQQNNSmSMBQKWd5qB15zFMy2KdPm6Ko1rJX6R4ZC3EtnNfvf0gT76V1Qk4l1vflxL1pDVwY8mrgbgAaFFtG6bzcrhJfQ86HdK5v8qZA9zTIge2ZBJcx9oNPOjk1DlQ8juGinZBuah0RDgbCd2vBvlNWr47GVz70BdPNzKRctCGphNJRI0Wm57UwKRmXOZAVfDP7zwZDZD"
VERIFY_TOKEN = "hsk_mat_khau_bi_mat"

# Google Gemini API Key
GEMINI_API_KEY = "AIzaSyB5V6sgqSOZO4v5DyuEZs3msgJqUk54HqQ"

# Database URL
DATABASE_URL = os.environ.get('DATABASE_URL') 

# Cấu hình AI
try:
    genai.configure(api_key=GEMINI_API_KEY)
    model = genai.GenerativeModel('gemini-1.5-flash')
except Exception as e:
    logger.error(f"Lỗi cấu hình AI: {e}")

# --- 2. NẠP DỮ LIỆU TỪ VỰNG ---
try:
    import hsk2_vocabulary_full as hsk_data
    HSK_DATA = hsk_data.HSK_DATA
    HSK_MAP = {word["Hán tự"]: word for word in HSK_DATA}
    logger.info(f"--> [SYSTEM] Đã nạp thành công {len(HSK_DATA)} từ vựng HSK 2.")
except ImportError:
    logger.error("--> [ERROR] Không tìm thấy file 'hsk2_vocabulary_full.py'.")
    # Dữ liệu giả lập để server không bị crash
    HSK_DATA = [{"Hán tự": "你好", "Pinyin": "nǐhǎo", "Nghĩa": "xin chào", "Ví dụ": "你好!", "Ví dụ Pinyin": "Nǐ hǎo!", "Dịch câu": "Chào bạn!"}]
    HSK_MAP = {word["Hán tự"]: word for word in HSK_DATA}

# --- 3. TỐI ƯU KẾT NỐI DATABASE ---
db_pool = None
if DATABASE_URL:
    try:
        db_pool = psycopg2.pool.ThreadedConnectionPool(1, 10, DATABASE_URL, sslmode='require')
        logger.info("--> [DB] Connection Pool đã sẵn sàng.")
    except Exception as e:
        logger.error(f"--> [DB ERROR] Không thể kết nối DB: {e}. Chuyển sang chế độ RAM.")

# Cache bộ nhớ (RAM)
USER_CACHE = {}

app = FastAPI()

# --- 4. HÀM QUẢN LÝ TRẠNG THÁI NGƯỜI DÙNG (CỰC KỲ QUAN TRỌNG) ---

def get_db_conn():
    if db_pool:
        return db_pool.getconn()
    return None

def release_db_conn(conn):
    if db_pool and conn:
        db_pool.putconn(conn)

def get_user_state(user_id: str) -> Dict[str, Any]:
    """
    Lấy trạng thái người dùng.
    ĐẢM BẢO: Luôn trả về đầy đủ các trường dữ liệu, không bao giờ thiếu key.
    """
    # Trạng thái mặc định chuẩn
    default_state = {
        "user_id": user_id,
        "mode": "IDLE",            # IDLE, LEARNING, QUIZ
        "session_words": [],       
        "current_index": 0,        
        "quiz_score": 0,           
        "current_quiz_word": None, 
        "quiz_type": None,         
        "last_interaction": 0
    }

    # 1. Check Cache
    if user_id in USER_CACHE:
        # Merge với default để đảm bảo nếu cache cũ thiếu key thì vẫn có
        cached_state = USER_CACHE[user_id]
        if not isinstance(cached_state, dict): cached_state = {}
        merged = default_state.copy()
        merged.update(cached_state)
        return merged

    # 2. Check DB
    if db_pool:
        conn = get_db_conn()
        try:
            with conn.cursor() as cur:
                # Tạo bảng nếu chưa có
                cur.execute("CREATE TABLE IF NOT EXISTS users (user_id VARCHAR(50) PRIMARY KEY, state JSONB);")
                
                cur.execute("SELECT state FROM users WHERE user_id = %s", (user_id,))
                res = cur.fetchone()
                
                if res:
                    db_data = res[0]
                    if not isinstance(db_data, dict): db_data = {}
                    
                    # --- AUTO FIX DỮ LIỆU ---
                    # Lấy default làm gốc, đè dữ liệu DB lên
                    # Những key DB thiếu sẽ lấy từ default
                    final_state = default_state.copy()
                    final_state.update(db_data)
                    
                    # Lưu lại vào cache
                    USER_CACHE[user_id] = final_state
                    return final_state
        except Exception as e:
            logger.error(f"Lỗi đọc DB: {e}")
        finally:
            release_db_conn(conn)
    
    # Nếu không có gì cả, trả về default
    return default_state

def save_user_state(user_id: str, state: Dict[str, Any]):
    """Lưu trạng thái vào Cache và DB"""
    state["last_interaction"] = int(time.time())
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
            logger.error(f"Lỗi lưu DB: {e}")
        finally:
            release_db_conn(conn)

def reset_user_state(user_id: str):
    """Xóa sạch dữ liệu người dùng để sửa lỗi"""
    if user_id in USER_CACHE:
        del USER_CACHE[user_id]
    
    if db_pool:
        conn = get_db_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM users WHERE user_id = %s", (user_id,))
                conn.commit()
            logger.info(f"Đã reset user {user_id}")
        except Exception as e:
            logger.error(f"Lỗi reset DB: {e}")
        finally:
            release_db_conn(conn)

# --- 5. LOGIC AI & HELPER ---

def ai_chat_chit(message: str) -> str:
    try:
        prompt = f"Bạn là trợ lý HSK vui tính. Người dùng nói: '{message}'. Trả lời ngắn gọn, thân thiện và nhắc họ gõ 'Bắt đầu' để học."
        response = model.generate_content(prompt)
        return response.text.strip()
    except:
        return "Chào bạn! Gõ 'Bắt đầu' để học tiếng Trung cùng mình nhé! 😄"

def ai_generate_example_smart(word_data: dict) -> dict:
    hanzi = word_data.get('Hán tự', '')
    meaning = word_data.get('Nghĩa', '')
    
    # Backup
    backup = {
        "han": word_data.get('Ví dụ', '...'),
        "pinyin": word_data.get('Ví dụ Pinyin', '...'),
        "viet": word_data.get('Dịch câu', '...')
    }

    try:
        prompt = f"""
        Tạo ví dụ HSK2 cho từ: {hanzi} ({meaning}).
        Chỉ trả về JSON: {{"han": "...", "pinyin": "...", "viet": "..."}}
        """
        response = model.generate_content(prompt)
        text = response.text.strip()
        match = re.search(r'\{.*\}', text, re.DOTALL)
        if match:
            return json.loads(match.group())
        return backup
    except:
        return backup

def send_fb_message(user_id: str, text: str):
    params = {"access_token": PAGE_ACCESS_TOKEN}
    headers = {"Content-Type": "application/json"}
    data = {"recipient": {"id": user_id}, "message": {"text": text}}
    try:
        r = requests.post("https://graph.facebook.com/v16.0/me/messages", params=params, headers=headers, json=data)
        if r.status_code != 200:
            logger.error(f"FB Error: {r.text}")
    except Exception as e:
        logger.error(f"Request Error: {e}")

# --- 6. LOGIC CHÍNH (XỬ LÝ TIN NHẮN) ---

def process_message_background(user_id: str, message_text: str):
    """
    Hàm xử lý logic chính.
    Được gọi trong Background Tasks nên không làm chậm request.
    """
    # Lấy state an toàn (đã được auto-fix)
    state = get_user_state(user_id)
    msg = message_text.strip().lower()

    # --- LỆNH RESET CỨU HỘ ---
    if msg == "reset":
        reset_user_state(user_id)
        send_fb_message(user_id, "🔄 Đã khởi động lại hệ thống cho bạn. Gõ 'Bắt đầu' để học nhé!")
        return

    # --- ĐIỀU HƯỚNG LỆNH ---
    if any(cmd in msg for cmd in ['bắt đầu', 'bat dau', 'start', 'hoc', 'học đi']):
        start_new_session(user_id, state)
        return
    
    if any(cmd in msg for cmd in ['thoát', 'dừng', 'stop', 'quit']):
        state["mode"] = "IDLE"
        save_user_state(user_id, state)
        send_fb_message(user_id, "Đã dừng học. Bye bye! 👋")
        return

    # --- XỬ LÝ THEO CHẾ ĐỘ ---
    # Dùng .get() để tránh crash tuyệt đối
    mode = state.get("mode", "IDLE")

    if mode == "IDLE":
        reply = ai_chat_chit(message_text)
        send_fb_message(user_id, reply)

    elif mode == "LEARNING":
        if any(w in msg for w in ["tiếp", "next", "ok", "tiếp tục"]):
            send_next_word(user_id, state)
        else:
            send_fb_message(user_id, "💡 Gõ 'Tiếp' để sang từ mới nha.")

    elif mode == "QUIZ":
        check_quiz_answer(user_id, state, message_text)
    
    else:
        # Nếu mode bị lỗi lạ -> Reset về IDLE
        state["mode"] = "IDLE"
        save_user_state(user_id, state)
        send_fb_message(user_id, "Gõ 'Bắt đầu' để học nhé!")

def start_new_session(user_id, state):
    sample_size = min(5, len(HSK_DATA))
    if sample_size == 0:
        send_fb_message(user_id, "Hệ thống đang bảo trì dữ liệu.")
        return

    session_words = random.sample(HSK_DATA, sample_size)
    state.update({
        "mode": "LEARNING",
        "session_words": session_words,
        "current_index": 0,
        "quiz_score": 0
    })
    
    send_fb_message(user_id, f"🚀 Bắt đầu học {sample_size} từ vựng HSK 2 nhé!")
    send_learning_card(user_id, session_words[0])
    save_user_state(user_id, state)

def send_learning_card(user_id, word_data):
    ex = ai_generate_example_smart(word_data)
    content = (
        f"📖 TỪ MỚI: {word_data.get('Hán tự', '')} ({word_data.get('Pinyin', '')})\n"
        f"Nghĩa: {word_data.get('Nghĩa', '')}\n"
        f"----------------\n"
        f"Ví dụ:\n🇨🇳 {ex['han']}\n🗣️ {ex['pinyin']}\n🇻🇳 {ex['viet']}\n\n"
        f"👉 Gõ 'Tiếp' để học tiếp."
    )
    send_fb_message(user_id, content)

def send_next_word(user_id, state):
    idx = state["current_index"] + 1
    if idx < len(state["session_words"]):
        state["current_index"] = idx
        save_user_state(user_id, state)
        send_learning_card(user_id, state["session_words"][idx])
    else:
        state["mode"] = "QUIZ"
        state["current_index"] = 0
        state["quiz_score"] = 0
        save_user_state(user_id, state)
        send_fb_message(user_id, "🎉 Học xong rồi! Giờ làm bài kiểm tra nhé. 3...2...1...")
        time.sleep(1)
        send_quiz_question(user_id, state)

def send_quiz_question(user_id, state):
    if state["current_index"] >= len(state["session_words"]):
        finish_session(user_id, state)
        return

    word = state["session_words"][state["current_index"]]
    state["current_quiz_word"] = word
    
    q_type = random.choice(["HANZI_TO_VIET", "VIET_TO_HANZI"])
    state["quiz_type"] = q_type
    
    if q_type == "HANZI_TO_VIET":
        q = f"❓ Câu {state['current_index']+1}: Chữ [{word['Hán tự']}] nghĩa là gì?"
    else:
        q = f"❓ Câu {state['current_index']+1}: Chữ Hán của từ '{word['Nghĩa']}' viết thế nào?"
        
    save_user_state(user_id, state)
    send_fb_message(user_id, q)

def check_quiz_answer(user_id, state, user_ans):
    target = state.get("current_quiz_word", {})
    if not target: 
        send_next_word(user_id, state)
        return

    user_ans = user_ans.lower().strip()
    is_correct = False
    
    if state["quiz_type"] == "HANZI_TO_VIET":
        keywords = target['Nghĩa'].lower().replace(",", " ").split()
        if any(kw in user_ans for kw in keywords if len(kw) > 1):
            is_correct = True
    else:
        if target['Hán tự'] in user_ans:
            is_correct = True

    if is_correct:
        state["quiz_score"] += 1
        msg = "Chính xác! 🎯"
    else:
        msg = f"Sai rồi 🥲. Đáp án: {target['Hán tự']} ({target['Nghĩa']})"
    
    send_fb_message(user_id, msg)
    
    state["current_index"] += 1
    save_user_state(user_id, state)
    time.sleep(1)
    send_quiz_question(user_id, state)

def finish_session(user_id, state):
    score = state["quiz_score"]
    total = len(state["session_words"])
    msg = f"🏆 KẾT QUẢ: {score}/{total}. "
    msg += "Xuất sắc! 🌟" if score == total else "Cố gắng thêm nhé! 💪"
    msg += "\nGõ 'Bắt đầu' để học tiếp."
    
    send_fb_message(user_id, msg)
    state["mode"] = "IDLE"
    save_user_state(user_id, state)

# --- 7. FASTAPI ROUTES ---

@app.get("/")
def home():
    return PlainTextResponse("HSK Server is RUNNING. Webhook at /webhook")

@app.get("/webhook")
def verify_webhook(request: Request):
    if request.query_params.get("hub.verify_token") == VERIFY_TOKEN:
        return PlainTextResponse(request.query_params.get("hub.challenge"))
    return PlainTextResponse("Error", status_code=403)

@app.post("/webhook")
async def webhook_handler(request: Request, background_tasks: BackgroundTasks):
    try:
        data = await request.json()
        if 'entry' in data:
            for entry in data['entry']:
                for messaging in entry.get('messaging', []):
                    if 'message' in messaging:
                        sender_id = messaging['sender']['id']
                        text = messaging['message'].get('text', '')
                        if text:
                            background_tasks.add_task(process_message_background, sender_id, text)
        return PlainTextResponse("EVENT_RECEIVED")
    except Exception as e:
        logger.error(f"Webhook Error: {e}")
        return PlainTextResponse("ERROR", status_code=500)

if __name__ == "__main__":
    logger.info("--> Server starting...")
    uvicorn.run(app, host="0.0.0.0", port=8000)
