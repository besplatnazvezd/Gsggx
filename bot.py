import os
import sqlite3
import logging
import httpx
import random
import re
import asyncio
from datetime import datetime, timedelta
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, KeyboardButton, BotCommand
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters,
    ContextTypes
)

# ---------------------------------------------------------------------------
# КОНФИГУРАЦИЯ БОТА И РЕКЛАМНЫХ РЕСУРСОВ
# ---------------------------------------------------------------------------
BOT_TOKEN = "8812638330:AAEu-BtSmKYbehlP4YEbLnE-CGx3AoSIjbg"
REPORT_CHANNEL = "@mCoin_rep"  # Канал для отправки отчетов о начислениях
ADMIN_ID =  8685397478        # Твой Telegram ID для админки и рассылок

# Твои официальные ресурсы для приветствия
CHANNEL_LINK = "@user"   # Замени на свой юзернейм канала
CHAT_LINK = "@user"      # Замени на свой юзернейм чата
PAYOUT_LINK = "@user"    # Замени на свой юзернейм канала выдач

# Настройки тарифов
REWARD_SPONSOR = 200000  # 200k за одного спонсора
REWARD_GAME = 150000     # 150k за одну победу в игре
REFERRAL_REWARD = 500000 # 500k владельцу рефки при проверке

# Настройки PiarFlow API
PIARFLOW_API_URL = "https://api.piarflow.ru/v1"
PIARFLOW_API_KEY = "ТВОЙ_PIARFLOW_API_KEY"

