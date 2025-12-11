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
# ĐÃ CẬP NHẬT TOKEN MỚI TỪ USER
PAGE_ACCESS_TOKEN = "EAAbQQNNSmSMBQKWd5qB15zFMy2KdPm6Ko1rJX6R4ZC3EtnNfvf0gT76V1Qk4l1vflxL1pDVwY8mrgbgAaFFtG6bzcrhJfQ86HdK5v8qZA9zTIge2ZBJcx9oNPOjk1DlQ8juGinZBuah0RDgbCd2vBvlNWr47GVz70BdPNzKRctCGphNJRI0Wm57UwKRmXOZAVfDP7zwZDZD"
VERIFY_TOKEN = "hsk_mat_khau_bi_mat" 
WORDS_PER_SESSION = 10 
REMINDER_INTERVAL_SECONDS = 3600 # 1 hour = 3600 seconds

# --- HSK DATA IMPORT ---
try:
    import hsk2_vocabulary_full as hsk_data
    HSK_DATA: List[Dict[str, Any]] = hsk_data.HSK_DATA
    # Tạo bản đồ từ Hán tự -> từ vựng để tra cứu nhanh
    HSK_MAP = {word["Hán tự"]: word for word in HSK_DATA}
    ALL_HANZI = list(HSK_MAP.keys()) # Danh sách tất cả Hán tự
    print(f"--> Successfully loaded {len(HSK_DATA)} vocabulary items.")
except ImportError:
    HSK_DATA = [{"Hán tự": "你好", "Pinyin": "nǐhǎo", "Nghĩa": "xin chào", "Ví dụ": "你好吗", "Dịch câu": "Bạn khỏe không"}]
    HSK_MAP = {word["Hán tự"]: word for word in HSK_DATA}
    ALL_HANZI = list(HSK_MAP.keys())

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
        "learned_hanzi": [], # DANH SÁCH HÁN TỰ ĐÃ HỌC/KIỂM TRA
        "mode_index": 0, 
        "task_queue": [], 
        "backup_queue": [],
        "mistake_made": False, 
        "current_task": None, 
        "score": 0, "total_questions": 0,
        "last_study_time": 0, 
        "reminder_sent": False,
        "current_phase": "IDLE", # IDLE, PREVIEW, READY_TO_QUIZ, QUIZ
        "preview_queue": [], # Danh sách Hán tự để học
    }
    if DB:
        try:
            CURSOR.execute("SELECT state FROM users WHERE user_id = %s", (user_id,))
            result = CURSOR.fetchone()
            if result:
                loaded_state = result[0]
                # FIX KeyError: Merging loaded state with default state to ensure all keys exist
                final_state = {**default_state, **loaded_state}
                return final_state
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
                state["reminder_sent"] = False
            
            # Use ON CONFLICT to UPSERT (UPDATE if exists, INSERT if not exists)
            CURSOR.execute("""
                INSERT INTO users (user_id, state, last_study_time)
                VALUES (%s, %s, %s)
                ON CONFLICT (user_id) DO UPDATE
                SET state = EXCLUDED.state, last_study_time = EXCLUDED.last_study_time
            """, (user_id, json.dumps(state), state.get("last_study_time", 0)))
            CONN.commit()
            
        except Exception as e:
            print(f"LỖI POSTGRESQL KHI GHI: {e}. Dữ liệu không được lưu.")
            CONN.rollback()
            
# --- BOT QUIZ LOGIC (FIXED) ---

def reset_and_start_new_cycle(user_id: str) -> str:
    """Xóa toàn bộ tiến trình học và bắt đầu vòng học mới."""
    state = get_user_state(user_id)
    state["learned_hanzi"] = [] # Đảm bảo learned_hanzi rỗng
    
    # Save the reset state (don't update time as this is a manual reset)
    save_user_state(user_id, state, update_time=False)
    
    # Sau khi reset, bắt đầu ngay giai đoạn học
    return "✅ ĐÃ RESET TOÀN BỘ TIẾN TRÌNH HỌC!\n" + start_learning_phase(user_id)

