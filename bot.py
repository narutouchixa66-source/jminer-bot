import os
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

BOT_TOKEN = os.environ.get("BOT_TOKEN")
GAME_URL = "https://strong-lokum-1e6127.netlify.app/"

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [[
        InlineKeyboardButton(
            text="⛏️ Открыть JMiner",
            web_app=WebAppInfo(url=GAME_URL)
        )
    ]]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await update.message.reply_text(
        "👋 Привет! Добро пожаловать в *JMiner* — симулятор майнинга!\n\n"
        "⛏️ Собирай майнинг-риг\n"
        "💰 Зарабатывай монеты\n"
        "🚀 Выводи в TON\n\n"
        "Нажми кнопку ниже чтобы начать:",
        parse_mode="Markdown",
        reply_markup=reply_markup
    )

if __name__ == "__main__":
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    print("Бот запущен...")
    app.run_polling()
