from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes
import gspread
from oauth2client.service_account import ServiceAccountCredentials

# Google Sheets подключение
scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
creds = ServiceAccountCredentials.from_json_keyfile_name("credentials.json", scope)
client = gspread.authorize(creds)
sheet = client.open("https://docs.google.com/spreadsheets/d/10RJvgX8t9qWQH3zIaCvp13uDHLQzfIB5ttQKT2kzCik").sheet1

# /start команда
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Привет! Я бот по аренде машин 🚗")

# запуск бота
app = ApplicationBuilder().token("ТВОЙ_ТОКЕН_ОТ_BOTFATHER").build()
app.add_handler(CommandHandler("start", start))
app.run_polling()
