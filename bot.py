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
GAME_URL = "https://gleaming-rugelach-e33a2d.netlify.app/"
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

# ---------------- PROMOCODES HELPERS ----------------
def get_promo_doc(code):
    # Промокоды всегда храним в верхнем регистре, чтобы "abc" и "ABC" были одним кодом
    return db.collection('promocodes').document(code.strip().upper())

def add_bonus_to_user(uid, field, amount):
    """Кладёт бонус в очередь — игра сама забирает его (как и /setfield)."""
    db.collection('balances').document(str(uid)).collection('bonuses').document().set({
        'field': field,
        'amount': amount,
        'applied': False,
        'created_at': datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    })

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

ADMIN_NOTIFY_IDS = ['7175060469', '6172801473']

def notify_admins(text):
    import requests as pyrequests
    for chat_id in ADMIN_NOTIFY_IDS:
        try:
            pyrequests.post(
                f'https://api.telegram.org/bot{BOT_TOKEN}/sendMessage',
                json={'chat_id': chat_id, 'text': text, 'parse_mode': 'HTML'},
                timeout=10
            )
        except Exception as e:
            logging.warning(f"notify_admins failed for {chat_id}: {e}")

@flask_app.route('/api/withdraw', methods=['POST'])
def api_withdraw():
    body = request.get_json(force=True, silent=True) or {}
    init_data = body.get('initData', '')
    parsed = verify_init_data(init_data)
    if not parsed:
        return jsonify({'error': 'invalid_auth'}), 403
    user = get_user_from_init(parsed)
    uid = str(user['id'])
    if is_banned(uid):
        return jsonify({'error': 'banned'}), 403

    wtype = body.get('type')
    amount = body.get('amount')
    details = body.get('details', {})
    if wtype not in ('ton', 'card', 'phone') or not amount:
        return jsonify({'error': 'bad_request'}), 400

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    username = user.get('username', '')
    first_name = user.get('first_name', '')

    # Сохраняем заявку в Firestore — навсегда, ничего не потеряется
    doc_ref = db.collection('withdrawals').document()
    doc_ref.set({
        'user_id': uid,
        'username': username,
        'first_name': first_name,
        'type': wtype,
        'amount': amount,
        'details': details,
        'status': 'pending',
        'created_at': now
    })

    if wtype == 'ton':
        text = (f"💎 <b>ЗАЯВКА НА ВЫВОД TON</b>\n"
                f"🆔 ID заявки: {doc_ref.id}\n"
                f"👤 ID: {uid}\n📛 Username: @{username}\n"
                f"💰 Сумма: {amount} TON\n🏦 Кошелёк: {details.get('addr','')}\n🕐 {now}")
    elif wtype == 'card':
        text = (f"💳 <b>ЗАЯВКА НА ВЫВОД (КАРТА)</b>\n"
                f"🆔 ID заявки: {doc_ref.id}\n"
                f"👤 ID: {uid}\n📛 Username: @{username}\n"
                f"💰 Сумма: {amount}$\n💳 Карта: {details.get('card','')}\n"
                f"👤 Имя: {details.get('name','')}\n🕐 {now}")
    else:
        text = (f"📱 <b>ЗАЯВКА НА ВЫВОД (ТЕЛЕФОН)</b>\n"
                f"🆔 ID заявки: {doc_ref.id}\n"
                f"👤 ID: {uid}\n📛 Username: @{username}\n"
                f"💰 Сумма: {amount} TON\n📞 Телефон: {details.get('phone','')}\n🕐 {now}")

    notify_admins(text)
    return jsonify({'ok': True, 'id': doc_ref.id})

@flask_app.route('/')
def home():
    return "JMiner backend is running."

@flask_app.route('/api/bonuses', methods=['GET'])
def api_get_bonuses():
    init_data = request.args.get('initData', '')
    parsed = verify_init_data(init_data)
    if not parsed:
        return jsonify({'error': 'invalid_auth'}), 403
    user = get_user_from_init(parsed)
    uid = str(user['id'])
    docs = db.collection('balances').document(uid).collection('bonuses').where('applied', '==', False).stream()
    bonuses = [{'id': d.id, 'field': d.to_dict().get('field'), 'amount': d.to_dict().get('amount')} for d in docs]
    return jsonify({'bonuses': bonuses})

@flask_app.route('/api/bonuses/ack', methods=['POST'])
def api_ack_bonuses():
    body = request.get_json(force=True, silent=True) or {}
    init_data = body.get('initData', '')
    parsed = verify_init_data(init_data)
    if not parsed:
        return jsonify({'error': 'invalid_auth'}), 403
    user = get_user_from_init(parsed)
    uid = str(user['id'])
    ids = body.get('ids', [])
    for bid in ids:
        db.collection('balances').document(uid).collection('bonuses').document(bid).set({'applied': True}, merge=True)
    return jsonify({'ok': True})

