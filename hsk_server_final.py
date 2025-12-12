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
        requests.post("https://graph.facebook.com/v16.0/me/messages", 
            params={"access_token": PAGE_ACCESS_TOKEN},
            json={"recipient": {"id": uid}, "message": {"text": txt}}, timeout=10)
    except Exception as e: logger.error(f"Send Err: {e}")

def send_audio_fb(user_id, text_content):
    if not text_content: return
    filename = f"voice_{user_id}_{int(time.time())}.mp3"
    try:
        tts = gTTS(text=text_content, lang='zh-cn')
        tts.save(filename)
        url = f"https://graph.facebook.com/v16.0/me/messages?access_token={PAGE_ACCESS_TOKEN}"
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

def send_guide_message(user_id):
    guide = (
        "📚 **HƯỚNG DẪN**\n"
        "🔹 `Bắt đầu`: Học từ mới.\n"
        "🔹 `Hiểu`: Xác nhận đã học (đếm 10p).\n"
        "🔹 `Tiếp`: Bỏ qua chờ, học tiếp.\n"
        "🔹 `Thi`: Sau 6 từ sẽ thi 3 cấp độ.\n"
        "🔹 `Học lại`: Xóa dữ liệu.\n"
    )
    send_fb(user_id, guide)

# --- CORE LOGIC ---

def send_next_auto_word(uid, state):
    if 0 <= datetime.now(timezone(timedelta(hours=7))).hour < 6: return
    
    if len(state["session"]) >= 6:
        # Chuyển sang Start Quiz Logic (Giữ nguyên như cũ)
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

def send_card(uid, state):
    send_next_auto_word(uid, state)

def cmd_confirm(uid, state, text_msg):
    # Logic xác nhận đã hiểu
    current_char = state.get("current_word_char", "").strip()
    is_correct = (current_char and current_char in text_msg) or any(w in text_msg.lower() for w in ["hiểu", "ok", "tiếp", "yes"])
    
    if is_correct:
        if len(state["session"]) >= 6:
            start_advanced_quiz(uid, state)
        else:
            now = get_ts()
            next_t = now + 540 # 9 phút
            state["next_time"] = next_t
            state["waiting"] = False
            state["reminder_sent"] = False
            send_fb(uid, f"✅ Đã xác nhận. Hẹn {get_vn_time_str(next_t)} gửi tiếp.")
            save_state(uid, state)
    else:
        send_fb(uid, f"⚠️ Hãy gõ lại từ **{current_char}** để ghi nhớ mặt chữ nhé!")

# --- QUIZ LOGIC (BATCH MODE - 3 LEVELS) ---

def start_advanced_quiz(uid, state):
    state["mode"] = "QUIZ"
    
    # Khởi tạo Queue cho Level 1 (toàn bộ 6 từ)
    indices = list(range(len(state["session"])))
    random.shuffle(indices)
    
    state["quiz_state"] = {
        "level": 1,
        "queue": indices, 
        "failed": [],     
        "current_idx": -1, 
        "current_question": None
    }
    
    state["waiting"] = False
    state["next_time"] = 0
    save_state(uid, state)
    
    send_fb(uid, "🛑 **KIỂM TRA 3 CẤP ĐỘ**\nQuy tắc: Đúng 100% mới qua màn.\n\n🚀 **CẤP 1: NHÌN HÁN TỰ -> ĐOÁN NGHĨA**")
    time.sleep(1)
    send_next_batch_question(uid, state)

def send_next_batch_question(uid, state):
    qs = state["quiz_state"]
    qs["current_idx"] += 1
    
    if qs["current_idx"] >= len(qs["queue"]):
        # Hết hàng đợi
        if len(qs["failed"]) > 0:
            send_fb(uid, f"⚠️ Sai {len(qs['failed'])} từ. Ôn lại ngay.")
            qs["queue"] = qs["failed"][:] 
            random.shuffle(qs["queue"])   
            qs["failed"] = []             
            qs["current_idx"] = 0         
            save_state(uid, state)
            time.sleep(1)
            send_batch_question_content(uid, state)
        else:
            # Qua Level
            next_level = qs["level"] + 1
            if next_level > 3:
                finish_session(uid, state)
            else:
                qs["level"] = next_level
                qs["queue"] = list(range(len(state["session"]))) 
                random.shuffle(qs["queue"])
                qs["failed"] = []
                qs["current_idx"] = 0
                
                level_names = {2: "CẤP 2: NHÌN NGHĨA -> VIẾT HÁN TỰ", 3: "CẤP 3: NGHE TỪ VỰNG -> VIẾT HÁN TỰ"}
                send_fb(uid, f"🎉 Xuất sắc! Qua màn.\n\n🚀 **{level_names.get(next_level, '')}**")
                save_state(uid, state)
                time.sleep(2)
                send_batch_question_content(uid, state)
    else:
        send_batch_question_content(uid, state)