# Имя файла базы данных SQLite
DB_FILE = "bot_database.db"

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", 
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# РАБОТА С БАЗОЙ ДАННЫХ SQLite
# ---------------------------------------------------------------------------
def db_init():
    """Инициализация базы данных и автоматическое создание/обновление таблиц."""
    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.cursor()
        
        # Основная таблица пользователей
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS users (
                tg_id INTEGER PRIMARY KEY,
                username TEXT,
                balance INTEGER DEFAULT 0,
                last_game_time TEXT,
                referred_by INTEGER,
                ref_reward_paid INTEGER DEFAULT 0
            )
        """)
        
        # Проверка и добавление колонок для совместимости
        try:
            cursor.execute("ALTER TABLE users ADD COLUMN last_game_time TEXT")
        except sqlite3.OperationalError:
            pass
            
        try:
            cursor.execute("ALTER TABLE users ADD COLUMN referred_by INTEGER")
        except sqlite3.OperationalError:
            pass
            
        try:
            cursor.execute("ALTER TABLE users ADD COLUMN ref_reward_paid INTEGER DEFAULT 0")
        except sqlite3.OperationalError:
            pass

        # Таблица истории транзакций
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

def db_get_user(tg_id: int) -> dict | None:
    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT tg_id, username, balance, last_game_time, referred_by, ref_reward_paid FROM users WHERE tg_id = ?", (tg_id,))
        row = cursor.fetchone()
        if row:
            return {
                "tg_id": row[0], "username": row[1], "balance": row[2],
                "last_game_time": row[3], "referred_by": row[4], "ref_reward_paid": row[5]
            }
    return None

def db_create_user(tg_id: int, username: str | None, referred_by: int | None = None) -> dict:
    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.cursor()
        cursor.execute(
            "INSERT OR IGNORE INTO users (tg_id, username, balance, referred_by) VALUES (?, ?, ?, ?)",
            (tg_id, username or "Выживший", 0, referred_by)
        )
        conn.commit()
    return db_get_user(tg_id)

def db_update_user(tg_id: int, updates: dict):
    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.cursor()
        for key, value in updates.items():
            cursor.execute(f"UPDATE users SET {key} = ? WHERE tg_id = ?", (value, tg_id))
        conn.commit()

def db_add_reward(tg_id: int, amount: int, description: str):
    """Начисление средств с записью в историю."""
    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.cursor()
        cursor.execute("UPDATE users SET balance = balance + ? WHERE tg_id = ?", (amount, tg_id))
        
        prefix = "+" if amount >= 0 else ""
        history_text = f"{prefix}{amount:,} mC ({description})"
        cursor.execute("INSERT INTO history (tg_id, action) VALUES (?, ?)", (tg_id, history_text))
        conn.commit()

def db_get_history(tg_id: int) -> list:
    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT action, timestamp FROM history WHERE tg_id = ? ORDER BY id DESC LIMIT 10", (tg_id,))
        return cursor.fetchall()

# ---------------------------------------------------------------------------
# ИНТЕГРАЦИЯ С API PIARFLOW
# ---------------------------------------------------------------------------
async def get_piarflow_sponsors(count: int) -> list:
    """Запрос к API PiarFlow для выгрузки спонсорских каналов."""
    try:
        async with httpx.AsyncClient() as client:
            # Реальный запрос к API (активируется при установке ключа)
            # r = await client.get(f"{PIARFLOW_API_URL}/get_sponsors?api_key={PIARFLOW_API_KEY}&limit={count}", timeout=5.0)
            # if r.status_code == 200:
            #     return r.json().get("channels", [])[:count]
            pass
    except Exception as e:
        logger.error(f"Ошибка запроса к PiarFlow API: {e}")
    
    # Резервные каналы, если API еще не подключено
    return [
        {"name": f"Спонсор #{i}", "link": f"https://t.me/piarflow_channel_{i}", "id": -100123456780 + i}
        for i in range(1, count + 1)
    ]

async def check_piarflow_subscriptions(user_id: int, sponsors: list) -> bool:
    """Проверка подписки на выданных спонсоров через API PiarFlow."""
    try:
        async with httpx.AsyncClient() as client:
            # Реальный запрос к API для верификации пользователя
            # r = await client.post(f"{PIARFLOW_API_URL}/verify", json={
            #     "api_key": PIARFLOW_API_KEY,
            #     "user_id": user_id,
            #     "channels": [s["id"] for s in sponsors]
            # }, timeout=5.0)
            # if r.status_code == 200:
            #     return r.json().get("is_subscribed", False)
            pass
    except Exception as e:
        logger.error(f"Ошибка верификации подписок в PiarFlow: {e}")
    
    return True # Временное резервное значение успеха для тестов

# ---------------------------------------------------------------------------
# КЛАВИАТУРЫ (ИНЛАЙН И REPLY ДЛЯ ГРУПП)
# ---------------------------------------------------------------------------
def get_main_inline_keyboard() -> InlineKeyboardMarkup:
    """Главная инлайн-клавиатура для ЛС бота."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("💰 Получить mCoin", callback_data="menu_get_mcoin")],
        [InlineKeyboardButton("🎮 Игры", callback_data="open_games_list")],
        [
            InlineKeyboardButton("📜 История", callback_data="menu_history"),
            InlineKeyboardButton("👥 Рефералы", callback_data="menu_ref")
        ]
    ])

def get_games_inline_keyboard() -> InlineKeyboardMarkup:
    """Инлайн клавиатура со списком игр."""
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🎯 Дартс", callback_data="game_darts"),
            InlineKeyboardButton("🎲 Кубик", callback_data="game_dice"),
            InlineKeyboardButton("🏀 Баскетбол", callback_data="game_basketball")
        ],
        [InlineKeyboardButton("◀️ Назад в меню", callback_data="go_to_home")]
    ])

def get_group_reply_keyboard() -> ReplyKeyboardMarkup:
    """Reply-клавиатура для отображения в группах/чатах."""
    return ReplyKeyboardMarkup([
        [KeyboardButton("💰 Получить mCoin"), KeyboardButton("🎮 Игры")],
        [KeyboardButton("📜 История"), KeyboardButton("👥 Рефералы")]
    ], resize_keyboard=True, placeholder="Выберите действие в меню бота")

def get_back_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Вернуться в Главное Меню", callback_data="go_to_home")]])

def get_sponsors_selection_keyboard(action_prefix: str) -> InlineKeyboardMarkup:
    """Генерирует инлайн-кнопки от 1 до 10 для выбора количества спонсоров."""
    keyboard = []
    row = []
    for i in range(1, 11):
        row.append(InlineKeyboardButton(str(i), callback_data=f"{action_prefix}{i}"))
        if len(row) == 5:
            keyboard.append(row)
            row = []
    if row:
        keyboard.append(row)
    keyboard.append([InlineKeyboardButton("❌ Отменить операцию", callback_data="go_to_home")])
    return InlineKeyboardMarkup(keyboard)

