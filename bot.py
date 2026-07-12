import os
import sqlite3
import logging
import httpx
import random
from datetime import datetime, timedelta
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters,
    ContextTypes
)

# ---------------------------------------------------------------------------
# КОНФИГУРАЦИЯ БОТА И ИНТЕГРАЦИЙ
# ---------------------------------------------------------------------------
BOT_TOKEN = "8812638330:AAEu-BtSmKYbehlP4YEbLnE-CGx3AoSIjbg"
REPORT_CHANNEL = "@mCoin_rep"  # Канал для отправки отчетов о начислениях
ADMIN_ID = 8685397478     # Твой Telegram ID для админки и рассылок

# Настройки тарифов
REWARD_SPONSOR = 200000  # 200k за одного спонсора
REWARD_GAME = 150000     # 150k базовая ставка за игру на спонсора
REFERRAL_REWARD = 500000 # 500k владельцу рефки при успешной проверке

# Настройки PiarFlow API (Сюда ты вставишь свои рабочие ключи)
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
# РАБОТА С БАЗОЙ ДАННЫХ SQLite (С поддержкой миграции полей)
# ---------------------------------------------------------------------------
def db_init():
    """Инициализация базы данных и автоматическое добавление колонок."""
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
        
        # Попытка добавить новые колонки, если БД уже существовала
        try:
            cursor.execute("ALTER TABLE users ADD COLUMN last_game_time TEXT")
        except sqlite3.OperationalError:
            pass # Колонка уже существует
            
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
    """Безопасное начисление или списание средств с записью в лог."""
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
    """
    Запрос к API PiarFlow для получения списка активных спонсорских каналов.
    Если API недоступно, возвращает резервный мок-список каналов.
    """
    try:
        async with httpx.AsyncClient() as client:
            # Реальный запрос к API (раскомментировать при установке токена)
            # r = await client.get(f"{PIARFLOW_API_URL}/get_sponsors?api_key={PIARFLOW_API_KEY}&limit={count}", timeout=5.0)
            # if r.status_code == 200:
            #     return r.json().get("channels", [])[:count]
            pass
    except Exception as e:
        logger.error(f"Ошибка запроса к PiarFlow API: {e}")
    
    # Резервные каналы (мок-данные)
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
    
    return True # Резервное значение успеха для локального тестирования

# ---------------------------------------------------------------------------
# СБОРКА ИНЛАЙН КЛАВИАТУР (ГЛАВНЫЕ ЭКРАНЫ)
# ---------------------------------------------------------------------------
def get_main_inline_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("💰 Получить mCoin", callback_data="menu_get_mcoin")],
        [
            InlineKeyboardButton("🎯 Дартс", callback_data="game_darts"),
            InlineKeyboardButton("🎲 Кубик", callback_data="game_dice"),
            InlineKeyboardButton("🏀 Баскетбол", callback_data="game_basketball")
        ],
        [
            InlineKeyboardButton("📜 История", callback_data="menu_history"),
            InlineKeyboardButton("👥 Рефералы", callback_data="menu_ref")
        ]
    ])

def get_back_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Вернуться в Главное Меню", callback_data="go_to_home")]])

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
    
    # Сценарий Реферала: Запуск по реф-ссылке в первый раз
    if not db_user and referred_by:
        db_user = db_create_user(user.id, user.username, referred_by)
        
        # Сразу принудительно показываем приветственных спонсоров (не за mCoin)
        welcome_sponsors = await get_piarflow_sponsors(3)
        context.user_data["ref_welcome_sponsors"] = welcome_sponsors
        
        links_text = "\n".join([f"🔗 <a href='{s['link']}'>{s['name']}</a>" for s in welcome_sponsors])
        text = (
            f"👋 <b>Добро пожаловать в бота, {user.first_name}!</b>\n\n"
            f"Вы перешли по реферальной ссылке. Чтобы разблокировать доступ к заработку и играм, "
            f"вам необходимо обязательно подписаться на наших спонсоров:\n\n{links_text}\n\n"
            f"После подписки нажмите кнопку проверки ниже 👇"
        )
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Проверить подписки", callback_data="check_ref_welcome_subs")]
        ])
        await update.message.reply_text(text, reply_markup=keyboard, parse_mode="HTML", disable_web_page_preview=True)
        return

    # Обычный запуск
    if not db_user:
        db_user = db_create_user(user.id, user.username)
        
    text = (
        f"<b>🤖 mCoin Hub приветствует тебя, {user.first_name}!</b>\n"
        f"═" * 30 + "\n"
        f"Это продвинутый бот для активного и развлекательного заработка mCoin.\n\n"
        f"Выполняйте простые задания подписки, играйте в интерактивные игры и выводите валюту!"
    )
    await update.message.reply_text(text, reply_markup=get_main_inline_keyboard(), parse_mode="HTML")

