import os
import json
import base64
import requests
from datetime import datetime
from dotenv import load_dotenv

from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters, ContextTypes
import openai
import gspread
from google.oauth2.service_account import Credentials

# --- Load environment variables FIRST ---
load_dotenv()

# --- Config ---
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
SHEET_ID = os.getenv("GOOGLE_SHEET_ID")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_KEY")

# --- Validate required env vars ---
required = ["TELEGRAM_BOT_TOKEN", "OPENAI_API_KEY", "GOOGLE_SHEET_ID", "GOOGLE_CREDS_JSON"]
for var in required:
    if not os.getenv(var):
        raise EnvironmentError(f"Missing required env var: {var}")

openai.api_key = OPENAI_API_KEY

# --- Headers ---
HEADERS = [
    "Timestamp", "UserID", "Username", "Name",
    "Date", "Amount", "Currency", "Category", "Description"
]

# --- Google Sheets Setup ---
def get_sheet():
    creds_json = os.environ.get("GOOGLE_CREDS_JSON")
    if not creds_json:
        raise Exception("GOOGLE_CREDS_JSON environment variable not found")
    creds_dict = json.loads(creds_json)
    creds = Credentials.from_service_account_info(
        creds_dict,
        scopes=[
            "https://spreadsheets.google.com/feeds",
            "https://www.googleapis.com/auth/drive"
        ]
    )
    client = gspread.authorize(creds)
    sheet = client.open_by_key(SHEET_ID).worksheet("Expenses")
    existing_headers = sheet.row_values(1)
    if existing_headers != HEADERS:
        sheet.delete_rows(1)
        sheet.insert_row(HEADERS, 1)
    return sheet

# --- Save to Supabase ---
def save_to_supabase(data: dict, user):
    if not SUPABASE_URL or not SUPABASE_KEY:
        print("Supabase not configured, skipping")
        return
    try:
        headers = {
            "apikey": SUPABASE_KEY,
            "Authorization": f"Bearer {SUPABASE_KEY}",
            "Content-Type": "application/json",
            "Prefer": "return=minimal"
        }
        payload = {
            "date": data.get("date"),
            "amount": data.get("amount"),
            "currency": data.get("currency", "INR"),
            "category": data.get("category"),
            "description": data.get("description"),
            "vendor": data.get("vendor", ""),
            "source": "telegram",
            "added_by_name": user.first_name,
            "added_by_id": str(user.id),
        }
        response = requests.post(
            f"{SUPABASE_URL}/rest/v1/expenses",
            headers=headers,
            json=payload
        )
        if response.status_code == 201:
            print("Saved to Supabase successfully")
        else:
            print(f"Supabase error: {response.status_code} - {response.text}")
    except Exception as e:
        print(f"Supabase save failed: {e}")

# --- Save to Google Sheets ---
def save_to_sheet(data: dict, user):
    sheet = get_sheet()
    row_data = {
        "Timestamp": datetime.now().isoformat(),
        "UserID": user.id,
        "Username": user.username,
        "Name": user.first_name,
        "Date": data.get("date"),
        "Amount": data.get("amount"),
        "Currency": data.get("currency", "INR"),
        "Category": data.get("category"),
        "Description": data.get("description"),
    }
    row = [row_data.get(col, "") for col in HEADERS]
    sheet.append_row(row)
    print("Saved to Google Sheets")
    print(f"Calling Supabase with URL: {SUPABASE_URL}")
    print(f"Supabase key set: {bool(SUPABASE_KEY)}")
    save_to_supabase(data, user)
    print("Supabase call completed")

# --- Parse expense from text ---
def parse_expense_text(text: str) -> dict:
    response = openai.chat.completions.create(
        model="gpt-4o-mini",
        messages=[{
            "role": "user",
            "content": f"""
You are an intelligent expense parser.

TASK:
Extract structured expense details from the message.

Message:
"{text}"

OUTPUT FORMAT:
Return JSON only:
{{
  "amount": number,
  "currency": "INR",
  "category": string,
  "description": string,
  "vendor": string,
  "date": "YYYY-MM-DD"
}}

RULES:

1. Amount: Extract the final payable amount.

2. Date:
   - Use "today", "yesterday", or specific date if mentioned.
   - If no date mentioned, use: {datetime.now().strftime('%Y-%m-%d')}

3. Description:
   - Remove filler words like "total", "amount", "paid", "spent", "today", "bill"
   - Keep only meaningful purchase details.
   - Include useful quantity info (e.g., "Fuel 15 liters").

4. Vendor:
   - Extract shop, app, or service name if mentioned.
   - Examples: Swiggy, Zomato, DMart, BPCL, Uber
   - If not mentioned, return empty string.

5. Category Mapping:
   - Fuel, petrol, diesel → Transport
   - Uber, Ola, taxi, metro → Transport
   - Swiggy, Zomato, restaurant → Food
   - Grocery, supermarket, DMart → Food
   - Medicine, hospital, pharmacy → Health
   - Movie, Netflix → Entertainment
   - Default → Other

6. Currency: Always INR unless stated otherwise.

Return JSON only. No explanations.
"""
        }],
        response_format={"type": "json_object"}
    )
    return json.loads(response.choices[0].message.content)

# --- Parse receipt image ---
def parse_expense_image(image_bytes: bytes) -> dict:
    b64 = base64.b64encode(image_bytes).decode()
    response = openai.chat.completions.create(
        model="gpt-4o",
        messages=[{
            "role": "user",
            "content": [
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
                {"type": "text", "text": f"""Extract expense from this receipt. Return JSON only:
                {{"amount": number, "currency": "INR", "category": string, "vendor": string, "description": string, "date": "YYYY-MM-DD"}}
                If date not found, use: {datetime.now().strftime('%Y-%m-%d')}
                If vendor not found, return empty string."""}
            ]
        }],
        response_format={"type": "json_object"}
    )
    return json.loads(response.choices[0].message.content)

# --- Telegram Handlers ---
async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    print("Message received:", update.message.text)
    await update.message.reply_text("⏳ Processing...")
    try:
        data = parse_expense_text(update.message.text)
        print("Parsed data:", data)
        save_to_sheet(data, update.message.from_user)
        await update.message.reply_text(
            f"✅ Saved!\n💰 ₹{data['amount']} | {data['category']}\n📝 {data['description']}\n📅 {data['date']}"
        )
    except Exception as e:
        print("ERROR:", e)
        await update.message.reply_text(f"❌ Error: {e}")

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("📸 Reading receipt...")
    try:
        photo = await update.message.photo[-1].get_file()
        image_bytes = await photo.download_as_bytearray()
        data = parse_expense_image(bytes(image_bytes))
        save_to_sheet(data, update.message.from_user)
        await update.message.reply_text(
            f"✅ Receipt saved!\n💰 ₹{data['amount']} | {data['category']}\n📝 {data['description']}\n📅 {data['date']}"
        )
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {str(e)}")

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Bot is active ✅")

# --- Main ---
app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
app.add_handler(CommandHandler("start", start))

PORT = int(os.getenv("PORT", 8080))
app.run_webhook(
    listen="0.0.0.0",
    port=PORT,
    webhook_url=os.getenv("WEBHOOK_URL")
)
