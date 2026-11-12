import io
import os
import re
import asyncio
import threading
import requests
import pandas as pd
import edge_tts
from http.server import HTTPServer, BaseHTTPRequestHandler
from groq import Groq
from telegram import Update
from telegram.ext import Application, MessageHandler, filters, ContextTypes

# --- 1. CREDENTIALS & LINKS (Cloud Environment + Local Fallback) ---
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "8851118107:AAG-1M6vMIimiF_5qCh88ZHJsSQboDjER4k")
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "YAHAN_APNA_GROQ_API_KEY_DALEIN")
GOOGLE_SHEET_URL = os.environ.get(
    "GOOGLE_SHEET_URL",
    "YAHAN_APNA_GOOGLE_SHEET_LINK_PASTE_KAREIN"
)

groq_client = Groq(api_key=GROQ_API_KEY)
uploaded_sheets = None
chat_memory = []

# Link se clean Sheet ID nikalne ka function
def extract_sheet_id(url_or_id):
    clean = str(url_or_id).strip().replace('[', '').replace(']', '')
    if "(" in clean and ")" in clean:
        clean = clean.split("(")[1].split(")")[0]
    if "/d/" in clean:
        return clean.split("/d/")[1].split("/")[0]
    return clean

# --- 2. GOOGLE SHEET KE SARE TABS (tarun, ajay) LOAD KARNA ---
def load_all_tabs():
    global uploaded_sheets
    if uploaded_sheets is not None:
        return uploaded_sheets, "Uploaded Excel File"

    sheet_id = extract_sheet_id(GOOGLE_SHEET_URL)
    if not sheet_id or "YAHAN" in sheet_id:
        return None, "Koi Sheet link nahi mila"

    # Method A: Poori workbook (.xlsx) fetch karna taaki 'tarun' aur 'ajay' dono tabs mil jayein
    try:
        xlsx_url = "https://docs.google.com/spreadsheets/d/" + sheet_id + "/export?format=xlsx"
        res = requests.get(xlsx_url, timeout=15)
        if res.status_code == 200:
            sheets_dict = pd.read_excel(io.BytesIO(res.content), sheet_name=None)
            return sheets_dict, "Live Google Sheet"
    except Exception as e:
        print(f"XLSX fetch fallback: {e}")

    # Method B: Fallback (Har tab ko naam se fetch karna)
    sheets_dict = {}
    for tab in ['tarun', 'ajay']:
        try:
            csv_url = "https://docs.google.com/spreadsheets/d/" + sheet_id + "/gviz/tq?tqx=out:csv&sheet=" + tab
            res = requests.get(csv_url, timeout=10)
            if res.status_code == 200 and len(res.text) > 20:
                sheets_dict[tab] = pd.read_csv(io.StringIO(res.text))
        except Exception:
            pass

    if sheets_dict:
        return sheets_dict, "Live Google Sheet"
    return None, "Sheet load nahi ho payi"

