import sys
import os

# --- 1. SỬA LỖI PHÔNG CHỮ TIẾNG VIỆT TRÊN WINDOWS ---
# Đoạn này bắt buộc phải đặt lên đầu tiên
if sys.stdout and hasattr(sys.stdout, 'reconfigure'):
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except:
        pass

from fastapi import FastAPI
import uvicorn
import random

# --- 2. NHẬP KHO HÀNG (Dữ liệu từ vựng) ---
# Đảm bảo file hsk2_vocabulary_full.py nằm CÙNG THƯ MỤC với file này.
try:
    # Thử import trực tiếp (nếu chạy cùng thư mục)
    import hsk2_vocabulary_full as hsk_data
    HSK_DATA = hsk_data.HSK_DATA
    print(f"--> Da nhap kho thanh cong: {len(HSK_DATA)} tu vung.") # Viết không dấu để an toàn tuyệt đối
except ImportError as e:
    print(f"--> LOI: Khong tim thay file du lieu! Chi tiet: {e}")
    # Dữ liệu mẫu dự phòng để Server không bị sập
    HSK_DATA = [
        {"Hán tự": "你好", "Pinyin": "nǐhǎo", "Nghĩa": "xin chào", "Ví dụ": "你好吗", "Dịch câu": "Bạn khỏe không"}
    ]

# Khởi tạo Server
app = FastAPI()

# Giả lập dữ liệu người dùng
user_progress = {
    "user_name": "Ong Chu", # Tránh tiếng Việt ở đây nếu không cần thiết
    "level": "HSK 2",
    "completed_words": 0, 
    "current_session": []
}

# --- CÁC CỬA SỔ GIAO DỊCH (API) ---

@app.get("/")
def read_root():
    return {"message": "Server HSK dang hoat dong tot!"}

@app.get("/api/get_random_word")
def get_random_word():
    word = random.choice(HSK_DATA)
    return word

@app.get("/api/new_session")
def create_new_session(count: int = 10):
    session_words = random.sample(HSK_DATA, min(count, len(HSK_DATA)))
    user_progress["current_session"] = session_words
    return {"message": "Da tao phien hoc moi", "data": session_words}

@app.get("/api/progress")
def get_progress():
    return user_progress

# --- KHỞI ĐỘNG MÁY CHỦ ---
if __name__ == "__main__":
    print("Dang khoi dong Server HSK...")
    # Nếu file này tên là 'hsk_server.py', dòng dưới phải là "hsk_server:app"
    try:
        uvicorn.run("hsk_server:app", host="127.0.0.1", port=8000, reload=True)
    except Exception as e:
        print(f"Loi khi khoi dong Server: {e}")
        input("Nhan Enter de thoat...")