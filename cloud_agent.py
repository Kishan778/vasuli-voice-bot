import io
import os
import re
import time
import asyncio
import threading
import requests
import pandas as pd
import edge_tts
from flask import Flask, request
from groq import Groq

# --- 1. CREDENTIALS & LINKS ---
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "8851118107:AAG-1M6vMIimiF_5qCh88ZHJsSQboDjER4k")
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "YAHAN_APNA_GROQ_API_KEY_DALEIN")
GOOGLE_SHEET_URL = os.environ.get(
    "GOOGLE_SHEET_URL",
    "https://docs.google.com/spreadsheets/d/1hMjxdXH8wCiCH6WU8VWlYhbXmxhYbr-THyGatXjzxQk/edit?usp=sharing"
)
RENDER_URL = os.environ.get("RENDER_EXTERNAL_URL", "https://vasuli-voice-bot.onrender.com").rstrip("/")
TG_API = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"

groq_client = Groq(api_key=GROQ_API_KEY)
app = Flask(__name__)

uploaded_sheets = None
chat_memory = []
processed_updates = set()

def extract_sheet_id(url_or_id):
    clean = str(url_or_id).strip().replace('[', '').replace(']', '')
    if "(" in clean and ")" in clean:
        clean = clean.split("(")[1].split(")")[0]
    if "/d/" in clean:
        return clean.split("/d/")[1].split("/")[0]
    return clean

# --- 2. DIRECT TELEGRAM SENDER (Zero Async Hang) ---
def tg_send_message(chat_id, text):
    try:
        requests.post(f"{TG_API}/sendMessage", json={"chat_id": chat_id, "text": text}, timeout=15)
    except Exception as e:
        print(f"Send msg error: {e}")

def tg_send_voice(chat_id, audio_path, caption=""):
    try:
        with open(audio_path, "rb") as f:
            requests.post(
                f"{TG_API}/sendVoice",
                data={"chat_id": chat_id, "caption": caption[:1020]},
                files={"voice": f},
                timeout=30
            )
    except Exception as e:
        print(f"Send voice error: {e}")
        tg_send_message(chat_id, caption)

# --- 3. GOOGLE SHEET LOADER ---
def load_all_tabs():
    global uploaded_sheets
    if uploaded_sheets is not None:
        return uploaded_sheets, "Uploaded Excel File"

    sheet_id = extract_sheet_id(GOOGLE_SHEET_URL)
    if not sheet_id or "YAHAN" in sheet_id:
        return None, "Sheet link missing"

    try:
        xlsx_url = f"https://docs.google.com/spreadsheets/d/{sheet_id}/export?format=xlsx"
        res = requests.get(xlsx_url, timeout=15)
        if res.status_code == 200:
            return pd.read_excel(io.BytesIO(res.content), sheet_name=None), "Live Google Sheet"
    except Exception as e:
        print(f"XLSX error: {e}")

    sheets_dict = {}
    for tab in ['tarun', 'ajay']:
        try:
            csv_url = f"https://docs.google.com/spreadsheets/d/{sheet_id}/gviz/tq?tqx=out:csv&sheet={tab}"
            res = requests.get(csv_url, timeout=10)
            if res.status_code == 200 and len(res.text) > 20:
                sheets_dict[tab] = pd.read_csv(io.StringIO(res.text))
        except Exception:
            pass

    return (sheets_dict, "Live Google Sheet") if sheets_dict else (None, "Load failed")