# --- 3. 'basuli' SHEET KA FULL ANALYSIS ENGINE ---
def analyze_basuli_sheet(user_question=""):
    sheets_dict, source_name = load_all_tabs()
    if not sheets_dict:
        return "NO_SHEET_LOADED"

    summary_parts = [f"SOURCE: {source_name}"]
    all_dfs = []

    for tab_name, df in sheets_dict.items():
        if df is None or df.empty:
            continue

        df.columns = [str(c).strip() for c in df.columns]
        cols = df.columns.tolist()

        party_col = next((c for c in cols if any(k in c.lower() for k in ['party', 'name', 'outlet', 'shop', 'ledger'])), None)
        amt_col = next((c for c in cols if any(k in c.lower() for k in ['pending', 'amount', 'balance', 'due', 'vasuli'])), None)
        overdue_col = next((c for c in cols if 'overdue' in c.lower()), None)
        ref_col = next((c for c in cols if 'ref' in c.lower()), None)

        if not party_col or not amt_col:
            continue

        work_df = df.copy()
        work_df[party_col] = work_df[party_col].astype(str).str.strip()

        # Bottom 'Total' row aur blank rows hatana (Double counting rokne ke liye)
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

        work_df['Salesman_Tab'] = tab_name
        all_dfs.append(work_df)

        # Ek hi Party ke multiple bills ko jodkar Party-Wise Total nikalna
        party_grouped = work_df.groupby(party_col).agg(
            Bills_Count=(amt_col, 'count'),
            Total_Pending=(amt_col, 'sum'),
            Max_Overdue_Days=(overdue_col, 'max')
        ).reset_index().sort_values(by='Total_Pending', ascending=False)

        tab_total = work_df[amt_col].sum()
        summary_parts.append(f"\n==========================================")
        summary_parts.append(f"SALESMAN: {tab_name.upper()}")
        summary_parts.append(f"- {tab_name.upper()} KUL PENDING AMOUNT: Rs {tab_total:,.0f}")
        summary_parts.append(f"- Kul Pending Bills: {len(work_df)} | Kul Unique Parties: {len(party_grouped)}")
        summary_parts.append(f"\n{tab_name.upper()} KI SARI PARTIES KA TOTAL HISAB (Bills Jodkar):")
        summary_parts.append(party_grouped.to_string(index=False))

        # Top 5 Sabse Purane Overdue Bills
        old_bills = work_df.sort_values(by=overdue_col, ascending=False).head(5)
        if not old_bills.empty:
            cols_to_show = [c for c in [party_col, ref_col, amt_col, overdue_col] if c]
            summary_parts.append(f"\n{tab_name.upper()} KE TOP 5 SABSE PURANE OVERDUE BILLS:")
            summary_parts.append(old_bills[cols_to_show].to_string(index=False))

    # Agar user ne kisi specific party ke baare mein poocha ho toh uske individual bills dikhana
    if all_dfs and user_question:
        full_df = pd.concat(all_dfs, ignore_index=True)
        ignore_w = {'tarun', 'ajay', 'top', 'party', 'paise', 'aaj', 'bolo', 'voice', 'note', 'kya', 'hai', 'kitna', 'batao', 'total', 'udhaari', 'vasuli', 'pending'}
        keywords = [w for w in user_question.lower().split() if len(w) > 2 and w not in ignore_w]
        matched = pd.DataFrame()
        for kw in keywords:
            m = full_df[full_df.astype(str).apply(lambda r: r.str.contains(kw, case=False, na=False)).any(axis=1)]
            matched = pd.concat([matched, m]).drop_duplicates()
        if not matched.empty:
            summary_parts.append(f"\nSAWAL SE MATCH HONE WALE INDIVIDUAL BILLS:\n{matched.head(15).to_string(index=False)}")

    return "\n".join(summary_parts)

# --- 4. MALE SENIOR MANAGER HINDI AUDIO GENERATOR ---
async def text_to_voice(text, filename="reply.mp3"):
    clean_text = re.sub(r'<think>.*?</think>', '', str(text), flags=re.DOTALL)
    clean_text = re.sub(r'[*#`_~>-]', '', clean_text).strip()
    if not clean_text:
        clean_text = "Maaf kijiyega, mujhe data nahi mila."

    # hi-IN-MadhurNeural = Male Manager Voice (Heavy pitch & crisp speed)
    communicate = edge_tts.Communicate(
        clean_text,
        voice="hi-IN-MadhurNeural",
        rate="+8%",
        pitch="-6Hz"
    )
    await communicate.save(filename)
    return filename, clean_text

# --- 5. GROQ AI CALL ('allam' blocked + Conversation Memory) ---
def ask_groq(system_prompt, user_text):
    global chat_memory
    try:
        all_models = [m.id for m in groq_client.models.list().data]
        bad_keywords = ["allam", "whisper", "orpheus", "guard", "tts", "vision", "playai", "distil", "safeguard"]
        valid_models = [
            m for m in all_models
            if not any(bad in m.lower() for bad in bad_keywords)
        ]
        priority = ["llama-3.3-70b-versatile", "llama-3.1-8b-instant"]
        llama_m = [m for m in valid_models if m.lower().startswith("llama") or "llama" in m.lower()]
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
                print(f"[Working Model: {m}]")
                chat_memory.append({"role": "user", "content": user_text})
                chat_memory.append({"role": "assistant", "content": reply})
                return reply
        except Exception as e:
            print(f"[Model {m} skipped: {e}]")
            continue

    return "Sir, abhi AI server se response nahi aa paya, kripya ek baar dubara bolein."

