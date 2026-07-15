import os
import asyncio
import logging
import random
import uuid
import secrets
import base64
import urllib.parse
from datetime import datetime, timedelta
import asyncpg
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.types import LabeledPrice
from dotenv import load_dotenv

load_dotenv()

# ---------------------------------------------------------------------------
# КОНФИГУРАЦИЯ БОТА И ОПЛАТЫ
# ---------------------------------------------------------------------------
BOT_TOKEN = os.getenv("BOT_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL") 
ADMIN_ID = int(os.getenv("ADMIN_ID", 0)) # ID твоего аккаунта для получения заявок

# Инструкция для P2P-оплаты
P2P_PAYMENT_INSTRUCTIONS = (
    "1️⃣ *Переведите игровую валюту на реквизиты администрации:*\n"
    "🔗 **Если вы передаете валюту ссылкой или показом скриншота, то в сообщении укажите игру/бота и подобное.\n"
    "💳 **ЮMoney** [4100119121976236]\n"
    "🎮 **Вы можете отправить оплату, как ссылкой так и напрямую в игре**\n"
    "💬 **Связаться с админом напрямую:** @nmproda \n\n"
    "2️⃣ **ОТПРАВЬТЕ ПОДТВЕРЖДЕНИЕ СЮДА (БОТУ):**\n"
    "Пришлите скриншот перевода (как фото) или ссылку на трейд/ваш ник в ответном сообщении.\n\n"
    "⚠️ *Напоминание по курсу:* Админ проверит поступление, умножит количество полученной валюты на курс Telegram Stars (коэффициент **1.40**) и начислит монеты NMP на ваш баланс.\n\n"
    "⏳ *Срок проверки заявки:* от 5 до 30 минут."
)

CHECK_IMAGE_URL = "https://i.ibb.co/tMRTCg7c/IMG-20260714-004428-315.jpg"
DEFAULT_NFT_IMAGE = "https://i.postimg.cc/85zXfM7h/nft-placeholder.png"

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
db_pool = None

# Стейты FSM
class Form(StatesGroup):
    waiting_for_promo = State()
    waiting_admin_user_id = State()
    waiting_admin_amount = State()
    waiting_admin_promo_code = State()
    waiting_admin_promo_reward = State()
    waiting_admin_promo_uses = State() # Стейт для лимита активаций промо
    waiting_admin_search_user = State() # Стейт для поиска игрока админом
    waiting_check_amount = State()
    waiting_check_claims = State()
    waiting_custom_handle = State()
    waiting_deposit_amount = State()
    waiting_p2p_user_proof = State() # Состояние ожидания скрина или текста от юзера