async def games_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Вызов списка игр напрямую текстом или по кнопке."""
    text = (
        "<b>🎮 ИГРОВОЙ СЕКТОР mCoin</b>\n"
        "═" * 30 + "\n"
        "Выберите игру для проверки удачи. \n"
        "⚠️ <i>Правило: принять участие в любой игре можно строго раз в 10 минут!</i>"
    )
    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🎯 Дартс", callback_data="game_darts"),
            InlineKeyboardButton("🎲 Кубик", callback_data="game_dice"),
            InlineKeyboardButton("🏀 Баскетбол", callback_data="game_basketball")
        ],
        [InlineKeyboardButton("◀️ В меню", callback_data="go_to_home")]
    ])
    await update.message.reply_text(text, reply_markup=keyboard, parse_mode="HTML")

# ---------------------------------------------------------------------------
# КУЛДАУН СИСТЕМА
# ---------------------------------------------------------------------------
def is_on_cooldown(user_id: int) -> tuple[bool, int]:
    """Проверяет, находится ли пользователь на кулдауне (10 минут)."""
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
# ОБРАБОТКА НАЖАТИЙ НА ИНЛАЙН-КНОПКИ
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

    # Возврат на главный экран
    if data == "go_to_home":
        text = (
            f"<b>🤖 mCoin Hub приветствует тебя, {query.from_user.first_name}!</b>\n"
            f"═" * 30 + "\n"
            f"Вы находитесь на главном экране управления активами. Выберите желаемое действие:"
        )
        await query.edit_message_text(text, reply_markup=get_main_inline_keyboard(), parse_mode="HTML")
        return

    # Проверка обязательных реф-подписок
    elif data == "check_ref_welcome_subs":
        welcome_sponsors = context.user_data.get("ref_welcome_sponsors", [])
        is_subscribed = await check_piarflow_subscriptions(user_id, welcome_sponsors)
        
        if is_subscribed:
            # Начисляем награду пригласившему
            referrer_id = user_data.get("referred_by")
            if referrer_id and user_data.get("ref_reward_paid") == 0:
                db_add_reward(referrer_id, REFERRAL_REWARD, f"Реферал {user_id} прошел проверку")
                db_update_user(user_id, {"ref_reward_paid": 1})
                
                # Оповещаем пригласившего
                try:
                    await context.bot.send_message(
                        chat_id=referrer_id,
                        text=f"🎁 <b>Реферальное начисление!</b>\nВаш приглашенный друг успешно выполнил условия. Вам зачислено +{REFERRAL_REWARD:,} mCoin."
                    )
                except Exception:
                    pass
            
            await query.edit_message_text(
                "🎉 <b>Проверка успешно пройдена!</b>\nВсе функции бота разблокированы. Приятной игры!",
                reply_markup=get_main_inline_keyboard(),
                parse_mode="HTML"
            )
        else:
            await query.message.reply_text("❌ Ошибка: вы подписались не на все каналы из списка. Попробуйте еще раз.")
        return

    # Заработок mCoin
    elif data == "menu_get_mcoin":
        context.user_data["state"] = "awaiting_mcoin_sponsors"
        await query.edit_message_text(
            "💰 <b>Раздел заработка mCoin</b>\n\n"
            "Напишите в ответном сообщении количество спонсоров, на которых вы хотите подписаться.\n"
            "<i>(Или отправьте команду /cancel для отмены действия)</i>",
            parse_mode="HTML"
        )
        return

    # Показ истории
    elif data == "menu_history":
        history = db_get_history(user_id)
        if not history:
            text = "Ваша история транзакций пока абсолютно пуста."
        else:
            text = "📜 <b>Ваша история начислений:</b>\n\n"
            for idx, (action, timestamp) in enumerate(history, 1):
                text += f"{idx}. {action} — <i>{timestamp}</i>\n"
        await query.edit_message_text(text, reply_markup=get_back_keyboard(), parse_mode="HTML")
        return

    # Меню рефералов
    elif data == "menu_ref":
        bot_info = await context.bot.get_me()
        link = f"https://t.me/{bot_info.username}?start=ref_{user_id}"
        text = (
            f"<b>👥 РЕФЕРАЛЬНАЯ СИСТЕМА</b>\n"
            f"═" * 30 + "\n"
            f"Приглашайте новых участников и расширяйте влияние!\n\n"
            f"🎁 <b>Ваша награда:</b> за каждого приведенного друга вы получите <code>{REFERRAL_REWARD:,} mCoin</code>, "
            f"как только он подпишется на обязательных приветственных спонсоров.\n\n"
            f"🔗 <b>Ваша партнерская ссылка:</b>\n<code>{link}</code>"
        )
        await query.edit_message_text(text, reply_markup=get_back_keyboard(), parse_mode="HTML")
        return

    # Проверка кулдауна на игры перед дальнейшими шагами
    on_cooldown, rem_seconds = is_on_cooldown(user_id)
    if on_cooldown and data.startswith("game_"):
        min_rem = rem_seconds // 60
        sec_rem = rem_seconds % 60
        await query.message.reply_text(
            f"⏳ <b>Доступ заблокирован!</b>\n\nВы сможете сыграть в игры снова через "
            f"<code>{min_rem:02d} мин. {sec_rem:02d} сек.</code>"
        )
        return

    # Запуск Игр
    if data == "game_darts":
        context.user_data["state"] = "awaiting_darts_sponsors"
        await query.edit_message_text("🎯 <b>Игра ДАРТС</b>\n\nВведите число спонсоров, на которых готовы подписаться ради участия:")
        
    elif data == "game_dice":
        context.user_data["state"] = "awaiting_dice_sponsors"
        await query.edit_message_text("🎲 <b>Игра КУБИК</b>\n\nВведите число спонсоров, на которых готовы подписаться ради участия:")
        
    elif data == "game_basketball":
        context.user_data["state"] = "awaiting_basketball_sponsors"
        await query.edit_message_text("🏀 <b>Игра БАСКЕТБОЛ</b>\n\nВведите число спонсоров, на которых готовы подписаться ради участия:")

    # Обработка игровых выборов прогноза
    elif data.startswith("predict_"):
        parts = data.split("_")
        game_type = parts[1] # "darts", "dice", "basket"
        prediction = parts[2] # выбранный вариант
        sponsors_count = context.user_data.get("game_sponsors_count", 1)
        
        # Обновляем время последней игры (навешиваем кулдаун)
        db_update_user(user_id, {"last_game_time": datetime.now().isoformat()})
        
        if game_type == "darts":
            await play_darts(query, context, prediction, sponsors_count)
        elif game_type == "dice":
            await play_dice(query, context, int(prediction), sponsors_count)
        elif game_type == "basket":
            await play_basketball(query, context, prediction, sponsors_count)

# ---------------------------------------------------------------------------
# ОБРАБОТЧИК ТЕКСТОВЫХ СООБЩЕНИЙ И ВВОДА ДАННЫХ (STATE MACHINE)
# ---------------------------------------------------------------------------
async def handle_text_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    text = update.message.text.strip()
    state = context.user_data.get("state")

    if not state:
        return

    # Обработка отмены
    if text == "/cancel":
        context.user_data["state"] = None
        await update.message.reply_text("❌ Ввод отменен. Возвращаю вас на главный экран.", reply_markup=get_main_inline_keyboard())
        return

    # Ввод числа спонсоров для начисления
    if state == "awaiting_mcoin_sponsors":
        try:
            count = int(text)
            if count < 1: raise ValueError
        except ValueError:
            await update.message.reply_text("Введите корректное положительное число или используйте /cancel.")
            return
            
        context.user_data["state"] = None
        sponsors = await get_piarflow_sponsors(count)
        links_text = "\n".join([f"{idx}. 🔗 <a href='{s['link']}'>{s['name']}</a>" for idx, s in enumerate(sponsors, 1)])
        
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Проверить подписки", callback_data=f"check_sub_{count}")]
        ])
        await update.message.reply_text(
            f"📋 <b>Для получения mCoin подпишитесь на следующих спонсоров:</b>\n\n{links_text}",
            reply_markup=keyboard, parse_mode="HTML", disable_web_page_preview=True
        )

    # Ввод числа спонсоров для Дартса
    elif state == "awaiting_darts_sponsors":
        try:
            count = int(text)
            if count < 1: raise ValueError
        except ValueError:
            await update.message.reply_text("Введите корректное число спонсоров.")
            return
            
        context.user_data["game_sponsors_count"] = count
        context.user_data["state"] = None
        
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("Центр 🎯", callback_data="predict_darts_Центр"), InlineKeyboardButton("Белое ⚪", callback_data="predict_darts_Белое")],
            [InlineKeyboardButton("Красное 🔴", callback_data="predict_darts_Красное"), InlineKeyboardButton("Мимо 💨", callback_data="predict_darts_Мимо")]
        ])
        await update.message.reply_text("🎯 Сделайте прогноз, куда попадет дротик:", reply_markup=keyboard)

    # Ввод числа спонсоров для Кубика
    elif state == "awaiting_dice_sponsors":
        try:
            count = int(text)
            if count < 1: raise ValueError
        except ValueError:
            await update.message.reply_text("Введите корректное число спонсоров.")
            return
            
        context.user_data["game_sponsors_count"] = count
        context.user_data["state"] = None
        
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("1", callback_data="predict_dice_1"), InlineKeyboardButton("2", callback_data="predict_dice_2"), InlineKeyboardButton("3", callback_data="predict_dice_3")],
            [InlineKeyboardButton("4", callback_data="predict_dice_4"), InlineKeyboardButton("5", callback_data="predict_dice_5"), InlineKeyboardButton("6", callback_data="predict_dice_6")]
        ])
        await update.message.reply_text("🎲 Выберите число, которое выпадет на кубике:", reply_markup=keyboard)

    # Ввод числа спонсоров для Баскетбола
    elif state == "awaiting_basketball_sponsors":
        try:
            count = int(text)
            if count < 1: raise ValueError
        except ValueError:
            await update.message.reply_text("Введите корректное число спонсоров.")
            return
            
        # Для игры с возможностью слива проверяем, есть ли у пользователя mCoin на балансе для "ставки"
        required_stake = count * REWARD_GAME
        user_db = db_get_user(user_id)
        if user_db["balance"] < required_stake:
            await update.message.reply_text(
                f"❌ <b>Недостаточно средств на балансе!</b>\n\n"
                f"Для игры с {count} спонсорами ставка составляет <code>{required_stake:,} mC</code>. "
                f"Ваш текущий баланс: <code>{user_db['balance']:,} mC</code>.\n"
                f"Введите меньшее число спонсоров или /cancel."
            )
            return

        context.user_data["game_sponsors_count"] = count
        context.user_data["state"] = None
        
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("Попал 🏀 (x1.6)", callback_data="predict_basket_Попал")],
            [InlineKeyboardButton("Мимо 💨 (x2.4)", callback_data="predict_basket_Мимо")]
        ])
        await update.message.reply_text("🏀 Каков ваш прогноз на бросок мяча?", reply_markup=keyboard)

# ---------------------------------------------------------------------------
# ЛОГИКА ИГРОВЫХ СЕССИЙ
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
        await query.message.reply_text(f"🎉 <b>ПОБЕДА!</b>\nВыпало: {actual}. Начислено: +{reward:,} mCoin!", reply_markup=get_back_keyboard())
    else:
        await query.message.reply_text(f"💨 <b>Промах!</b>\nВыпало: {actual}, а ставили на: {prediction}.", reply_markup=get_back_keyboard())

async def play_dice(query, context, prediction: int, count: int):
    user_id = query.from_user.id
    await query.message.reply_text("🎲 Бросаем кубик...")
    
    msg = await query.message.reply_dice(emoji="🎲")
    val = msg.dice.value
    is_win = prediction == val
    
    if is_win:
        reward = count * REWARD_GAME
        db_add_reward(user_id, reward, "Кубик")
        await query.message.reply_text(f"🎉 <b>УГАДАЛИ!</b>\nВыпало число: {val}. Начислено: +{reward:,} mCoin!", reply_markup=get_back_keyboard())
    else:
        await query.message.reply_text(f"😢 <b>Не угадали!</b>\nВыпало: {val}, ваш выбор: {prediction}.", reply_markup=get_back_keyboard())

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
            f"🎉 <b>БЛИСТАТЕЛЬНО! Прогноз {prediction} подтвержден!</b>\n\n"
            f"Ваш множитель: {mult}x\n"
            f"Выигрыш составил: +{reward:,} mCoin!",
            reply_markup=get_back_keyboard()
        )
    else:
        # Списание при проигрыше
        db_add_reward(user_id, -stake, "Проигрыш в Баскетбол")
        await query.message.reply_text(
            f"💀 <b>УВЫ! Мяч полетел по другой траектории!</b>\n\n"
            f"Результат: {actual}\n"
            f"С вашего баланса списана ставка: -{stake:,} mCoin.",
            reply_markup=get_back_keyboard()
        )

# ---------------------------------------------------------------------------
# АДМИНИСТРАТИВНЫЙ ФУНКЦИОНАЛ
# ---------------------------------------------------------------------------
async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID: return
    stats = db_get_stats()
    await update.message.reply_text(
        f"📊 <b>СТАТИСТИКА БОТА:</b>\n\n"
        f"• Всего пользователей: {stats['total_users']}\n"
        f"• Выдано через игры: {stats['total_distributed']:,} mC",
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
            # Извлекаем сумму начисления
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
        await update.message.reply_text("✅ Бот успешно активирован для данной группы!", reply_markup=get_main_inline_keyboard())

async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Сброс состояния принудительно командой /cancel."""
    context.user_data["state"] = None
    await update.message.reply_text("Ввод сброшен. Используйте меню:", reply_markup=get_main_inline_keyboard())

# ---------------------------------------------------------------------------
# ЗАПУСК БОТА (MAIN)
# ---------------------------------------------------------------------------
def main() -> None:
    # Инициализируем БД
    db_init()
    
    app = Application.builder().token(BOT_TOKEN).build()
    
    # Регистрация команд
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("games", games_command))
    app.add_handler(CommandHandler("on", enable_in_group))
    app.add_handler(CommandHandler("cancel", cancel_command))
    app.add_handler(CommandHandler("stats", stats_command))
    
    # Inline кнопки
    app.add_handler(CallbackQueryHandler(inline_callback_handler))
    
    # Чтение текстового ввода пользователя
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_input))
    
    # Telegram Меню
    app.bot.set_my_commands([
        BotCommand("start", "🏠 В меню"),
        BotCommand("games", "🎮 Выбрать игру"),
        BotCommand("cancel", "❌ Сбросить ввод"),
        BotCommand("on", "👥 Активировать в группе")
    ])
    
    logger.info("Бот Аванпоста успешно запущен на Railway!")
    app.run_polling()

if __name__ == "__main__":
    main()
