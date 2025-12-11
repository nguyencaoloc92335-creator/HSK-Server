import sys
import os
import time
import json
import random
import re
import requests
import threading
from typing import Dict, Any, List, Optional

# --- CÁC THƯ VIỆN CHÍNH ---
from fastapi import FastAPI, Request, BackgroundTasks
from starlette.responses import PlainTextResponse
import uvicorn
import psycopg2
from psycopg2 import pool
import google.generativeai as genai

# --- 1. CẤU HÌNH HỆ THỐNG (HARDCODE ĐỂ CHẠY NGAY) ---

# Token Facebook (Thay bằng Token thật của bạn nếu cần)
PAGE_ACCESS_TOKEN = "EAAbQQNNSmSMBQKWd5qB15zFMy2KdPm6Ko1rJX6R4ZC3EtnNfvf0gT76V1Qk4l1vflxL1pDVwY8mrgbgAaFFtG6bzcrhJfQ86HdK5v8qZA9zTIge2ZBJcx9oNPOjk1DlQ8juGinZBuah0RDgbCd2vBvlNWr47GVz70BdPNzKRctCGphNJRI0Wm57UwKRmXOZAVfDP7zwZDZD"
VERIFY_TOKEN = "hsk_mat_khau_bi_mat"

# Google Gemini API Key
GEMINI_API_KEY = "AIzaSyB5V6sgqSOZO4v5DyuEZs3msgJqUk54HqQ"

# Database URL (Nếu không có sẽ chạy chế độ bộ nhớ tạm - RAM)
DATABASE_URL = os.environ.get('DATABASE_URL') 

# Cấu hình AI
genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel('gemini-1.5-flash')

# --- 2. NẠP DỮ LIỆU TỪ VỰNG ---
try:
    import hsk2_vocabulary_full as hsk_data
    HSK_DATA = hsk_data.HSK_DATA
    # Tạo map để tra cứu nhanh từ Hán tự
    HSK_MAP = {word["Hán tự"]: word for word in HSK_DATA}
    print(f"--> [SYSTEM] Đã nạp thành công {len(HSK_DATA)} từ vựng HSK 2.")
except ImportError:
    print("--> [ERROR] Không tìm thấy file 'hsk2_vocabulary_full.py'. Hãy đảm bảo file này nằm cùng thư mục.")
    # Tạo dữ liệu giả để không crash app nếu thiếu file
    HSK_DATA = [{"Hán tự": "你好", "Pinyin": "nǐhǎo", "Nghĩa": "xin chào", "Ví dụ": "你好!", "Ví dụ Pinyin": "Nǐ hǎo!", "Dịch câu": "Chào bạn!"}]
    HSK_MAP = {word["Hán tự"]: word for word in HSK_DATA}

# --- 3. TỐI ƯU KẾT NỐI DATABASE (CONNECTION POOLING) ---
db_pool = None
if DATABASE_URL:
    try:
        # Tạo hồ chứa 5-20 kết nối sẵn sàng. Nhanh hơn gấp 10 lần so với kết nối đơn lẻ.
        db_pool = psycopg2.pool.ThreadedConnectionPool(5, 20, DATABASE_URL, sslmode='require')
        print("--> [DB] Connection Pool đã sẵn sàng.")
    except Exception as e:
        print(f"--> [DB ERROR] Không thể kết nối DB: {e}. Chuyển sang chế độ RAM.")

# Bộ nhớ đệm (Cache) để truy xuất siêu tốc
USER_CACHE = {}

app = FastAPI()

# --- 4. CÁC HÀM XỬ LÝ DATABASE & STATE ---

def get_db_conn():
    if db_pool:
        return db_pool.getconn()
    return None

def release_db_conn(conn):
    if db_pool and conn:
        db_pool.putconn(conn)