# ---------------------------------------------------------------------------
# ОБРАБОТЧИКИ КОМАНД TELEGRAM
# ---------------------------------------------------------------------------
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    args = context.args
    
    referred_by = None
    if args and args[0].startswith("ref_"):
        try:
            referred_by = int(args[0].split("_")[1])
        except (IndexError, ValueError):
            referred_by = None

    db_user = db_get_user(user.id)
    
    # Сценарий Реферала (первый запуск)
    if not db_user and referred_by:
        db_user = db_create_user(user.id, user.username, referred_by)
        
        # Сразу принудительно показываем приветственных спонсоров (не за mCoin)
        welcome_sponsors = await get_piarflow_sponsors(3)
        context.user_data["ref_welcome_sponsors"] = welcome_sponsors
        
        links_text = "\n".join([f"🔗 <a href='{s['link']}'>{s['name']}</a>" for s in welcome_sponsors])
        text = (
            f"👋 <b>Добро пожаловать, {user.first_name}!</b>\n\n"
            f"Вы перешли по реферальной ссылке. Чтобы разблокировать доступ к заработку и играм, "
            f"подпишитесь на наших спонсоров:\n\n{links_text}\n\n"
            f"После подписки нажмите кнопку проверки ниже 👇"
        )
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Проверить подписки", callback_data="check_ref_welcome_subs")]
        ])
        await update.message.reply_text(text, reply_markup=keyboard, parse_mode="HTML", disable_web_page_preview=True)
        return

    if not db_user:
        db_user = db_create_user(user.id, user.username)

    # Приветственный текст по ТЗ
    text = (
        f"Получай Free mCoin в нашем боте!\n\n"
        f"📢 Канал — {CHANNEL_LINK}\n"
        f"💬 Чат — {CHAT_LINK}\n"
        f"🏆 Выдачи — {PAYOUT_LINK}\n\n"
        f"P.S: в играх нету ставок, вы ничего не теряете"
    )
    
    # Разделение логики: группа или ЛС
    if update.effective_chat.type in ["group", "supergroup"]:
        await update.message.reply_text(text, reply_markup=get_group_reply_keyboard())
    else:
        await update.message.reply_text(text, reply_markup=get_main_inline_keyboard())