def start_learning_phase(user_id: str) -> str:
    """[LỆNH: HỌC / LEARN] Chọn 10 từ mới và bắt đầu giai đoạn Preview."""
    state = get_user_state(user_id)
    
    available_hanzi = [h for h in ALL_HANZI if h not in state["learned_hanzi"]]
    
    if len(available_hanzi) < WORDS_PER_SESSION:
        # Nếu đã học gần hết hoặc hết từ, RESET danh sách đã học và bắt đầu vòng mới
        state["learned_hanzi"] = []
        available_hanzi = ALL_HANZI
        
        # Lấy từ vựng mới
        session_hanzi = random.sample(available_hanzi, min(WORDS_PER_SESSION, len(available_hanzi)))
        reset_message = "🔄 ĐÃ HOÀN TẤT VÒNG HỌC CŨ. BẮT ĐẦU VÒNG HỌC MỚI!\n"
    else:
        session_hanzi = random.sample(available_hanzi, WORDS_PER_SESSION)
        reset_message = ""
    
    state["session_hanzi"] = session_hanzi
    state["preview_queue"] = list(state["session_hanzi"])
    
    state.update({
        "current_phase": "PREVIEW",
        "mode_index": 0, 
        "score": 0, 
        "total_questions": 0
    })
    save_user_state(user_id, state, update_time=True) # Cập nhật thời gian khi BẮT ĐẦU HỌC
    
    return reset_message + show_next_preview_word(user_id)

def show_next_preview_word(user_id: str) -> str:
    """Hiển thị từ tiếp theo trong hàng đợi Preview."""
    state = get_user_state(user_id)
    
    if not state["preview_queue"]:
        # Kết thúc giai đoạn Preview
        state["current_phase"] = "READY_TO_QUIZ"
        state["current_task"] = None
        save_user_state(user_id, state, update_time=False)
        return (
            f"✅ HOÀN TẤT GIAI ĐOẠN HỌC!\n\n"
            f"Bạn đã xem hết {WORDS_PER_SESSION} từ mới. "
            f"Gõ `bắt đầu` hoặc `start` để chuyển sang chế độ kiểm tra Perfect Run."
        )

    hanzi_to_show = state["preview_queue"].pop(0)
    word = HSK_MAP.get(hanzi_to_show, HSK_DATA[0])
    remaining = len(state["preview_queue"])
    
    # Cập nhật task (chỉ để lưu từ đang xem)
    state["current_task"] = {"hanzi": hanzi_to_show, "mode": "PREVIEW"}
    save_user_state(user_id, state, update_time=True) # Cập nhật thời gian khi xem từ

    # THAY ĐỔI: Thêm Pinyin Ví dụ vào nội dung hiển thị
    ví_dụ_pinyin = word.get('Ví dụ Pinyin', 'Không có Pinyin câu ví dụ.')

    return (
        f"📖 TỪ MỚI ({WORDS_PER_SESSION - remaining}/{WORDS_PER_SESSION})\n"
        f"🇨🇳 {word['Hán tự']} ({word['Pinyin']})\n"
        f"🇻🇳 Nghĩa: {word['Nghĩa']}\n"
        f"Câu Ví dụ (Hán): {word['Ví dụ']}\n"
        f"Pinyin Ví dụ: {ví_dụ_pinyin}\n"
        f"Dịch câu: {word['Dịch câu']}\n"
        f"Gõ `tiếp tục` hoặc `continue` để xem từ tiếp theo, hoặc gõ `bắt đầu` để vào bài kiểm tra."
    )

def start_quiz_phase(user_id: str) -> str:
    """[LỆNH: BẮT ĐẦU / START] Bắt đầu giai đoạn Quizzing (Dạng 1)."""
    state = get_user_state(user_id)
    
    state["current_phase"] = "QUIZ"
    
    # Reset quiz mode index and score for fresh start
    state.update({"mode_index": 0, "score": 0, "total_questions": 0})
    save_user_state(user_id, state, update_time=True)
    
    return load_next_mode_bot(user_id)