def get_user_state(user_id: str) -> Dict[str, Any]:
    """Lấy trạng thái người dùng (Ưu tiên Cache -> DB -> Mặc định)"""
    # 1. Check Cache (Nhanh nhất)
    if user_id in USER_CACHE:
        return USER_CACHE[user_id]

    # Cấu trúc mặc định của một người dùng
    default_state = {
        "user_id": user_id,
        "mode": "IDLE",            # IDLE (Rảnh), LEARNING (Học), QUIZ (Thi)
        "session_words": [],       # Danh sách từ đang học
        "current_index": 0,        # Vị trí hiện tại
        "quiz_score": 0,           # Điểm số
        "current_quiz_word": None, # Từ đang hỏi thi
        "quiz_type": None,         # Loại câu hỏi
        "last_interaction": 0
    }

    # 2. Check DB (Nếu cache không có)
    if db_pool:
        conn = get_db_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("CREATE TABLE IF NOT EXISTS users (user_id VARCHAR(50) PRIMARY KEY, state JSONB);")
                cur.execute("SELECT state FROM users WHERE user_id = %s", (user_id,))
                res = cur.fetchone()
                if res:
                    state = res[0]
                    USER_CACHE[user_id] = state # Cập nhật lại vào cache
                    return state
        except Exception as e:
            print(f"Lỗi đọc DB: {e}")
        finally:
            release_db_conn(conn)
    
    return default_state

def save_user_state(user_id: str, state: Dict[str, Any]):
    """Lưu trạng thái (Cập nhật Cache ngay lập tức + Lưu DB bất đồng bộ)"""
    state["last_interaction"] = int(time.time())
    USER_CACHE[user_id] = state # Update Cache
    
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
            print(f"Lỗi lưu DB: {e}")
        finally:
            release_db_conn(conn)

# --- 5. TRÍ TUỆ NHÂN TẠO & XỬ LÝ NGÔN NGỮ ---

def ai_chat_chit(message: str) -> str:
    """Bot giao tiếp tự nhiên khi người dùng không học"""
    try:
        # Prompt ngắn gọn, tự nhiên
        prompt = f"""Bạn là trợ lý học tiếng Trung HSK vui tính. 
        Người dùng nói: "{message}". 
        Trả lời ngắn gọn (dưới 20 từ), thân thiện, dùng emoji. 
        Cuối câu hãy nhắc họ gõ 'Bắt đầu' để học."""
        
        response = model.generate_content(prompt)
        return response.text.strip()
    except:
        return "Chào bạn! Mình là Bot HSK. Gõ 'Bắt đầu' để học ngay nhé! 😄"

def ai_generate_example_smart(word_data: dict) -> dict:
    """
    Tạo ví dụ thông minh. 
    Cơ chế Fallback: Nếu AI lỗi hoặc trả về sai định dạng -> Dùng ngay dữ liệu tĩnh trong sách.
    """
    hanzi = word_data['Hán tự']
    meaning = word_data['Nghĩa']
    
    # Dữ liệu dự phòng (Lấy từ file hsk2_vocabulary_full.py)
    backup_data = {
        "han": word_data.get('Ví dụ', 'N/A'),
        "pinyin": word_data.get('Ví dụ Pinyin', 'N/A'),
        "viet": word_data.get('Dịch câu', 'N/A')
    }

    try:
        prompt = f"""
        Tạo 1 ví dụ tiếng Trung HSK2 cực đơn giản cho từ: {hanzi} ({meaning}).
        Yêu cầu: Chỉ trả về JSON, không giải thích thêm.
        Format: {{"han": "Câu chữ Hán", "pinyin": "Pinyin có thanh điệu", "viet": "Dịch tiếng Việt"}}
        """
        response = model.generate_content(prompt)
        text = response.text.strip()
        
        # Dùng Regex để trích xuất JSON (đề phòng AI nói nhảm)
        match = re.search(r'\{.*\}', text, re.DOTALL)
        if match:
            return json.loads(match.group())
        else:
            return backup_data
    except Exception:
        # Nếu có bất kỳ lỗi gì (mạng, AI, parse), dùng backup ngay
        return backup_data

# --- 6. GỬI TIN NHẮN FACEBOOK ---

def send_fb_message(user_id: str, text: str):
    """Gửi tin nhắn qua Graph API"""
    params = {"access_token": PAGE_ACCESS_TOKEN}
    headers = {"Content-Type": "application/json"}
    data = {
        "recipient": {"id": user_id},
        "message": {"text": text}
    }
    try:
        r = requests.post("https://graph.facebook.com/v16.0/me/messages", params=params, headers=headers, json=data)
        if r.status_code != 200:
            print(f"FB Error: {r.text}")
    except Exception as e:
        print(f"Request Error: {e}")