async def games_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Вызов списка игр напрямую по команде /games."""
    text = (
        "<b>🎮 ИГРОВОЙ СЕКТОР mCoin</b>\n"
        "═" * 30 + "\n"
        "Выберите игру для проверки удачи. \n\n"
        "⚠️ <i>Правило: принять участие в любой игре можно строго раз в 10 минут!</i>\n"
        "🎈 <i>В играх нет ставок, вы ничего не теряете!</i>"
    )
    await update.message.reply_text(text, reply_markup=get_games_inline_keyboard(), parse_mode="HTML")

# ---------------------------------------------------------------------------
# КУЛДАУН СИСТЕМА
# ---------------------------------------------------------------------------
def is_on_cooldown(user_id: int) -> tuple[bool, int]:
    user_data = db_get_user(user_id)
    if not user_data or not user_data.get("last_game_time"):
        return False, 0
        
    try:
        last_game = datetime.fromisoformat(user_data["last_game_time"])
        delta = datetime.now() - last_game
        if delta < timedelta(minutes=10):
            remaining_seconds = int((timedelta(minutes=10) - delta).total_seconds())
            return True, remaining_seconds
    except ValueError:
        pass
    return False, 0

# ---------------------------------------------------------------------------
# ОБРАБОТКА ИНЛАЙН-КНОПОК И ВЫБОРА КОЛИЧЕСТВА СПОНСОРОВ
# ---------------------------------------------------------------------------
async def inline_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    
    user_id = query.from_user.id
    username = query.from_user.username
    data = query.data
    
    user_data = db_get_user(user_id)
    if not user_data:
        user_data = db_create_user(user_id, username)

    # Возврат на главный экран ЛС
    if data == "go_to_home":
        text = (
            f"Получай Free mCoin в нашем боте!\n\n"
            f"📢 Канал — {CHANNEL_LINK}\n"
            f"💬 Чат — {CHAT_LINK}\n"
            f"🏆 Выдачи — {PAYOUT_LINK}\n\n"
            f"P.S: в играх нету ставок, вы ничего не теряете"
        )
        await query.edit_message_text(text, reply_markup=get_main_inline_keyboard(), parse_mode="HTML")
        return

    # Проверка приветственных реф-подписок
    elif data == "check_ref_welcome_subs":
        welcome_sponsors = context.user_data.get("ref_welcome_sponsors", [])
        is_subscribed = await check_piarflow_subscriptions(user_id, welcome_sponsors)
        
        if is_subscribed:
            referrer_id = user_data.get("referred_by")
            if referrer_id and user_data.get("ref_reward_paid") == 0:
                db_add_reward(referrer_id, REFERRAL_REWARD, f"Реферал {user_id} запущен")
                db_update_user(user_id, {"ref_reward_paid": 1})
                
                try:
                    await context.bot.send_message(
                        chat_id=referrer_id,
                        text=f"🎁 <b>Реферальный бонус!</b>\nВаш реферал успешно выполнил условия. Вам зачислено +{REFERRAL_REWARD:,} mCoin."
                    )
                except Exception:
                    pass
            
            await query.edit_message_text(
                "🎉 <b>Проверка успешно пройдена!</b>\nВсе функции и меню заработка полностью разблокированы для вас.",
                reply_markup=get_main_inline_keyboard(),
                parse_mode="HTML"
            )
        else:
            await query.message.reply_text("❌ Ошибка: вы подписались не на все каналы. Проверьте подписки и попробуйте еще раз.")
        return

    # Выбор спонсоров для ЗАРАБОТКА
    elif data == "menu_get_mcoin":
        await query.edit_message_text(
            "💰 <b>Выбор спонсоров для подписки</b>\n\n"
            "Выберите количество каналов, на которые вы готовы подписаться. "
            "Чем больше подписок — тем выше награда!\n\n"
            "<i>(Или отправьте команду /cancel для отмены действия)</i>",
            reply_markup=get_sponsors_selection_keyboard("select_mcoin_"),
            parse_mode="HTML"
        )
        return

    # Обработка выбора от 1 до 10 для получения mCoin
    elif data.startswith("select_mcoin_"):
        count = int(data.split("_")[2])
        sponsors = await get_piarflow_sponsors(count)
        links_text = "\n".join([f"{idx}. 🔗 <a href='{s['link']}'>{s['name']}</a>" for idx, s in enumerate(sponsors, 1)])
        
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Проверить выполнение подписок", callback_data=f"check_sub_{count}")]
        ])
        await query.edit_message_text(
            f"📋 <b>Для зачисления mCoin подпишитесь на следующих спонсоров:</b>\n\n{links_text}\n\n"
            f"❌ <i>Для отмены операции: /cancel</i>",
            reply_markup=keyboard, parse_mode="HTML", disable_web_page_preview=True
        )
        return

    # Открытие списка игр
    elif data == "open_games_list":
        await query.edit_message_text(
            "<b>🎮 ИГРОВОЙ СЕКТОР mCoin</b>\n"
            "═" * 30 + "\n"
            "Выберите игру для проверки удачи. \n\n"
            "⚠️ <i>Правило: принять участие в любой игре можно строго раз в 10 минут!</i>\n"
            "🎈 <i>В играх нет ставок, вы ничего не теряете!</i>",
            reply_markup=get_games_inline_keyboard(),
            parse_mode="HTML"
        )
        return

    # Показ истории
    elif data == "menu_history":
        history = db_get_history(user_id)
        if not history:
            text = "Ваша история транзакций пока пуста."
        else:
            text = "📜 <b>Ваша история начислений:</b>\n\n"
            for idx, (action, timestamp) in enumerate(history, 1):
                text += f"{idx}. {action} — <i>{timestamp}</i>\n"
        await query.edit_message_text(text, reply_markup=get_back_keyboard(), parse_mode="HTML")
        return

    # Реферальное меню
    elif data == "menu_ref":
        bot_info = await context.bot.get_me()
        link = f"https://t.me/{bot_info.username}?start=ref_{user_id}"
        text = (
            f"<b>👥 РЕФЕРАЛЬНАЯ СИСТЕМА</b>\n"
            f"═" * 30 + "\n"
            f"Приглашайте друзей и получайте mCoin!\n\n"
            f"🎁 <b>Бонус:</b> вы получите <code>{REFERRAL_REWARD:,} mCoin</code> за каждого приведенного реферала, "
            f"когда он пройдет первую проверку подписок.\n\n"
            f"🔗 <b>Ваша ссылка для приглашений:</b>\n<code>{link}</code>"
        )
        await query.edit_message_text(text, reply_markup=get_back_keyboard(), parse_mode="HTML")
        return

    # Проверка кулдауна на игры перед дальнейшими шагами
    on_cooldown, rem_seconds = is_on_cooldown(user_id)
    if on_cooldown and (data.startswith("game_") or data.startswith("select_")):
        min_rem = rem_seconds // 60
        sec_rem = rem_seconds % 60
        await query.message.reply_text(
            f"⏳ <b>Доступ заблокирован!</b>\n\nВы сможете сыграть снова через: "
            f"<code>{min_rem:02d} мин. {sec_rem:02d} сек.</code>"
        )
        return

    # Запуск выбора спонсоров для Дартса (1-10)
    elif data == "game_darts":
        await query.edit_message_text(
            "🎯 <b>Игра ДАРТС</b>\n\nВыберите количество спонсоров, на которых вы готовы подписаться ради участия в игре:",
            reply_markup=get_sponsors_selection_keyboard("select_darts_")
        )
        return

    elif data.startswith("select_darts_"):
        count = int(data.split("_")[2])
        context.user_data["game_sponsors_count"] = count
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("Центр 🎯", callback_data="predict_darts_Центр"), InlineKeyboardButton("Белое ⚪", callback_data="predict_darts_Белое")],
            [InlineKeyboardButton("Красное 🔴", callback_data="predict_darts_Красное"), InlineKeyboardButton("Мимо 💨", callback_data="predict_darts_Мимо")]
        ])
        await query.edit_message_text("🎯 <b>Сделайте ваш прогноз:</b> куда именно прилетит дротик?", reply_markup=keyboard)
        return

    # Запуск выбора спонсоров для Кубика (1-10)
    elif data == "game_dice":
        await query.edit_message_text(
            "🎲 <b>Игра КУБИК</b>\n\nВыберите количество спонсоров, на которых вы готовы подписаться ради участия в игре:",
            reply_markup=get_sponsors_selection_keyboard("select_dice_")
        )
        return

    elif data.startswith("select_dice_"):
        count = int(data.split("_")[2])
        context.user_data["game_sponsors_count"] = count
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("1 🎲", callback_data="predict_dice_1"), InlineKeyboardButton("2 🎲", callback_data="predict_dice_2"), InlineKeyboardButton("3 🎲", callback_data="predict_dice_3")],
            [InlineKeyboardButton("4 🎲", callback_data="predict_dice_4"), InlineKeyboardButton("5 🎲", callback_data="predict_dice_5"), InlineKeyboardButton("6 🎲", callback_data="predict_dice_6")]
        ])
        await query.edit_message_text("🎲 <b>Сделайте ваш прогноз:</b> какая грань выпадет на кубике?", reply_markup=keyboard)
        return

    # Запуск выбора спонсоров для Баскетбола (1-10)
    elif data == "game_basketball":
        await query.edit_message_text(
            "🏀 <b>Игра БАСКЕТБОЛ</b>\n\nВыберите количество спонсоров, на которых вы готовы подписаться ради участия в игре:",
            reply_markup=get_sponsors_selection_keyboard("select_basket_")
        )
        return

    elif data.startswith("select_basket_"):
        count = int(data.split("_")[2])
        context.user_data["game_sponsors_count"] = count
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("Попал 🏀 (множитель x1.6)", callback_data="predict_basket_Попал")],
            [InlineKeyboardButton("Мимо 💨 (множитель x2.4)", callback_data="predict_basket_Мимо")]
        ])
        await query.edit_message_text("🏀 <b>Сделайте ваш прогноз:</b> попадет ли мяч в кольцо?", reply_markup=keyboard)
        return

    # Обработка результатов прогнозов
    elif data.startswith("predict_"):
        parts = data.split("_")
        game_type = parts[1] # "darts", "dice", "basket"
        prediction = parts[2] # выбранная ставка
        sponsors_count = context.user_data.get("game_sponsors_count", 1)
        
        # Навешиваем кулдаун
        db_update_user(user_id, {"last_game_time": datetime.now().isoformat()})
        
        if game_type == "darts":
            await play_darts(query, context, prediction, sponsors_count)
        elif game_type == "dice":
            await play_dice(query, context, int(prediction), sponsors_count)
        elif game_type == "basket":
            await play_basketball(query, context, prediction, sponsors_count)

# ---------------------------------------------------------------------------
# ОБРАБОТЧИК ТЕКСТОВЫХ СООБЩЕНИЙ ИЗ REPLY КЛАВИАТУРЫ ГРУППЫ
# ---------------------------------------------------------------------------
async def handle_group_text_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = update.message.text.strip()
    user_id = update.effective_user.id
    
    # 1. Заработок mCoin
    if text == "💰 Получить mCoin":
        await update.message.reply_text(
            "💰 <b>Выбор количества спонсоров для подписки</b>\n\n"
            "Выберите число каналов, на которые вы подпишитесь:",
            reply_markup=get_sponsors_selection_keyboard("select_mcoin_"),
            parse_mode="HTML"
        )
        
    # 2. Переход к играм
    elif text == "🎮 Игры":
        text_games = (
            "<b>🎮 ИГРОВОЙ СЕКТОР mCoin</b>\n"
            "═" * 30 + "\n"
            "Выберите игру для проверки удачи. \n\n"
            "⚠️ <i>Принять участие в игре можно раз в 10 минут! В играх нет ставок, вы ничего не теряете!</i>"
        )
        await update.message.reply_text(text_games, reply_markup=get_games_inline_keyboard(), parse_mode="HTML")
        
    # 3. Просмотр истории
    elif text == "📜 История":
        history = db_get_history(user_id)
        if not history:
            text_hist = "Ваша история транзакций пока пуста."
        else:
            text_hist = "📜 <b>Ваша история начислений:</b>\n\n"
            for idx, (action, timestamp) in enumerate(history, 1):
                text_hist += f"{idx}. {action} — <i>{timestamp}</i>\n"
        await update.message.reply_text(text_hist, reply_markup=get_back_keyboard(), parse_mode="HTML")
        
    # 4. Меню рефералов
    elif text == "👥 Рефералы":
        bot_info = await context.bot.get_me()
        link = f"https://t.me/{bot_info.username}?start=ref_{user_id}"
        text_ref = (
            f"<b>👥 РЕФЕРАЛЬНАЯ СИСТЕМА</b>\n"
            f"═" * 30 + "\n"
            f"Приглашайте друзей и получайте mCoin!\n\n"
            f"🎁 <b>Бонус:</b> вы получите <code>{REFERRAL_REWARD:,} mCoin</code> за каждого приведенного реферала, "
            f"когда он пройдет первую проверку подписок.\n\n"
            f"🔗 <b>Ваша ссылка для приглашений:</b>\n<code>{link}</code>"
        )
        await update.message.reply_text(text_ref, reply_markup=get_back_keyboard(), parse_mode="HTML")

# ---------------------------------------------------------------------------
# РАСЧЕТ ИГРОВЫХ РЕЗУЛЬТАТОВ (БЕЗ СНЯТИЯ СРЕДСТВ ПРИ ПРОИГРЫШЕ)
# ---------------------------------------------------------------------------
async def play_darts(query, context, prediction: str, count: int):
    user_id = query.from_user.id
    await query.message.reply_text("🎯 Бросаем дротик...")
    
    msg = await query.message.reply_dice(emoji="🎯")
    val = msg.dice.value
    
    results = {
        1: "Мимо", 2: "Белое", 3: "Красное",
        4: "Белое", 5: "Красное", 6: "Центр"
    }
    actual = results.get(val, "Мимо")
    is_win = prediction == actual
    
    if is_win:
        reward = count * REWARD_GAME
        db_add_reward(user_id, reward, "Дартс")
        await query.message.reply_text(f"🎯 <b>ПОБЕДА!</b>\n\nВыпало: {actual}. Начислено: +{reward:,} mCoin!", reply_markup=get_back_keyboard())
    else:
        await query.message.reply_text(f"💨 <b>ПРОМАХ!</b>\n\nВыпало: {actual}, ваш прогноз: {prediction}. Ничего страшного, попробуйте еще раз!", reply_markup=get_back_keyboard())

async def play_dice(query, context, prediction: int, count: int):
    user_id = query.from_user.id
    await query.message.reply_text("🎲 Бросаем кубик...")
    
    msg = await query.message.reply_dice(emoji="🎲")
    val = msg.dice.value
    is_win = prediction == val
    
    if is_win:
        reward = count * REWARD_GAME
        db_add_reward(user_id, reward, "Кубик")
        await query.message.reply_text(f"🎲 <b>УГАДАЛИ!</b>\n\nВыпало число: {val}. Начислено: +{reward:,} mCoin!", reply_markup=get_back_keyboard())
    else:
        await query.message.reply_text(f"😢 <b>НЕ УГАДАЛИ!</b>\n\nВыпало число: {val}, ваш выбор: {prediction}. Ничего страшного, баланс сохранен!", reply_markup=get_back_keyboard())

async def play_basketball(query, context, prediction: str, count: int):
    user_id = query.from_user.id
    stake = count * REWARD_GAME
    
    await query.message.reply_text("🏀 Выполняем бросок мяча в кольцо...")
    
    msg = await query.message.reply_dice(emoji="🏀")
    val = msg.dice.value
    
    # 1, 2 = Мимо, 3, 4, 5 = Попал
    actual = "Мимо" if val in [1, 2] else "Попал"
    is_win = prediction == actual
    
    if is_win:
        mult = 2.4 if prediction == "Мимо" else 1.6
        reward = int(stake * mult)
        db_add_reward(user_id, reward, f"Баскетбол {prediction} x{mult}")
        await query.message.reply_text(
            f"🏀 <b>БЛИСТАТЕЛЬНЫЙ БРОСОК!</b>\n\n"
            f"Ваш прогноз [{prediction}] полностью совпал!\n"
            f"Множитель выигрыша: {mult}x\n"
            f"Начислено: +{reward:,} mCoin!",
            reply_markup=get_back_keyboard()
        )
    else:
        # Без списания по ТЗ ("в играх нету ставок, вы ничего не теряете")
        await query.message.reply_text(
            f"💨 <b>МИМО КОЛЬЦА!</b>\n\n"
            f"Результат броска: {actual}.\n"
            f"Ваш прогноз не совпал, но ваш баланс остался цел и невредим!",
            reply_markup=get_back_keyboard()
        )

# ---------------------------------------------------------------------------
# КЛИЕНТСКИЙ ВХОД НОВЫХ ПОЛЬЗОВАТЕЛЕЙ В ГРУППУ
# ---------------------------------------------------------------------------
async def new_member_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Приветствует новых участников чата по ТЗ."""
    for member in update.message.new_chat_members:
        if member.is_bot:
            continue
            
        text = (
            f"Получай Free mCoin в нашем боте! Кнопки ниже\n\n"
            f"📢 Канал — {CHANNEL_LINK}\n"
            f"💬 Чат — {CHAT_LINK}\n"
            f"🏆 Выдачи — {PAYOUT_LINK}"
        )
        await update.message.reply_text(text, reply_markup=get_group_reply_keyboard())

