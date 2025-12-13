import os
import json
import time
import random
import logging
import threading
import re
import requests
from datetime import datetime, timedelta, timezone

# Web Framework
import uvicorn
from fastapi import FastAPI, Request, BackgroundTasks
from fastapi.responses import PlainTextResponse

# AI & Audio
import google.generativeai as genai
from gtts import gTTS

# Database
import psycopg2
from psycopg2 import pool

# Dữ liệu từ vựng (Fallback)
try:
    from hsk2_vocabulary_full import HSK_DATA
except ImportError:
    HSK_DATA = []

# --- CẤU HÌNH ---
PAGE_ACCESS_TOKEN = "EAAbQQNNSmSMBQM5JdL7WYT15Kpz2WUip1Tte40vI75VbtRNm1O1F5mauEtTpzsTvetV9DFjEj4rRsWMUvZB8c2RvwV4FIhX0ky4bjoup8vjJrhyjiUPgUCpR0Gkg1UDxEiorU6C5LORUGwhBrRBIvRL7a8WQmtoafKpaxRkgjeZCfWQZBsqGZBNxEMoUuaFclIqWkwZDZD"
VERIFY_TOKEN = "hsk_mat_khau_bi_mat"
GEMINI_API_KEY = "AIzaSyB5V6sgqSOZO4v5DyuEZs3msgJqUk54HqQ"
DATABASE_URL = os.environ.get('DATABASE_URL')

# --- SETUP ---
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
app = FastAPI()

model = None
if GEMINI_API_KEY:
    try:
        genai.configure(api_key=GEMINI_API_KEY)
        model = genai.GenerativeModel('gemini-1.5-flash')
    except: logger.error("Gemini Error")

db_pool = None
if DATABASE_URL:
    try:
        db_pool = psycopg2.pool.SimpleConnectionPool(1, 20, dsn=DATABASE_URL)
        logger.info("✅ Database connected!")
    except: logger.error("❌ Database connection failed")

USER_CACHE = {}

# --- DB HELPERS ---
def get_db_conn(): return db_pool.getconn() if db_pool else None
def release_db_conn(conn): 
    if db_pool and conn: db_pool.putconn(conn)

def init_db():
    conn = get_db_conn()
    if not conn: return
    try:
        with conn.cursor() as cur:
            cur.execute("""CREATE TABLE IF NOT EXISTS users (user_id VARCHAR(50) PRIMARY KEY, state JSONB, updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP);""")
            cur.execute("""CREATE TABLE IF NOT EXISTS words (id SERIAL PRIMARY KEY, hanzi VARCHAR(50) UNIQUE NOT NULL, pinyin VARCHAR(100), meaning TEXT, level INT DEFAULT 2);""")
            cur.execute("SELECT COUNT(*) FROM words")
            if cur.fetchone()[0] == 0 and HSK_DATA:
                valid_data = [x for x in HSK_DATA if 'Hán tự' in x]
                if valid_data:
                    args_str = ','.join(cur.mogrify("(%s,%s,%s)", (x['Hán tự'], x['Pinyin'], x['Nghĩa'])).decode('utf-8') for x in valid_data)
                    cur.execute("INSERT INTO words (hanzi, pinyin, meaning) VALUES " + args_str)
        conn.commit()
    except Exception as e: logger.error(f"Init DB Error: {e}"); conn.rollback()
    finally: release_db_conn(conn)

def get_random_words(exclude, count=1):
    conn = get_db_conn(); 
    if not conn: return []
    try:
        with conn.cursor() as cur:
            if exclude:
                cur.execute("SELECT hanzi, pinyin, meaning FROM words WHERE hanzi NOT IN %s ORDER BY RANDOM() LIMIT %s", (tuple(exclude), count))
            else:
                cur.execute("SELECT hanzi, pinyin, meaning FROM words ORDER BY RANDOM() LIMIT %s", (count,))
            return [{"Hán tự": r[0], "Pinyin": r[1], "Nghĩa": r[2]} for r in cur.fetchall()]
    except: return []
    finally: release_db_conn(conn)

def get_total_words():
    conn = get_db_conn(); 
    if not conn: return 0
    try:
        with conn.cursor() as cur: cur.execute("SELECT COUNT(*) FROM words"); return cur.fetchone()[0]
    finally: release_db_conn(conn)

def add_word_db(hanzi, pinyin, meaning):
    conn = get_db_conn(); 
    if not conn: return False
    try:
        with conn.cursor() as cur:
            cur.execute("INSERT INTO words (hanzi, pinyin, meaning) VALUES (%s, %s, %s) ON CONFLICT (hanzi) DO NOTHING", (hanzi, pinyin, meaning))
        conn.commit(); return True
    except: return False
    finally: release_db_conn(conn)