# --- 7. LOGIC HỌC TẬP (CỐT LÕI) ---

def process_message_background(user_id: str, message_text: str):
    """Xử lý logic chính (Chạy ngầm để không block Facebook)"""
    state = get_user_state(user_id)
    msg = message_text.strip().lower()

    # --- NHẬN DIỆN LỆNH HỆ THỐNG ---
    if any(cmd in msg for cmd in ['bắt đầu', 'bat dau', 'start', 'hoc', 'học đi']):
        start_new_session(user_id, state)
        return
    
    if any(cmd in msg for cmd in ['thoát', 'dừng', 'stop', 'quit', 'nghỉ']):
        state["mode"] = "IDLE"
        save_user_state(user_id, state)
        send_fb_message(user_id, "Đã dừng bài học. Khi nào rảnh quay lại nhé! 👋")
        return

    # --- XỬ LÝ THEO CHẾ ĐỘ (STATE MACHINE) ---
    
    # 1. Chế độ Rảnh rỗi
    if state["mode"] == "IDLE":
        # Chat vui vẻ với AI
        reply = ai_chat_chit(message_text)
        send_fb_message(user_id, reply)

    # 2. Chế độ Học từ (Learning)
    elif state["mode"] == "LEARNING":
        if any(w in msg for w in ["tiếp", "next", "ok", "tiếp tục", "kế tiếp"]):
            send_next_word(user_id, state)
        else:
            send_fb_message(user_id, "💡 Gõ 'Tiếp' để sang từ mới, hoặc 'Dừng' để nghỉ nha.")

    # 3. Chế độ Thi (Quiz)
    elif state["mode"] == "QUIZ":
        check_quiz_answer(user_id, state, message_text)

def start_new_session(user_id, state):
    """Bắt đầu phiên mới: Chọn 5 từ ngẫu nhiên"""
    # Nếu file từ vựng ít hơn 5 từ thì lấy hết
    sample_size = min(5, len(HSK_DATA))
    session_words = random.sample(HSK_DATA, sample_size)
    
    state.update({
        "mode": "LEARNING",
        "session_words": session_words,
        "current_index": 0,
        "quiz_score": 0
    })
    
    send_fb_message(user_id, f"🚀 Tuyệt vời! Chúng ta sẽ học {sample_size} từ vựng HSK 2 nhé. Bắt đầu nào!")
    # Gửi từ đầu tiên luôn
    send_learning_card(user_id, session_words[0])
    save_user_state(user_id, state)

def send_learning_card(user_id, word_data):
    """Gửi thẻ học từ (Có AI hỗ trợ)"""
    # Lấy ví dụ thông minh (hoặc backup)
    ex = ai_generate_example_smart(word_data)
    
    content = (
        f"📖 TỪ MỚI: {word_data['Hán tự']} ({word_data['Pinyin']})\n"
        f"Nghĩa: {word_data['Nghĩa']}\n"
        f"----------------\n"
        f"Ví dụ:\n"
        f"🇨🇳 {ex['han']}\n"
        f"🗣️ {ex['pinyin']}\n"
        f"🇻🇳 {ex['viet']}\n\n"
        f"👉 Gõ 'Tiếp' để học từ sau."
    )
    send_fb_message(user_id, content)

def send_next_word(user_id, state):
    """Chuyển sang từ tiếp theo hoặc qua phần thi"""
    idx = state["current_index"] + 1
    if idx < len(state["session_words"]):
        state["current_index"] = idx
        save_user_state(user_id, state)
        send_learning_card(user_id, state["session_words"][idx])
    else:
        # Hết từ -> Chuyển sang Quiz
        state["mode"] = "QUIZ"
        state["current_index"] = 0
        state["quiz_score"] = 0
        save_user_state(user_id, state)
        send_fb_message(user_id, "🎉 Bạn đã học xong! Giờ mình kiểm tra chút nhé. Chuẩn bị...")
        time.sleep(1)
        send_quiz_question(user_id, state)

