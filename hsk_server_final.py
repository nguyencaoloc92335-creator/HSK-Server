return json.loads(res)
    except: return None

def ai_smart_reply(text, context):
    if not model: return "Gõ 'Bắt đầu' để học nhé."
    try:
        prompt = f"""
        Bạn là trợ lý HSK. Ngữ cảnh: {context}. User: "{text}".
        Trả lời ngắn gọn tiếng Việt. Nếu user muốn thêm/sửa từ vựng, hãy hướng dẫn họ gõ: "Thêm từ [Hán] [Pinyin] [Nghĩa]".
        """
        return model.generate_content(prompt).text.strip()
    except: return "Gõ 'Hướng dẫn' để xem menu."

def ai_generate_example_smart(word_data):
    # (Giữ nguyên logic cũ, chỉ thay đổi input dict key)
    hanzi = word_data.get('Hán tự', '')
    meaning = word_data.get('Nghĩa', '')
    backup = {"han": f"{hanzi} 很重要", "pinyin": "...", "viet": f"{meaning} rất quan trọng"}
    try:
        prompt = f"Tạo ví dụ HSK2 đơn giản cho từ: {hanzi} ({meaning}). JSON: {{\"han\": \"...\", \"pinyin\": \"...\", \"viet\": \"...\"}}"
        res = model.generate_content(prompt).text.strip()
        match = re.search(r'\{.*\}', res, re.DOTALL)
        if match: return json.loads(match.group())
        return backup
    except: return backup

# --- HELPER FUNCTIONS ---
def get_ts(): return int(time.time())
def get_vn_time_str(ts=None):
    if ts is None: ts = time.time()
    return datetime.fromtimestamp(ts, timezone(timedelta(hours=7))).strftime("%H:%M")
def draw_bar(c, t): return f"[{'▓'*int(8*c/t)}{'░'*(8-int(8*c/t))}]" if t>0 else ""

def send_fb(uid, txt):
    try:
        requests.post("[https://graph.facebook.com/v16.0/me/messages](https://graph.facebook.com/v16.0/me/messages)", 
            params={"access_token": PAGE_ACCESS_TOKEN},
            json={"recipient": {"id": uid}, "message": {"text": txt}}, timeout=10)
    except Exception as e: logger.error(f"Send Err: {e}")

def send_audio_fb(user_id, text_content):
    if not text_content: return
    filename = f"voice_{user_id}_{int(time.time())}.mp3"
    try:
        tts = gTTS(text=text_content, lang='zh-cn')
        tts.save(filename)
        url = f"[https://graph.facebook.com/v16.0/me/messages?access_token=](https://graph.facebook.com/v16.0/me/messages?access_token=){PAGE_ACCESS_TOKEN}"
        data = {'recipient': json.dumps({'id': user_id}), 'message': json.dumps({'attachment': {'type': 'audio', 'payload': {}}})}
        with open(filename, 'rb') as f:
            files = {'filedata': (filename, f, 'audio/mp3')}
            requests.post(url, data=data, files=files, timeout=20)
    except: pass
    finally:
        if os.path.exists(filename): os.remove(filename)

# --- STATE MANAGER ---
def get_state(uid):
    if uid in USER_CACHE: return USER_CACHE[uid]
    s = {"user_id": uid, "mode": "IDLE", "learned": [], "session": [], "next_time": 0, "waiting": False, "last_interaction": 0, "reminder_sent": False, "quiz_state": {"word_idx": 0, "level": 0, "current_question": None}, "current_word_char": ""}
    if db_pool:
        conn = get_db_conn()
        if conn:
            try:
                with conn.cursor() as cur:
                    cur.execute("SELECT state FROM users WHERE user_id = %s", (uid,))
                    row = cur.fetchone()
                    if row: 
                        db_s = row[0]
                        if "quiz_state" not in db_s: db_s["quiz_state"] = s["quiz_state"]
                        if "current_word_char" not in db_s: db_s["current_word_char"] = ""
                        s.update(db_s)
            finally: release_db_conn(conn)
    USER_CACHE[uid] = s
    return s

def save_state(uid, s):
    USER_CACHE[uid] = s
    if db_pool:
        conn = get_db_conn()
        if conn:
            try:
                with conn.cursor() as cur:
                    cur.execute("INSERT INTO users (user_id, state) VALUES (%s, %s) ON CONFLICT (user_id) DO UPDATE SET state = EXCLUDED.state", (uid, json.dumps(s)))
                    conn.commit()
            finally: release_db_conn(conn)

# --- CORE LOGIC ---