def load_next_mode_bot(user_id: str) -> str:
    """Nạp bài tập cho dạng tiếp theo hoặc kết thúc session (Chỉ chạy trong phase QUIZ)."""
    state = get_user_state(user_id)
    
    if state["current_phase"] != "QUIZ":
        return "Bot bị lỗi trạng thái. Gõ `học` hoặc `learn` để bắt đầu lại phiên mới."
    
    if state["mode_index"] >= len(BOT_MODES):
        # KẾT THÚC VÀ LƯU TỪ VỰNG ĐÃ HỌC/KIỂM TRA
        state["current_phase"] = "IDLE"
        state["task_queue"] = []; state["current_task"] = None
        
        # Thêm các từ đã học trong session này vào danh sách đã học
        state["learned_hanzi"].extend(state["session_hanzi"]) 
        
        save_user_state(user_id, state, update_time=True) 
        
        return (
            f"🎉 CHÚC MỪNG! Bạn đã hoàn thành TẤT CẢ các Dạng bài!\n"
            f"Tiến độ đã được lưu lại. Gõ `học` hoặc `learn` để bắt đầu phiên mới với 10 từ khác."
        )

    current_mode = BOT_MODES[state["mode_index"]]
    
    # Thiết lập Task Queue (chỉ lưu Hán tự và mode_name)
    state["task_queue"] = []
    for hanzi in state["session_hanzi"]:
        state["task_queue"].append({"hanzi": hanzi, "mode_name": current_mode["name"]})
        
    random.shuffle(state["task_queue"])
    state["backup_queue"] = list(state["task_queue"])
    state["mistake_made"] = False
    
    save_user_state(user_id, state, update_time=True) 

    return f"🌟 BẮT ĐẦU DẠNG {state['mode_index'] + 1}: {current_mode['title']}\n\n" + get_next_question(user_id, is_new_mode=True)

def get_next_question(user_id: str, is_new_mode: bool = False) -> str:
    """Lấy câu hỏi tiếp theo và kiểm tra luật Perfect Run."""
    state = get_user_state(user_id)

    # 1. Kiểm tra luật Perfect Run (Khi hết Task Queue)
    if not state["task_queue"]:
        if state["mistake_made"]:
            # Sai -> Trộn lại và làm lại mode này
            state["task_queue"] = list(state["backup_queue"])
            random.shuffle(state["task_queue"])
            state["mistake_made"] = False
            save_user_state(user_id, state, update_time=True)
            return "❌ BẠN ĐÃ SAI!\nLàm lại Dạng này cho đến khi đúng hết 100% nhé.\n\n" + get_next_question(user_id)
        else:
            # Đúng 100% -> Tăng Mode Index và YÊU CẦU xác nhận chuyển Mode
            state["mode_index"] += 1
            state["current_task"] = None # Rất quan trọng để Bot dừng lại
            save_user_state(user_id, state, update_time=True)
            
            # Gửi thông báo hoàn thành và yêu cầu xác nhận tiếp tục
            if state["mode_index"] >= len(BOT_MODES):
                return load_next_mode_bot(user_id) # Kết thúc (Hàm này sẽ trả về thông báo kết thúc)
            else:
                return f"✅ HOÀN THÀNH DẠNG BÀI {state['mode_index']}/{len(BOT_MODES)}!\n\nGõ `tiếp tục` hoặc `continue` để bắt đầu Dạng bài mới nhé."
            
    # 2. Lấy task tiếp theo
    task = state["task_queue"].pop(0)
    state["current_task"] = task
    
    if not is_new_mode:
        state["total_questions"] += 1
    
    save_user_state(user_id, state, update_time=True) # Cập nhật thời gian khi GỬI CÂU HỎI MỚI
    
    # Tra cứu thông tin từ vựng đầy đủ từ Hán tự
    hanzi = task["hanzi"]
    word = HSK_MAP.get(hanzi, HSK_DATA[0]) 
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
        return f"({remaining} câu còn lại)\nDịch câu sau sang Hán tự:\n🇻🇳 {word['Dịch câu']}\n(Gợi ý: {word['Ví dụ Pinyin']})" # HIỂN THỊ PINYIN CÂU VÍ DỤ
    
    return "Lỗi nạp câu hỏi."