def send_quiz_question(user_id, state):
    """Gửi câu hỏi trắc nghiệm"""
    if state["current_index"] >= len(state["session_words"]):
        # Hết câu hỏi -> Tổng kết
        finish_session(user_id, state)
        return

    word = state["session_words"][state["current_index"]]
    state["current_quiz_word"] = word
    
    # Random loại câu hỏi để đỡ chán
    q_type = random.choice(["HANZI_TO_VIET", "VIET_TO_HANZI"])
    state["quiz_type"] = q_type
    
    if q_type == "HANZI_TO_VIET":
        q = f"❓ Câu {state['current_index']+1}: Chữ [{word['Hán tự']}] nghĩa là gì?"
    else:
        q = f"❓ Câu {state['current_index']+1}: Chữ Hán của từ '{word['Nghĩa']}' viết thế nào?"
        
    save_user_state(user_id, state)
    send_fb_message(user_id, q)

def check_quiz_answer(user_id, state, user_ans):
    """Chấm điểm câu trả lời"""
    target = state["current_quiz_word"]
    user_ans = user_ans.lower().strip()
    is_correct = False
    
    # Logic chấm điểm đơn giản nhưng hiệu quả
    if state["quiz_type"] == "HANZI_TO_VIET":
        # Check xem trong câu trả lời có từ khóa nghĩa đúng không
        keywords = target['Nghĩa'].lower().replace(",", " ").split()
        if any(kw in user_ans for kw in keywords if len(kw) > 1):
            is_correct = True
    else:
        # Check chữ Hán
        if target['Hán tự'] in user_ans:
            is_correct = True

    # Phản hồi
    if is_correct:
        state["quiz_score"] += 1
        msg = random.choice(["Chính xác! 🎯", "Giỏi quá! 👏", "Tuyệt vời! 🔥"])
    else:
        msg = f"Sai rồi 🥲. Đáp án đúng là: {target['Hán tự']} ({target['Nghĩa']})"
    
    send_fb_message(user_id, msg)
    
    # Câu tiếp theo
    state["current_index"] += 1
    save_user_state(user_id, state)
    time.sleep(1) # Nghỉ 1 xíu cho tự nhiên
    send_quiz_question(user_id, state)

def finish_session(user_id, state):
    """Tổng kết điểm"""
    score = state["quiz_score"]
    total = len(state["session_words"])
    
    if score == total:
        msg = f"🏆 KẾT QUẢ: {score}/{total}. Xuất sắc! Bạn thuộc hết bài rồi! 🌟"
    elif score >= total/2:
        msg = f"📊 KẾT QUẢ: {score}/{total}. Khá lắm, cố gắng thêm nhé! 💪"
    else:
        msg = f"📉 KẾT QUẢ: {score}/{total}. Cần ôn luyện thêm nha! 😅"
        
    msg += "\nGõ 'Bắt đầu' để học tiếp nhé."
    send_fb_message(user_id, msg)
    
    state["mode"] = "IDLE"
    save_user_state(user_id, state)

# --- 8. API ROUTE (FASTAPI) ---

@app.get("/")
def verify_webhook(request: Request):
    """Xác thực Webhook với Facebook"""
    if request.query_params.get("hub.verify_token") == VERIFY_TOKEN:
        return PlainTextResponse(request.query_params.get("hub.challenge"))
    return PlainTextResponse("Error", status_code=403)

@app.post("/")
async def webhook_handler(request: Request, background_tasks: BackgroundTasks):
    """
    Nhận tin nhắn từ Facebook.
    Quan trọng: Trả về 200 OK ngay lập tức, xử lý logic ở Background.
    """
    try:
        data = await request.json()
        if 'entry' in data:
            for entry in data['entry']:
                for messaging in entry.get('messaging', []):
                    if 'message' in messaging:
                        sender_id = messaging['sender']['id']
                        text = messaging['message'].get('text', '')
                        if text:
                            # Đẩy vào hàng đợi xử lý ngầm -> Web mượt mà
                            background_tasks.add_task(process_message_background, sender_id, text)
        return PlainTextResponse("EVENT_RECEIVED")
    except Exception as e:
        print(f"Error: {e}")
        return PlainTextResponse("ERROR", status_code=500)

if __name__ == "__main__":
    print("--> Server HSK đang khởi động...")
    uvicorn.run(app, host="0.0.0.0", port=8000)
