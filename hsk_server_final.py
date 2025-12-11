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
import psycopg2
import google.generativeai as genai 

# --- CẤU HÌNH ---
DATABASE_URL = os.environ.get('DATABASE_URL')
DB_STATUS = "Postgres" if DATABASE_URL else None

# FACEBOOK TOKEN
PAGE_ACCESS_TOKEN = "EAAbQQNNSmSMBQKWd5qB15zFMy2KdPm6Ko1rJX6R4ZC3EtnNfvf0gT76V1Qk4l1vflxL1pDVwY8mrgbgAaFFtG6bzcrhJfQ86HdK5v8qZA9zTIge2ZBJcx9oNPOjk1DlQ8juGinZBuah0RDgbCd2vBvlNWr47GVz70BdPNzKRctCGphNJRI0Wm57UwKRmXOZAVfDP7zwZDZD"
VERIFY_TOKEN = "hsk_mat_khau_bi_mat"

# GOOGLE GEMINI API (KEY CỦA BẠN)
GEMINI_API_KEY = "AIzaSyB5V6sgqSOZO4v5DyuEZs3msgJqUk54HqQ"
genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel('gemini-1.5-flash')

WORDS_PER_SESSION = 10 

# --- DATABASE SETUP ---
if DB_STATUS:
    try:
        with psycopg2.connect(DATABASE_URL, sslmode='require') as conn:
            with conn.cursor() as cursor:
                cursor.execute("""
                    CREATE TABLE IF NOT EXISTS users (
                        user_id VARCHAR(50) PRIMARY KEY,
                        state JSONB,
                        last_study_time INTEGER
                    );
                """)
            conn.commit()
        print("--> Kết nối PostgreSQL thành công.")
    except Exception as e:
        print(f"--> LỖI KẾT NỐI DB: {e}")
        DB_STATUS = None

# --- LOAD DATA ---
try:
    import hsk2_vocabulary_full as hsk_data
    HSK_DATA: List[Dict[str, Any]] = hsk_data.HSK_DATA
    HSK_MAP = {word["Hán tự"]: word for word in HSK_DATA}
    ALL_HANZI = list(HSK_MAP.keys())
except ImportError:
    HSK_DATA = [{"Hán tự": "你好", "Pinyin": "nǐhǎo", "Nghĩa": "xin chào", "Ví dụ": "你好吗", "Dịch câu": "Bạn khỏe không"}]
    HSK_MAP = {word["Hán tự"]: word for word in HSK_DATA}
    ALL_HANZI = list(HSK_MAP.keys())

# CÁC DẠNG BÀI (GIỮ NGUYÊN THEO Ý BẠN)
BOT_MODES = [
    {"name": "hanzi_to_viet", "title": "DẠNG 1: NHÌN HÁN TỰ -> ĐOÁN NGHĨA"},
    {"name": "viet_to_hanzi", "title": "DẠNG 2: NHÌN NGHĨA -> VIẾT HÁN TỰ"},
    {"name": "example_to_hanzi", "title": "DẠNG 3: ĐIỀN TỪ VÀO CÂU"},
    {"name": "translate_sentence", "title": "DẠNG 4: DỊCH CÂU SANG TIẾNG TRUNG"}
]

app = FastAPI()

# --- HELPER: DATABASE ---
def get_user_state(user_id: str) -> Dict[str, Any]:
    default_state = {
        "session_hanzi": [], "learned_hanzi": [], "mode_index": 0, 
        "task_queue": [], "backup_queue": [], "mistake_made": False, 
        "current_task": None, "score": 0, "total_questions": 0,
        "current_phase": "IDLE", "preview_queue": []
    }
    if DB_STATUS:
        try:
            with psycopg2.connect(DATABASE_URL, sslmode='require') as conn:
                with conn.cursor() as cursor:
                    cursor.execute("SELECT state FROM users WHERE user_id = %s", (user_id,))
                    res = cursor.fetchone()
                    if res: return {**default_state, **res[0]}
                    save_user_state(user_id, default_state, False)
                    return default_state
        except: return default_state
    return default_state

