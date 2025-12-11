import sys
import os
from fastapi import FastAPI, Request, HTTPException, BackgroundTasks
from starlette.responses import PlainTextResponse
import uvicorn
import random
import requests
import json
from typing import List, Dict, Any, Optional
import firebase_admin 
from firebase_admin import credentials, firestore, initialize_app
import time

# --- CẤU HÌNH FIREBASE ---
try:
    # ĐÃ SỬA: Tìm kiếm file 'firebase_admin.json' (tên file bạn đã cung cấp)
    CRED = credentials.Certificate("firebase_admin.json")
    initialize_app(CRED)
    DB = firestore.client()
    print("--> Firebase Firestore connection successful!")
except Exception as e:
    print(f"--> FIREBASE CONNECTION ERROR: {e}. Dữ liệu sẽ không được lưu.")
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

# --- DATABASE HANDLERS ---

def get_user_state(user_id: str) -> Dict[str, Any]:
    """Retrieves user state from Firestore, or returns a default state."""
    default_state = {
        # session_words: chỉ lưu Hán tự
        "session_hanzi": [], 
        "mode_index": 0, 
        # task_queue: chỉ lưu Hán tự và mode_name
        "task_queue": [], 
        "backup_queue": [],
        "mistake_made": False, 
        # current_task: chỉ lưu Hán tự và mode_name
        "current_task": None, 
        "score": 0, "total_questions": 0,
        "last_study_time": 0, "reminder_sent": False
    }
    if DB:
        doc_ref = DB.collection('users').document(user_id)
        doc = doc_ref.get()
        if doc.exists:
            return doc.to_dict()
        doc_ref.set(default_state)
        return default_state
    return default_state

def save_user_state(user_id: str, state: Dict[str, Any], update_time: bool = True):
    """Saves user state to Firestore."""
    if DB:
        if update_time:
            state["last_study_time"] = time.time()
            state["reminder_sent"] = False # Reset reminder flag on user interaction
        DB.collection('users').document(user_id).set(state)

# --- BOT QUIZ LOGIC (Full State Management) ---

def start_new_session_bot(user_id: str) -> str:
    """Initializes a new session and saves state to DB."""
    state = get_user_state(user_id)
    session_words = random.sample(HSK_DATA, min(WORDS_PER_SESSION, len(HSK_DATA)))
    
    # LƯU TRỮ CHỈ HÁN TỰ (để database ổn định)
    state["session_hanzi"] = [word["Hán tự"] for word in session_words]
    
    state.update({
        "mode_index": 0, "score": 0, "total_questions": 0
    })
    save_user_state(user_id, state)
    
    # Khởi động Mode đầu tiên
    return load_next_mode_bot(user_id)

def load_next_mode_bot(user_id: str) -> str:
    """Loads the next quiz mode or concludes the session (Perfect Run logic)."""
    state = get_user_state(user_id)
    
    if state["mode_index"] >= len(BOT_MODES):
        # Kết thúc session
        state["task_queue"] = []; state["current_task"] = None
        save_user_state(user_id, state)
        return "🎉 CHÚC MỪNG! Bạn đã hoàn thành xuất sắc phiên học này!\n\nGõ 'học' để bắt đầu phiên mới."

    current_mode = BOT_MODES[state["mode_index"]]
    
    # Thiết lập Task Queue (chỉ lưu Hán tự và mode_name)
    state["task_queue"] = []
    for hanzi in state["session_hanzi"]:
        state["task_queue"].append({"hanzi": hanzi, "mode_name": current_mode["name"]})
        
    random.shuffle(state["task_queue"])
    state["backup_queue"] = list(state["task_queue"])
    state["mistake_made"] = False
    
    save_user_state(user_id, state)
    
    # Trả về thông báo bắt đầu và câu hỏi đầu tiên
    return f"🌟 BẮT ĐẦU DẠNG {state['mode_index'] + 1}: {current_mode['title']}\n\n" + get_next_question(user_id, is_new_mode=True)