def check_answer_bot(user_id: str, answer: str) -> str:
    """Checks the user's answer and saves state."""
    state = get_user_state(user_id)
    if state["current_phase"] != "QUIZ":
        return "Gõ `bắt đầu` hoặc `start` để chuyển sang chế độ kiểm tra sau khi học xong."
        
    if not state or not state["current_task"]: return "Xin lỗi, hình như chưa có câu hỏi nào. Gõ `học` hoặc `learn` để bắt đầu nhé!"

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
        # THAY ĐỔI: Hiển thị đầy đủ Pinyin Ví dụ
        ví_dụ_pinyin = word.get('Ví dụ Pinyin', 'N/A')
        feedback = (
            f"❌ SAI RỒI!\n"
            f"Hán tự: 🇨🇳 {word['Hán tự']} ({word['Pinyin']})\n"
            f"Nghĩa: 🇻🇳 {word['Nghĩa']}\n"
            f"Câu đúng: {word['Ví dụ']}\n"
            f"Pinyin: {ví_dụ_pinyin}"
        )
    
    save_user_state(user_id, state, update_time=True) # Cập nhật thời gian khi TRẢ LỜI
    return feedback + "\n\n" + get_next_question(user_id)

def process_chat_logic(user_id: str, user_text: str) -> str:
    """Main Chatbot logic handler."""
    user_text = user_text.lower().strip()
    state = get_user_state(user_id)
    
    # --- 1. Xử lý lệnh HƯỚNG DẪN / HELP ---
    if user_text in ["hướng dẫn", "help", "menu"]:
        return (
            f"📚 HƯỚNG DẪN SỬ DỤNG HSK BOT\n\n"
            f"1. GIAI ĐOẠN HỌC (PREVIEW):\n"
            f"   Lệnh: `học` / `learn`\n"
            f"   -> Chọn 10 từ ngẫu nhiên (chưa từng học) và hiển thị đầy đủ thông tin.\n\n"
            f"2. GIAI ĐOẠN KIỂM TRA (QUIZ):\n"
            f"   Lệnh: `bắt đầu` / `start`\n"
            f"   -> Bắt đầu bài kiểm tra 4 Dạng bài với 10 từ bạn vừa học (Perfect Run).\n\n"
            f"3. ĐẶT LẠI TIẾN TRÌNH:\n"
            f"   Lệnh: `reset` / `clear`\n"
            f"   -> Xóa toàn bộ danh sách từ đã học và bắt đầu vòng học mới từ đầu (tất cả {len(ALL_HANZI)} từ).\n\n"
            f"4. LỆNH TRONG KHI HỌC:\n"
            f"   - Gõ: `tiếp tục` / `continue` (Trong PREVIEW: Xem từ tiếp theo. Trong QUIZ: Bắt đầu Dạng bài mới).\n"
            f"   - Gõ: `bỏ qua` / `skip`: Xem đáp án câu hiện tại (chỉ dùng trong QUIZ).\n"
            f"   - Gõ: `điểm` / `score`: Xem thống kê kết quả hiện tại.\n"
        )
    
    # --- 2. Xử lý lệnh RESET (XÓA TOÀN BỘ) ---
    if user_text in ["reset", "clear", "xóa"]:
        return reset_and_start_new_cycle(user_id)

    # --- 3. Xử lý lệnh BẮT ĐẦU HỌC (PREVIEW) ---
    if user_text in ["học", "learn"]: 
        return start_learning_phase(user_id)

    # --- 4. Xử lý lệnh BẮT ĐẦU KIỂM TRA (QUIZ) ---
    if user_text in ["bắt đầu", "start"]: 
        if state["current_phase"] == "QUIZ":
            return "Bạn đang trong bài kiểm tra rồi! Hãy trả lời câu hỏi hiện tại."
        if not state["session_hanzi"]:
            return "Bạn chưa chọn từ để học. Gõ `học` hoặc `learn` để bắt đầu phiên mới."
        
        return start_quiz_phase(user_id)

    # --- 5. Xử lý lệnh TIẾP TỤC ---
    if user_text in ["tiếp tục", "continue"]:
        if state["current_phase"] == "PREVIEW":
            return show_next_preview_word(user_id)
        
        elif state["current_phase"] == "READY_TO_QUIZ":
            return start_quiz_phase(user_id)
            
        elif state["current_phase"] == "QUIZ" and state["current_task"] is None:
            # Tiếp tục khi hoàn thành 100% một Mode và Bot yêu cầu gõ tiếp tục
            return load_next_mode_bot(user_id)
            
        else:
            return "Bạn đang học dở, hãy trả lời câu hỏi hiện tại trước."

    # --- 6. Trả lời câu hỏi (Chỉ chấp nhận trong phase QUIZ) ---
    if state["current_phase"] == "QUIZ" and state["current_task"] is not None:
        return check_answer_bot(user_id, user_text)
    
    # --- 7. Xử lý lệnh BỎ QUA (Chỉ chấp nhận trong phase QUIZ) ---
    elif user_text in ["bỏ qua", "skip", "dap an"]:
        if state["current_phase"] == "QUIZ" and state["current_task"] is not None:
            state["mistake_made"] = True
            hanzi = state["current_task"]["hanzi"]
            word = HSK_MAP.get(hanzi, HSK_DATA[0])
            next_question = get_next_question(user_id)
            save_user_state(user_id, state, update_time=True) 
            return (f"⏩ Bỏ qua\nĐáp án là: 🇨🇳 {word['Hán tự']} ({word['Pinyin']})\n🇻🇳 Nghĩa: {word['Nghĩa']}\n\n") + next_question
        else:
            return "Lệnh `bỏ qua` chỉ dùng trong bài kiểm tra. Gõ `học` để bắt đầu phiên mới."
            
    # --- 8. Lệnh tra cứu (KHÔNG CẦN CẬP NHẬT LAST_STUDY_TIME) ---
    elif user_text in ["điểm", "score"]: 
        return f"📊 KẾT QUẢ HIỆN TẠI:\n\nĐúng: {state['score']}/{state['total_questions']}. Tiếp tục làm bài nhé!"
        
    # --- 9. Mặc định/Trạng thái IDLE ---
    else: 
        return "Chào bạn! Gõ `học` hoặc `learn` để bắt đầu ôn tập nhanh.\n(Gõ `hướng dẫn` hoặc `help` để xem thêm các lệnh)."


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
                
                # --- THAY ĐỔI: GỌI HÀM HỌC ĐỂ CHỌN 10 TỪ MỚI CHO USER ---
                # 1. Khởi tạo 10 từ mới cho người dùng
                # Lưu ý: Hàm này sẽ tự động update time và reset reminder_sent = False
                reply_message = start_learning_phase(user_id) 

                # 2. Gửi tin nhắn nhắc nhở và thông báo bắt đầu học
                reminder_message = (
                    "🔔 Đã 1 tiếng rồi! Đã đến lúc học tiếp!\n\n"
                    "Tôi đã chọn 10 từ mới (khác hoàn toàn từ cũ) cho bạn.\n"
                ) + reply_message
                
                send_facebook_message(user_id, reminder_message)
                
                # 3. Cập nhật cờ nhắc nhở trong DB (KHÔNG CẦN VÌ start_learning_phase đã làm)
                # Tuy nhiên, ta cần set lại reminder_sent = True để không gửi lại ngay
                state = get_user_state(user_id)
                state['reminder_sent'] = True
                save_user_state(user_id, state, update_time=False) # update_time=False: CHỈ CẬP NHẬT FLAG
                
                print(f"--> Sent reminder and started new session for user: {user_id}")
                
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
