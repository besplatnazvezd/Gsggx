import os
import sqlite3
import logging
import httpx
from telegram import Update, ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters,
    ContextTypes,
    ConversationHandler
)

# --- НАСТРОЙКИ ---
BOT_TOKEN = "8812638330:AAEu-BtSmKYbehlP4YEbLnE-CGx3AoSIjbg"
REPORT_CHANNEL = "@mCoin_rep"  # Канал для отчетов
ADMIN_ID = 8894416195         # Твой ID для рассылки и статистики

# Настройки тарифов
REWARD_SPONSOR = 200000  # 200k за спонсора
REWARD_GAME = 150000     # 150k за игру

# Имя файла базы данных SQLite
DB_FILE = "bot_database.db"

# Состояния диалогов
SPONSORS_COUNT, DARTS_PREDICTION, DICE_PREDICTION = range(3)

logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# ИНИЦИАЛИЗАЦИЯ И РАБОТА С БАЗОЙ ДАННЫХ SQLite
# ---------------------------------------------------------------------------
def db_init():
    """Создает таблицы в базе данных, если они отсутствуют."""
    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.cursor()
        
        # Таблица пользователей
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS users (
                tg_id INTEGER PRIMARY KEY,
                username TEXT,
                balance INTEGER DEFAULT 0
            )
        """)
        
        # Таблица истории начислений
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tg_id INTEGER,
                action TEXT,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)
        
        # Таблица разрешенных групп
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS allowed_groups (
                chat_id INTEGER PRIMARY KEY
            )
        """)
        conn.commit()

def db_get_or_create_user(tg_id: int, username: str | None) -> dict:
    """Возвращает данные пользователя. Создает запись, если пользователя нет."""
    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT tg_id, username, balance FROM users WHERE tg_id = ?", (tg_id,))
        row = cursor.fetchone()
        if row:
            return {"tg_id": row[0], "username": row[1], "balance": row[2]}
        
        cursor.execute("INSERT INTO users (tg_id, username, balance) VALUES (?, ?, ?)", (tg_id, username or "Выживший", 0))
        conn.commit()
        return {"tg_id": tg_id, "username": username or "Выживший", "balance": 0}

def db_add_reward(tg_id: int, amount: int, description: str):
    """Начисляет награду пользователю и записывает операцию в историю."""
    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.cursor()
        # Обновляем баланс
        cursor.execute("UPDATE users SET balance = balance + ? WHERE tg_id = ?", (amount, tg_id))
        # Записываем в историю
        history_text = f"+{amount:,} mC ({description})"
        cursor.execute("INSERT INTO history (tg_id, action) VALUES (?, ?)", (tg_id, history_text))
        conn.commit()

def db_get_history(tg_id: int) -> list:
    """Возвращает последние 10 записей из истории транзакций пользователя."""
    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT action, timestamp FROM history WHERE tg_id = ? ORDER BY id DESC LIMIT 10", (tg_id,))
        return cursor.fetchall()

def db_get_stats() -> dict:
    """Возвращает общую статистику по боту."""
    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM users")
        total_users = cursor.fetchone()[0]
        
        # Считаем сумму всех выданных коинов из таблицы истории (только положительные начисления)
        cursor.execute("SELECT action FROM history")
        rows = cursor.fetchall()
        total_distributed = 0
        for row in rows:
            # Извлекаем числовое значение из логов формата "+150,000 mC (...)"
            match = re.search(r"\+(\d[\d,]*)\s+mC", row[0])
            if match:
                total_distributed += int(match.group(1).replace(",", ""))
                
        return {"total_users": total_users, "total_distributed": total_distributed}

# ---------------------------------------------------------------------------
# КЛАВИАТУРЫ
# ---------------------------------------------------------------------------
def get_main_keyboard():
    return ReplyKeyboardMarkup([
        [KeyboardButton("💰 Получить mCoin")],
        [KeyboardButton("🎯 Дартс на mCoin"), KeyboardButton("🎲 Кубик на mCoin")],
        [KeyboardButton("📜 История")]
    ], resize_keyboard=True)

# ---------------------------------------------------------------------------
# ОБРАБОТЧИКИ КОМАНД
# ---------------------------------------------------------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    db_get_or_create_user(user.id, user.username)
    
    await update.message.reply_text(
        "Привет! Это бот для заработка mCoin. Выберите интересующий раздел меню:",
        reply_markup=get_main_keyboard()
    )

# --- СЦЕНАРИЙ «ПОЛУЧИТЬ mCoin» ---
async def get_mcoin_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Напиши количество спонсоров, на которых хочешь подписаться.\n"
        "Помни: чем больше спонсоров — тем выше награда! (Минимум: 1)"
    )
    return SPONSORS_COUNT