# --- 4. BASULI SHEET ANALYSIS ---
def analyze_basuli_sheet():
    sheets_dict, source_name = load_all_tabs()
    if not sheets_dict:
        return "NO_SHEET_LOADED"

    summary_parts = [f"SOURCE: {source_name}"]
    for tab_name, df in sheets_dict.items():
        if df is None or df.empty:
            continue

        df.columns = [str(c).strip() for c in df.columns]
        cols = df.columns.tolist()

        party_col = next((c for c in cols if any(k in c.lower() for k in ['party', 'name', 'outlet', 'shop', 'ledger'])), None)
        amt_col = next((c for c in cols if any(k in c.lower() for k in ['pending', 'amount', 'balance', 'due', 'vasuli'])), None)
        overdue_col = next((c for c in cols if 'overdue' in c.lower()), None)
        ref_col = next((c for c in cols if 'ref' in c.lower()), None)
        date_col = next((c for c in cols if 'date' in c.lower()), None)

        if not party_col or not amt_col:
            continue

        work_df = df.copy()
        work_df[party_col] = work_df[party_col].astype(str).str.strip()
        work_df = work_df[
            ~work_df[party_col].str.lower().isin(['total', 'grand total', 'nan', 'none', ''])
            & ~work_df[party_col].str.lower().str.startswith('total')
        ]

        work_df[amt_col] = work_df[amt_col].astype(str).str.replace(',', '').str.replace('₹', '').str.replace('Rs', '', case=False).str.strip()
        work_df[amt_col] = pd.to_numeric(work_df[amt_col], errors='coerce').fillna(0)
        work_df = work_df[work_df[amt_col] > 0]

        if overdue_col:
            work_df[overdue_col] = pd.to_numeric(work_df[overdue_col], errors='coerce').fillna(0)
        else:
            work_df['Overdue'] = 0
            overdue_col = 'Overdue'

        party_grouped = work_df.groupby(party_col).agg(
            Bills_Count=(amt_col, 'count'),
            Total_Pending=(amt_col, 'sum'),
            Max_Overdue_Days=(overdue_col, 'max')
        ).reset_index().sort_values(by='Total_Pending', ascending=False)

        tab_total = work_df[amt_col].sum()
        summary_parts.append(f"\n==========================================")
        summary_parts.append(f"SALESMAN: {tab_name.upper()}")
        summary_parts.append(f"- {tab_name.upper()} KUL BAKI AMOUNT: Rs {tab_total:,.0f}")
        summary_parts.append(f"- Kul Bills: {len(work_df)} | Kul Parties: {len(party_grouped)}")
        summary_parts.append(f"\n{tab_name.upper()} PARTY-WISE TOTAL:")
        summary_parts.append(party_grouped.to_string(index=False))

        cols_to_show = [c for c in [date_col, ref_col, party_col, amt_col, overdue_col] if c]
        summary_parts.append(f"\n{tab_name.upper()} INDIVIDUAL BILLS:")
        summary_parts.append(work_df[cols_to_show].to_string(index=False))

    return "\n".join(summary_parts)

# --- 5. FAST PROFESSIONAL MANAGER VOICE ---
def generate_voice_sync(text, filename):
    clean_text = re.sub(r'<think>.*?</think>', '', str(text), flags=re.DOTALL)
    clean_text = re.sub(r'[*#`_~>-]', '', clean_text).strip()
    if not clean_text:
        clean_text = "Sir, data nahi mil pa raha hai."

    async def _save():
        communicate = edge_tts.Communicate(
            clean_text,
            voice="hi-IN-MadhurNeural",
            rate="+18%",
            pitch="-4Hz"
        )
        await communicate.save(filename)

    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(_save())
    finally:
        loop.close()
    return filename, clean_text