# Инициализация пула БД PostgreSQL и создание таблицы P2P-заявок
async def init_db():
    global db_pool
    db_pool = await asyncpg.create_pool(dsn=DATABASE_URL)
    async with db_pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS p2p_deposits (
                id SERIAL PRIMARY KEY,
                user_id BIGINT NOT NULL,
                game VARCHAR(50),
                amount NUMERIC,
                comment TEXT,
                status VARCHAR(20) DEFAULT 'pending',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        try:
            await conn.execute("ALTER TABLE users ADD COLUMN last_interest_accrued TIMESTAMP DEFAULT NOW()")
        except asyncpg.exceptions.DuplicateColumnError:
            pass

# ---------------------------------------------------------------------------
# ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ (NFT, СТАТИСТИКА)
# ---------------------------------------------------------------------------
async def get_user_boosts(conn, user_id: int):
    row = await conn.fetchrow(
        """SELECT COALESCE(SUM(n.boost_staking_pct), 0) as stake_boost, 
                  COALESCE(SUM(n.boost_cashback_pct), 0) as cash_boost 
           FROM user_nfts un 
           JOIN nfts n ON un.nft_id = n.id 
           WHERE un.user_id = $1""", user_id
    )
    return float(row['stake_boost']), float(row['cash_boost'])

async def get_user_monthly_stats(conn, user_id: int):
    spent = await conn.fetchval(
        "SELECT ABS(COALESCE(SUM(amount), 0)) FROM transactions WHERE user_id = $1 AND amount < 0 AND created_at >= NOW() - INTERVAL '30 days'",
        user_id
    )
    earned = await conn.fetchval(
        "SELECT COALESCE(SUM(amount), 0) FROM transactions WHERE user_id = $1 AND amount > 0 AND tx_type != 'welcome' AND created_at >= NOW() - INTERVAL '30 days'",
        user_id
    )
    return float(earned), float(spent)

# ---------------------------------------------------------------------------
# ОБРАБОТЧИК /START И ГЛУБОКИХ ССЫЛОК
# ---------------------------------------------------------------------------
@dp.message(CommandStart())
async def cmd_start(message: types.Message):
    user_id = message.from_user.id
    username = message.from_user.username or "User"
    
    args = message.text.split()
    referrer_id = None
    check_code_to_claim = None
    
    # 1. Проверяем и создаем пользователя в БД
    async with db_pool.acquire() as conn:
        user = await conn.fetchrow("SELECT * FROM users WHERE telegram_id = $1", user_id)
        
        if not user:
            if len(args) > 1 and args[1].isdigit():
                referrer_id = int(args[1])
                if referrer_id == user_id:
                    referrer_id = None

            await conn.execute(
                "INSERT INTO users (telegram_id, username, referrer_id, balance, last_interest_accrued) VALUES ($1, $2, $3, 100.00, NOW())",
                user_id, username, referrer_id
            )
            await conn.execute(
                "INSERT INTO transactions (user_id, amount, tx_type, description) VALUES ($1, 100.00, 'welcome', 'Приветственный бонус')",
                user_id
            )
            
            if referrer_id:
                await conn.execute("UPDATE users SET balance = balance + 27 WHERE telegram_id = $1", referrer_id)
                await conn.execute(
                    "INSERT INTO transactions (user_id, amount, tx_type, description) VALUES ($1, 27.00, 'referral', 'Реферальный бонус за друга')",
                    referrer_id
                )
                try:
                    await bot.send_message(referrer_id, f"🎉 По вашей ссылке зарегистрировался @{username}! Вам зачислено +27 NMP.")
                except: pass
            
            welcome_text = "🏦 *Добро пожаловать в NMVal Bank!*\n\nЛичный кабинет успешно открыт. На ваш баланс начислено приветственные **100.00 NMP**!"
        else:
            welcome_text = f"🏦 *С возвращением в NMVal Bank, @{username}!*"

    # 2. Обработка глубоких переходов (Payloads)
    if len(args) > 1:
        payload = args[1]
        
        # ОБРАБОТКА ИГРОВЫХ P2P ДЕПОЗИТОВ С САЙТА
        if payload.startswith("dep_p2p_"):
            try:
                parts = payload.split("_")
                game = parts[2]
                amount = float(parts[4])
                encoded_comment = parts[6]
                encoded_comment += "=" * ((4 - len(encoded_comment) % 4) % 4)
                decoded_bytes = base64.b64decode(encoded_comment)
                comment = urllib.parse.unquote(decoded_bytes.decode('utf-8'))
            except Exception as e:
                logging.error(f"Ошибка P2P парсинга: {e}")
                await message.reply("❌ Ошибка обработки ссылки на пополнение. Попробуйте еще раз из Mini App.")
                return

            async with db_pool.acquire() as conn:
                dep_id = await conn.fetchval(
                    "INSERT INTO p2p_deposits (user_id, game, amount, comment, status) VALUES ($1, $2, $3, $4, 'pending') RETURNING id",
                    user_id, game, amount, comment
                )

            # Выдаем пошаговую инструкцию
            await message.answer(
                f"📝 *Заявка на P2P-пополнение (Заявка #{dep_id}) создана!*\n\n"
                f"🎮 Игра: `{game.upper()}`\n"
                f"💰 Сумма перевода: `{amount} ед. валюты`\n"
                f"📝 Ваш коммент с сайта: `{comment}`\n\n"
                f"{P2P_PAYMENT_INSTRUCTIONS.format(admin_id=ADMIN_ID)}\n\n"
                f"👉 **Отправьте скриншот оплаты (как фото) или текст-подтверждение прямо сюда, ответным сообщением:**",
                parse_mode="Markdown"
            )
            await Form.waiting_p2p_user_proof.set()
            return

        # ОПЛАТА ЧЕРЕЗ TELEGRAM STARS
        elif payload.startswith("pay_stars_"):
            try:
                amount = float(payload.split("_")[2])
                stars_cost = int(amount / 3)
                await message.reply(f"💳 Выставляем счет за {amount} NMP...")
                await bot.send_invoice(
                    chat_id=message.chat.id,
                    title=f"Покупка {amount} NMP",
                    description=f"Пакет пополнения NMP для личного счета",
                    payload=f"buy_nmp_{int(amount)}",
                    provider_token="",
                    currency="XTR",
                    prices=[LabeledPrice(label="NMP Stars Pack", amount=stars_cost)]
                )
            except Exception as e:
                logging.error(f"Ошибка Stars: {e}")
                await message.reply("❌ Не удалось сгенерировать счет Stars.")
            return

        elif payload.startswith("c_"):
            check_code_to_claim = payload
            await claim_check_logic(message, check_code_to_claim)
            return

    await send_main_menu(message.chat.id, welcome_text)

# Функция отправки главного меню
async def send_main_menu(chat_id: int, text: str):
    builder = InlineKeyboardBuilder()
    builder.button(text="💳 Мой Кабинет", callback_data="my_account")
    builder.button(text="🎁 Маркетплейс NFT", callback_data="shop_nfts")
    builder.button(text="💸 Чеки", callback_data="checks_menu")
    builder.button(text="📈 Депозиты (Сейвинг)", callback_data="deposits_menu")
    builder.button(text="🎰 Колесо Фортуны", callback_data="wheel_spin")
    builder.button(text="👤 Никнейм Счета (*)", callback_data="custom_handle_menu")
    builder.button(text="👥 Партнерам (API)", callback_data="merchant_api")
    builder.button(text="🎫 Промокод", callback_data="promo_activate")
    
    if chat_id == ADMIN_ID:
        builder.button(text="⚙️ Админка", callback_data="admin_panel")
        
    builder.adjust(2, 2, 2, 2)
    await bot.send_message(chat_id, text, reply_markup=builder.as_markup(), parse_mode="Markdown")

# ---------------------------------------------------------------------------
# ОБРАБОТЧИК ПОЛУЧЕНИЯ ПОДТВЕРЖДЕНИЯ P2P ОТ ПОЛЬЗОВАТЕЛЯ
# ---------------------------------------------------------------------------
@dp.message(Form.waiting_p2p_user_proof, F.text | F.photo)
async def process_p2p_user_proof(message: types.Message, state: FSMContext):
    user_id = message.from_user.id
    username = message.from_user.username or f"ID:{user_id}"
    
    async with db_pool.acquire() as conn:
        last_pending = await conn.fetchrow(
            "SELECT id FROM p2p_deposits WHERE user_id = $1 AND status = 'pending' ORDER BY created_at DESC LIMIT 1", user_id
        )
    
    dep_id = last_pending['id'] if last_pending else 'N/A'
    proof_text = message.text or (message.caption if message.caption else "Отправлен скриншот без описания")
    
    async with db_pool.acquire() as conn:
        await conn.execute("UPDATE p2p_deposits SET comment = $1 WHERE id = $2", proof_text, dep_id)

    # Строка уведомления админа
    admin_notification = (
        f"🔔 *НОВАЯ ЗАЯВКА НА P2P ПОДТВЕРЖДЕНИЕ!* (Заявка #{dep_id})\n"
        f"👤 *Пользователь:* @{username} (ID: `{user_id}`)\n\n"
        f"📝 *Подтверждение от юзера:*\n"
        f"» {proof_text}\n\n"
        f"Проверьте поступление игровой валюты. Для начисления монет NMP используйте команду:\n"
        f"`/give {user_id} [кол-во_NMP]`"
    )

    try:
        if message.photo:
            await bot.send_photo(ADMIN_ID, photo=message.photo[-1].file_id, caption=admin_notification, parse_mode="Markdown")
        else:
            await bot.send_message(ADMIN_ID, admin_notification, parse_mode="Markdown")
        
        await message.reply("✅ *Подтверждение отправлено на проверку!*\nАдминистратор проверит транзакцию и начислит баланс. Ожидайте уведомления.")
    except Exception as e:
        logging.error(f"Ошибка пересылки заявки админу: {e}")
        await message.reply("❌ Ошибка отправки. Свяжитесь напрямую с поддержкой.")
    
    await state.clear()

# ---------------------------------------------------------------------------
# ПОПОЛНЕНИЕ (STARS & ИГРЫ ИЗ БОТА)
# ---------------------------------------------------------------------------
@dp.callback_query(F.data == "buy_stars_menu")
async def cb_buy_stars_menu(callback: types.CallbackQuery):
    await callback.answer() # Убирает зависание
    text = (
        "📥 *Пополнение баланса NMP*\n\n"
        "Выберите удобный способ оплаты пакета валюты:"
    )
    builder = InlineKeyboardBuilder()
    builder.button(text="⭐️ Telegram Stars (Курс 1=3)", callback_data="stars_list")
    builder.button(text="🎮 Игровая валюта P2P (Любая игра)", callback_data="start_p2p_from_bot")
    builder.button(text="🔙 Назад", callback_data="my_account")
    builder.adjust(1)
    await callback.message.edit_text(text, reply_markup=builder.as_markup(), parse_mode="Markdown")

@dp.callback_query(F.data == "start_p2p_from_bot")
async def cb_start_p2p_from_bot(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer() # ИСПРАВЛЕНО: Теперь кнопка моментально нажимается и не зависает!
    user_id = callback.from_user.id
    async with db_pool.acquire() as conn:
        dep_id = await conn.fetchval(
            "INSERT INTO p2p_deposits (user_id, game, amount, comment, status) VALUES ($1, 'P2P_BOT', 0, 'Инициация из меню бота', 'pending') RETURNING id",
            user_id # ИСПРАВЛЕНО: Добавлен недостающий аргумент в SQL-запрос!
        )
    
    await state.set_state(Form.waiting_p2p_user_proof)
    await callback.message.edit_text(
        f"🎮 *ПОПОЛНЕНИЕ ИГРОВОЙ ВАЛЮТОЙ (P2P) - Заявка #{dep_id}*\n\n"
        f"{P2P_PAYMENT_INSTRUCTIONS.format(admin_id=ADMIN_ID)}\n\n"
        f"👉 **Отправьте скриншот оплаты (как фото) или текст-подтверждение прямо сюда, ответным сообщением:**",
        parse_mode="Markdown"
    )

@dp.callback_query(F.data == "my_account")
async def cb_my_account(callback: types.CallbackQuery):
    await callback.answer() # Снимает зависание кнопки
    user_id = callback.from_user.id
    async with db_pool.acquire() as conn:
        user = await conn.fetchrow("SELECT balance, custom_handle FROM users WHERE telegram_id = $1", user_id)
        stake_b, cash_b = await get_user_boosts(conn, user_id)
        earned, spent = await get_user_monthly_stats(conn, user_id)
        
    handle_str = f"🏷 Счет: `{user['custom_handle']}`" if user['custom_handle'] else f"💳 ID Счета: `{user_id}`"
    
    text = (
        f"🏦 *Ваш Личный Кабинет*\n\n"
        f"{handle_str}\n"
        f"💵 Баланс: `{user['balance']:.2f} NMP`\n\n"
        f"📊 *Статистика за 30 дней:*\n"
        f"📥 Заработано / Получено: `+{earned:.2f} NMP`\n"
        f"📤 Потрачено / Переведено: `-{spent:.2f} NMP`\n\n"
        f"📈 Стейкинг: `{9.2 + stake_b:.2f}%` в месяц\n"
        f"🛍 Кэшбэк: `{1.0 + cash_b:.2f}%` на все покупки"
    )
    
    builder = InlineKeyboardBuilder()
    builder.button(text="📥 Купить NMP (Stars / Игры)", callback_data="buy_stars_menu")
    builder.button(text="🔙 В меню", callback_data="main_menu")
    builder.adjust(1)
    await callback.message.edit_text(text, reply_markup=builder.as_markup(), parse_mode="Markdown")

@dp.callback_query(F.data == "main_menu")
async def cb_main_menu(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer() # Снимает зависание
    await state.clear()
    await callback.message.delete()
    await send_main_menu(callback.message.chat.id, "🏦 *Главный экран NMVal Bank:*")

# ---------------------------------------------------------------------------
# РАБОТА С STARS ИНВОЙСАМИ
# ---------------------------------------------------------------------------
@dp.callback_query(F.data == "stars_list")
async def cb_stars_list(callback: types.CallbackQuery):
    await callback.answer()
    builder = InlineKeyboardBuilder()
    builder.button(text="🪙 100 NMP (за 33 Stars)", callback_data="buy_pack:100:33")
    builder.button(text="🪙 300 NMP (за 100 Stars)", callback_data="buy_pack:300:100")
    builder.button(text="🪙 900 NMP (за 300 Stars)", callback_data="buy_pack:900:300")
    builder.button(text="🔙 Назад", callback_data="buy_stars_menu")
    builder.adjust(1)
    await callback.message.edit_text("Выберите пакет пополнения за Stars:", reply_markup=builder.as_markup())

@dp.callback_query(F.data.startswith("buy_pack:"))
async def cb_buy_pack(callback: types.CallbackQuery):
    await callback.answer()
    _, nmp_amount, stars_cost = callback.data.split(":")
    await bot.send_invoice(
        chat_id=callback.message.chat.id,
        title=f"Покупка {nmp_amount} NMP",
        description=f"Зачисление NMP на ваш банковский баланс.",
        payload=f"buy_nmp_{nmp_amount}",
        provider_token="",
        currency="XTR",
        prices=[LabeledPrice(label="NMP Coins", amount=int(stars_cost))]
    )

@dp.pre_checkout_query()
async def pre_checkout_query_handler(pre_checkout_query: types.PreCheckoutQuery):
    await bot.answer_pre_checkout_query(pre_checkout_query.id, ok=True)

@dp.message(F.successful_payment)
async def successful_payment_handler(message: types.Message):
    payload = message.successful_payment.invoice_payload
    nmp_to_add = int(payload.split("_")[2])
    user_id = message.from_user.id
    
    async with db_pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("UPDATE users SET balance = balance + $1 WHERE telegram_id = $2", nmp_to_add, user_id)
            await conn.execute(
                "INSERT INTO transactions (user_id, amount, tx_type, description) VALUES ($1, $2, 'stars_buy', 'Покупка валюты за Telegram Stars')",
                user_id, nmp_to_add
            )
            
    await message.reply(f"🎉 *Оплата подтверждена!*\nНа ваш баланс зачислено **+{nmp_to_add:.2f} NMP**!")

# ---------------------------------------------------------------------------
# КОМАНДА /GIVE И АДМИН-ФУНКЦИИ
# ---------------------------------------------------------------------------
@dp.message(Command("give"))
async def cmd_give_shortcut(message: types.Message):
    if message.from_user.id != ADMIN_ID: return
    
    args = message.text.split()
    if len(args) != 3:
        await message.reply("📖 Справка использования:\n`/give [ID_получателя] [количество_NMP]`")
        return
        
    try:
        target_uid = int(args[1])
        amount = float(args[2])
        if amount <= 0: raise ValueError()
    except ValueError:
        await message.reply("❌ Ошибка ввода. Сумма должна быть положительным числом.")
        return
        
    async with db_pool.acquire() as conn:
        async with conn.transaction():
            user_exists = await conn.fetchval("SELECT telegram_id FROM users WHERE telegram_id = $1", target_uid)
            if not user_exists:
                await message.reply("❌ Пользователь с таким ID не зарегистрирован в базе данных!")
                return
                
            await conn.execute("UPDATE users SET balance = balance + $1 WHERE telegram_id = $2", amount, target_uid)
            await conn.execute(
                "INSERT INTO transactions (user_id, amount, tx_type, description) VALUES ($1, $2, 'admin_direct', 'Начисление от администрации')",
                target_uid, amount
            )
            await conn.execute("UPDATE p2p_deposits SET status = 'approved' WHERE user_id = $1 AND status = 'pending'", target_uid)
            
    await message.reply(f"✅ Успешно зачислено **{amount:.2f} NMP** пользователю `{target_uid}`!")
    try:
        await bot.send_message(
            target_uid, 
            f"🎁 Администратор начислил на ваш личный баланс **+{amount:.2f} NMP**!"
        )
    except: pass

# --- КОЛЕСО ФОРТУНЫ (СТРОГО РАЗ В 24 ЧАСА ПО БАЗЕ ДАННЫХ) ---
@dp.callback_query(F.data == "wheel_spin")
async def cb_wheel_spin(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    async with db_pool.acquire() as conn:
        last_used = await conn.fetchval("SELECT daily_spin_last_used FROM users WHERE telegram_id = $1", user_id)
        
    now = datetime.now()
    if last_used and now - last_used.replace(tzinfo=None) < timedelta(days=1):
        remains = timedelta(days=1) - (now - last_used.replace(tzinfo=None))
        hours, remainder = divmod(remains.seconds, 3600)
        minutes, _ = divmod(remainder, 60)
        await callback.answer(f"❌ Колесо доступно раз в 24 часа! Ждать еще: {hours}ч {minutes}м.", show_alert=True)
        return
    
    await callback.answer()
    prizes = [("0.50 NMP", 0.5), ("1.00 NMP", 1.0), ("5.00 NMP", 5.0), ("10.00 NMP", 10.0), ("27.00 NMP", 27.0)]
    prize_name, prize_val = random.choices(prizes, weights=[50, 30, 14, 5, 1], k=1)[0]
    
    async with db_pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("UPDATE users SET balance = balance + $1, daily_spin_last_used = NOW() WHERE telegram_id = $2", prize_val, user_id)
            await conn.execute("INSERT INTO transactions (user_id, amount, tx_type, description) VALUES ($1, $2, 'spin', 'Колесо Фортуны')", user_id, prize_val)
            
    await callback.message.edit_text(f"🎰 *Колесо Фортуны!*\n\nВы выиграли: **{prize_name}**! Баланс успешно пополнен.", reply_markup=InlineKeyboardBuilder().button(text="🔙 В меню", callback_data="main_menu").as_markup(), parse_mode="Markdown")

# ---------------------------------------------------------------------------
# АДМИН-ФУНКЦИОНАЛ (ПОИСК ИГРОКОВ И СОЗДАНИЕ ПРОМОКОДОВ)
# ---------------------------------------------------------------------------
@dp.callback_query(F.data == "admin_panel")
async def cb_admin_panel(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID: return
    await callback.answer()
    async with db_pool.acquire() as conn:
        total_users = await conn.fetchval("SELECT COUNT(*) FROM users")
        total_balance = await conn.fetchval("SELECT SUM(balance) FROM users")
        pending_p2p = await conn.fetchval("SELECT COUNT(*) FROM p2p_deposits WHERE status = 'pending'")
    
    text = (
        f"⚙️ *Админка*\n\n"
        f"Юзеров: `{total_users}`\n"
        f"Балансы: `{total_balance:.2f} NMP`\n"
        f"🎮 Ожидают P2P: `{pending_p2p}`"
    )
    builder = InlineKeyboardBuilder()
    builder.button(text="✍️ Выдать баланс (/give)", callback_data="admin_give_coins")
    builder.button(text="🎫 Создать Промо", callback_data="admin_create_promo")
    builder.button(text="🔍 Поиск игрока", callback_data="admin_search_user")
    builder.button(text="🔙 В меню", callback_data="main_menu")
    builder.adjust(1)
    await callback.message.edit_text(text, reply_markup=builder.as_markup(), parse_mode="Markdown")

# 1. Поиск игрока и его статистика
@dp.callback_query(F.data == "admin_search_user")
async def cb_admin_search_user(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID: return
    await callback.answer()
    await state.set_state(Form.waiting_admin_search_user)
    await callback.message.edit_text("🔍 Введите числовой Telegram ID пользователя для просмотра его статистики:")

@dp.message(Form.waiting_admin_search_user)
async def process_admin_search_user(message: types.Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID: return
    try:
        target_id = int(message.text.strip())
    except ValueError:
        await message.reply("❌ Введите корректный числовой Telegram ID:")
        return
    
    async with db_pool.acquire() as conn:
        user = await conn.fetchrow("SELECT * FROM users WHERE telegram_id = $1", target_id)
        if not user:
            await message.reply("❌ Пользователь с таким ID не найден в базе данных.")
            await state.clear()
            return
        
        stake_b, cash_b = await get_user_boosts(conn, target_id)
        earned, spent = await get_user_monthly_stats(conn, target_id)
        active_locks = await conn.fetchval("SELECT COUNT(*) FROM lock_deposits WHERE user_id = $1 AND is_active = true", target_id)
        owned_nfts = await conn.fetchval("SELECT COUNT(*) FROM user_nfts WHERE user_id = $1", target_id)
        invited_count = await conn.fetchval("SELECT COUNT(*) FROM users WHERE referrer_id = $1", target_id)
        
    handle_str = f"🏷 Кастомный никнейм: `{user['custom_handle']}`" if user['custom_handle'] else "Кастомный никнейм: отсутствует"
    
    stats_text = (
        f"👤 *Информация о пользователе `{target_id}`*\n\n"
        f"📝 Имя пользователя: @{user['username'] or 'User'}\n"
        f"{handle_str}\n"
        f"💵 Баланс: `{user['balance']:.2f} NMP`\n\n"
        f"📊 *Статистика за 30 дней:*\n"
        f"📥 Получено: `+{earned:.2f} NMP`\n"
        f"📤 Списано: `-{spent:.2f} NMP`\n\n"
        f"📈 Стейкинг буст: `+{stake_b:.2f}%` (Общий стейкинг: `{9.2 + stake_b:.2f}%`)\n"
        f"🛍 Кэшбэк буст: `+{cash_b:.2f}%` (Общий кэшбэк: `{1.0 + cash_b:.2f}%`)\n\n"
        f"🔒 Активных вкладов: `{active_locks}`\n"
        f"🖼 Куплено NFT: `{owned_nfts}`\n"
        f"👥 Приглашено друзей: `{invited_count}`"
    )
    
    await message.reply(stats_text, parse_mode="Markdown")
    await state.clear()

# 2. Создание промокодов админом
@dp.callback_query(F.data == "admin_create_promo")
async def cb_admin_create_promo(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID: return
    await callback.answer()
    await state.set_state(Form.waiting_admin_promo_code)
    await callback.message.edit_text("🎫 Введите кодовое слово для нового промокода (например, VIP50):")

@dp.message(Form.waiting_admin_promo_code)
async def process_admin_promo_code(message: types.Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID: return
    await state.update_data(new_promo_code=message.text.strip())
    await state.set_state(Form.waiting_admin_promo_reward)
    await message.reply("💰 Введите размер награды в NMP за активацию этого промокода:")

@dp.message(Form.waiting_admin_promo_reward)
async def process_admin_promo_reward(message: types.Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID: return
    try:
        reward = float(message.text.strip())
        if reward <= 0: raise ValueError
    except ValueError:
        await message.reply("❌ Введите корректное положительное число:")
        return
    await state.update_data(new_promo_reward=reward)
    await state.set_state(Form.waiting_admin_promo_uses)
    await message.reply("👥 Введите лимит активаций (максимальное число использований):")

@dp.message(Form.waiting_admin_promo_uses)
async def process_admin_promo_uses(message: types.Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID: return
    try:
        uses = int(message.text.strip())
        if uses <= 0: raise ValueError
    except ValueError:
        await message.reply("❌ Введите целое положительное число:")
        return
    
    data = await state.get_data()
    code = data['new_promo_code']
    reward = data['new_promo_reward']
    
    async with db_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO promo_codes (code, reward, max_uses, used_count) VALUES ($1, $2, $3, 0) ON CONFLICT (code) DO UPDATE SET reward = $2, max_uses = $3, used_count = 0",
            code, reward, uses
        )
    
    await message.reply(f"✅ Промокод `{code}` успешно создан!\n🎁 Награда: `{reward:.2f} NMP` | Лимит активаций: `{uses}`")
    await state.clear()

# ---------------------------------------------------------------------------
# ВСЕ ОСТАЛЬНЫЕ БАНКОВСКИЕ МОДУЛИ (ЧЕКИ, NFT, СЕЙВИНГ, ПРОМО)
# ---------------------------------------------------------------------------

# --- ЧЕКИ ---
@dp.callback_query(F.data == "checks_menu")
async def cb_checks_menu(callback: types.CallbackQuery):
    await callback.answer()
    text = (
        "💸 *Виртуальные Чеки*\n\n"
        "Вы можете создать чек на любую сумму NMP и поделиться им с друзьями. "
        "Активировавший чек мгновенно получит средства на баланс."
    )
    builder = InlineKeyboardBuilder()
    builder.button(text="🎫 Создать Чек", callback_data="create_check_start")
    builder.button(text="📋 История моих чеков", callback_data="my_checks_history")
    builder.button(text="🔙 Назад", callback_data="main_menu")
    builder.adjust(1)
    await callback.message.edit_text(text, reply_markup=builder.as_markup(), parse_mode="Markdown")

@dp.callback_query(F.data == "create_check_start")
async def cb_create_check(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer()
    await state.set_state(Form.waiting_check_amount)
    await callback.message.edit_text("💸 Введите общую сумму NMP для чека:")

@dp.message(Form.waiting_check_amount)
async def process_check_amount(message: types.Message, state: FSMContext):
    try:
        amount = float(message.text)
        if amount <= 0: raise ValueError()
    except ValueError:
        await message.reply("❌ Введите положительное число:")
        return
        
    async with db_pool.acquire() as conn:
        user_bal = await conn.fetchval("SELECT balance FROM users WHERE telegram_id = $1", message.from_user.id)
        if user_bal < amount:
            await message.reply("❌ Недостаточно средств на балансе. Введите другую сумму:")
            return
            
    await state.update_data(check_amount=amount)
    await state.set_state(Form.waiting_check_claims)
    await message.reply("👥 На сколько человек рассчитать чек? (Введите от 1 до 100):")

@dp.message(Form.waiting_check_claims)
async def process_check_claims(message: types.Message, state: FSMContext):
    try:
        claims = int(message.text)
        if claims < 1 or claims > 100: raise ValueError()
    except ValueError:
        await message.reply("❌ Введите целое число от 1 до 100:")
        return
        
    data = await state.get_data()
    amount = data['check_amount']
    user_id = message.from_user.id
    
    amount_per_claim = amount / claims
    check_code = f"c_{secrets.token_hex(4)}"
    is_multi = claims > 1
    
    async with db_pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("UPDATE users SET balance = balance - $1 WHERE telegram_id = $2", amount, user_id)
            await conn.execute(
                "INSERT INTO checks (code, creator_id, amount, max_claims, is_multi, amount_per_claim) VALUES ($1, $2, $3, $4, $5, $6)",
                check_code, user_id, amount, claims, is_multi, amount_per_claim
            )
            await conn.execute(
                "INSERT INTO transactions (user_id, amount, tx_type, description) VALUES ($1, $2, 'check_create', $3)",
                user_id, -amount, f"Создание чека {check_code}"
            )
            
    bot_info = await bot.get_me()
    check_link = f"https://t.me/{bot_info.username}?start={check_code}"
    
    caption_text = (
        f"✅ *Ваш Виртуальный Чек успешно создан!*\n\n"
        f"💰 Сумма чека: `{amount:.2f} NMP`\n"
        f"👥 Кол-во активаций: `{claims}`\n"
        f"💵 На одного человека: `{amount_per_claim:.2f} NMP`\n\n"
        f"🔗 Ссылка для отправки:\n`{check_link}`"
    )
    
    await state.clear()
    await message.answer_photo(photo=CHECK_IMAGE_URL, caption=caption_text, parse_mode="Markdown")
    await send_main_menu(message.chat.id, "Вернуться в главное меню:")

@dp.callback_query(F.data == "my_checks_history")
async def cb_my_checks(callback: types.CallbackQuery):
    await callback.answer()
    user_id = callback.from_user.id
    async with db_pool.acquire() as conn:
        my_checks = await conn.fetch("SELECT * FROM checks WHERE creator_id = $1 ORDER BY created_at DESC LIMIT 10", user_id)
        
    text = "📋 *История ваших чеков (последние 10):*\n\n"
    if not my_checks:
        text += "Вы еще не создавали чеков."
    else:
        bot_info = await bot.get_me()
        for idx, c in enumerate(my_checks, 1):
            text += (
                f"{idx}. Код: `{c['code']}` | Сумма: `{c['amount']:.2f} NMP`\n"
                f"📊 Использовано: `{c['claimed_count']}/{c['max_claims']}`\n"
                f"🔗 Ссылка: `https://t.me/{bot_info.username}?start={c['code']}`\n\n"
            )
            
    builder = InlineKeyboardBuilder().button(text="🔙 Назад", callback_data="checks_menu")
    await callback.message.edit_text(text, reply_markup=builder.as_markup(), parse_mode="Markdown")

async def claim_check_logic(message: types.Message, code: str):
    user_id = message.from_user.id
    async with db_pool.acquire() as conn:
        async with conn.transaction():
            check = await conn.fetchrow("SELECT * FROM checks WHERE code = $1 FOR UPDATE", code)
            if not check:
                await message.reply("❌ Чек не существует.")
                return
                
            if check['claimed_count'] >= check['max_claims']:
                await message.reply("❌ Этот чек уже полностью обналичен!")
                return
                
            claimed = await conn.fetchrow("SELECT * FROM check_claims WHERE check_code = $1 AND user_id = $2", code, user_id)
            if claimed:
                await message.reply("❌ Вы уже забирали средства из этого чека!")
                return
                
            amount_to_pay = check['amount_per_claim']
            await conn.execute("UPDATE users SET balance = balance + $1 WHERE telegram_id = $2", amount_to_pay, user_id)
            await conn.execute("UPDATE checks SET claimed_count = claimed_count + 1 WHERE code = $1", code)
            await conn.execute("INSERT INTO check_claims (check_code, user_id) VALUES ($1, $2)", code, user_id)
            await conn.execute(
                "INSERT INTO transactions (user_id, amount, tx_type, description) VALUES ($1, $2, 'check_claim', $3)",
                user_id, amount_to_pay, f"Активация чека {code}"
            )
            
            await message.answer_photo(
                photo=CHECK_IMAGE_URL,
                caption=f"🎉 *Успех!*\nВы обналичили чек `{code}`!\nНачислено: **+{amount_to_pay:.2f} NMP**!"
            )
            try:
                await bot.send_message(check['creator_id'], f"💸 Чек `{code}` активирован пользователем @{message.from_user.username or user_id}.")
            except: pass

# --- NFT МАРКЕТПЛЕЙС ---
@dp.callback_query(F.data == "shop_nfts")
async def cb_shop_nfts(callback: types.CallbackQuery):
    await callback.answer()
    async with db_pool.acquire() as conn:
        nfts = await conn.fetch("SELECT id, name, price FROM nfts ORDER BY id LIMIT 30")
        
    text = "🛍 *NFT-Маркетплейс NMVal Bank*\n\nВыберите интересующий NFT:"
    builder = InlineKeyboardBuilder()
    
    for n in nfts:
        builder.button(text=f"🖼 {n['name']} — {n['price']:.0f} NMP", callback_data=f"view_nft:{n['id']}")
    builder.button(text="🔙 Назад", callback_data="main_menu")
    builder.adjust(1)
    await callback.message.edit_text(text, reply_markup=builder.as_markup(), parse_mode="Markdown")

@dp.callback_query(F.data.startswith("view_nft:"))
async def cb_view_nft(callback: types.CallbackQuery):
    await callback.answer()
    nft_id = int(callback.data.split(":")[1])
    async with db_pool.acquire() as conn:
        nft = await conn.fetchrow("SELECT * FROM nfts WHERE id = $1", nft_id)
        
    if not nft:
        await callback.answer("NFT не найден!")
        return
        
    await callback.message.delete()
    caption_text = (
        f"🖼 *Уникальный NFT:* {nft['name']}\n\n"
        f"💰 Цена покупки: `{nft['price']:.2f} NMP`\n"
        f"📦 Осталось в наличии: `{nft['remaining_supply']}/{nft['total_supply']}`\n\n"
        f"⚡️ *Бонусы холдера:*\n"
        f"📈 Пассивный Стейкинг: `+{nft['boost_staking_pct']:.2f}%` в месяц\n"
        f"🛍 Дополнительный Кэшбэк: `+{nft['boost_cashback_pct']:.2f}%`"
    )
    
    builder = InlineKeyboardBuilder()
    builder.button(text="🛒 Купить этот NFT", callback_data=f"buy_nft_pro:{nft['id']}")
    builder.button(text="🔙 В магазин", callback_data="back_to_shop")
    builder.adjust(1)
    
    img = nft['image_url'] if nft['image_url'].startswith("http") else DEFAULT_NFT_IMAGE
    await callback.message.answer_photo(photo=img, caption=caption_text, reply_markup=builder.as_markup(), parse_mode="Markdown")

@dp.callback_query(F.data == "back_to_shop")
async def cb_back_to_shop(callback: types.CallbackQuery):
    await callback.answer()
    await callback.message.delete()
    await cb_shop_nfts(callback)

@dp.callback_query(F.data.startswith("buy_nft_pro:"))
async def cb_buy_nft_pro(callback: types.CallbackQuery):
    nft_id = int(callback.data.split(":")[1])
    user_id = callback.from_user.id
    
    async with db_pool.acquire() as conn:
        async with conn.transaction():
            user = await conn.fetchrow("SELECT balance FROM users WHERE telegram_id = $1 FOR UPDATE", user_id)
            nft = await conn.fetchrow("SELECT * FROM nfts WHERE id = $1 FOR UPDATE", nft_id)
            
            if not nft or nft['remaining_supply'] <= 0:
                await callback.answer("❌ Этот лимитированный NFT распродан!")
                return
                
            if user['balance'] < nft['price']:
                await callback.answer("❌ На балансе недостаточно NMP!")
                return
                
            price = float(nft['price'])
            stake_b, cash_b = await get_user_boosts(conn, user_id)
            total_cashback_rate = 0.01 + (cash_b / 100.0)
            cashback_reward = price * total_cashback_rate
            
            new_balance = float(user['balance']) - price + cashback_reward
            
            await conn.execute("UPDATE users SET balance = $1 WHERE telegram_id = $2", new_balance, user_id)
            await conn.execute("UPDATE nfts SET remaining_supply = remaining_supply - 1 WHERE id = $1", nft_id)
            await conn.execute("INSERT INTO user_nfts (user_id, nft_id) VALUES ($1, $2)", user_id, nft_id)
            
            await conn.execute("INSERT INTO transactions (user_id, amount, tx_type, description) VALUES ($1, $2, 'purchase', $3)", user_id, -price, f"Покупка NFT {nft['name']}")
            await conn.execute("INSERT INTO transactions (user_id, amount, tx_type, description) VALUES ($1, $2, 'cashback', $3)", user_id, cashback_reward, f"Кэшбэк за {nft['name']}")
            
    await callback.answer(f"🎉 NFT успешно куплен!\nПолучен кэшбэк {cashback_reward:.2f} NMP!", show_alert=True)
    await callback.message.delete()
    await send_main_menu(callback.message.chat.id, "🏦 Вы вернулись на главный экран:")

# --- ДЕПОЗИТЫ / СЕЙВИНГ ---
@dp.callback_query(F.data == "deposits_menu")
async def cb_deposits_menu(callback: types.CallbackQuery):
    await callback.answer()
    user_id = callback.from_user.id
    async with db_pool.acquire() as conn:
        active_deposits = await conn.fetch("SELECT * FROM lock_deposits WHERE user_id = $1 AND is_active = TRUE", user_id)
        
    text = "📈 *Срочные вклады (Lock-up Сейвинг)*\n\nЗаморозьте свободные NMP на определенный срок и получите высокий процент:\n"
    text += "• 3 месяца: *12% годовых*\n• 6 месяцев: *15% годовых*\n• 12 месяцев: *20% годовых*\n\n"
    
    if active_deposits:
        text += "💼 *Ваши вклады:*\n"
        for d in active_deposits:
            text += f"• Сумма: `{d['amount']:.2f} NMP` | Ставка: `{d['rate']}%` | Конец: `{d['end_date'].strftime('%Y-%m-%d')}`\n"
    else:
        text += "У вас нет активных вкладов."
        
    builder = InlineKeyboardBuilder()
    builder.button(text="💼 Открыть Вклад", callback_data="deposit_open_start")
    builder.button(text="🔙 В меню", callback_data="main_menu")
    builder.adjust(1)
    await callback.message.edit_text(text, reply_markup=builder.as_markup(), parse_mode="Markdown")

@dp.callback_query(F.data == "deposit_open_start")
async def cb_open_deposit_start(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer()
    await state.set_state(Form.waiting_deposit_amount)
    await callback.message.edit_text("📈 Введите количество NMP для вклада:")

@dp.message(Form.waiting_deposit_amount)
async def process_deposit_amount(message: types.Message, state: FSMContext):
    try:
        amount = float(message.text)
        if amount <= 0: raise ValueError()
    except ValueError:
        await message.reply("❌ Введите корректную сумму:")
        return
        
    async with db_pool.acquire() as conn:
        bal = await conn.fetchval("SELECT balance FROM users WHERE telegram_id = $1", message.from_user.id)
        if bal < amount:
            await message.reply("❌ Недостаточно средств.")
            return
            
    await state.update_data(dep_amount=amount)
    builder = InlineKeyboardBuilder()
    builder.button(text="3 мес (12% APR)", callback_data="dep_plan:3:12.0")
    builder.button(text="6 мес (15% APR)", callback_data="dep_plan:6:15.0")
    builder.button(text="12 мес (20% APR)", callback_data="dep_plan:12:20.0")
    builder.button(text="❌ Отмена", callback_data="main_menu")
    builder.adjust(1)
    await message.reply("📅 Выберите срок вклада:", reply_markup=builder.as_markup())

@dp.callback_query(F.data.startswith("dep_plan:"))
async def cb_select_dep_plan(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer()
    data = await state.get_data()
    amount = data.get("dep_amount")
    if not amount: return
    
    _, m_str, r_str = callback.data.split(":")
    months, rate = int(m_str), float(r_str)
    user_id = callback.from_user.id
    end_date = datetime.now() + timedelta(days=months * 30)
    
    async with db_pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("UPDATE users SET balance = balance - $1 WHERE telegram_id = $2", amount, user_id)
            await conn.execute("INSERT INTO lock_deposits (user_id, amount, rate, end_date) VALUES ($1, $2, $3, $4)", user_id, amount, rate, end_date)
            await conn.execute("INSERT INTO transactions (user_id, amount, tx_type, description) VALUES ($1, $2, 'deposit_open', $3)", user_id, -amount, f"Заморозка вклада под {rate}%")
            
    await callback.message.edit_text(f"🎉 *Вклад открыт!*\nСумма `{amount:.2f} NMP` заморожена под `{rate}% годовых` до `{end_date.strftime('%Y-%m-%d')}`.", parse_mode="Markdown")
    await state.clear()
    await send_main_menu(callback.message.chat.id, "Выберите действие:")

# --- КАСТОМНЫЙ НИКНЕЙМ ---
@dp.callback_query(F.data == "custom_handle_menu")
async def cb_custom_handle(callback: types.CallbackQuery):
    await callback.answer()
    user_id = callback.from_user.id
    async with db_pool.acquire() as conn:
        handle = await conn.fetchval("SELECT custom_handle FROM users WHERE telegram_id = $1", user_id)
        
    if handle:
        text = f"✨ Ваш никнейм счета: `{handle}`"
        builder = InlineKeyboardBuilder().button(text="🔙 Назад", callback_data="main_menu")
    else:
        text = "👤 *Уникальный Никнейм Счета*\n\nСтоимость услуги: **500.00 NMP**"
        builder = InlineKeyboardBuilder()
        builder.button(text="✍️ Зарегистрировать", callback_data="custom_handle_buy_start")
        builder.button(text="🔙 Назад", callback_data="main_menu")
        builder.adjust(1)
    await callback.message.edit_text(text, reply_markup=builder.as_markup(), parse_mode="Markdown")

@dp.callback_query(F.data == "custom_handle_buy_start")
async def cb_handle_buy_start(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer()
    await state.set_state(Form.waiting_custom_handle)
    await callback.message.edit_text("👤 Введите желаемый никнейм (должен начинаться со знака `*`, от 4 до 15 символов):")

@dp.message(Form.waiting_custom_handle)
async def process_custom_handle(message: types.Message, state: FSMContext):
    handle = message.text.strip().upper()
    user_id = message.from_user.id
    
    if not handle.startswith("*") or len(handle) < 4 or len(handle) > 16:
        await message.reply("❌ Неверный формат! Начинается с `*`, 4-15 символов:")
        return
        
    async with db_pool.acquire() as conn:
        async with conn.transaction():
            exists = await conn.fetchrow("SELECT telegram_id FROM users WHERE custom_handle = $1", handle)
            if exists:
                await message.reply("❌ Никнейм уже занят:")
                return
            user_bal = await conn.fetchval("SELECT balance FROM users WHERE telegram_id = $1 FOR UPDATE", user_id)
            if user_bal < 500.00:
                await message.reply("❌ Недостаточно средств.")
                await state.clear()
                return
            await conn.execute("UPDATE users SET balance = balance - 500.00, custom_handle = $1 WHERE telegram_id = $2", handle, user_id)
            await conn.execute("INSERT INTO transactions (user_id, amount, tx_type, description) VALUES ($1, -500.00, 'handle_buy', $2)", user_id, f"Куплен никнейм {handle}")
            
    await message.reply(f"🎉 Счет `{handle}` зарегистрирован!")
    await state.clear()
    await send_main_menu(message.chat.id, "Выберите действие:")

# --- API ---
@dp.callback_query(F.data == "merchant_api")
async def cb_merchant_api(callback: types.CallbackQuery):
    await callback.answer()
    user_id = callback.from_user.id
    async with db_pool.acquire() as conn:
        key = await conn.fetchrow("SELECT api_key FROM merchant_api_keys WHERE user_id = $1", user_id)
        if not key:
            await conn.execute("INSERT INTO merchant_api_keys (user_id, service_name, api_key) VALUES ($1, 'По умолчанию', $2)", user_id, secrets.token_hex(16))
            key = await conn.fetchrow("SELECT api_key FROM merchant_api_keys WHERE user_id = $1", user_id)
    text = f"🔗 *NMVal Merchant API*\n\n🔑 Токен:\n`{key['api_key']}`"
    builder = InlineKeyboardBuilder().button(text="🔙 В меню", callback_data="main_menu")
    await callback.message.edit_text(text, reply_markup=builder.as_markup(), parse_mode="Markdown")

# --- ПРОМОКОДЫ ---
@dp.callback_query(F.data == "promo_activate")
async def cb_promo_activate(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer()
    await state.set_state(Form.waiting_for_promo)
    await callback.message.edit_text("🎫 Введите промокод:")

@dp.message(Form.waiting_for_promo)
async def process_promo(message: types.Message, state: FSMContext):
    code = message.text.strip()
    user_id = message.from_user.id
    async with db_pool.acquire() as conn:
        promo = await conn.fetchrow("SELECT * FROM promo_codes WHERE code = $1", code)
        if not promo or promo['used_count'] >= promo['max_uses']:
            await message.reply("❌ Промокод не существует или исчерпан.")
            await state.clear()
            return
        used = await conn.fetchrow("SELECT id FROM promo_uses WHERE user_id = $1 AND code = $2", user_id, code)
        if used:
            await message.reply("❌ Вы уже активировали этот код!")
            await state.clear()
            return
        async with conn.transaction():
            await conn.execute("UPDATE users SET balance = balance + $1 WHERE telegram_id = $2", promo['reward'], user_id)
            await conn.execute("UPDATE promo_codes SET used_count = used_count + 1 WHERE code = $1", code)
            await conn.execute("INSERT INTO promo_uses (user_id, code) VALUES ($1, $2)", user_id, code)
            await conn.execute("INSERT INTO transactions (user_id, amount, tx_type, description) VALUES ($1, $2, 'promo', $3)", user_id, promo['reward'], f"Активация {code}")
    await message.reply(f"🎉 Начислено +{promo['reward']:.2f} NMP!")
    await state.clear()
    await send_main_menu(message.chat.id, "Выберите действие:")

# --- ФОНОВЫЕ ПРОЦЕССЫ СТЕЙКИНГА ---
async def financial_background_scheduler():
    while True:
        await asyncio.sleep(60)
        try:
            async with db_pool.acquire() as conn:
                # Стейкинг
                users_to_pay = await conn.fetch("SELECT telegram_id, balance FROM users WHERE last_interest_accrued < NOW() - INTERVAL '30 days'")
                for u in users_to_pay:
                    user_id = u['telegram_id']
                    stake_b, _ = await get_user_boosts(conn, user_id)
                    total_rate = 0.092 + (stake_b / 100.0)
                    bonus = float(u['balance']) * total_rate
                    async with conn.transaction():
                        await conn.execute("UPDATE users SET balance = balance + $1, last_interest_accrued = NOW() WHERE telegram_id = $2", bonus, user_id)
                        await conn.execute("INSERT INTO transactions (user_id, amount, tx_type, description) VALUES ($1, $2, 'staking', $3)", user_id, bonus, f"Стейкинг {total_rate*100:.2f}%")
                    try: await bot.send_message(user_id, f"📈 Проценты зачислены: *+{bonus:.2f} NMP*!")
                    except: pass
                
                # Депозиты
                expired_deposits = await conn.fetch("SELECT * FROM lock_deposits WHERE is_active = TRUE AND end_date <= NOW()")
                for d in expired_deposits:
                    payout = float(d['amount']) * (1 + (float(d['rate']) / 100.0))
                    async with conn.transaction():
                        await conn.execute("UPDATE lock_deposits SET is_active = FALSE WHERE id = $1", d['id'])
                        await conn.execute("UPDATE users SET balance = balance + $1 WHERE telegram_id = $2", payout, d['user_id'])
                        await conn.execute("INSERT INTO transactions (user_id, amount, tx_type, description) VALUES ($1, $2, 'deposit_payout', 'Выплата вклада')", d['user_id'], payout)
                    try: await bot.send_message(d['user_id'], f"📈 Вклад закрыт! Выплачено: *{payout:.2f} NMP*!")
                    except: pass
        except Exception as e:
            logging.error(f"Шедулер ошибка: {e}", exc_info=True)

async def main():
    logging.basicConfig(level=logging.INFO)
    await init_db()
    asyncio.create_task(financial_background_scheduler())
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