def save_user_state(user_id: str, state: Dict[str, Any], update_time: bool = True):
    if DB_STATUS:
        try:
            t = time.time() if update_time else state.get("last_study_time", 0)
            state["last_study_time"] = t
            with psycopg2.connect(DATABASE_URL, sslmode='require') as conn:
                with conn.cursor() as cursor:
                    cursor.execute("""
                        INSERT INTO users (user_id, state, last_study_time) VALUES (%s, %s, %s)
                        ON CONFLICT (user_id) DO UPDATE SET state = EXCLUDED.state, last_study_time = EXCLUDED.last_study_time
                    """, (user_id, json.dumps(state), t))
                conn.commit()
        except: pass

# --- AI HELPER: CHẤM ĐIỂM THÔNG MINH ---
def ai_grade_answer(user_answer, task_info):
    """Dùng AI để chấm điểm linh hoạt hơn."""
    hanzi = task_info["hanzi"]
    word_data = HSK_MAP.get(hanzi, {})
    mode = task_info["mode_name"]
    
    # Tạo Prompt gửi cho AI
    prompt = f"""
    Bạn là giáo viên chấm bài tiếng Trung. Hãy chấm điểm câu trả lời của học sinh.
    
    THÔNG TIN ĐỀ BÀI:
    - Từ vựng gốc: {word_data['Hán tự']} ({word_data['Pinyin']}) - Nghĩa: {word_data['Nghĩa']}
    - Dạng bài tập: {mode}
    - Yêu cầu đề bài: 
      { "Dịch nghĩa từ này sang tiếng Việt" if mode == "hanzi_to_viet" else 
        "Viết lại Hán tự của từ này" if mode == "viet_to_hanzi" else 
        "Điền từ còn thiếu vào câu ví dụ: " + word_data['Ví dụ'] if mode == "example_to_hanzi" else 
        "Dịch câu sau sang tiếng Trung: " + word_data['Dịch câu'] }
    
    CÂU TRẢ LỜI CỦA HỌC SINH: "{user_answer}"
    
    NHIỆM VỤ:
    1. Xác định ĐÚNG hay SAI. (Chấp nhận lỗi chính tả nhỏ, hoặc dùng từ đồng nghĩa nếu hợp lý).
    2. Nếu Sai, hãy giải thích ngắn gọn tại sao và đưa ra đáp án đúng.
    3. Trả về format JSON duy nhất: {{"is_correct": true/false, "feedback": "Lời giải thích ngắn gọn"}}
    """
    
    try:
        response = model.generate_content(prompt)
        # Cố gắng parse JSON từ phản hồi của AI
        txt = response.text.strip()
        if "```json" in txt: txt = txt.split("```json")[1].split("```")[0]
        elif "```" in txt: txt = txt.split("```")[1].split("```")[0]
        
        result = json.loads(txt)
        return result
    except:
        # Fallback nếu AI lỗi: Chấm thủ công đơn giản
        print("AI Error, falling back to manual check")
        is_correct = False
        if mode == "hanzi_to_viet": is_correct = user_answer.lower() in word_data["Nghĩa"].lower()
        else: is_correct = word_data["Hán tự"] in user_answer
        return {"is_correct": is_correct, "feedback": f"Đáp án đúng là: {word_data['Hán tự']} - {word_data['Nghĩa']}"}

# --- LOGIC QUY TRÌNH HỌC (GIỮ NGUYÊN CẤU TRÚC CỦA BẠN) ---

