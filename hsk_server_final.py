import sys
import os
from fastapi import FastAPI, Request, HTTPException, BackgroundTasks
from starlette.responses import PlainTextResponse
import uvicorn
import random
import requests
import json
from typing import List, Dict, Any, Optional
import time
import psycopg2 # Thư viện PostgreSQL

# --- CẤU HÌNH DATABASE ---
DATABASE_URL = os.environ.get('DATABASE_URL')
if not DATABASE_URL:
    print("CẢNH BÁO: KHÔNG TÌM THẤY DATABASE_URL. Dữ liệu sẽ không được lưu.")
    DB = None
else:
    try:
        # Connect to PostgreSQL and initialize table
        CONN = psycopg2.connect(DATABASE_URL, sslmode='require')
        CURSOR = CONN.cursor()
        
        # Tạo bảng nếu chưa tồn tại
        CURSOR.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id VARCHAR(50) PRIMARY KEY,
                state JSONB,
                last_study_time INTEGER
            );
        """)
        CONN.commit()
        DB = "Postgres" # Dùng chuỗi đánh dấu đã kết nối
        print("--> Kết nối PostgreSQL thành công và khởi tạo bảng.")
        
    except Exception as e:
        print(f"--> LỖI KẾT NỐI POSTGRESQL: {e}. Dữ liệu sẽ không được lưu.")
        DB = None 

# --- FACEBOOK CONFIGURATION (MANDATORY) ---
PAGE_ACCESS_TOKEN = "EAAbQQNNSmSMBQCSLHPqo2Y2HfW8GvdyfPc6oOCqVb8X61h6HadIILwTn7uDkZAIqgdEKEDMDFmhNYfoPVSevT907qEpFE5OYZC9VtfEwyR1uZA3b49k5VlBVZAPpfmsFqURLl5Pn0P4LZAaxWMzhuHmEhJeZB6Gq1NXeZAxQ3dp940k3P2VMJmjorafaFWeiAvU7YtOZCgZDZD"
VERIFY_TOKEN = "hsk_mat_khau_bi_mat" 
WORDS_PER_SESSION = 10 
REMINDER_INTERVAL_SECONDS = 3600 # 1 hour = 3600 seconds

# --- HSK DATA IMPORT ---
try:
    import hsk2_vocabulary_full as hsk_data
    HSK_DATA: List[Dict[str, Any]] = hsk_data.HSK_DATA
    # Tạo bản đồ từ Hán tự -> từ vựng để tra cứu nhanh
    HSK_MAP = {word["Hán tự"]: word for word in HSK_DATA}
    print(f"--> Successfully loaded {len(HSK_DATA)} vocabulary items.")
except ImportError:
    HSK_DATA = [{"Hán tự": "你好", "Pinyin": "nǐhǎo", "Nghĩa": "xin chào", "Ví dụ": "你好吗", "Dịch câu": "Bạn khỏe không"}]
    HSK_MAP = {word["Hán tự"]: word for word in HSK_DATA}

# Define Quiz Modes (Matching PC App logic)
BOT_MODES = [
    {"name": "hanzi_to_viet", "title": "DẠNG 1: [HÁN TỰ -> NGHĨA]"},
    {"name": "viet_to_hanzi", "title": "DẠNG 2: [NGHĨA -> HÁN TỰ]"},
    {"name": "example_to_hanzi", "title": "DẠNG 3: [ĐIỀN VÀO CHỖ TRỐNG]"},
    {"name": "translate_sentence", "title": "DẠNG 4: [DỊCH CÂU -> TRUNG]"}
]

app = FastAPI()

# --- DATABASE HANDLERS (POSTGRESQL) ---

def get_user_state(user_id: str) -> Dict[str, Any]:
    """Retrieves user state from PostgreSQL, or returns a default state."""
    default_state = {
        "session_hanzi": [], 
        "mode_index": 0, 
        "task_queue": [], 
        "backup_queue": [],
        "mistake_made": False, 
        "current_task": None, 
        "score": 0, "total_questions": 0,
        "last_study_time": 0, "reminder_sent": False
    }
    if DB:
        try:
            CURSOR.execute("SELECT state FROM users WHERE user_id = %s", (user_id,))
            result = CURSOR.fetchone()
            if result:
                # PostgreSQL JSONB column returns a Python dict
                return result[0]
            else:
                # Insert default state if user not found
                save_user_state(user_id, default_state, update_time=False)
                return default_state
        except Exception as e:
            print(f"LỖI POSTGRESQL KHI ĐỌC: {e}. Sử dụng trạng thái mặc định.")
            return default_state
    return default_state

def save_user_state(user_id: str, state: Dict[str, Any], update_time: bool = True):
    """Saves user state to PostgreSQL."""
    if DB:
        try:
            if update_time:
                state["last_study_time"] = time.time()
                state["reminder_sent"] = False # <--- BỎ RESET FLAG NẾU KHÔNG CÓ TƯƠNG TÁC THỰC SỰ
            
            # Use ON CONFLICT to UPSERT (UPDATE if exists, INSERT if not exists)
            CURSOR.execute("""
                INSERT INTO users (user_id, state, last_study_time)
                VALUES (%s, %s, %s)
                ON CONFLICT (user_id) DO UPDATE
                SET state = EXCLUDED.state, last_study_time = EXCLUDED.last_study_time
            """, (user_id, json.dumps(state), state.get("last_study_time", 0))) # SỬ DỤNG GET ĐỂ TRÁNH LỖI KEY ERROR NẾU KHÔNG UPDATE TIME
            CONN.commit()
            
        except Exception as e:
            print(f"LỖI POSTGRESQL KHI GHI: {e}. Dữ liệu không được lưu.")
            CONN.rollback()
            
# --- BOT QUIZ LOGIC (FIXED) ---

def start_new_session_bot(user_id: str) -> str:
    state = get_user_state(user_id)
    session_words = random.sample(HSK_DATA, min(WORDS_PER_SESSION, len(HSK_DATA)))
    
    state["session_hanzi"] = [word["Hán tự"] for word in session_words]
    state.update({"mode_index": 0, "score": 0, "total_questions": 0})
    save_user_state(user_id, state, update_time=True) # Cập nhật thời gian khi BẮT ĐẦU
    
    return load_next_mode_bot(user_id)

def load_next_mode_bot(user_id: str) -> str:
    state = get_user_state(user_id)
    
    if state["mode_index"] >= len(BOT_MODES):
        state["task_queue"] = []; state["current_task"] = None
        save_user_state(user_id, state, update_time=True) # Cập nhật thời gian khi KẾT THÚC
        return "🎉 CHÚC MỪNG! Bạn đã hoàn thành xuất sắc phiên học này!\n\nGõ 'học' để bắt đầu phiên mới."

    current_mode = BOT_MODES[state["mode_index"]]
    
    state["task_queue"] = []
    for hanzi in state["session_hanzi"]:
        state["task_queue"].append({"hanzi": hanzi, "mode_name": current_mode["name"]})
        
    random.shuffle(state["task_queue"])
    state["backup_queue"] = list(state["task_queue"])
    state["mistake_made"] = False
    
    save_user_state(user_id, state, update_time=True) # Cập nhật thời gian khi CHUYỂN DẠNG

    return f"🌟 BẮT ĐẦU DẠNG {state['mode_index'] + 1}: {current_mode['title']}\n\n" + get_next_question(user_id, is_new_mode=True)

def get_next_question(user_id: str, is_new_mode: bool = False) -> str:
    state = get_user_state(user_id)

    if not state["task_queue"]:
        if state["mistake_made"]:
            state["task_queue"] = list(state["backup_queue"])
            random.shuffle(state["task_queue"])
            state["mistake_made"] = False
            save_user_state(user_id, state, update_time=True) # Cập nhật thời gian khi LÀM LẠI
            return "❌ BẠN ĐÃ SAI!\nLàm lại Dạng này cho đến khi đúng hết 100% nhé.\n\n" + get_next_question(user_id)
        else:
            state["mode_index"] += 1
            state["current_task"] = None 
            save_user_state(user_id, state, update_time=True) # Cập nhật thời gian khi HOÀN THÀNH

            if state["mode_index"] >= len(BOT_MODES):
                return load_next_mode_bot(user_id) 
            else:
                return f"✅ HOÀN THÀNH DẠNG BÀI {state['mode_index']}/{len(BOT_MODES)}!\n\nGõ `tiếp tục` để bắt đầu Dạng bài mới nhé."
            
    task = state["task_queue"].pop(0)
    state["current_task"] = task
    
    if not is_new_mode:
        state["total_questions"] += 1
    
    save_user_state(user_id, state, update_time=True) # Cập nhật thời gian khi GỬI CÂU HỎI MỚI
    
    hanzi = task["hanzi"]
    word = HSK_MAP.get(hanzi, HSK_DATA[0])
    mode = task["mode_name"]
    remaining = len(state['task_queue']) + 1
    
    if mode == "hanzi_to_viet":
        return f"({remaining} câu còn lại)\nTừ này nghĩa là gì?\n🇨🇳 {word['Hán tự']} ({word['Pinyin']})"
    elif mode == "viet_to_hanzi":
        return f"({remaining} câu còn lại)\nViết Hán tự cho từ có nghĩa là:\n🇻🇳 {word['Nghĩa']}"
    elif mode == "example_to_hanzi":
        masked = word["Ví dụ"].replace(word["Hán tự"], "___")
        return f"({remaining} câu còn lại)\nViết Hán tự còn thiếu:\n{masked}\n({word['Dịch câu']})"
    elif mode == "translate_sentence":
        return f"({remaining} câu còn lại)\nDịch câu sau sang Hán tự:\n🇻🇳 {word['Dịch câu']}\n(Gợi ý: {word['Pinyin']})"
    
    return "Lỗi nạp câu hỏi."

def check_answer_bot(user_id: str, answer: str) -> str:
    state = get_user_state(user_id)
    if not state or not state["current_task"]: return "Xin lỗi, hình như chưa có câu hỏi nào. Gõ 'học' để bắt đầu nhé!"

    hanzi = state["current_task"]["hanzi"]
    word = HSK_MAP.get(hanzi, HSK_DATA[0])
    mode = state["current_task"]["mode_name"]
    is_correct = False
    
    if mode == "hanzi_to_viet":
        keywords = word["Nghĩa"].lower().split(',')
        is_correct = any(k.strip() in answer.lower() for k in keywords) or (answer.lower() in word["Nghĩa"].lower())
    elif mode in ["viet_to_hanzi", "example_to_hanzi"]:
        is_correct = (answer == word["Hán tự"])
    elif mode == "translate_sentence":
        is_correct = (answer == word["Ví dụ"] or word["Hán tự"] in answer)
        
    if is_correct:
        state["score"] += 1
        feedback = "✅ CHÍNH XÁC!"
    else:
        state["mistake_made"] = True
        feedback = (f"❌ SAI RỒI!\nĐáp án đúng là: 🇨🇳 {word['Hán tự']} ({word['Pinyin']})\n🇻🇳 Nghĩa: {word['Nghĩa']}\nCâu mẫu: {word['Ví dụ']}")
    
    save_user_state(user_id, state, update_time=True) # Cập nhật thời gian khi TRẢ LỜI
    return feedback + "\n\n" + get_next_question(user_id)

def process_chat_logic(user_id: str, user_text: str) -> str:
    user_text = user_text.lower().strip()
    state = get_user_state(user_id)
    
    # Hướng dẫn (KHÔNG CẦN CẬP NHẬT LAST_STUDY_TIME)
    if user_text in ["hướng dẫn", "help", "menu"]:
        return (
            f"📚 HƯỚNG DẪN SỬ DỤNG HSK BOT\n\n"
            f"1. Bắt đầu phiên học:\n"
            f"   Gõ: `học` hoặc `bắt đầu`\n"
            f"2. Tiếp tục Dạng bài:\n"
            f"   Gõ: `tiếp tục`\n"
            f"3. Các lệnh trong khi học:\n"
            f"   - Gõ: `bỏ qua` hoặc `dap an`: Xem đáp án và chuyển sang câu mới.\n"
            f"   - Gõ: `điểm` hoặc `score`: Xem thống kê kết quả hiện tại.\n"
        )

    # 1. Xử lý lệnh TIẾP TỤC (Chuyển mode) - CÓ CẬP NHẬT THỜI GIAN
    if user_text in ["tiếp tục"]:
        if state["current_task"] is None and not state["task_queue"]:
            return load_next_mode_bot(user_id)
        else:
            return "Bạn đang học dở, hãy trả lời câu hỏi hiện tại trước."
            
    # 2. Trả lời câu hỏi (chạy trước để ưu tiên trả lời)
    if state["current_task"] is not None:
        return check_answer_bot(user_id, user_text)
    
    # 3. Logic bắt đầu (chỉ chạy khi không có câu hỏi nào đang chờ) - CÓ CẬP NHẬT THỜI GIAN
    if user_text in ["học", "bắt đầu", "start"]: 
        return start_new_session_bot(user_id)
    
    # 4. Lệnh khác
    elif user_text in ["bỏ qua", "skip", "dap an"]:
        # CÓ CẬP NHẬT THỜI GIAN
        if state["current_task"] is not None:
            state["mistake_made"] = True
            hanzi = state["current_task"]["hanzi"]
            word = HSK_MAP.get(hanzi, HSK_DATA[0])
            next_question = get_next_question(user_id)
            save_user_state(user_id, state, update_time=True) # Cập nhật thời gian khi BỎ QUA
            return (f"⏩ Bỏ qua\nĐáp án là: 🇨🇳 {word['Hán tự']} ({word['Pinyin']})\n🇻🇳 Nghĩa: {word['Nghĩa']}\n\n") + next_question
        else:
            return "Bạn chưa bắt đầu học. Gõ 'học' để nhận câu hỏi."
            
    # Lệnh tra cứu (KHÔNG CẦN CẬP NHẬT LAST_STUDY_TIME)
    elif user_text in ["điểm", "score"]: 
        return f"📊 KẾT QUẢ HIỆN TẠI:\n\nĐúng: {state['score']}/{state['total_questions']}. Tiếp tục làm bài nhé!"
        
    else: 
        return "Chào bạn! Gõ 'học' để bắt đầu ôn tập nhanh.\n(Gõ 'điểm' hoặc 'hướng dẫn' để xem thêm)."


# --- REMINDER LOGIC ---

def check_and_send_reminders_async():
    """Background task to check all users and send reminders after 1 hour."""
    if not DB:
        print("Cannot check reminders: DB connection error.")
        return
    
    try:
        # Lấy tất cả người dùng từ DB
        CURSOR.execute("SELECT user_id, state, last_study_time FROM users WHERE last_study_time > 0")
        docs = CURSOR.fetchall()
        current_time = time.time()
        
        for user_id, state, last_study_time in docs:
            
            # Check if 1 hour passed and reminder hasn't been sent
            if (current_time - last_study_time) > REMINDER_INTERVAL_SECONDS and not state.get('reminder_sent', False):
                
                reminder_message = "🔔 Đã 1 tiếng rồi! Bạn có muốn học tiếp không?\n\nGõ 'học' để tiếp tục phiên học HSK của bạn nhé!"
                send_facebook_message(user_id, reminder_message)
                
                # Cập nhật cờ nhắc nhở trong DB
                state['reminder_sent'] = True
                save_user_state(user_id, state, update_time=False) # update_time=False: CHỈ CẬP NHẬT FLAG
                print(f"--> Sent reminder to user: {user_id}")
                
    except Exception as e:
        print(f"LỖI POSTGRESQL KHI KIỂM TRA NHẮC NHỞ: {e}")
        
# --- API ENDPOINTS ---

@app.get("/check_reminders")
async def check_reminders_endpoint(background_tasks: BackgroundTasks):
    """API called by the Render Cron Job to trigger the reminder check."""
    background_tasks.add_task(check_and_send_reminders_async)
    return {"status": "Reminder check started in background."}

@app.get("/api/new_session")
def create_new_session_pc(count: int = 10):
    session_words = random.sample(HSK_DATA, min(count, len(HSK_DATA)))
    return {"message": "ok", "data": session_words}

@app.get("/webhook")
async def verify_webhook(request: Request):
    mode = request.query_params.get("hub.mode")
    token = request.query_params.get("hub.verify_token")
    challenge = request.query_params.get("hub.challenge")
    if mode and token:
        if mode == "subscribe" and token == VERIFY_TOKEN:
            return PlainTextResponse(str(challenge))
        else:
            raise HTTPException(status_code=403, detail="Sai mật khẩu Verify Token")
    return {"status": "Đây là đường dẫn Webhook"}

@app.post("/webhook")
async def handle_message(request: Request):
    data = await request.json()
    if data.get("object") == "page":
        for entry in data.get("entry", []):
            for event in entry.get("messaging", []):
                if "message" in event:
                    sender_id = event["sender"]["id"]
                    text = event["message"].get("text", "")
                    
                    reply_text = process_chat_logic(sender_id, text)
                    send_facebook_message(sender_id, reply_text)
                    
        return {"status": "EVENT_RECEIVED"}
    else:
        raise HTTPException(status_code=404)

def send_facebook_message(recipient_id, text):
    params = {"access_token": PAGE_ACCESS_TOKEN}
    headers = {"Content-Type": "application/json"}
    data = {
        "recipient": {"id": recipient_id},
        "message": {"text": text}
    }
    r = requests.post("https://graph.facebook.com/v21.0/me/messages", params=params, headers=headers, json=data)
    if r.status_code != 200:
        print(f"Lỗi gửi tin: {r.text}")

if __name__ == "__main__":
    print("Đang khởi động Server HSK...")
    # SỬA LỖI: Đảm bảo chạy đúng module name
    uvicorn.run("hsk_server_final:app", host="127.0.0.1", port=8000, reload=True)