def send_next_auto_word(uid, state):
    if 0 <= datetime.now(timezone(timedelta(hours=7))).hour < 6: return
    
    if len(state["session"]) >= 6:
        # (Start Quiz Logic - Giữ nguyên như cũ, chỉ thay đổi nguồn từ vựng)
        # Để gọn code tôi tạm gọi hàm placeholder, bạn dùng lại logic quiz cũ
        start_advanced_quiz(uid, state)
        return

    # LẤY TỪ DB
    learned = state["learned"]
    new_words = get_random_words_from_db(learned, 1)
    
    if not new_words:
        send_fb(uid, "🎉 Đã học hết từ vựng! Reset lại nhé.")
        state["learned"] = []
        new_words = get_random_words_from_db([], 1)
    
    word = new_words[0]
    state["session"].append(word)
    state["learned"].append(word['Hán tự'])
    state["current_word_char"] = word['Hán tự']
    
    ex = ai_generate_example_smart(word)
    total_count = get_total_words_count()
    
    msg = (f"🔔 **TỪ MỚI** ({len(state['session'])}/6 | Tổng: {len(state['learned'])}/{total_count})\n\n"
           f"🇨🇳 **{word['Hán tự']}** ({word['Pinyin']})\n"
           f"🇻🇳 Nghĩa: {word['Nghĩa']}\n"
           f"----------------\n"
           f"Ví dụ: {ex['han']}\n{ex['pinyin']}\n👉 {ex['viet']}\n\n"
           f"👉 Gõ lại từ **{word['Hán tự']}** để xác nhận.")
    send_fb(uid, msg)
    
    threading.Thread(target=send_audio_fb, args=(uid, word['Hán tự'])).start()
    def send_ex(): time.sleep(2); send_audio_fb(uid, ex['han'])
    threading.Thread(target=send_ex).start()
    
    state["waiting"] = True; state["next_time"] = 0; state["last_interaction"] = get_ts()
    save_state(uid, state)

# --- QUIZ & PROCESS (Giữ logic cũ, chỉ cập nhật việc gọi hàm DB) ---
# (Phần Quiz Logic bạn giữ nguyên từ file cũ vì không phụ thuộc nguồn dữ liệu, 
# nó chỉ dùng state['session'] đã có sẵn)

def start_advanced_quiz(uid, state):
    # ... (Giữ nguyên code quiz cũ)
    state["mode"] = "QUIZ"
    indices = list(range(len(state["session"])))
    random.shuffle(indices)
    state["quiz_state"] = {"level": 1, "queue": indices, "failed": [], "current_idx": -1, "current_question": None}
    state["waiting"] = False; state["next_time"] = 0
    save_state(uid, state)
    send_fb(uid, "🛑 **KIỂM TRA**\n(Logic thi 3 cấp độ như cũ...)")
    time.sleep(1)
    send_next_batch_question(uid, state) # Hàm này cần copy từ file cũ vào

def send_next_batch_question(uid, state):
    # ... (Copy logic quiz cũ vào đây)
    pass # Placeholder

def check_quiz_answer(uid, state, text):
    # ... (Copy logic quiz cũ vào đây)
    pass # Placeholder

# --- MESSAGE ROUTER ---
def process(uid, text):
    state = get_state(uid)
    msg = text.lower().strip()
    state["last_interaction"] = get_ts()

    # 1. QUẢN LÝ TỪ VỰNG (Feature Mới)
    if "thêm từ" in msg or "xóa từ" in msg:
        parsed = ai_parse_command(text)
        if parsed:
            if parsed['action'] == 'ADD':
                if add_word_to_db(parsed['hanzi'], parsed.get('pinyin',''), parsed.get('meaning','')):
                    send_fb(uid, f"✅ Đã thêm: {parsed['hanzi']} - {parsed.get('meaning')}")
                else:
                    send_fb(uid, "❌ Lỗi khi thêm từ.")
            elif parsed['action'] == 'DELETE':
                if delete_word_from_db(parsed['hanzi']):
                    send_fb(uid, f"🗑️ Đã xóa: {parsed['hanzi']}")
                else:
                    send_fb(uid, "❌ Lỗi xóa từ.")
        else:
            send_fb(uid, "⚠️ Mình không hiểu lệnh. Ví dụ: 'Thêm từ Mèo nghĩa là con mèo'")
        return

    # 2. LOGIC HỌC (Giữ nguyên)
    if any(c in msg for c in ['bắt đầu', 'start']):
        state["mode"] = "AUTO"; state["session"] = []
        send_card(uid, state)
        return
        
    if state["mode"] == "AUTO":
        if state["waiting"]:
            # Confirm Logic
            curr = state.get("current_word_char", "")
            if (curr and curr in text) or "tiếp" in msg:
                # ... (Logic confirm cũ)
                pass
    
    # ... (Các logic khác giữ nguyên)

# --- CRON & WEBHOOK (Giữ nguyên) ---
@app.get("/trigger_scan")
def trigger_scan():
    # ... (Logic cron cũ, gọi send_card)
    return PlainTextResponse("SCAN")

@app.post("/webhook")
async def wh(req: Request, bg: BackgroundTasks):
    # ... (Logic webhook cũ)
    return PlainTextResponse("OK")

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