def get_next_question(user_id: str, is_new_mode: bool = False) -> str:
    """Retrieves the next question from the queue. FIX LỖI: Loại bỏ gọi đệ quy."""
    state = get_user_state(user_id)

    # 1. Kiểm tra luật Perfect Run (Khi hết Task Queue)
    if not state["task_queue"]:
        if state["mistake_made"]:
            # Sai -> Trộn lại và làm lại mode này
            state["task_queue"] = list(state["backup_queue"])
            random.shuffle(state["task_queue"])
            state["mistake_made"] = False
            save_user_state(user_id, state)
            return "❌ BẠN ĐÃ SAI!\nLàm lại Dạng này cho đến khi đúng hết 100% nhé.\n\n" + get_next_question(user_id)
        else:
            # Đúng 100% -> Tăng Mode Index và YÊU CẦU xác nhận chuyển Mode
            state["mode_index"] += 1
            state["current_task"] = None # Rất quan trọng để Bot dừng lại
            save_user_state(user_id, state)
            
            # Gửi thông báo hoàn thành và yêu cầu xác nhận tiếp tục
            if state["mode_index"] >= len(BOT_MODES):
                return load_next_mode_bot(user_id) # Kết thúc
            else:
                return f"✅ HOÀN THÀNH DẠNG BÀI {state['mode_index']}/{len(BOT_MODES)}!\n\nGõ `tiếp tục` để bắt đầu Dạng bài mới nhé."
            
    # 2. Lấy task tiếp theo
    task = state["task_queue"].pop(0)
    state["current_task"] = task
    
    # Chỉ tăng total_questions khi không phải là lỗi đệ quy/lặp lại
    if not is_new_mode:
        state["total_questions"] += 1
    
    save_user_state(user_id, state)
    
    # Tra cứu thông tin từ vựng đầy đủ từ Hán tự
    hanzi = task["hanzi"]
    word = HSK_MAP.get(hanzi, HSK_DATA[0]) # Fallback nếu lỗi
    mode = task["mode_name"]
    remaining = len(state['task_queue']) + 1
    
    # Generate question text
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
    """Checks the user's answer and saves state."""
    state = get_user_state(user_id)
    if not state or not state["current_task"]: return "Xin lỗi, hình như chưa có câu hỏi nào. Gõ 'học' để bắt đầu nhé!"

    # Tra cứu từ vựng đầy đủ từ Hán tự
    hanzi = state["current_task"]["hanzi"]
    word = HSK_MAP.get(hanzi, HSK_DATA[0])
    mode = state["current_task"]["mode_name"]
    is_correct = False
    
    # Scoring Logic
    if mode == "hanzi_to_viet":
        keywords = word["Nghĩa"].lower().split(',')
        is_correct = any(k.strip() in answer.lower() for k in keywords) or (answer.lower() in word["Nghĩa"].lower())
    elif mode in ["viet_to_hanzi", "example_to_hanzi"]:
        is_correct = (answer == word["Hán tự"])
    elif mode == "translate_sentence":
        is_correct = (answer == word["Ví dụ"] or word["Hán tự"] in answer)
        
    # Response Generation
    if is_correct:
        state["score"] += 1
        feedback = "✅ CHÍNH XÁC!"
    else:
        state["mistake_made"] = True
        feedback = (f"❌ SAI RỒI!\nĐáp án đúng là: 🇨🇳 {word['Hán tự']} ({word['Pinyin']})\n🇻🇳 Nghĩa: {word['Nghĩa']}\nCâu mẫu: {word['Ví dụ']}")
    
    save_user_state(user_id, state)
    # Sau khi trả lời xong, lấy câu hỏi tiếp theo
    return feedback + "\n\n" + get_next_question(user_id)

