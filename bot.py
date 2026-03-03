import os, json, base64
from telegram import Update
from telegram.ext import ApplicationBuilder, MessageHandler, filters, ContextTypes
import openai
import gspread
from google.oauth2.service_account import Credentials
from datetime import datetime
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters, ContextTypes

# Load environment variables
load_dotenv()

# --- Config ---
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
SHEET_ID = os.getenv("GOOGLE_SHEET_ID")

openai.api_key = OPENAI_API_KEY
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

    # Check header row
    existing_headers = sheet.row_values(1)

    if existing_headers != HEADERS:
        sheet.delete_rows(1)
        sheet.insert_row(HEADERS, 1)

    return sheet
# --- Parse expense from text using LLM ---
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
  "date": "YYYY-MM-DD"
}}

RULES:

1. Amount:
   - Extract the final payable amount.
   - Ignore quantities unless clearly total amount.

2. Date:
   - If words like "today", "yesterday", or a specific date are mentioned,
     use them ONLY to determine the date.
   - Do NOT include date words in the description.
   - If no date is mentioned, use: {datetime.now().strftime('%Y-%m-%d')}

3. Description:
   - Remove filler words like:
     "total", "amount", "paid", "spent", "today", "bill"
   - Keep only meaningful purchase details.
   - Make it short and natural.
   - Include useful quantity info (e.g., "Fuel 15 liters").
   - Do not repeat the amount in description.

4. Category Mapping:
   - Fuel, petrol, diesel → Transport
   - Uber, Ola, taxi, metro → Transport
   - Swiggy, Zomato, restaurant → Food
   - Grocery, supermarket → Food
   - Medicine, hospital → Health
   - Movie, Netflix → Entertainment
   - Default → Other

5. Currency:
   - Always use INR unless explicitly stated otherwise.

Return JSON only. No explanations.
"""
    }],
    response_format={"type": "json_object"}
)
    
    return json.loads(response.choices[0].message.content)

#define headers
HEADERS = [
    "Timestamp",
    "UserID",
    "Username",
    "Name",
    "Date",
    "Amount",
    "Currency",
    "Category",
    "Description"
]


# --- Parse receipt image using Vision ---
def parse_expense_image(image_bytes: bytes) -> dict:
    b64 = base64.b64encode(image_bytes).decode()
    response = openai.chat.completions.create(
        model="gpt-4o",
        messages=[{
            "role": "user",
            "content": [
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
                {"type": "text", "text": f"""Extract expense from this receipt. Return JSON only:
                {{"amount": number, "currency": "INR", "category": string, "description": string, "date": "YYYY-MM-DD"}}
                If date not found, use: {datetime.now().strftime('%Y-%m-%d')}"""}
            ]
        }],
        response_format={"type": "json_object"}
    )
    return json.loads(response.choices[0].message.content)


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
# --- Telegram Handlers ---
async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    print("Message received:", update.message.text)
    await update.message.reply_text("⏳ Processing...")

    try:
        data = parse_expense_text(update.message.text)
        print("Parsed data:", data)

        save_to_sheet(data, update.message.from_user)
        print("Saved to sheet")

        await update.message.reply_text("✅ Saved!")
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


#Telegram_bot_activate
from telegram.ext import CommandHandler

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Bot is active ✅")




# --- Main ---
app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
app.add_handler(CommandHandler("start", start))
app.run_polling()