# --- 6. TELEGRAM PAR DIRECT EXCEL (.xlsx / .csv) UPLOAD HANDLER ---
async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global uploaded_sheets
    doc = update.message.document
    file_name = doc.file_name.lower()

    if not (file_name.endswith('.xlsx') or file_name.endswith('.xls') or file_name.endswith('.csv')):
        await update.message.reply_text("Kripya sirf Excel (.xlsx) ya CSV (.csv) file hi bhejein.")
        return

    file = await doc.get_file()
    local_path = f"uploaded_{file_name}"
    await file.download_to_drive(local_path)

    try:
        if file_name.endswith('.csv'):
            uploaded_sheets = {"Sheet1": pd.read_csv(local_path)}
        else:
            uploaded_sheets = pd.read_excel(local_path, sheet_name=None)

        sheet_summary = analyze_basuli_sheet()
        prompt = "User ne abhi nayi Excel file upload ki hai. Ek strict Senior Vasuli Manager ki tarah 3 line mein Hindi mein total pending amount aur top parties ki summary sunao."
        ai_reply = ask_groq(f"Sheet Data:\n{sheet_summary}", prompt)

        voice_note, clean_reply = await text_to_voice(ai_reply, "sheet_loaded.mp3")
        with open(voice_note, "rb") as audio:
            await update.message.reply_voice(
                voice=audio,
                caption=f"✅ Excel File Load Ho Gayi!\n\n{clean_reply}"
            )
    except Exception as e:
        await update.message.reply_text(f"File padhne mein error aaya: {e}")

# --- 7. VOICE MESSAGE HANDLER ---
async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    voice_file = await update.message.voice.get_file()
    audio_path = "user_voice.ogg"
    await voice_file.download_to_drive(audio_path)

    with open(audio_path, "rb") as file:
        transcription = groq_client.audio.transcriptions.create(
            file=(audio_path, file.read()),
            model="whisper-large-v3-turbo",
            language="hi",
            prompt="Tarun aur Ajay ki total udhaari kitni hai. Unse kitna paisa lena hai. Sitaram Rathi, Gurukrapa Traders, Ravindra Kumar, Brajmohan Kirana, Namo Kirana, Gadarwara."
        )
    user_text = transcription.text
    print(f"Aapne Kaha: {user_text}")

    sheet_analysis = analyze_basuli_sheet(user_text)

    if sheet_analysis == "NO_SHEET_LOADED":
        msg = "Sir, Google Sheet load nahi ho payi. Kripya GOOGLE_SHEET_URL check karein ya Telegram par seedha Excel file bhej dein."
        voice_note, clean_reply = await text_to_voice(msg, "err.mp3")
        with open(voice_note, "rb") as audio:
            await update.message.reply_voice(voice=audio, caption=clean_reply)
        return

    system_prompt = f"""
    Tum Gadarwara ke business boss ke Senior Vasuli Manager AI Assistant ho. Tumhari aawaz aur bolne ka andaz ekdum kadak, professional aur to-the-point Manager jaisa hai.
    Salesmen ke naam: Tarun (Local market) aur Ajay (Upcountry market).

    NICHE GOOGLE SHEET KA LIVE DATA HAI:
    {sheet_analysis}

    SAKHT NIYAM:
    1. Party ka naam bolte waqt aage laga "PARLE " ya "ZB " mat bolna, seedha dukan ka naam bolna (Jaise "Sitaram Rathi Gadarwara", "Shri Gurukrapa Traders").
    2. Jab kisi party ke baare mein poocha jaye, toh uske kul pending bills aur total rupaye dono ek sath batana (Jaise: "Sir, Sitaram Rathi Gadarwara ke kul 8 bills pending hain aur unse total 1,00,401 rupaye vasul karne hain").
    3. Agar Boss bole ki "Tarun ko bolo..." ya "Tarun ke liye voice note do...", toh seedha Tarun ko address karte hue kadak Manager tone mein aadesh likho (Jaise: "Tarun, dhyan do! Aaj Gadarwara market se in 5 partiyon ka payment har haal mein vasul hona chahiye...") taaki Boss woh voice note seedha Tarun ko forward kar sakein.
    4. Jawab chhota (2-4 lines), saaf bolchal ki Hindi mein rakho aur koi star (**) ya markdown mat lagao.
    """

    ai_reply = ask_groq(system_prompt, user_text)
    print(f"AI Jawab: {ai_reply}")

    voice_note, clean_reply = await text_to_voice(ai_reply, "vasuli_reply.mp3")
    with open(voice_note, "rb") as audio:
        await update.message.reply_voice(
            voice=audio,
            caption=f"📋 Manager Report:\n{clean_reply}"
        )

# --- 8. RENDER 24x7 KEEP-ALIVE WEB SERVER ---
class KeepAliveHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Vasuli Manager Voice Agent is Running 24x7!")

    def log_message(self, format, *args):
        return

def run_keep_alive():
    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(("0.0.0.0", port), KeepAliveHandler)
    server.serve_forever()

def main():
    # Cloud port background mein chalu rakhna
    threading.Thread(target=run_keep_alive, daemon=True).start()

    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    app.add_handler(MessageHandler(filters.VOICE, handle_voice))
    print("24x7 Senior Vasuli Manager Voice Agent Chalu Hai!")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