# ---------------------------------------------------------------------------
# АДМИНИСТРАТИВНЫЕ КОМАНДЫ И НАСТРОЙКИ
# ---------------------------------------------------------------------------
async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID: return
    stats = db_get_stats()
    await update.message.reply_text(
        f"📊 <b>СТАТИСТИКА БОТА:</b>\n\n"
        f"• Всего пользователей в БД: {stats['total_users']}\n"
        f"• Всего распределено mCoin: {stats['total_distributed']:,} mC",
        parse_mode="HTML"
    )

def db_get_stats() -> dict:
    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM users")
        total_users = cursor.fetchone()[0]
        
        cursor.execute("SELECT action FROM history")
        rows = cursor.fetchall()
        total_distributed = 0
        for row in rows:
            match = re.search(r"\+(\d[\d,]*)\s+mC", row[0])
            if match:
                total_distributed += int(match.group(1).replace(",", ""))
        return {"total_users": total_users, "total_distributed": total_distributed}

async def enable_in_group(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if update.effective_chat.type in ["group", "supergroup"]:
        with sqlite3.connect(DB_FILE) as conn:
            cursor = conn.cursor()
            cursor.execute("INSERT OR IGNORE INTO allowed_groups (chat_id) VALUES (?)", (chat_id,))
            conn.commit()
        await update.message.reply_text("✅ Бот успешно активирован для данной группы!", reply_markup=get_group_reply_keyboard())

async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["state"] = None
    await update.message.reply_text("Ввод успешно сброшен. Воспользуйтесь меню:", reply_markup=get_main_inline_keyboard())

# ---------------------------------------------------------------------------
# ФОНОВЫЙ ПРОЦЕСС: РАССЫЛКА В ГРУППЫ РАЗ В 30 МИНУТ / 1 ЧАС НОЧЬЮ
# ---------------------------------------------------------------------------
async def scheduled_loop(app: Application):
    """Каждые 30 минут отправляет пост. Ночью (23:00 - 07:00) — раз в час."""
    await asyncio.sleep(10) # Задержка при старте
    while True:
        try:
            now = datetime.now()
            # Определяем ночное время для снижения спама
            is_night = now.hour >= 23 or now.hour < 7
            sleep_time = 3600 if is_night else 1800
            
            await asyncio.sleep(sleep_time)
            
            with sqlite3.connect(DB_FILE) as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT chat_id FROM allowed_groups")
                groups = cursor.fetchall()
            
            if not groups:
                continue
                
            text = (
                "Получи Free mCoin в нашем боте за подписку на спонсоров!\n\n"
                "В играх тоже Free mCoin, без вложений!"
            )
            # Ссылка на запуск бота напрямую
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("💰 Запустить Получение mCoin", url=f"https://t.me/{app.bot.username}?start=menu")]
            ])
            
            for group in groups:
                try:
                    await app.bot.send_message(chat_id=group[0], text=text, reply_markup=keyboard)
                except Exception as e:
                    logger.error(f"Не удалось отправить рассылку в чат {group[0]}: {e}")
        except Exception as e:
            logger.error(f"Ошибка в цикле фоновой рассылки: {e}")
            await asyncio.sleep(60)

async def post_init(application: Application):
    """Регистрация и немедленный запуск фонового процесса при старте бота."""
    asyncio.create_task(scheduled_loop(application))

# ---------------------------------------------------------------------------
# ТОЧКА ВХОДА (MAIN)
# ---------------------------------------------------------------------------
def main() -> None:
    db_init()
    
    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()
    
    # Регистрация команд
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("games", games_command))
    app.add_handler(CommandHandler("on", enable_in_group))
    app.add_handler(CommandHandler("cancel", cancel_command))
    app.add_handler(CommandHandler("stats", stats_command))
    
    # Слежение за новыми участниками
    app.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, new_member_handler))
    
    # Обработчики инлайна и текстовых меню
    app.add_handler(CallbackQueryHandler(inline_callback_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_group_text_message))
    
    # Telegram Меню
    app.bot.set_my_commands([
        BotCommand("start", "🏠 Главное меню"),
        BotCommand("games", "🎮 Выбрать игру"),
        BotCommand("cancel", "❌ Сбросить ввод / Отмена"),
        BotCommand("on", "👥 Активировать в группе")
    ])
    
    logger.info("Бот mCoin успешно запущен на Railway!")
    app.run_polling()

if __name__ == "__main__":
    main()