def process_chat_logic(user_id: str, user_text: str) -> str:
    """Main Chatbot logic handler."""
    user_text = user_text.lower().strip()
    state = get_user_state(user_id)
    
    # Hướng dẫn
    if user_text in ["hướng dẫn", "help", "menu"]:
        return (
            f"📚 HƯỚNG DẪN SỬ DỤNG HSK BOT\n\n"
            f"1. Bắt đầu phiên học:\n"
            f"   Gõ: `học` hoặc `bắt đầu`\n"
            f"   -> Bot sẽ chọn ngẫu nhiên 10 từ và bắt đầu Dạng 1.\n\n"
            f"2. Tiếp tục Dạng bài:\n"
            f"   Gõ: `tiếp tục`\n"
            f"   -> Dùng khi Bot yêu cầu xác nhận để chuyển sang Dạng bài mới.\n\n"
            f"3. Chế độ học tập:\n"
            f"   Bot sẽ đố bạn qua 4 Dạng bài liên tục, giống hệt App PC.\n"
            f"   *Lưu ý: Bạn phải trả lời đúng 100% (Perfect Run) mới qua được Dạng tiếp theo!*\n\n"
            f"4. Các lệnh trong khi học:\n"
            f"   - Gõ: `bỏ qua` hoặc `dap an`: Xem đáp án và chuyển sang câu mới.\n"
            f"   - Gõ: `điểm` hoặc `score`: Xem thống kê kết quả hiện tại.\n\n"
            f"5. Nhắc nhở:\n"
            f"   - Bot sẽ tự động nhắn tin nhắc nhở bạn sau mỗi 1 tiếng nếu bạn không tương tác."
        )

    # 1. Xử lý lệnh TIẾP TỤC (Chuyển mode)
    if user_text in ["tiếp tục"]:
        # Chỉ cho phép tiếp tục khi current_task rỗng VÀ task_queue rỗng (chờ chuyển mode)
        if state["current_task"] is None and not state["task_queue"]:
            return load_next_mode_bot(user_id)
        else:
            return "Bạn đang học dở, hãy trả lời câu hỏi hiện tại trước."
            
    # 2. Trả lời câu hỏi (chạy trước để ưu tiên trả lời)
    if state["current_task"] is not None:
        return check_answer_bot(user_id, user_text)
    
    # 3. Logic bắt đầu (chỉ chạy khi không có câu hỏi nào đang chờ)
    if user_text in ["học", "bắt đầu", "start"]: 
        return start_new_session_bot(user_id)
    
    # 4. Lệnh khác
    elif user_text in ["bỏ qua", "skip", "dap an"]:
        # Cần phải thực hiện việc bỏ qua ở đây thay vì trả lời 'Bạn chưa bắt đầu học'
        if state["current_task"] is not None:
            # Nếu có câu hỏi đang chạy, thực hiện logic bỏ qua
            state["mistake_made"] = True
            hanzi = state["current_task"]["hanzi"]
            word = HSK_MAP.get(hanzi, HSK_DATA[0])
            next_question = get_next_question(user_id)
            return (f"⏩ Bỏ qua\nĐáp án là: 🇨🇳 {word['Hán tự']} ({word['Pinyin']})\n🇻🇳 Nghĩa: {word['Nghĩa']}\n\n") + next_question
        else:
            # Nếu không có câu hỏi nào
            return "Bạn chưa bắt đầu học. Gõ 'học' để nhận câu hỏi."
            
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
        
    users_ref = DB.collection('users')
    docs = users_ref.where('last_study_time', '>', 0).get() 
    current_time = time.time()
    
    for doc in docs:
        user_id = doc.id
        state = doc.to_dict()
        
        # Check if 1 hour passed and reminder hasn't been sent
        if (current_time - state.get('last_study_time', 0)) > REMINDER_INTERVAL_SECONDS and not state.get('reminder_sent', False):
            
            # Send Facebook reminder
            reminder_message = "🔔 Đã 1 tiếng rồi! Bạn có muốn học tiếp không?\n\nGõ 'học' để tiếp tục phiên học HSK của bạn nhé!"
            send_facebook_message(user_id, reminder_message)
            
            # Update reminder flag in DB
            state['reminder_sent'] = True
            save_user_state(user_id, state, update_time=False)
            print(f"--> Sent reminder to user: {user_id}")
        
# --- API ENDPOINTS ---

@app.get("/check_reminders")
async def check_reminders_endpoint(background_tasks: BackgroundTasks):
    """API called by the Render Cron Job to trigger the reminder check."""
    background_tasks.add_task(check_and_send_reminders_async)
    return {"status": "Reminder check started in background."}

# Standard API for PC App
@app.get("/api/new_session")
def create_new_session_pc(count: int = 10):
    session_words = random.sample(HSK_DATA, min(count, len(HSK_DATA)))
    return {"message": "ok", "data": session_words}

# Webhook Verification
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

# Webhook Message Handler
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
    uvicorn.run("hsk_server_test:app", host="127.0.0.1", port=8000, reload=True)
```

Tôi đã nhận được log bạn cung cấp. Log này cho thấy một vấn đề rất quan trọng trong quá trình triển khai Render của bạn:

```
==> Running 'uvicorn hsk_server:app --host 0.0.0.0 --port $PORT'
...
INFO: 34.82.80.145:0 - "GET / HTTP/1.1" 404 Not Found
```

### Phân Tích Lỗi

Lỗi này xác nhận vấn đề mà chúng ta đã thảo luận:

1.  **Start Command Lỗi:** Render vẫn đang cố gắng chạy file có tên **`hsk_server:app`** (tức là file `hsk_server.py`) theo lệnh khởi động mặc định.
2.  **Tên File Không Khớp:** Tên file Server hiện tại của bạn là **`hsk_server_test.py`**.

Lệnh khởi động Render đang chạy là: `uvicorn hsk_server:app --host 0.0.0.0 --port $PORT`.

Điều này có nghĩa là bạn đã quên **thay đổi lệnh khởi động** trong mục **Settings** (Cài đặt) trên Render Dashboard.

### HƯỚNG DẪN KHẮC PHỤC CUỐI CÙNG (Fix Lỗi Khởi Động)

Bạn cần thực hiện lại các bước sau trên Render Dashboard để nói cho Render biết file Server nào cần chạy:

1.  **Vào Render Dashboard:** Mở tab Render (có URL `https://dashboard.render.com/service/...`).
2.  **Vào Settings:** Bấm vào menu **Settings** (Cài đặt) ở cột bên trái.
3.  **Sửa Start Command:** Tìm mục **Start Command** và thay thế nội dung cũ bằng lệnh khởi động file Server mới nhất của bạn:

    ```bash
    uvicorn hsk_server_test:app --host 0.0.0.0 --port $PORT