# --- 6. GROQ AI (1000 Tokens + Memory) ---
def ask_groq(system_prompt, user_text):
    global chat_memory
    try:
        all_models = [m.id for m in groq_client.models.list().data]
        bad_keywords = ["allam", "whisper", "orpheus", "guard", "tts", "vision", "playai", "distil", "safeguard"]
        valid_models = [m for m in all_models if not any(bad in m.lower() for bad in bad_keywords)]
        priority = ["llama-3.3-70b-versatile", "llama-3.1-8b-instant"]
        llama_m = [m for m in valid_models if "llama" in m.lower()]
        models_to_try = [m for m in priority if m in valid_models] + [m for m in llama_m if m not in priority] + [m for m in valid_models if m not in llama_m]
    except Exception:
        models_to_try = ["llama-3.3-70b-versatile", "llama-3.1-8b-instant"]

    messages = [{"role": "system", "content": system_prompt}] + chat_memory[-6:] + [{"role": "user", "content": user_text}]

    for m in models_to_try:
        try:
            completion = groq_client.chat.completions.create(
                model=m,
                messages=messages,
                temperature=0.2,
                max_tokens=1000
            )
            reply = completion.choices[0].message.content
            if reply:
                reply = re.sub(r'<think>.*?</think>', '', reply, flags=re.DOTALL).strip()
            if reply and len(reply) > 3:
                chat_memory.append({"role": "user", "content": user_text})
                chat_memory.append({"role": "assistant", "content": reply})
                return reply
        except Exception as e:
            print(f"Model {m} error: {e}")
            continue

    return "Sir, abhi AI server se jawab nahi aa paya, ek baar dubara bolein."

# --- 7. BACKGROUND WORKER ---
def process_telegram_update(data):
    global uploaded_sheets
    message = data.get("message") or data.get("edited_message")
    if not message:
        return

    chat_id = message["chat"]["id"]
    msg_id = message.get("message_id", 0)

    try:
        # Case A: Excel / CSV Document
        if "document" in message:
            doc = message["document"]
            file_name = doc.get("file_name", "").lower()
            if not (file_name.endswith('.xlsx') or file_name.endswith('.xls') or file_name.endswith('.csv')):
                tg_send_message(chat_id, "Sir, kripya sirf Excel (.xlsx) ya CSV file bhejein.")
                return

            file_info = requests.get(f"{TG_API}/getFile", params={"file_id": doc["file_id"]}, timeout=15).json()
            file_path = file_info["result"]["file_path"]
            file_bytes = requests.get(f"https://api.telegram.org/file/bot{TELEGRAM_TOKEN}/{file_path}", timeout=20).content

            local_path = f"/tmp/up_{msg_id}_{file_name}"
            with open(local_path, "wb") as f:
                f.write(file_bytes)

            if file_name.endswith('.csv'):
                uploaded_sheets = {"Sheet1": pd.read_csv(local_path)}
            else:
                uploaded_sheets = pd.read_excel(local_path, sheet_name=None)

            sheet_summary = analyze_basuli_sheet()
            ai_reply = ask_groq(f"Sheet Data:\n{sheet_summary}", "User ne nayi Excel file bheji hai. Strict Senior Manager ki tarah 3 line mein total pending amount aur top parties batayein.")
            audio_file, clean_reply = generate_voice_sync(ai_reply, f"/tmp/doc_{msg_id}.mp3")
            tg_send_voice(chat_id, audio_file, f"✅ Excel File Load Ho Gayi!\n\n{clean_reply}")
            return

        # Case B: Voice or Text Message
        user_text = ""
        if "voice" in message:
            file_info = requests.get(f"{TG_API}/getFile", params={"file_id": message["voice"]["file_id"]}, timeout=15).json()
            file_path = file_info["result"]["file_path"]
            voice_bytes = requests.get(f"https://api.telegram.org/file/bot{TELEGRAM_TOKEN}/{file_path}", timeout=20).content

            ogg_path = f"/tmp/voice_{msg_id}.ogg"
            with open(ogg_path, "wb") as f:
                f.write(voice_bytes)

            with open(ogg_path, "rb") as f:
                transcription = groq_client.audio.transcriptions.create(
                    file=(ogg_path, f.read()),
                    model="whisper-large-v3-turbo",
                    language="hi",
                    prompt="Tarun aur Ajay ki total udhaari kitni hai. Sitaram Rathi ke sare bill batao. Gurukrapa Traders, Ravindra Kumar, Brajmohan Kirana, Namo Kirana, Gadarwara."
                )
            user_text = transcription.text
        elif "text" in message:
            user_text = message["text"]
            if user_text.startswith("/start"):
                tg_send_message(chat_id, "Sir, Senior Vasuli Manager Agent active hai. Voice note ya text bhej kar hisab poochein.")
                return
        else:
            return

        print(f"User Query: {user_text}", flush=True)
        sheet_analysis = analyze_basuli_sheet()

        if sheet_analysis == "NO_SHEET_LOADED":
            msg = "Sir, Google Sheet load nahi ho payi hai. Link check kar lein ya seedha Excel file bhej dein."
            audio_file, clean_reply = generate_voice_sync(msg, f"/tmp/err_{msg_id}.mp3")
            tg_send_voice(chat_id, audio_file, clean_reply)
            return

        system_prompt = f"""
        Tum ek strict, to-the-point aur Professional Senior Vasuli Manager ho.
        Salesmen ke naam: Tarun (Local market) aur Ajay (Upcountry market).

        NICHE GOOGLE SHEET KA LIVE DATA HAI:
        {sheet_analysis}

        SAKHT NIYAM:
        1. 'Bhai', 'Yaar', ya 'Bade bhai' jaise shabd Kisi bhi haal mein mat bolna. Boss ko 'Sir' bolna, aur Salesman (Tarun/Ajay) ko seedha unke naam se address karna.
        2. Tone ekdum professional, vyaparik aur crisp rakhna (Jaise: 'Sir, Sitaram Rathi ke 8 bills mein kul 1 lakh 401 rupaye baki hain').
        3. Party ka naam bolte waqt aage laga 'PARLE' ya 'ZB' hata kar seedha dukan ka naam bolna.
        4. Agar bill-wise detail mangi jaye, toh bina ek bhi bill chhode saare bills ki tarikh, bill number aur rupaye fatafat point-to-point ginwana.
        5. Agar Boss bolein ki Tarun ya Ajay ke liye voice note do, toh ekdum kadak Manager ki tarah seedha aadesh dena (Jaise: 'Tarun, aaj Gadarwara market mein in 5 partiyon ka payment har haal mein clear karwana hai, dhyan rahe').
        6. Koi star (**) ya markdown mat lagana.
        """

        ai_reply = ask_groq(system_prompt, user_text)
        print(f"AI Reply: {ai_reply}", flush=True)

        audio_file, clean_reply = generate_voice_sync(ai_reply, f"/tmp/rep_{msg_id}.mp3")
        tg_send_voice(chat_id, audio_file, f"📋 Manager Report:\n{clean_reply}")

    except Exception as e:
        print(f"Processing error: {e}", flush=True)
        tg_send_message(chat_id, f"Sir, technical dikkat aayi: {str(e)[:150]}")