# --- AI & LOGIC ---
def ai_simple_example(word):
    hanzi, meaning = word.get('Hán tự',''), word.get('Nghĩa','')
    backup = {"han": f"{hanzi}", "pinyin": "...", "viet": f"{meaning}"}
    if not model: return backup
    try:
        prompt = f"Đặt 1 câu tiếng Trung CỰC KỲ ĐƠN GIẢN (HSK 1, <10 từ) dùng từ: {hanzi} ({meaning}). Trả JSON: {{\"han\": \"...\", \"pinyin\": \"...\", \"viet\": \"...\"}}"
        res = model.generate_content(prompt).text.strip()
        match = re.search(r'\{.*\}', res, re.DOTALL)
        return json.loads(match.group()) if match else backup
    except: return backup

def ai_lookup(text):
    if not model: return None
    try:
        prompt = f"Tra từ điển từ: '{text}'. Trả JSON: {{\"hanzi\": \"{text}\", \"pinyin\": \"...\", \"meaning\": \"...\"}}. Nếu không phải tiếng Trung trả null."
        res = model.generate_content(prompt).text.strip()
        res = res.replace('```json', '').replace('```', '')
        return json.loads(res)
    except: return None

def ai_chat(text):
    if not model: return "Gõ 'Menu' để xem hướng dẫn."
    try: return model.generate_content(f"Bạn là trợ lý học tiếng Trung. User: '{text}'. Trả lời ngắn gọn tiếng Việt.").text.strip()
    except: return "Lỗi mạng."

# --- UTILS ---
def get_ts(): return int(time.time())
def get_vn_time(): return datetime.now(timezone(timedelta(hours=7)))
def send_fb(uid, txt):
    try:
        requests.post("https://graph.facebook.com/v16.0/me/messages", params={"access_token": PAGE_ACCESS_TOKEN}, json={"recipient": {"id": uid}, "message": {"text": txt}}, timeout=10)
    except: pass

def send_audio(uid, txt):
    if not txt: return
    fname = f"tts_{uid}_{get_ts()}.mp3"
    try:
        gTTS(text=txt, lang='zh-cn').save(fname)
        requests.post(f"https://graph.facebook.com/v16.0/me/messages?access_token={PAGE_ACCESS_TOKEN}", 
            data={'recipient': json.dumps({'id': uid}), 'message': json.dumps({'attachment': {'type': 'audio', 'payload': {}}})}, 
            files={'filedata': (fname, open(fname, 'rb'), 'audio/mp3')}, timeout=20)
    except: pass
    finally: 
        if os.path.exists(fname): os.remove(fname)

# --- STATE ---
def get_state(uid):
    if uid in USER_CACHE: return USER_CACHE[uid]
    s = {"user_id": uid, "mode": "IDLE", "learned": [], "session": [], "next_time": 0, "waiting": False, "temp_word": None, "last_greet": "", 
         "quiz": {"level": 1, "queue": [], "failed": [], "idx": 0}}
    conn = get_db_conn()
    if conn:
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT state FROM users WHERE user_id = %s", (uid,))
                row = cur.fetchone()
                if row and row[0]: s.update(json.loads(row[0]) if isinstance(row[0], str) else row[0])
        finally: release_db_conn(conn)
    USER_CACHE[uid] = s
    return s

def save_state(uid, s):
    USER_CACHE[uid] = s
    conn = get_db_conn()
    if conn:
        try:
            with conn.cursor() as cur:
                cur.execute("INSERT INTO users (user_id, state) VALUES (%s, %s) ON CONFLICT (user_id) DO UPDATE SET state = EXCLUDED.state", (uid, json.dumps(s)))
            conn.commit()
        finally: release_db_conn(conn)

# --- LEARNING LOGIC ---
def send_word(uid, state):
    if 0 <= get_vn_time().hour < 6: return
    if len(state["session"]) >= 6: start_quiz_level(uid, state, 1); return

    w = get_random_words(state.get("learned", []), 1)
    if not w: send_fb(uid, "🎉 Hết từ vựng! Reset hoặc thêm từ mới."); return
    
    word = w[0]
    state["session"].append(word); state["learned"].append(word['Hán tự'])
    state["current_word"] = word['Hán tự']
    
    ex = ai_simple_example(word)
    total = get_total_words()
    
    msg = (f"🔔 **TỪ MỚI** ({len(state['session'])}/6 | Kho: {total})\n\n"
           f"🇨🇳 **{word['Hán tự']}** ({word['Pinyin']})\n"
           f"🇻🇳 {word['Nghĩa']}\n"
           f"----------------\n"
           f"VD: {ex['han']}\n👉 {ex['viet']}\n\n"
           f"👉 Gõ lại từ **{word['Hán tự']}** để học.")
    send_fb(uid, msg)
    threading.Thread(target=send_audio, args=(uid, word['Hán tự'])).start()
    threading.Thread(target=lambda: (time.sleep(2), send_audio(uid, ex['han']))).start()
    
    state["waiting"] = True; state["next_time"] = 0; save_state(uid, state)

