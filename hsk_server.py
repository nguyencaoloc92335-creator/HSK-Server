import sys
import os
from fastapi import FastAPI, Request, HTTPException
from starlette.responses import PlainTextResponse
import uvicorn
import random
import requests
import json
from typing import List, Dict, Any

# --- CẤU HÌNH FACEBOOK (BẮT BUỘC PHẢI ĐIỀN) ---
# 1. PAGE_ACCESS_TOKEN: Mã siêu dài bạn đã lấy từ Facebook Developer
PAGE_ACCESS_TOKEN = "EAAbQQNNSmSMBQCSLHPqo2Y2HfW8GvdyfPc6oOCqVb8X61h6HadIILwTn7uDkZAIqgdEKEDMDFmhNYfoPVSevT907qEpFE5OYZC9VtfEwyR1uZA3b49k5VlBVZAPpfmsFqURLl5Pn0P4LZAaxWMzhuHmEhJeZB6Gq1NXeZAxQ3dp940k3P2VMJmjorafaFWeiAvU7YtOZCgZDZD"

# 2. VERIFY_TOKEN: Mật khẩu bạn đã điền trong form xác minh (hsk_mat_khau_bi_mat)
VERIFY_TOKEN = "hsk_mat_khau_bi_mat" 

# --- SỬA LỖI PHÔNG CHỮ TIẾNG VIỆT TRÊN WINDOWS (CHỈ KHI CHẠY LOCAL) ---
if sys.stdout and hasattr(sys.stdout, 'reconfigure'):
    try: sys.stdout.reconfigure(encoding='utf-8')
    except: pass

# --- NHẬP KHO HÀNG (HSK_DATA) ---
try:
    import hsk2_vocabulary_full as hsk_data
    HSK_DATA: List[Dict[str, Any]] = hsk_data.HSK_DATA
    print(f"--> Đã nhập kho thành công: {len(HSK_DATA)} từ vựng.")
except ImportError as e:
    print(f"--> LỖI: Không tìm thấy file dữ liệu! Chỉ dùng dữ liệu mẫu.")
    HSK_DATA = [
        {"Hán tự": "你好", "Pinyin": "nǐhǎo", "Nghĩa": "xin chào", "Ví dụ": "你好吗", "Dịch câu": "Bạn khỏe không"}
    ]

# Khởi tạo Server
app = FastAPI()

# Dữ liệu người dùng tạm thời
user_progress = {
    "user_name": "Ong Chu", 
    "level": "HSK 2",
    "completed_words": 0, 
    "current_session": []
}

# --- CÁC API CŨ (CHO APP PC) ---

@app.get("/")
def read_root(): return {"message": "Server HSK + Facebook Bot đang chạy!"}

@app.get("/api/new_session")
def create_new_session(count: int = 10):
    session_words = random.sample(HSK_DATA, min(count, len(HSK_DATA)))
    user_progress["current_session"] = session_words
    return {"message": "ok", "data": session_words}

# --- API CHO FACEBOOK (WEBHOOK) ---

# 1. Xác minh (GET request)
@app.get("/webhook")
async def verify_webhook(request: Request):
    mode = request.query_params.get("hub.mode")
    token = request.query_params.get("hub.verify_token")
    challenge = request.query_params.get("hub.challenge")

    if mode and token:
        if mode == "subscribe" and token == VERIFY_TOKEN:
            print(f"WEBHOOK_VERIFIED. CHALLENGE: {challenge}")
            # Trả về challenge DẠNG PLAIN TEXT (Khắc phục lỗi xác minh)
            return PlainTextResponse(str(challenge))
        else:
            raise HTTPException(status_code=403, detail="Sai mật khẩu Verify Token")
    return {"status": "Đây là đường dẫn Webhook"}

# 2. Nhận tin nhắn (POST request)
@app.post("/webhook")
async def handle_message(request: Request):
    data = await request.json()
    
    if data.get("object") == "page":
        for entry in data.get("entry", []):
            for event in entry.get("messaging", []):
                if "message" in event:
                    sender_id = event["sender"]["id"]
                    text = event["message"].get("text", "")
                    
                    print(f"Nhận tin từ {sender_id}: {text}")
                    
                    reply_text = process_chat_logic(text)
                    send_facebook_message(sender_id, reply_text)
                    
        return {"status": "EVENT_RECEIVED"}
    else:
        raise HTTPException(status_code=404)

# --- HÀM LOGIC TRẢ LỜI CHO BOT ---
def process_chat_logic(user_text):
    user_text = user_text.lower().strip()
    
    if "học" in user_text or "bắt đầu" in user_text:
        word = random.choice(HSK_DATA)
        # Sử dụng f-string gọn gàng cho câu trả lời
        return (
            f"📖 Từ mới cho bạn:\n\n"
            f"🇨🇳 {word['Hán tự']} ({word['Pinyin']})\n"
            f"🇻🇳 Nghĩa: {word['Nghĩa']}\n\n"
            f"Ví dụ: {word['Ví dụ']}"
        )
    else:
        return "Chào Đại Ca! Gõ 'học' để ôn tập ngay, hoặc mở App trên máy tính để học bài bản hơn nhé."

# --- HÀM GỬI TIN NHẮN LẠI CHO FB ---
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
    uvicorn.run("hsk_server_v1:app", host="127.0.0.1", port=8000, reload=True)