def start_learning_phase(user_id: str) -> str:
    state = get_user_state(user_id)
    # Logic chọn từ giữ nguyên
    available = [h for h in ALL_HANZI if h not in state["learned_hanzi"]]
    if len(available) < WORDS_PER_SESSION:
        state["learned_hanzi"] = []
        available = ALL_HANZI
        msg = "🔄 Bắt đầu vòng học mới!\n"
    else: msg = ""
    
    state["session_hanzi"] = random.sample(available, min(WORDS_PER_SESSION, len(available)))
    state["preview_queue"] = list(state["session_hanzi"])
    state.update({"current_phase": "PREVIEW", "mode_index": 0, "score": 0, "total_questions": 0})
    save_user_state(user_id, state)
    return msg + show_next_preview_word(user_id)

def show_next_preview_word(user_id: str) -> str:
    state = get_user_state(user_id)
    if not state["preview_queue"]:
        state["current_phase"] = "READY_TO_QUIZ"
        state["current_task"] = None
        save_user_state(user_id, state)
        return "✅ Đã học xong từ mới! Gõ `bắt đầu` để vào bài kiểm tra."

    hanzi = state["preview_queue"].pop(0)
    word = HSK_MAP.get(hanzi, {})
    state["current_task"] = {"hanzi": hanzi, "mode": "PREVIEW"}
    save_user_state(user_id, state)

    # Có thể dùng AI để sinh lời giải thích thú vị hơn ở đây nếu muốn
    # Nhưng để giữ đúng ý bạn, ta dùng format chuẩn
    return (
        f"📖 TỪ MỚI ({WORDS_PER_SESSION - len(state['preview_queue'])}/{WORDS_PER_SESSION})\n"
        f"🇨🇳 {word['Hán tự']} ({word['Pinyin']})\n"
        f"🇻🇳 {word['Nghĩa']}\n"
        f"Ví dụ: {word['Ví dụ']}\n"
        f"Dịch: {word['Dịch câu']}\n\n"
        f"Gõ `tiếp` để xem từ sau."
    )

def start_quiz_phase(user_id: str) -> str:
    state = get_user_state(user_id)
    state["current_phase"] = "QUIZ"
    state.update({"mode_index": 0, "score": 0, "total_questions": 0})
    save_user_state(user_id, state)
    return load_next_mode_bot(user_id)

def load_next_mode_bot(user_id: str) -> str:
    state = get_user_state(user_id)
    if state["mode_index"] >= len(BOT_MODES):
        state["current_phase"] = "IDLE"
        state["learned_hanzi"].extend(state["session_hanzi"])
        save_user_state(user_id, state)
        return "🎉 Chúc mừng! Bạn đã hoàn thành bài kiểm tra."

    current_mode = BOT_MODES[state["mode_index"]]
    state["task_queue"] = []
    for h in state["session_hanzi"]:
        state["task_queue"].append({"hanzi": h, "mode_name": current_mode["name"]})
    random.shuffle(state["task_queue"])
    state["backup_queue"] = list(state["task_queue"])
    state["mistake_made"] = False
    save_user_state(user_id, state)
    return f"🌟 {current_mode['title']}\n\n" + get_next_question(user_id, True)

def get_next_question(user_id: str, is_new_mode: bool = False) -> str:
    state = get_user_state(user_id)
    if not state["task_queue"]:
        if state["mistake_made"]: # Perfect Run Logic
            state["task_queue"] = list(state["backup_queue"])
            random.shuffle(state["task_queue"])
            state["mistake_made"] = False
            save_user_state(user_id, state)
            return "❌ Vẫn còn lỗi sai! Làm lại dạng này nhé.\n\n" + get_next_question(user_id)
        else:
            state["mode_index"] += 1
            state["current_task"] = None
            save_user_state(user_id, state)
            if state["mode_index"] >= len(BOT_MODES): return load_next_mode_bot(user_id)
            return f"✅ Xong dạng này! Gõ `tiếp` để sang dạng sau."

    task = state["task_queue"].pop(0)
    state["current_task"] = task
    if not is_new_mode: state["total_questions"] += 1
    save_user_state(user_id, state)

    hanzi = task["hanzi"]
    word = HSK_MAP.get(hanzi, {})
    mode = task["mode_name"]
    
    if mode == "hanzi_to_viet":
        return f"Từ này nghĩa là gì?\n🇨🇳 {word['Hán tự']} ({word['Pinyin']})"
    elif mode == "viet_to_hanzi":
        return f"Viết Hán tự cho nghĩa:\n🇻🇳 {word['Nghĩa']}"
    elif mode == "example_to_hanzi":
        masked = word["Ví dụ"].replace(word["Hán tự"], "___")
        return f"Điền từ còn thiếu:\n{masked}\n({word['Dịch câu']})"
    elif mode == "translate_sentence":
        return f"Dịch câu này sang tiếng Trung:\n🇻🇳 {word['Dịch câu']}"
    return "Lỗi."