# --- QUIZ 3 LEVELS LOGIC ---
def start_quiz_level(uid, state, level):
    state["mode"] = "QUIZ"
    # Reset queue nếu là level mới
    if level == 1: 
        state["quiz"]["queue"] = list(range(len(state["session"]))) # [0, 1, 2, 3, 4, 5]
        random.shuffle(state["quiz"]["queue"])
    elif level > 1:
        state["quiz"]["queue"] = list(range(len(state["session"]))) # Reset queue cho level sau
        random.shuffle(state["quiz"]["queue"])
        
    state["quiz"]["level"] = level
    state["quiz"]["idx"] = 0
    state["quiz"]["failed"] = [] # Reset failed mỗi level mới
    
    titles = {1: "CẤP 1: NHÌN HÁN -> ĐOÁN NGHĨA", 2: "CẤP 2: NHÌN NGHĨA -> VIẾT HÁN", 3: "CẤP 3: NGHE -> VIẾT HÁN"}
    send_fb(uid, f"🛑 **KIỂM TRA {titles[level]}**\n(Phải đúng 6/6 từ mới qua màn)")
    time.sleep(1)
    send_quiz_question(uid, state)

def send_quiz_question(uid, state):
    q = state["quiz"]
    if q["idx"] >= len(q["queue"]): # Hết câu hỏi
        if len(q["failed"]) > 0:
            send_fb(uid, f"⚠️ Sai {len(q['failed'])} từ. Ôn lại ngay!")
            q["queue"] = q["failed"][:] # Chỉ hỏi lại các câu sai
            q["failed"] = []
            q["idx"] = 0
            random.shuffle(q["queue"])
            save_state(uid, state)
            time.sleep(1)
            send_quiz_question(uid, state)
        else:
            # Qua màn
            if q["level"] < 3:
                send_fb(uid, f"🎉 Xuất sắc! Lên Cấp {q['level']+1}...")
                start_quiz_level(uid, state, q["level"] + 1)
            else:
                send_fb(uid, "🏆 **CHÚC MỪNG!** Bạn đã hoàn thành 3 cấp độ.\nNghỉ ngơi nhé, 10 phút nữa học tiếp!")
                state["mode"] = "AUTO"; state["session"] = []
                state["next_time"] = get_ts() + 600 # 10p nghỉ
                state["waiting"] = False
                save_state(uid, state)
        return

    # Lấy câu hỏi hiện tại
    w_idx = q["queue"][q["idx"]]
    word = state["session"][w_idx]
    lvl = q["level"]
    
    if lvl == 1:
        msg = f"❓ ({q['idx']+1}/{len(q['queue'])}) **{word['Hán tự']}** nghĩa là gì?"
    elif lvl == 2:
        msg = f"❓ ({q['idx']+1}/{len(q['queue'])}) Viết chữ Hán cho: **{word['Nghĩa']}**"
    elif lvl == 3:
        msg = f"🎧 ({q['idx']+1}/{len(q['queue'])}) Nghe và viết lại từ (Audio đang gửi...)"
        threading.Thread(target=send_audio, args=(uid, word['Hán tự'])).start()

    send_fb(uid, msg)
    save_state(uid, state)

def check_quiz(uid, state, text):
    q = state["quiz"]
    w_idx = q["queue"][q["idx"]]
    word = state["session"][w_idx]
    ans = text.lower().strip()
    
    correct = False
    if q["level"] == 1: # Check nghĩa
        # AI check nghĩa hoặc check string cơ bản
        if any(x in ans for x in word['Nghĩa'].lower().split(',')) or len(ans) > 2: correct = True # Chấp nhận tương đối
    elif q["level"] in [2, 3]: # Check Hán tự
        if word['Hán tự'] in text: correct = True

    if correct:
        send_fb(uid, "✅ Đúng!")
    else:
        send_fb(uid, f"❌ Sai. Đáp án: {word['Hán tự']} - {word['Nghĩa']}")
        if w_idx not in q["failed"]: q["failed"].append(w_idx)

    q["idx"] += 1
    save_state(uid, state)
    time.sleep(1)
    send_quiz_question(uid, state)

