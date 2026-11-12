import io
import os
import re
import requests
import pandas as pd
import edge_tts
from groq import Groq
from telegram import Update
from telegram.ext import Application, MessageHandler, filters, ContextTypes

# --- 1. CREDENTIALS & LINKS ---
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "8851118107:AAG-1M6vMIimiF_5qCh88ZHJsSQboDjER4k")
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "YAHAN_APNA_GROQ_API_KEY_DALEIN")
GOOGLE_SHEET_URL = os.environ.get(
    "GOOGLE_SHEET_URL",
    "https://docs.google.com/spreadsheets/d/1hMjxdXH8wCiCH6WU8VWlYhbXmxhYbr-THyGatXjzxQk/edit?usp=sharing"
)
# Render apna URL khud RENDER_EXTERNAL_URL mein deta hai
RENDER_URL = os.environ.get("RENDER_EXTERNAL_URL", "https://vasuli-voice-bot.onrender.com")

groq_client = Groq(api_key=GROQ_API_KEY)
uploaded_sheets = None
chat_memory = []

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

    try:
        xlsx_url = "https://docs.google.com/spreadsheets/d/" + sheet_id + "/export?format=xlsx"
        res = requests.get(xlsx_url, timeout=15)
        if res.status_code == 200:
            sheets_dict = pd.read_excel(io.BytesIO(res.content), sheet_name=None)
            return sheets_dict, "Live Google Sheet"
    except Exception as e:
        print(f"XLSX fetch fallback: {e}")

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

# --- 3. 'basuli' SHEET KA FULL ANALYSIS ENGINE (Sare Bills ke sath) ---
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

        work_df['Salesman_Tab'] = tab_name
        all_dfs.append(work_df)

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
        summary_parts.append(f"\n{tab_name.upper()} KI SARI PARTIES KA TOTAL HISAB (Bills Jodkar):")
        summary_parts.append(party_grouped.to_string(index=False))

        # Har party ke individual bills bhi dena taaki 8 ke 8 bills poore bata sake
        cols_to_show = [c for c in [date_col, ref_col, party_col, amt_col, overdue_col] if c]
        summary_parts.append(f"\n{tab_name.upper()} KE SARE INDIVIDUAL BILLS KI LIST:")
        summary_parts.append(work_df[cols_to_show].to_string(index=False))

    return "\n".join(summary_parts)

# --- 4. FAST & PROFESSIONAL MANAGER AUDIO ---
async def text_to_voice(text, filename="reply.mp3"):
    clean_text = re.sub(r'<think>.*?</think>', '', str(text), flags=re.DOTALL)
    clean_text = re.sub(r'[*#`_~>-]', '', clean_text).strip()
    if not clean_text:
        clean_text = "Sir, abhi data nahi mil pa raha hai."

    # rate="+18%" = Fast, crisp professional speaking speed
    # pitch="-4Hz" = Clear, authoritative Senior Manager voice
    communicate = edge_tts.Communicate(
        clean_text,
        voice="hi-IN-MadhurNeural",
        rate="+18%",
        pitch="-4Hz"
    )
    await communicate.save(filename)
    return filename, clean_text

# --- 5. GROQ AI CALL (max_tokens=1000 taaki 8 ke 8 bills poore aayein) ---
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
                temperature=0.3,
                max_tokens=1000  # Poore 8-10 bills bolne ke liye limit badha di gayi hai
            )
            reply = completion.choices[0].message.content
            if reply:
                reply = re.sub(r'<think>.*?</think>', '', reply, flags=re.DOTALL).strip()
            if reply and len(reply) > 3:
                chat_memory.append({"role": "user", "content": user_text})
                chat_memory.append({"role": "assistant", "content": reply})
                return reply
        except Exception:
            continue

    return "Bhai, abhi server se jawab nahi aa paya, ek baar dubara bolna."

# --- 6. EXCEL UPLOAD HANDLER ---
async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global uploaded_sheets
    doc = update.message.document
    file_name = doc.file_name.lower()

    if not (file_name.endswith('.xlsx') or file_name.endswith('.xls') or file_name.endswith('.csv')):
        await update.message.reply_text("Bhai sirf Excel (.xlsx) ya CSV file hi bhejo.")
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
        prompt = "User ne abhi Excel file bheji hai. Ekdum aam bolchal ki desi Hindi mein 3 line mein batao ki file lag gayi hai aur kul kitna paisa baki hai."
        ai_reply = ask_groq(f"Sheet Data:\n{sheet_summary}", prompt)

        voice_note, clean_reply = await text_to_voice(ai_reply, "sheet_loaded.mp3")
        with open(voice_note, "rb") as audio:
            await update.message.reply_voice(voice=audio, caption=f"✅ File Load Ho Gayi!\n\n{clean_reply}")
    except Exception as e:
        await update.message.reply_text(f"File padhne mein dikkat aayi: {e}")