async def get_mcoin_process(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        count = int(update.message.text)
        if count < 1: raise ValueError
    except ValueError:
        await update.message.reply_text("Пожалуйста, введите корректное число спонсоров (больше 0).")
        return SPONSORS_COUNT

    sponsors = [f"https://t.me/sponsor_channel_{i}" for i in range(1, count + 1)]
    sponsors_text = "\n".join([f"{idx}. {link}" for idx, link in enumerate(sponsors, 1)])
    
    context.user_data["sponsors_count"] = count
    
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Проверить выполнение", callback_data=f"check_sub_{count}")]
    ])
    
    await update.message.reply_text(
        f"Выполни задание: подпишись на всех указанных спонсоров:\n\n{sponsors_text}",
        reply_markup=keyboard
    )
    return ConversationHandler.END

async def check_subscription(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    user_id = query.from_user.id
    count = int(query.data.split("_")[2])
    
    # Эмуляция успешной проверки подписки
    is_valid = True 
    
    if is_valid:
        reward = count * REWARD_SPONSOR
        db_get_or_create_user(user_id, query.from_user.username)
        db_add_reward(user_id, reward, f"Спонсоры: {count}")
        
        await query.edit_message_text(f"🎉 Задание выполнено! Награда в размере {reward:,} mCoin начислена на ваш баланс.")
        
        # Отчет в канал
        try:
            await context.bot.send_message(
                chat_id=REPORT_CHANNEL,
                text=f"👤 Пользователь ID: {user_id}\n💰 Выполнено подписок: {count}\n🎁 Начислено: {reward:,} mCoin"
            )
        except Exception as e:
            logger.error(f"Не удалось отправить отчет в канал: {e}")
    else:
        await query.message.reply_text("❌ Ошибка проверки: подпишись на всех спонсоров из списка и попробуй снова.")

# --- СЦЕНАРИЙ «ДАРТС НА mCoin» ---
async def darts_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Введите количество спонсоров для участия в игре:")
    return SPONSORS_COUNT

async def darts_get_sponsors(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        count = int(update.message.text)
        if count < 1: raise ValueError
    except ValueError:
        await update.message.reply_text("Введите корректное число спонсоров.")
        return SPONSORS_COUNT
        
    context.user_data["game_sponsors"] = count
    
    keyboard = ReplyKeyboardMarkup([
        [KeyboardButton("Центр 🎯"), KeyboardButton("Белое ⚪")],
        [KeyboardButton("Красное 🔴"), KeyboardButton("Мимо 💨")]
    ], resize_keyboard=True)
    
    await update.message.reply_text("Сделайте ваш прогноз на сектор попадания:", reply_markup=keyboard)
    return DARTS_PREDICTION

async def darts_play(update: Update, context: ContextTypes.DEFAULT_TYPE):
    prediction = update.message.text
    count = context.user_data.get("game_sponsors", 1)
    user_id = update.effective_user.id
    
    await update.message.reply_text("Бросаем дротик...", reply_markup=get_main_keyboard())
    
    msg = await update.message.reply_dice(emoji="🎯")
    value = msg.dice.value
    
    results_map = {
        1: "Мимо 💨", 2: "Белое ⚪", 3: "Красное 🔴",
        4: "Белое ⚪", 5: "Красное 🔴", 6: "Центр 🎯"
    }
    
    actual_result = results_map.get(value, "Мимо 💨")
    is_win = prediction == actual_result
    
    if is_win:
        reward = count * REWARD_GAME
        db_get_or_create_user(user_id, update.effective_user.username)
        db_add_reward(user_id, reward, "Дартс")
        await update.message.reply_text(f"🎉 Победа! Выпало: {actual_result}. Начислено {reward:,} mCoin!")
    else:
        await update.message.reply_text(f"💨 Промах! Выпало: {actual_result}, а ваш прогноз: {prediction}. Попробуйте еще раз!")
        
    return ConversationHandler.END

# --- СЦЕНАРИЙ «КУБИК НА mCoin» ---
async def dice_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Введите количество спонсоров для участия в игре:")
    return SPONSORS_COUNT

async def dice_get_sponsors(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        count = int(update.message.text)
        if count < 1: raise ValueError
    except ValueError:
        await update.message.reply_text("Введите корректное число спонсоров.")
        return SPONSORS_COUNT
        
    context.user_data["game_sponsors"] = count
    
    keyboard = ReplyKeyboardMarkup([
        [KeyboardButton("1"), KeyboardButton("2"), KeyboardButton("3")],
        [KeyboardButton("4"), KeyboardButton("5"), KeyboardButton("6")]
    ], resize_keyboard=True)
    
    await update.message.reply_text("Выберите число от 1 до 6 на кубике:", reply_markup=keyboard)
    return DICE_PREDICTION

async def dice_play(update: Update, context: ContextTypes.DEFAULT_TYPE):
    prediction = int(update.message.text)
    count = context.user_data.get("game_sponsors", 1)
    user_id = update.effective_user.id
    
    await update.message.reply_text("Бросаем кубик...", reply_markup=get_main_keyboard())
    
    msg = await update.message.reply_dice(emoji="🎲")
    value = msg.dice.value
    
    is_win = prediction == value
    
    if is_win:
        reward = count * REWARD_GAME
        db_get_or_create_user(user_id, update.effective_user.username)
        db_add_reward(user_id, reward, "Кубик")
        await update.message.reply_text(f"🎉 Вы выиграли! Выпало число {value}. Начислено {reward:,} mCoin!")
    else:
        await update.message.reply_text(f"😢 Не повезло! Выпало число {value}, а вы ставали на {prediction}.")
        
    return ConversationHandler.END

# --- ПРОЧИЕ ФУНКЦИИ ---
async def show_history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    db_get_or_create_user(user_id, update.effective_user.username)
    history = db_get_history(user_id)
    
    if not history:
        await update.message.reply_text("Ваша история транзакций пока пуста.")
    else:
        text = "📜 <b>Последние начисления:</b>\n\n"
        for idx, (action, timestamp) in enumerate(history, 1):
            text += f"{idx}. {action} — <i>{timestamp}</i>\n"
        await update.message.reply_text(text, parse_mode="HTML")

async def enable_in_group(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if update.effective_chat.type in ["group", "supergroup"]:
        with sqlite3.connect(DB_FILE) as conn:
            cursor = conn.cursor()
            cursor.execute("INSERT OR IGNORE INTO allowed_groups (chat_id) VALUES (?)", (chat_id,))
            conn.commit()
        await update.message.reply_text("✅ Бот успешно активирован для данной группы!", reply_markup=get_main_keyboard())
    else:
        await update.message.reply_text("Команда доступна только внутри групп.")

# --- АДМИН-ФУНКЦИОНАЛ ---
async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID: return
    stats = db_get_stats()
    await update.message.reply_text(
        f"📊 <b>СТАТИСТИКА БОТА:</b>\n\n"
        f"• Всего пользователей: {stats['total_users']}\n"
        f"• Выдано через игры и подписки: {stats['total_distributed']:,} mC",
        parse_mode="HTML"
    )

async def broadcast_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Рассылает сообщение всем пользователям из базы данных SQLite."""
    if update.effective_user.id != ADMIN_ID: return
    text_to_send = " ".join(context.args)
    if not text_to_send:
        await update.message.reply_text("Использование: `/broadcast [текст сообщения]`", parse_mode="HTML")
        return
        
    await update.message.reply_text("🚀 Начинаю рассылку...")
    
    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT tg_id FROM users")
        users = cursor.fetchall()
        
    success, fail = 0, 0
    for user in users:
        try:
            await context.bot.send_message(chat_id=user[0], text=text_to_send)
            success += 1
        except Exception:
            fail += 1
            
    await update.message.reply_text(f"🏁 Рассылка завершена!\n\nУспешно отправлено: {success}\nНе удалось: {fail}")

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Действие отменено.", reply_markup=get_main_keyboard())
    return ConversationHandler.END

import re

# --- ЗАПУСК ---
def main():
    # Создаем таблицы при старте
    db_init()
    
    app = Application.builder().token(BOT_TOKEN).build()
    
    # Настройка диалогов
    mcoin_conv = ConversationHandler(
        entry_points=[MessageHandler(filters.Text("💰 Получить mCoin"), get_mcoin_start)],
        states={SPONSORS_COUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_mcoin_process)]},
        fallbacks=[CommandHandler("cancel", cancel)]
    )
    
    darts_conv = ConversationHandler(
        entry_points=[MessageHandler(filters.Text("🎯 Дартс на mCoin"), darts_start)],
        states={
            SPONSORS_COUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, darts_get_sponsors)],
            DARTS_PREDICTION: [MessageHandler(filters.TEXT & ~filters.COMMAND, darts_play)]
        },
        fallbacks=[CommandHandler("cancel", cancel)]
    )

    dice_conv = ConversationHandler(
        entry_points=[MessageHandler(filters.Text("🎲 Кубик на mCoin"), dice_start)],
        states={
            SPONSORS_COUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, dice_get_sponsors)],
            DICE_PREDICTION: [MessageHandler(filters.TEXT & ~filters.COMMAND, dice_play)]
        },
        fallbacks=[CommandHandler("cancel", cancel)]
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("on", enable_in_group))
    app.add_handler(CommandHandler("stats", stats_command))
    app.add_handler(CommandHandler("broadcast", broadcast_command))
    
    app.add_handler(mcoin_conv)
    app.add_handler(darts_conv)
    app.add_handler(dice_conv)
    
    app.add_handler(MessageHandler(filters.Text("📜 История"), show_history))
    app.add_handler(CallbackQueryHandler(check_subscription, pattern="^check_sub_"))

    logger.info("Бот на SQLite запущен!")
    app.run_polling()

if __name__ == "__main__":
    main()