# --- PROCESS ---
def process(uid, text):
    # 1. SLEEP MODE 0H-6H
    if 0 <= get_vn_time().hour < 6:
        send_fb(uid, "💤 Hệ thống đang nghỉ (0h-6h). Mai học tiếp nhé!"); return

    state = get_state(uid)
    msg = text.lower().strip()

    # 2. ADD WORD (3 Steps)
    if msg == "thêm từ":
        state["mode"] = "ADD_1"; send_fb(uid, "📝 Nhập **Hán tự** muốn thêm:"); save_state(uid, state); return
    
    if state["mode"] == "ADD_1":
        if msg in ["hủy","không"]: state["mode"]="IDLE"; send_fb(uid, "❌ Hủy."); save_state(uid, state); return
        send_fb(uid, "⏳ Đang tra cứu...")
        data = ai_lookup(text)
        if data and data.get('pinyin'):
            state["temp"] = data; state["mode"] = "ADD_2"
            send_fb(uid, f"📖 {data['hanzi']} - {data['pinyin']}\nNghĩa: {data['meaning']}\n❓ Thêm không? (OK/Không)")
        else: send_fb(uid, "⚠️ Lỗi. Nhập lại hoặc Hủy.")
        save_state(uid, state); return

    if state["mode"] == "ADD_2":
        if msg in ["ok","có","lưu"]:
            d = state["temp"]
            if add_word_db(d['hanzi'], d['pinyin'], d['meaning']): send_fb(uid, f"✅ Đã thêm {d['hanzi']}")
            else: send_fb(uid, "⚠️ Từ đã có.")
        else: send_fb(uid, "❌ Hủy.")
        state["mode"]="IDLE"; state["temp"]=None; save_state(uid, state); return

    # 3. QUIZ MODE
    if state["mode"] == "QUIZ":
        check_quiz(uid, state, text); return

    # 4. NORMAL COMMANDS
    if msg in ["bắt đầu", "start"]:
        state["mode"]="AUTO"; state["session"]=[]; send_word(uid, state); return
    
    if msg in ["reset", "học lại"]:
        state.update({"mode":"IDLE", "learned":[], "session":[]}); save_state(uid, state); send_fb(uid, "🔄 Reset."); return

    # 5. LEARNING FLOW
    if state["mode"] == "AUTO":
        if state["waiting"]:
            cur = state.get("current_word","")
            if (cur in text) or (msg in ["hiểu","ok","tiếp"]):
                state["next_time"] = get_ts() + 540 # 9 mins
                state["waiting"] = False
                send_fb(uid, "✅ Đã thuộc. Hẹn 9p nữa."); save_state(uid, state)
            else: send_fb(uid, f"⚠️ Gõ lại từ **{cur}** nhé.")
        else:
            if "tiếp" in msg: send_word(uid, state)
            else: send_fb(uid, ai_chat(text))
    else:
        send_fb(uid, ai_chat(text))

# --- TRIGGER & SERVER ---
@app.on_event("startup")
def startup(): init_db()

@app.get("/trigger_scan")
def scan():
    now = get_vn_time()
    if 0 <= now.hour < 6: return PlainTextResponse("SLEEP") # Cronjob cũng ngủ
    
    if db_pool:
        conn = get_db_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT state FROM users")
                for row in cur.fetchall():
                    s = json.loads(row[0]) if isinstance(row[0], str) else row[0]
                    uid = s["user_id"]
                    
                    # Chào buổi sáng
                    if s.get("last_greet") != now.strftime("%Y-%m-%d"):
                        send_fb(uid, "☀️ Chào buổi sáng! Gõ 'Bắt đầu' để học.")
                        s["last_greet"] = now.strftime("%Y-%m-%d"); save_state(uid, s); continue

                    # Gửi từ mới
                    if s["mode"]=="AUTO" and not s["waiting"] and s["next_time"]>0 and get_ts()>=s["next_time"]:
                        USER_CACHE[uid] = s; send_word(uid, s)
        finally: release_db_conn(conn)
    return PlainTextResponse("OK")

@app.post("/webhook")
async def wh(req: Request, bg: BackgroundTasks):
    try:
        d = await req.json()
        if 'entry' in d:
            for e in d['entry']:
                for m in e.get('messaging', []):
                    if 'message' in m: bg.add_task(process, m['sender']['id'], m['message'].get('text', ''))
    except: pass
    return PlainTextResponse("OK")

@app.get("/webhook")
def verify(req: Request):
    if req.query_params.get("hub.verify_token") == VERIFY_TOKEN: return PlainTextResponse(req.query_params.get("hub.challenge"))
    return PlainTextResponse("Error", 403)

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