# ---------------- PROMOCODE API (для игры) ----------------
@flask_app.route('/api/promo', methods=['POST'])
def api_promo():
    body = request.get_json(force=True, silent=True) or {}
    init_data = body.get('initData', '')
    parsed = verify_init_data(init_data)
    if not parsed:
        return jsonify({'error': 'invalid_auth'}), 403
    user = get_user_from_init(parsed)
    uid = str(user['id'])
    if is_banned(uid):
        return jsonify({'error': 'banned'}), 403

    code = (body.get('code') or '').strip()
    if not code:
        return jsonify({'error': 'empty_code'}), 400

    promo_ref = get_promo_doc(code)
    promo_doc = promo_ref.get()
    if not promo_doc.exists:
        return jsonify({'error': 'not_found'}), 404

    promo = promo_doc.to_dict()

    if not promo.get('active', True):
        return jsonify({'error': 'inactive'}), 400

    used_by = promo.get('used_by', [])
    if uid in used_by:
        return jsonify({'error': 'already_used'}), 400

    limit = promo.get('limit', 0)  # 0 = безлимит
    used_count = promo.get('used_count', 0)
    if limit and used_count >= limit:
        return jsonify({'error': 'limit_reached'}), 400

    field = promo.get('field', 'pCoins')
    amount = promo.get('amount', 0)

    # Награда применяется мгновенно на клиенте (см. index.html) и синхронизируется
    # через /api/state. В очередь bonuses не кладём, чтобы не начислить дважды.

    promo_ref.update({
        'used_by': firestore.ArrayUnion([uid]),
        'used_count': firestore.Increment(1)
    })

    return jsonify({'ok': True, 'field': field, 'amount': amount})

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
        [InlineKeyboardButton("💰 Заявки на вывод", callback_data="admin_withdrawals")],
        [InlineKeyboardButton("🎁 Начислить монеты", callback_data="admin_addcoins")],
        [InlineKeyboardButton("🎫 Промокоды", callback_data="admin_promo")],
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

    elif query.data == "admin_withdrawals":
        docs = db.collection('withdrawals').where('status', '==', 'pending').stream()
        text = "💰 Заявки на вывод (в ожидании):\n\n"
        count = 0
        for d in docs:
            w = d.to_dict()
            text += (f"🆔 {d.id}\n👤 {w.get('first_name','')} (@{w.get('username','')}) | ID: {w.get('user_id')}\n"
                     f"💰 {w.get('amount')} | Тип: {w.get('type')}\n🕐 {w.get('created_at')}\n"
                     f"Закрыть: /paid {d.id}\n\n")
            count += 1
        if count == 0:
            text = "✅ Нет заявок в ожидании."
        await query.edit_message_text(text)

    elif query.data == "admin_addcoins":
        await query.edit_message_text(
            "🎁 Начислить игроку:\n"
            "/setfield ID поле значение\n\n"
            "Пример:\n/setfield 123456789 ton 1.5\n\n"
            "(применится автоматически, в течение 15 сек пока игрок онлайн)"
        )

    elif query.data == "admin_promo":
        await query.edit_message_text(
            "🎫 <b>Управление промокодами</b>\n\n"
            "Создать код:\n"
            "<code>/addpromo КОД ПОЛЕ СУММА ЛИМИТ</code>\n\n"
            "Пример (даёт 1000 P-Coins, можно использовать 50 раз):\n"
            "<code>/addpromo NEWYEAR pCoins 1000 50</code>\n\n"
            "Поля: pCoins, jCoins, ton\n"
            "ЛИМИТ = 0 значит без ограничения по количеству активаций\n\n"
            "Посмотреть все коды:\n<code>/promocodes</code>\n\n"
            "Выключить код:\n<code>/delpromo КОД</code>",
            parse_mode="HTML"
        )

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
    """Админ-команда: начисляет игроку бонус, который применится безопасно (не теряется).
    /setfield ID поле значение
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
    # Кладём в очередь бонусов — игра сама заберёт и применит при следующей проверке (раз в 15 сек)
    add_bonus_to_user(uid, field, value_parsed)
    await update.message.reply_text(f"✅ Игроку {uid} поставлено в очередь: {field} +{value_parsed}\n(применится автоматически когда игрок в сети, в течение 15 сек)")

async def withdrawals(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показывает все заявки на вывод (ожидающие). /withdrawals"""
    if update.effective_user.id != ADMIN_ID:
        return
    docs = db.collection('withdrawals').where('status', '==', 'pending').stream()
    text = "💰 Заявки на вывод (в ожидании):\n\n"
    count = 0
    for d in docs:
        w = d.to_dict()
        text += (f"🆔 {d.id}\n👤 {w.get('first_name','')} (@{w.get('username','')}) | ID: {w.get('user_id')}\n"
                 f"💰 {w.get('amount')} | Тип: {w.get('type')}\n🕐 {w.get('created_at')}\n"
                 f"Закрыть: /paid {d.id}\n\n")
        count += 1
    if count == 0:
        text = "✅ Нет заявок в ожидании."
    await update.message.reply_text(text)