# --- 7. VOICE & TEXT MESSAGE HANDLER ---
async def process_query(update: Update, user_text: str):
    print(f"Aapne Kaha: {user_text}")
    sheet_analysis = analyze_basuli_sheet(user_text)

    if sheet_analysis == "NO_SHEET_LOADED":
        msg = "Sir, Google Sheet load nahi ho payi hai. Ek baar link check kar lein ya Excel file bhej dein."
        voice_note, clean_reply = await text_to_voice(msg, "err.mp3")
        with open(voice_note, "rb") as audio:
            await update.message.reply_voice(voice=audio, caption=clean_reply)
        return

    system_prompt = f"""
    Tum ek strict, to-the-point aur Professional Senior Vasuli Manager ho.
    Salesmen ke naam: Tarun (Local market) aur Ajay (Upcountry market).

    NICHE GOOGLE SHEET KA LIVE DATA HAI:
    {sheet_analysis}

    SAKHT NIYAM:
    1. 'Bhai', 'Yaar', ya 'Bade bhai' jaise shabd Kisi bhi haal mein mat bolna. Boss se baat karte waqt 'Sir' bolna, aur Salesman (Tarun/Ajay) ko seedha unke naam se address karna.
    2. Tone ekdum professional, vyaparik aur crisp rakhna (Jaise: 'Sir, Sitaram Rathi ke 8 bills mein kul 1 lakh 401 rupaye baki hain').
    3. Party ka naam bolte waqt aage laga 'PARLE' ya 'ZB' hata kar seedha dukan ka naam bolna.
    4. Agar bill-wise detail mangi jaye, toh bina ek bhi bill chhode saare bills ki tarikh, bill number aur rupaye fatafat point-to-point ginwana.
    5. Agar Boss bolein ki Tarun ya Ajay ke liye voice note do, toh ekdum kadak Manager ki tarah seedha aadesh dena (Jaise: 'Tarun, aaj Gadarwara market mein in 5 partiyon ka payment har haal mein clear karwana hai, dhyan rahe').
    6. Koi star (**) ya markdown mat lagana.
    """

    ai_reply = ask_groq(system_prompt, user_text)
    print(f"AI Jawab: {ai_reply}")

    voice_note, clean_reply = await text_to_voice(ai_reply, "vasuli_reply.mp3")
    with open(voice_note, "rb") as audio:
        await update.message.reply_voice(voice=audio, caption=f"📋 Manager Report:\n{clean_reply}")

async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    voice_file = await update.message.voice.get_file()
    audio_path = "user_voice.ogg"
    await voice_file.download_to_drive(audio_path)

    with open(audio_path, "rb") as file:
        transcription = groq_client.audio.transcriptions.create(
            file=(audio_path, file.read()),
            model="whisper-large-v3-turbo",
            language="hi",
            prompt="Tarun aur Ajay ki total udhaari kitni hai. Sitaram Rathi ke sare bill batao. Gurukrapa Traders, Ravindra Kumar, Brajmohan Kirana, Namo Kirana, Gadarwara."
        )
    await process_query(update, transcription.text)

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message and update.message.text:
        await process_query(update, update.message.text)

# --- 8. WEBHOOK SERVER (Message bhejte hi Render ko jagane ke liye) ---
def main():
    port = int(os.environ.get("PORT", 10000))
    app = Application.builder().token(TELEGRAM_TOKEN).build()

    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    app.add_handler(MessageHandler(filters.VOICE, handle_voice))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    # Agar Render par chal raha hai toh Webhook chalu karega (Jo sleep se khud jaga dega)
    if os.environ.get("RENDER") or os.environ.get("RENDER_EXTERNAL_URL"):
        clean_url = RENDER_URL.rstrip("/")
        print(f"Starting Webhook on {clean_url} (Port {port})...")
        app.run_webhook(
            listen="0.0.0.0",
            port=port,
            url_path="telegram",
            webhook_url=f"{clean_url}/telegram",
            drop_pending_updates=True
        )
    else:
        print("Local PC Mode Chalu Hai!")
        app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
