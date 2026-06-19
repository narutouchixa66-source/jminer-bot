import os
import json
import logging
import threading
import hashlib
import hmac
from urllib.parse import parse_qsl
from datetime import datetime

from flask import Flask, request, jsonify
from flask_cors import CORS

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo
from telegram.ext import ApplicationBuilder, CommandHandler, CallbackQueryHandler, ContextTypes

import gspread
from google.oauth2.service_account import Credentials as GCredentials

import firebase_admin
from firebase_admin import credentials as fb_credentials, firestore

logging.basicConfig(level=logging.INFO)

# ---------------- CONFIG ----------------
BOT_TOKEN = os.environ.get("BOT_TOKEN")
SHEET_ID = os.environ.get("SHEET_ID")
ADMIN_ID = 7175060469
GAME_URL = "https://strong-lokum-1e6127.netlify.app/"
BOT_USERNAME = "ProjectNBot"

# ---------------- FIREBASE INIT ----------------
fb_creds_json = os.environ.get("FIREBASE_CREDENTIALS")
fb_creds_dict = json.loads(fb_creds_json)
cred = fb_credentials.Certificate(fb_creds_dict)
firebase_admin.initialize_app(cred)
db = firestore.client()

# ---------------- GOOGLE SHEETS (users / referrals / bans) ----------------
def get_sheet():
    creds_json = os.environ.get("GOOGLE_CREDENTIALS")
    creds_dict = json.loads(creds_json)
    scopes = ["https://www.googleapis.com/auth/spreadsheets"]
    creds = GCredentials.from_service_account_info(creds_dict, scopes=scopes)
    client = gspread.authorize(creds)
    return client.open_by_key(SHEET_ID).sheet1

def get_all_users():
    return get_sheet().get_all_records()

def add_user(user_id, username, first_name, referred_by=None):
    sheet = get_sheet()
    records = sheet.get_all_records()
    ids = [str(r["user_id"]) for r in records]
    if str(user_id) not in ids:
        sheet.append_row([
            str(user_id), username or "", first_name or "",
            datetime.now().strftime("%Y-%m-%d %H:%M"), "no",
            str(referred_by) if referred_by else ""
        ])

def is_banned(user_id):
    records = get_sheet().get_all_records()
    for r in records:
        if str(r["user_id"]) == str(user_id):
            return str(r["banned"]).lower() == "yes"
    return False

def set_ban(user_id, banned: bool):
    sheet = get_sheet()
    records = sheet.get_all_records()
    for i, r in enumerate(records):
        if str(r["user_id"]) == str(user_id):
            sheet.update_cell(i + 2, 5, "yes" if banned else "no")
            return True
    return False

# ---------------- TELEGRAM INITDATA VERIFICATION ----------------
def verify_init_data(init_data: str):
    """Проверяет что запрос реально пришёл из Telegram, а не подделан."""
    try:
        parsed = dict(parse_qsl(init_data, strict_parsing=True))
        received_hash = parsed.pop('hash', None)
        if not received_hash:
            return None
        data_check_string = '\n'.join(f"{k}={v}" for k, v in sorted(parsed.items()))
        secret_key = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
        calculated_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
        if calculated_hash != received_hash:
            return None
        return parsed
    except Exception:
        return None

def get_user_from_init(parsed):
    return json.loads(parsed['user'])

# ---------------- FIRESTORE BALANCE HELPERS ----------------
def get_balance_doc(user_id):
    return db.collection('balances').document(str(user_id))

# ---------------- FLASK API (для игры) ----------------
flask_app = Flask(__name__)
CORS(flask_app)

@flask_app.route('/api/state', methods=['GET'])
def api_get_state():
    init_data = request.args.get('initData', '')
    parsed = verify_init_data(init_data)
    if not parsed:
        return jsonify({'error': 'invalid_auth'}), 403
    user = get_user_from_init(parsed)
    uid = str(user['id'])
    if is_banned(uid):
        return jsonify({'error': 'banned'}), 403
    doc = get_balance_doc(uid).get()
    return jsonify(doc.to_dict() if doc.exists else {})

@flask_app.route('/api/state', methods=['POST'])
def api_save_state():
    body = request.get_json(force=True, silent=True) or {}
    init_data = body.get('initData', '')
    parsed = verify_init_data(init_data)
    if not parsed:
        return jsonify({'error': 'invalid_auth'}), 403
    user = get_user_from_init(parsed)
    uid = str(user['id'])
    if is_banned(uid):
        return jsonify({'error': 'banned'}), 403
    state = body.get('state')
    if not isinstance(state, dict):
        return jsonify({'error': 'bad_state'}), 400
    state['_updated'] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    state['_username'] = user.get('username', '')
    state['_first_name'] = user.get('first_name', '')
    get_balance_doc(uid).set(state, merge=True)
    return jsonify({'ok': True})

@flask_app.route('/')
def home():
    return "JMiner backend is running."