def send_batch_question_content(uid, state):
    qs = state["quiz_state"]
    word_idx = qs["queue"][qs["current_idx"]]
    word = state["session"][word_idx]
    level = qs["level"]
    
    prog = f"({qs['current_idx'] + 1}/{len(qs['queue'])})"
    msg = ""
    
    if level == 1:
        msg = f"🔥 {prog} Nghĩa của từ **[{word['Hán tự']}]** là gì?"
        qs["current_question"] = {"type": "HAN_VIET", "answer": word["Nghĩa"]}
    elif level == 2:
        msg = f"🔥 {prog} Viết chữ Hán cho từ **'{word['Nghĩa']}'**:"
        qs["current_question"] = {"type": "VIET_HAN", "answer": word["Hán tự"]}
    elif level == 3:
        msg = f"🔥 {prog} Nghe và gõ lại từ (Audio đang gửi...):"
        qs["current_question"] = {"type": "LISTEN_WRITE", "answer": word["Hán tự"]}
        threading.Thread(target=send_audio_fb, args=(uid, word['Hán tự'])).start()

    send_fb(uid, msg)
    save_state(uid, state)

def check_quiz_answer(uid, state, text):
    qs = state["quiz_state"]
    target = qs.get("current_question")
    if not target: return

    is_correct = False
    ans = target["answer"].lower().strip()
    usr = text.lower().strip().replace(".", "").replace("!", "")
    
    if target["type"] == "HAN_VIET":
        if any(k.strip() in usr for k in ans.split(",")): is_correct = True
    elif target["type"] in ["VIET_HAN", "LISTEN_WRITE"]:
        if ans in usr: is_correct = True
        
    if is_correct:
        send_fb(uid, "✅ Chính xác!")
    else:
        word_idx = qs["queue"][qs["current_idx"]]
        if word_idx not in qs["failed"]: qs["failed"].append(word_idx)
        send_fb(uid, f"❌ Sai rồi. Đáp án: {target['answer']}")
        
    save_state(uid, state)
    time.sleep(1)
    send_next_batch_question(uid, state)

def finish_session(uid, state):
    send_fb(uid, "🏆 Hoàn thành bài thi! Nghỉ 10 phút nhé.")
    state["mode"] = "AUTO"
    state["session"] = [] 
    state["next_time"] = get_ts() + 540
    state["waiting"] = False 
    send_fb(uid, f"⏰ Hẹn {get_vn_time_str(state['next_time'])}.")
    save_state(uid, state)

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

    # 2. LOGIC HỌC
    if any(c in msg for c in ['bắt đầu', 'start', 'chào buổi sáng']):
        state["mode"] = "AUTO"; state["session"] = []
        send_card(uid, state)
        return
        
    if "reset" in msg:
        state = {"user_id": uid, "mode": "IDLE", "learned": [], "session": [], "next_time": 0, "waiting": False, "last_interaction": 0, "reminder_sent": False, "quiz_state": {"word_idx": 0, "level": 0, "current_question": None}, "current_word_char": ""}
        save_state(uid, state)
        send_fb(uid, "Đã reset.")
        return

    if state["mode"] == "AUTO":
        if state["waiting"]:
            cmd_confirm(uid, state, text)
        else:
            if "tiếp" in msg:
                send_card(uid, state)
            elif "bao lâu" in msg:
                rem = state["next_time"] - get_ts()
                if rem > 0: send_fb(uid, f"⏳ Còn {rem//60} phút.")
                else: send_card(uid, state)
            else:
                send_fb(uid, ai_smart_reply(text, "User đang chờ timer"))

    elif state["mode"] == "QUIZ":
        check_quiz_answer(uid, state, text)
        
    else:
        send_fb(uid, ai_smart_reply(text, "User đang rảnh"))

# --- CRON & WEBHOOK ---
@app.get("/trigger_scan")
def trigger_scan():
    try:
        now = get_ts()
        if db_pool:
            conn = db_pool.getconn()
            try:
                with conn.cursor() as cur:
                    cur.execute("CREATE TABLE IF NOT EXISTS users (user_id VARCHAR(50) PRIMARY KEY, state JSONB)")
                    cur.execute("SELECT state FROM users")
                    rows = cur.fetchall()
                    for row in rows:
                        state = row[0]
                        uid = state["user_id"]
                        USER_CACHE[uid] = state
                        
                        if state["mode"] == "AUTO" and not state["waiting"] and state["next_time"] > 0:
                            if now >= state["next_time"]:
                                logger.info(f"CRON: Triggering send for {uid}")
                                send_card(uid, state)
                        
                        if state["mode"] == "AUTO" and state["waiting"]:
                            last_act = state.get("last_interaction", 0)
                            if (now - last_act > 1800) and not state.get("reminder_sent", False):
                                send_fb(uid, "🔔 Bạn ơi, học xong chưa? Gõ lại từ để tiếp tục nhé!")
                                state["reminder_sent"] = True
                                save_state(uid, state)
            finally:
                db_pool.putconn(conn)
        return PlainTextResponse("SCAN COMPLETED")
    except Exception as e:
        logger.error(f"Scan Error: {e}")
        return PlainTextResponse(f"ERROR: {e}", status_code=500)

@app.post("/webhook")
async def wh(req: Request, bg: BackgroundTasks):
    try:
        d = await req.json()
        if 'entry' in d:
            for e in d['entry']:
                for m in e.get('messaging', []):
                    if 'message' in m:
                        bg.add_task(process, m['sender']['id'], m['message'].get('text', ''))
        return PlainTextResponse("EVENT_RECEIVED")
    except: return PlainTextResponse("ERROR")

@app.get("/webhook")
def verify(request: Request):
    if request.query_params.get("hub.verify_token") == VERIFY_TOKEN:
        return PlainTextResponse(request.query_params.get("hub.challenge"))
    return PlainTextResponse("Error", 403)

@app.get("/")
def home(): return PlainTextResponse("Server OK")

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