# --- 8. FLASK ROUTES ---
@app.route("/", methods=["GET", "HEAD"])
def health_check():
    return "Vasuli Manager Bot is Alive and Active!", 200

@app.route("/telegram", methods=["POST"])
def telegram_webhook():
    data = request.get_json(silent=True)
    if data:
        update_id = data.get("update_id")
        if update_id and update_id not in processed_updates:
            processed_updates.add(update_id)
            if len(processed_updates) > 200:
                processed_updates.clear()
            threading.Thread(target=process_telegram_update, args=(data,), daemon=True).start()
    return "OK", 200

def register_webhook_delayed():
    time.sleep(3)
    try:
        webhook_target = f"{RENDER_URL}/telegram"
        res = requests.get(
            f"{TG_API}/setWebhook",
            params={"url": webhook_target, "drop_pending_updates": True},
            timeout=10
        )
        print(f"Webhook Setup Status: {res.json()}", flush=True)
    except Exception as e:
        print(f"Webhook setup error: {e}", flush=True)

if __name__ == "__main__":
    threading.Thread(target=register_webhook_delayed, daemon=True).start()
    port = int(os.environ.get("PORT", 10000))
    print(f"Starting Flask Server on port {port}...", flush=True)
    app.run(host="0.0.0.0", port=port)