# ---------------- TELEGRAM BOT HANDLERS ----------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if is_banned(user.id):
        await update.message.reply_text("🚫 Вы заблокированы и не можете использовать этого бота.")
        return

    referred_by = None
    if context.args and context.args[0].startswith("ref_"):
        referred_by = context.args[0].replace("ref_", "")

    add_user(user.id, user.username, user.first_name, referred_by)

    ref_link = f"https://t.me/{BOT_USERNAME}?start=ref_{user.id}"
    game_url_with_ref = f"{GAME_URL}?ref={user.id}"

    keyboard = [[InlineKeyboardButton("⛏️ Начать зарабатывать", web_app=WebAppInfo(url=game_url_with_ref))]]
    await update.message.reply_text(
        "👋 Привет! Добро пожаловать в *JMiner*!\n\n"
        "⛏️ Собирай майнинг-риг\n"
        "💰 Зарабатывай монеты\n"
        "🚀 Выводи в TON\n\n"
        f"🔗 Твоя реферальная ссылка:\n`{ref_link}`\n\n"
        "Нажми кнопку ниже чтобы начать:",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    keyboard = [
        [InlineKeyboardButton("👥 Все игроки", callback_data="admin_users")],
        [InlineKeyboardButton("🚫 Забанить", callback_data="admin_ban")],
        [InlineKeyboardButton("✅ Разбанить", callback_data="admin_unban")],
        [InlineKeyboardButton("📢 Рассылка", callback_data="admin_broadcast")],
    ]
    await update.message.reply_text("👑 Админ панель:", reply_markup=InlineKeyboardMarkup(keyboard))

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.from_user.id != ADMIN_ID:
        return
    if query.data == "admin_users":
        users = get_all_users()
        text = f"👥 Всего игроков: {len(users)}\n\n"
        for u in users[:30]:
            banned = "🚫" if str(u["banned"]).lower() == "yes" else "✅"
            text += f"{banned} {u['first_name']} (@{u['username']}) | ID: {u['user_id']}\n"
        await query.edit_message_text(text)
    elif query.data == "admin_ban":
        await query.edit_message_text("🚫 Напиши:\n/ban ID_игрока")
    elif query.data == "admin_unban":
        await query.edit_message_text("✅ Напиши:\n/unban ID_игрока")
    elif query.data == "admin_broadcast":
        await query.edit_message_text("📢 Напиши:\n/broadcast Текст сообщения")

async def ban(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    if not context.args:
        await update.message.reply_text("Напиши: /ban ID_игрока")
        return
    if set_ban(context.args[0], True):
        await update.message.reply_text(f"🚫 Игрок {context.args[0]} забанен!")
    else:
        await update.message.reply_text("❌ Игрок не найден!")

async def unban(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    if not context.args:
        await update.message.reply_text("Напиши: /unban ID_игрока")
        return
    if set_ban(context.args[0], False):
        await update.message.reply_text(f"✅ Игрок {context.args[0]} разбанен!")
    else:
        await update.message.reply_text("❌ Игрок не найден!")

async def broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    if not context.args:
        await update.message.reply_text("Напиши: /broadcast Текст сообщения")
        return
    text = " ".join(context.args)
    users = get_all_users()
    sent = 0
    keyboard = [[InlineKeyboardButton("⛏️ Начать зарабатывать", web_app=WebAppInfo(url=GAME_URL))]]
    for u in users:
        if str(u["banned"]).lower() != "yes":
            try:
                await context.bot.send_message(
                    chat_id=int(u["user_id"]), text=text,
                    reply_markup=InlineKeyboardMarkup(keyboard)
                )
                sent += 1
            except Exception:
                pass
    await update.message.reply_text(f"📢 Отправлено {sent} игрокам!")

async def setfield(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Админ-команда для теста: /setfield ID поле значение
    Пример: /setfield 123456789 ton 1.5"""
    if update.effective_user.id != ADMIN_ID:
        return
    if len(context.args) < 3:
        await update.message.reply_text("Напиши: /setfield ID поле значение\nПример: /setfield 123456789 ton 1.5")
        return
    uid, field, value = context.args[0], context.args[1], context.args[2]
    try:
        value_parsed = float(value) if '.' in value else int(value)
    except ValueError:
        value_parsed = value
    get_balance_doc(uid).set({field: value_parsed}, merge=True)
    await update.message.reply_text(f"✅ Игроку {uid} установлено {field} = {value_parsed}")

# ---------------- RUN ----------------
def run_flask():
    port = int(os.environ.get("PORT", 8080))
    flask_app.run(host="0.0.0.0", port=port)

if __name__ == "__main__":
    threading.Thread(target=run_flask, daemon=True).start()

    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("admin", admin))
    app.add_handler(CommandHandler("ban", ban))
    app.add_handler(CommandHandler("unban", unban))
    app.add_handler(CommandHandler("broadcast", broadcast))
    app.add_handler(CommandHandler("setfield", setfield))
    app.add_handler(CallbackQueryHandler(button_handler))
    print("Бот и сервер запущены...")
    app.run_polling()