def process_chat_logic(user_id: str, user_text: str) -> str:
    text = user_text.lower().strip()
    state = get_user_state(user_id)

    # 1. Các lệnh điều hướng cơ bản (Logic cứng)
    if text in ["học", "learn"]: return start_learning_phase(user_id)
    if text in ["bắt đầu", "start"]: return start_quiz_phase(user_id)
    if text in ["reset", "xóa"]: 
        state["learned_hanzi"] = []
        save_user_state(user_id, state)
        return "Đã xóa tiến trình. Gõ `học` để bắt đầu lại."
    
    # 2. Xử lý trong giai đoạn PREVIEW
    if state["current_phase"] == "PREVIEW":
        if text in ["tiếp", "next", "continue", "tiếp tục"]: return show_next_preview_word(user_id)
        # Nếu người dùng hỏi linh tinh trong lúc học, dùng AI giải thích từ đang học
        if state["current_task"]:
            return f"🤖 (AI): {text}\nTôi đang dạy bạn từ {state['current_task']['hanzi']}. Gõ 'tiếp' để sang từ mới nhé."

    # 3. Xử lý trong giai đoạn QUIZ (Quan trọng nhất: Dùng AI chấm điểm)
    if state["current_phase"] == "QUIZ":
        if state["current_task"] is None:
             if text in ["tiếp", "tiếp tục"]: return load_next_mode_bot(user_id)
             return "Gõ `tiếp tục` để sang bài mới."
        
        # Gọi AI để chấm điểm
        ai_result = ai_grade_answer(user_text, state["current_task"])
        
        if ai_result["is_correct"]:
            state["score"] += 1
            feedback = "✅ " + ai_result["feedback"]
        else:
            state["mistake_made"] = True
            feedback = "❌ " + ai_result["feedback"]
            
        save_user_state(user_id, state)
        return feedback + "\n\n" + get_next_question(user_id)

    # 4. Chat tự do (Dùng AI trả lời)
    try:
        response = model.generate_content(f"Bạn là gia sư tiếng Trung. Người dùng hỏi: {user_text}. Hãy trả lời ngắn gọn.")
        return response.text
    except:
        return "Gõ `học` để bắt đầu nhé."

# --- API ---
@app.get("/webhook")
async def verify(request: Request):
    if request.query_params.get("hub.verify_token") == VERIFY_TOKEN:
        return PlainTextResponse(request.query_params.get("hub.challenge"))
    raise HTTPException(403)

@app.post("/webhook")
async def msg(request: Request):
    data = await request.json()
    if data.get("object") == "page":
        for e in data.get("entry", []):
            for m in e.get("messaging", []):
                if "message" in m:
                    send_msg(m["sender"]["id"], process_chat_logic(m["sender"]["id"], m["message"].get("text","")))
        return {"status": "ok"}
    raise HTTPException(404)

def send_msg(uid, txt):
    requests.post("https://graph.facebook.com/v21.0/me/messages", 
        params={"access_token": PAGE_ACCESS_TOKEN},
        json={"recipient": {"id": uid}, "message": {"text": txt}},
        headers={"Content-Type": "application/json"})

if __name__ == "__main__":
    uvicorn.run("hsk_server_final:app", host="0.0.0.0", port=8000)