async def paid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Отмечает заявку как выполненную. /paid ID_заявки"""
    if update.effective_user.id != ADMIN_ID:
        return
    if not context.args:
        await update.message.reply_text("Напиши: /paid ID_заявки")
        return
    wid = context.args[0]
    db.collection('withdrawals').document(wid).set({'status': 'paid'}, merge=True)
    await update.message.reply_text(f"✅ Заявка {wid} отмечена как выполненная.")

async def addpromo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Создаёт новый промокод.
    /addpromo КОД ПОЛЕ СУММА ЛИМИТ
    Пример: /addpromo NEWYEAR pCoins 1000 50  (лимит 0 = без ограничения)"""
    if update.effective_user.id != ADMIN_ID:
        return
    if len(context.args) < 4:
        await update.message.reply_text(
            "Напиши: /addpromo КОД ПОЛЕ СУММА ЛИМИТ\n"
            "Пример: /addpromo NEWYEAR pCoins 1000 50\n"
            "(ЛИМИТ = 0 значит без ограничения)"
        )
        return
    code, field, amount_raw, limit_raw = context.args[0], context.args[1], context.args[2], context.args[3]
    try:
        amount = float(amount_raw) if '.' in amount_raw else int(amount_raw)
    except ValueError:
        await update.message.reply_text("❌ СУММА должна быть числом.")
        return
    try:
        limit = int(limit_raw)
    except ValueError:
        await update.message.reply_text("❌ ЛИМИТ должен быть целым числом (0 = без ограничения).")
        return

    get_promo_doc(code).set({
        'code': code.strip().upper(),
        'field': field,
        'amount': amount,
        'limit': limit,
        'used_count': 0,
        'used_by': [],
        'active': True,
        'created_at': datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    })
    limit_text = "без ограничения" if limit == 0 else f"{limit} раз"
    await update.message.reply_text(
        f"✅ Промокод создан!\n\n"
        f"🎫 Код: {code.strip().upper()}\n"
        f"🎁 Награда: {field} +{amount}\n"
        f"🔢 Лимит активаций: {limit_text}"
    )

async def delpromo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Выключает промокод (он перестаёт работать, но история сохраняется).
    /delpromo КОД"""
    if update.effective_user.id != ADMIN_ID:
        return
    if not context.args:
        await update.message.reply_text("Напиши: /delpromo КОД")
        return
    code = context.args[0]
    promo_ref = get_promo_doc(code)
    if not promo_ref.get().exists:
        await update.message.reply_text("❌ Такой промокод не найден.")
        return
    promo_ref.update({'active': False})
    await update.message.reply_text(f"🚫 Промокод {code.strip().upper()} выключен.")

async def promocodes(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показывает все промокоды. /promocodes"""
    if update.effective_user.id != ADMIN_ID:
        return
    docs = db.collection('promocodes').stream()
    text = "🎫 <b>Промокоды:</b>\n\n"
    count = 0
    for d in docs:
        p = d.to_dict()
        status = "✅" if p.get('active', True) else "🚫"
        limit = p.get('limit', 0)
        limit_text = "∞" if limit == 0 else f"{p.get('used_count', 0)}/{limit}"
        text += (f"{status} <code>{d.id}</code> — {p.get('field')} +{p.get('amount')} "
                 f"(использован: {limit_text})\n")
        count += 1
    if count == 0:
        text = "📭 Промокодов пока нет."
    await update.message.reply_text(text, parse_mode="HTML")

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
    app.add_handler(CommandHandler("withdrawals", withdrawals))
    app.add_handler(CommandHandler("paid", paid))
    app.add_handler(CommandHandler("addpromo", addpromo))
    app.add_handler(CommandHandler("delpromo", delpromo))
    app.add_handler(CommandHandler("promocodes", promocodes))
    app.add_handler(CallbackQueryHandler(button_handler))
    print("Бот и сервер запущены...")
    app.run_polling()
    
