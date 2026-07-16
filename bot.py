import os
import asyncio
import logging
import random
import uuid
import secrets
import base64
import urllib.parse
from datetime import datetime, timedelta, timezone
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
# CONFIG
# ---------------------------------------------------------------------------
BOT_TOKEN = os.getenv("BOT_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL") 
ADMIN_ID = int(os.getenv("ADMIN_ID", 0))

P2P_PAYMENT_INSTRUCTIONS = (
    "1️⃣ *Переведите игровую валюту на реквизиты администрации:*\n"
    "🔗 Перевод иожно осуществить, как напрямую в игре с тех.поддержкой или передачей валюты через чек и подобное.\n"
    "💳 **ЮMoney:** [4100119121976236]\n"
    "🎮 **ID / Ник в игре (для прямого обмена):** [@Tex_pod_NMValBank]\n"
    "💬 **Связаться с тех.поддержкой напрямую:** @Tex_pod_NMValBank \n\n"
    "2️⃣ **ОТПРАВЬТЕ ПОДТВЕРЖДЕНИЕ СЮДА (БОТУ):**\n"
    "Пришлите скриншот перевода (как фото) или ссылку на трейд/ваш ник в ответном сообщении.\n\n"
    "⚠️ *Напоминание по курсу:* Админ проверит поступление, умножит количество полученной валюты на курс Telegram Stars (коэффициент **1.40**) и начислит монеты NMP на ваш баланс через команду `/give`.\n\n"
    "⏳ *Срок проверки заявки:* от 5 до 30 минут."
)

CHECK_IMAGE_URL = "https://i.ibb.co/tMRTCg7c/IMG-20260714-004428-315.jpg"
SECRET_FEATURE_IMG = "https://i.postimg.cc/K8L9WpG0/secret-jackpot.png"

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
db_pool = None

class Form(StatesGroup):
    waiting_for_promo = State()
    waiting_admin_user_id = State()
    waiting_admin_amount = State()
    waiting_admin_promo_code = State()
    waiting_admin_promo_reward = State()
    waiting_admin_promo_uses = State()
    waiting_admin_search_user = State()
    waiting_check_amount = State()
    waiting_check_claims = State()
    waiting_custom_handle = State()
    waiting_deposit_amount = State()
    waiting_p2p_user_proof = State()
    # Для пагинации чеков
    check_history_offset = State()
    # Для досрочного закрытия вклада
    confirm_close_deposit = State()

async def init_db():
    global db_pool
    db_pool = await asyncpg.create_pool(dsn=DATABASE_URL)
    async with db_pool.acquire() as conn:
        # Создание всех таблиц (если ещё не существуют)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                telegram_id BIGINT PRIMARY KEY,
                username TEXT,
                referrer_id BIGINT,
                balance NUMERIC DEFAULT 0,
                custom_handle TEXT UNIQUE,
                daily_spin_last_used TIMESTAMP,
                last_interest_accrued TIMESTAMP DEFAULT NOW(),
                created_at TIMESTAMP DEFAULT NOW(),
                staking_bonus NUMERIC DEFAULT 0,
                cashback_bonus NUMERIC DEFAULT 0
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS transactions (
                id SERIAL PRIMARY KEY,
                user_id BIGINT REFERENCES users(telegram_id),
                amount NUMERIC,
                tx_type TEXT,
                description TEXT,
                created_at TIMESTAMP DEFAULT NOW()
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS checks (
                code TEXT PRIMARY KEY,
                creator_id BIGINT REFERENCES users(telegram_id),
                amount NUMERIC,
                max_claims INTEGER,
                is_multi BOOLEAN,
                amount_per_claim NUMERIC,
                claimed_count INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT NOW()
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS check_claims (
                id SERIAL PRIMARY KEY,
                check_code TEXT REFERENCES checks(code),
                user_id BIGINT REFERENCES users(telegram_id),
                claimed_at TIMESTAMP DEFAULT NOW(),
                UNIQUE(check_code, user_id)
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS lock_deposits (
                id SERIAL PRIMARY KEY,
                user_id BIGINT REFERENCES users(telegram_id),
                amount NUMERIC,
                rate NUMERIC,
                end_date TIMESTAMP,
                is_active BOOLEAN DEFAULT TRUE,
                created_at TIMESTAMP DEFAULT NOW()
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS promo_codes (
                code TEXT PRIMARY KEY,
                reward NUMERIC,
                max_uses INTEGER,
                used_count INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT NOW()
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS promo_uses (
                id SERIAL PRIMARY KEY,
                user_id BIGINT REFERENCES users(telegram_id),
                code TEXT REFERENCES promo_codes(code),
                used_at TIMESTAMP DEFAULT NOW(),
                UNIQUE(user_id, code)
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS merchant_api_keys (
                id SERIAL PRIMARY KEY,
                user_id BIGINT REFERENCES users(telegram_id),
                service_name TEXT,
                api_key TEXT UNIQUE,
                created_at TIMESTAMP DEFAULT NOW()
            )
        """)
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
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS pool_entries (
                id SERIAL PRIMARY KEY,
                user_id BIGINT NOT NULL,
                ticket_code VARCHAR(50) NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        # Новые таблицы для достижений
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS achievements (
                id SERIAL PRIMARY KEY,
                name TEXT UNIQUE,
                description TEXT,
                reward NUMERIC,
                condition_type TEXT,  -- 'first_deposit', 'invites_5', 'syndicate_win', 'promo_used', 'check_created'
                created_at TIMESTAMP DEFAULT NOW()
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS user_achievements (
                id SERIAL PRIMARY KEY,
                user_id BIGINT REFERENCES users(telegram_id),
                achievement_id INTEGER REFERENCES achievements(id),
                unlocked_at TIMESTAMP DEFAULT NOW(),
                UNIQUE(user_id, achievement_id)
            )
        """)
        # Таблица логов админа
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS admin_logs (
                id SERIAL PRIMARY KEY,
                admin_id BIGINT,
                action TEXT,
                target_id BIGINT,
                details TEXT,
                created_at TIMESTAMP DEFAULT NOW()
            )
        """)
        # Добавляем стандартные достижения, если их нет
        await conn.execute("""
            INSERT INTO achievements (name, description, reward, condition_type)
            VALUES 
                ('Первый депозит', 'Открыть первый срочный вклад', 50, 'first_deposit'),
                ('Друг-золото', 'Пригласить 5 друзей', 150, 'invites_5'),
                ('Победитель Синдиката', 'Выиграть в Инвест-Синдикате', 300, 'syndicate_win'),
                ('Промо-мастер', 'Активировать промокод', 25, 'promo_used'),
                ('Чек-мейкер', 'Создать виртуальный чек', 30, 'check_created')
            ON CONFLICT (name) DO NOTHING
        """)
        # Добавляем поля, если они отсутствуют (для совместимости)
        for col in ['staking_bonus', 'cashback_bonus']:
            try:
                await conn.execute(f"ALTER TABLE users ADD COLUMN {col} NUMERIC DEFAULT 0")
            except asyncpg.exceptions.DuplicateColumnError:
                pass
        try:
            await conn.execute("ALTER TABLE users ADD COLUMN last_fortune_date TIMESTAMP")
        except asyncpg.exceptions.DuplicateColumnError:
            pass

# ---------------------------------------------------------------------------
# STATS HELPERS
# ---------------------------------------------------------------------------
async def get_user_boosts(conn, user_id: int):
    # В будущем можно брать из поля users, сейчас всегда 0
    return 0.0, 0.0

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
# START & DEEP LINKS
# ---------------------------------------------------------------------------
@dp.message(CommandStart())
async def cmd_start(message: types.Message):
    user_id = message.from_user.id
    username = message.from_user.username or "User"
    args = message.text.split()
    referrer_id = None
    
    async with db_pool.acquire() as conn:
        user = await conn.fetchrow("SELECT * FROM users WHERE telegram_id = $1", user_id)
        if not user:
            if len(args) > 1 and args[1].isdigit():
                referrer_id = int(args[1])
                if referrer_id == user_id: referrer_id = None

            await conn.execute(
                "INSERT INTO users (telegram_id, username, referrer_id, balance, last_interest_accrued) VALUES ($1, $2, $3, 100.00, NOW())",
                user_id, username, referrer_id
            )
            await conn.execute(
                "INSERT INTO transactions (user_id, amount, tx_type, description) VALUES ($1, 100.00, 'welcome', 'Приветственный бонус')",
                user_id
            )
            if referrer_id:
                async with conn.transaction():
                    await conn.execute("UPDATE users SET balance = balance + 27 WHERE telegram_id = $1", referrer_id)
                    await conn.execute(
                        "INSERT INTO transactions (user_id, amount, tx_type, description) VALUES ($1, 27.00, 'referral', 'Реферальный бонус за друга')",
                        referrer_id
                    )
                    # Проверка достижения "5 приглашений"
                    invites = await conn.fetchval("SELECT COUNT(*) FROM users WHERE referrer_id = $1", referrer_id)
                    if invites >= 5:
                        await check_and_unlock_achievement(conn, referrer_id, 'invites_5')
                try:
                    await bot.send_message(referrer_id, f"🎉 По вашей ссылке зарегистрировался @{username}! Вам зачислено +27 NMP.")
                except Exception as e:
                    logging.warning(f"Не удалось отправить сообщение рефереру {referrer_id}: {e}")
            welcome_text = "🏦 *Добро пожаловать в NMVal Bank!*\n\nЛичный кабинет успешно открыт. На ваш баланс начислено приветственные **100.00 NMP**!"
        else:
            welcome_text = f"🏦 *С возвращением в NMVal Bank, @{username}!*"

    if len(args) > 1:
        payload = args[1]
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

        elif payload.startswith("pay_stars_"):
            try:
                amount = float(payload.split("_")[2])
                stars_cost = max(1, int(round(amount / 3)))
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
            await claim_check_logic(message, payload)
            return

    await send_main_menu(message.chat.id, welcome_text)

async def send_main_menu(chat_id: int, text: str):
    builder = InlineKeyboardBuilder()
    builder.button(text="💳 Мой Кабинет", callback_data="my_account")
    builder.button(text="🌀 Инвест-Синдикат", callback_data="secret_feature_menu")
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
# ДОСТИЖЕНИЯ
# ---------------------------------------------------------------------------
async def check_and_unlock_achievement(conn, user_id: int, condition_type: str):
    # Получаем айди достижения по условию
    ach = await conn.fetchrow("SELECT id, reward FROM achievements WHERE condition_type = $1", condition_type)
    if not ach:
        return
    # Проверяем, не получено ли уже
    already = await conn.fetchval("SELECT 1 FROM user_achievements WHERE user_id = $1 AND achievement_id = $2", user_id, ach['id'])
    if already:
        return
    # Выдаём награду
    async with conn.transaction():
        await conn.execute("UPDATE users SET balance = balance + $1 WHERE telegram_id = $2", ach['reward'], user_id)
        await conn.execute(
            "INSERT INTO transactions (user_id, amount, tx_type, description) VALUES ($1, $2, 'achievement', $3)",
            user_id, ach['reward'], f"Достижение: {condition_type}"
        )
        await conn.execute(
            "INSERT INTO user_achievements (user_id, achievement_id) VALUES ($1, $2)",
            user_id, ach['id']
        )
    try:
        await bot.send_message(user_id, f"🏆 *Достижение разблокировано!*\n+{ach['reward']} NMP на баланс.")
    except Exception as e:
        logging.warning(f"Не удалось уведомить о достижении {user_id}: {e}")

# ---------------------------------------------------------------------------
# СЕКРЕТНЫЙ ИНВЕСТ-СИНДИКАТ
# ---------------------------------------------------------------------------
@dp.callback_query(F.data == "secret_feature_menu")
async def cb_secret_feature_menu(callback: types.CallbackQuery):
    await callback.answer()
    user_id = callback.from_user.id
    
    async with db_pool.acquire() as conn:
        total_tickets = await conn.fetchval("SELECT COUNT(*) FROM pool_entries")
        user_tickets = await conn.fetch("SELECT ticket_code FROM pool_entries WHERE user_id = $1", user_id)
        user_bal = await conn.fetchval("SELECT balance FROM users WHERE telegram_id = $1", user_id)
    
    current_pool = total_tickets * 100
    user_ticket_list = [t['ticket_code'] for t in user_tickets]
    tickets_str = ", ".join(user_ticket_list) if user_ticket_list else "нет активных билетов"
    
    desc_text = (
        "🌀 *Инвест-Синдикат NMVal Bank* 🌀\n\n"
        "Объединяйте капиталы с другими игроками в глобальный фонд! Каждое участие увеличивает общий джекпот. Чем больше у вас долей (билетов), тем выше шанс сорвать весь куш!\n\n"
        f"💰 *Текущий накопительный фонд:* `{current_pool:.2f} NMP`\n"
        f"🎟 *Всего долей в пуле:* `{total_tickets}` шт.\n\n"
        f"💳 Ваши активные доли: \n`{tickets_str}`\n\n"
        f"💵 Стоимость одной доли: *100.00 NMP*\n"
        f"Ваш текущий баланс: `{user_bal:.2f} NMP`"
    )
    
    builder = InlineKeyboardBuilder()
    builder.button(text="➕ Приобрести долю за 100 NMP", callback_data="buy_syndicate_ticket")
    builder.button(text="🔙 В меню", callback_data="main_menu")
    builder.adjust(1)
    
    await callback.message.delete()
    await callback.message.answer_photo(
        photo=SECRET_FEATURE_IMG,
        caption=desc_text,
        reply_markup=builder.as_markup(),
        parse_mode="Markdown"
    )

@dp.callback_query(F.data == "buy_syndicate_ticket")
async def cb_buy_syndicate_ticket(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    async with db_pool.acquire() as conn:
        async with conn.transaction():
            user_bal = await conn.fetchval("SELECT balance FROM users WHERE telegram_id = $1 FOR UPDATE", user_id)
            if user_bal < 100.00:
                await callback.answer("❌ Недостаточно средств! Требуется 100.00 NMP.", show_alert=True)
                return
            ticket_code = f"#SYND-{random.randint(1000, 9999)}"
            await conn.execute("UPDATE users SET balance = balance - 100 WHERE telegram_id = $1", user_id)
            await conn.execute("INSERT INTO pool_entries (user_id, ticket_code) VALUES ($1, $2)", user_id, ticket_code)
            await conn.execute(
                "INSERT INTO transactions (user_id, amount, tx_type, description) VALUES ($1, -100.00, 'syndicate_buy', $2)",
                user_id, f"Покупка доли {ticket_code} в Инвест-Синдикате"
            )
    await callback.answer(f"🎉 Доля {ticket_code} успешно приобретена!", show_alert=True)
    await cb_secret_feature_menu(callback)

# ---------------------------------------------------------------------------
# ОБРАБОТЧИК P2P ПОДТВЕРЖДЕНИЯ ОТ ПОЛЬЗОВАТЕЛЯ
# ---------------------------------------------------------------------------
@dp.message(Form.waiting_p2p_user_proof, F.text | F.photo)
async def process_p2p_user_proof(message: types.Message, state: FSMContext):
    user_id = message.from_user.id
    username = message.from_user.username or f"ID:{user_id}"
    
    async with db_pool.acquire() as conn:
        last_pending = await conn.fetchrow(
            "SELECT id FROM p2p_deposits WHERE user_id = $1 AND status = 'pending' ORDER BY created_at DESC LIMIT 1", user_id
        )
        if not last_pending:
            await message.reply("❌ У вас нет активных P2P-заявок.")
            await state.clear()
            return
        dep_id = last_pending['id']
        proof_text = message.text or (message.caption if message.caption else "Отправлен скриншот без описания")
        await conn.execute("UPDATE p2p_deposits SET comment = $1 WHERE id = $2", proof_text, dep_id)

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
# ПОПОЛНЕНИЕ (STARS & ИГРЫ)
# ---------------------------------------------------------------------------
@dp.callback_query(F.data == "buy_stars_menu")
async def cb_buy_stars_menu(callback: types.CallbackQuery):
    await callback.answer()
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
    await callback.answer()
    user_id = callback.from_user.id
    async with db_pool.acquire() as conn:
        dep_id = await conn.fetchval(
            "INSERT INTO p2p_deposits (user_id, game, amount, comment, status) VALUES ($1, 'P2P_BOT', 0, 'Инициация из меню бота', 'pending') RETURNING id",
            user_id
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
    await callback.answer()
    user_id = callback.from_user.id
    async with db_pool.acquire() as conn:
        user = await conn.fetchrow("SELECT balance, custom_handle, staking_bonus, cashback_bonus FROM users WHERE telegram_id = $1", user_id)
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
    await callback.answer()
    await state.clear()
    await callback.message.delete()
    await send_main_menu(callback.message.chat.id, "🏦 *Главный экран NMVal Bank:*")

# ---------------------------------------------------------------------------
# STARS
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
# КОМАНДА /top
# ---------------------------------------------------------------------------
@dp.message(Command("top"))
async def cmd_top(message: types.Message):
    async with db_pool.acquire() as conn:
        rows = await conn.fetch("SELECT telegram_id, username, custom_handle, balance FROM users ORDER BY balance DESC LIMIT 10")
    if not rows:
        await message.reply("Пока нет пользователей.")
        return
    text = "🏆 *Топ-10 пользователей по балансу:*\n\n"
    for i, r in enumerate(rows, 1):
        name = r['custom_handle'] or f"@{r['username']}" or str(r['telegram_id'])
        text += f"{i}. {name} – `{r['balance']:.2f} NMP`\n"
    await message.reply(text, parse_mode="Markdown")

# ---------------------------------------------------------------------------
# КОМАНДА /fortune (секретный сюрприз)
# ---------------------------------------------------------------------------
@dp.message(Command("fortune"))
async def cmd_fortune(message: types.Message):
    user_id = message.from_user.id
    async with db_pool.acquire() as conn:
        last = await conn.fetchval("SELECT last_fortune_date FROM users WHERE telegram_id = $1", user_id)
        now = datetime.now(timezone.utc)
        if last and (now - last.replace(tzinfo=timezone.utc)) < timedelta(days=1):
            await message.reply("⏳ Вы уже получали предсказание сегодня. Приходите завтра!")
            return
        # Список предсказаний
        fortunes = [
            "🌟 Сегодня ваш день! Удача на вашей стороне.",
            "💎 Инвестируйте смело – прибыль будет выше ожидаемой.",
            "🌀 Синдикат ждёт вашего участия – не упустите шанс.",
            "🎲 Судьба благоволит рисковым решениям.",
            "💰 В ближайшее время вас ждёт неожиданный доход.",
            "🌙 Луна в вашем знаке – время для крупных ставок.",
            "🔥 Ваш энтузиазм привлечёт удачу.",
            "📈 Рынок растёт – держите курс."
        ]
        prediction = random.choice(fortunes)
        bonus = 5.0
        async with conn.transaction():
            await conn.execute("UPDATE users SET balance = balance + $1, last_fortune_date = $2 WHERE telegram_id = $3", bonus, now, user_id)
            await conn.execute(
                "INSERT INTO transactions (user_id, amount, tx_type, description) VALUES ($1, $2, 'fortune', 'Предсказание судьбы')",
                user_id, bonus
            )
    await message.reply(f"🔮 *Ваше предсказание:*\n{prediction}\n\n✨ За это вы получили +{bonus:.2f} NMP!", parse_mode="Markdown")

# ---------------------------------------------------------------------------
# ADMIN COMMANDS
# ---------------------------------------------------------------------------
@dp.message(Command("give"))
async def cmd_give_shortcut(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        return
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
                await message.reply("❌ Пользователь с таким ID не найден в базе данных!")
                return
            await conn.execute("UPDATE users SET balance = balance + $1 WHERE telegram_id = $2", amount, target_uid)
            await conn.execute(
                "INSERT INTO transactions (user_id, amount, tx_type, description) VALUES ($1, $2, 'admin_direct', 'Начисление от администрации')",
                target_uid, amount
            )
            # Закрываем все pending P2P для этого пользователя
            await conn.execute("UPDATE p2p_deposits SET status = 'approved' WHERE user_id = $1 AND status = 'pending'", target_uid)
            # Логируем
            await conn.execute(
                "INSERT INTO admin_logs (admin_id, action, target_id, details) VALUES ($1, 'give', $2, $3)",
                ADMIN_ID, target_uid, f"Начислено {amount} NMP"
            )
    await message.reply(f"✅ Успешно зачислено **{amount:.2f} NMP** пользователю `{target_uid}`!")
    try:
        await bot.send_message(target_uid, f"🎁 Администратор начислил на ваш личный баланс **+{amount:.2f} NMP**!")
    except Exception as e:
        logging.warning(f"Не удалось уведомить {target_uid}: {e}")

@dp.callback_query(F.data == "wheel_spin")
async def cb_wheel_spin(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    async with db_pool.acquire() as conn:
        last_used = await conn.fetchval("SELECT daily_spin_last_used FROM users WHERE telegram_id = $1", user_id)
    now = datetime.now(timezone.utc)
    if last_used and (now - last_used.replace(tzinfo=timezone.utc)) < timedelta(days=1):
        remains = timedelta(days=1) - (now - last_used.replace(tzinfo=timezone.utc))
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
# АДМИН-ПАНЕЛЬ
# ---------------------------------------------------------------------------
@dp.callback_query(F.data == "admin_panel")
async def cb_admin_panel(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        return
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
    builder.button(text="🎰 Разыграть Синдикат", callback_data="admin_draw_syndicate")
    builder.button(text="📋 Список P2P заявок", callback_data="admin_p2p_list")
    builder.button(text="🎁 Джекпот (админ-розыгрыш)", callback_data="admin_jackpot")  # Секретная кнопка
    builder.button(text="🔙 В меню", callback_data="main_menu")
    builder.adjust(1)
    await callback.message.edit_text(text, reply_markup=builder.as_markup(), parse_mode="Markdown")

# РОЗЫГРЫШ СИНДИКАТА (с уведомлением всех участников)
@dp.callback_query(F.data == "admin_draw_syndicate")
async def cb_admin_draw_syndicate(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        return
    await callback.answer()
    
    async with db_pool.acquire() as conn:
        tickets = await conn.fetch("SELECT user_id, ticket_code FROM pool_entries")
        if not tickets:
            await callback.message.reply("❌ В Синдикате пока нет ни одной купленной доли!")
            return
        
        winner_ticket = random.choice(tickets)
        winner_uid = winner_ticket['user_id']
        winner_code = winner_ticket['ticket_code']
        total_pool = len(tickets) * 100
        payout = total_pool * 0.90
        
        # Получаем всех участников для рассылки
        all_participants = list(set([t['user_id'] for t in tickets]))
        
        async with conn.transaction():
            await conn.execute("UPDATE users SET balance = balance + $1 WHERE telegram_id = $2", payout, winner_uid)
            await conn.execute(
                "INSERT INTO transactions (user_id, amount, tx_type, description) VALUES ($1, $2, 'syndicate_win', $3)",
                winner_uid, payout, f"Победа в Инвест-Синдикате {winner_code}"
            )
            await conn.execute("DELETE FROM pool_entries")
            # Лог
            await conn.execute(
                "INSERT INTO admin_logs (admin_id, action, target_id, details) VALUES ($1, 'syndicate_draw', $2, $3)",
                ADMIN_ID, winner_uid, f"Победитель {winner_code}, выплачено {payout}"
            )
            # Достижение для победителя
            await check_and_unlock_achievement(conn, winner_uid, 'syndicate_win')
            
        winner_username = await conn.fetchval("SELECT username FROM users WHERE telegram_id = $1", winner_uid) or f"ID:{winner_uid}"
        
    broadcast_text = (
        "🌀 *ИНВЕСТ-СИНДИКАТ: РОЗЫГРЫШ ЗАВЕРШЕН!* 🌀\n\n"
        f"👑 Счастливый билет: `{winner_code}`\n"
        f"👤 Победитель: @{winner_username}\n"
        f"💰 Сумма выигрыша: **+{payout:.2f} NMP** (90% от пула)!\n\n"
        "Пул сброшен. Начинается новый раунд инвестирования! 🚀"
    )
    
    # Уведомляем победителя
    try:
        await bot.send_message(winner_uid, f"🎁 Поздравляем! Ваш билет `{winner_code}` выиграл в Инвест-Синдикате! Начислено: **+{payout:.2f} NMP**!")
    except Exception as e:
        logging.warning(f"Не удалось уведомить победителя {winner_uid}: {e}")
    
    # Уведомляем всех участников
    for uid in all_participants:
        if uid != winner_uid:
            try:
                await bot.send_message(uid, f"🌀 Синдикат разыгран! Победитель: @{winner_username}. Ждём новый раунд!")
            except:
                pass
    
    await callback.message.reply(broadcast_text, parse_mode="Markdown")

# СЕКРЕТНЫЙ АДМИН-ДЖЕКПОТ
@dp.callback_query(F.data == "admin_jackpot")
async def cb_admin_jackpot(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        return
    await callback.answer()
    async with db_pool.acquire() as conn:
        # Выбираем случайного пользователя с балансом > 0
        user = await conn.fetchrow("SELECT telegram_id, username FROM users WHERE balance > 0 ORDER BY RANDOM() LIMIT 1")
        if not user:
            await callback.message.reply("❌ Нет активных пользователей с балансом > 0.")
            return
        uid = user['telegram_id']
        prize = 1000.0
        async with conn.transaction():
            await conn.execute("UPDATE users SET balance = balance + $1 WHERE telegram_id = $2", prize, uid)
            await conn.execute(
                "INSERT INTO transactions (user_id, amount, tx_type, description) VALUES ($1, $2, 'admin_jackpot', 'Администраторский джекпот')",
                uid, prize
            )
            await conn.execute(
                "INSERT INTO admin_logs (admin_id, action, target_id, details) VALUES ($1, 'jackpot', $2, $3)",
                ADMIN_ID, uid, f"Выигрыш джекпота {prize} NMP"
            )
    await callback.message.reply(f"🎁 *ДЖЕКПОТ!*\n\nПользователь @{user['username'] or uid} получил **+{prize:.2f} NMP**!")
    try:
        await bot.send_message(uid, f"🎉 Вы выиграли администраторский джекпот! +{prize:.2f} NMP на баланс!")
    except Exception as e:
        logging.warning(f"Не удалось уведомить победителя джекпота {uid}: {e}")

# АДМИН-СПИСОК P2P ЗАЯВОК
@dp.callback_query(F.data == "admin_p2p_list")
async def cb_admin_p2p_list(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        return
    await callback.answer()
    async with db_pool.acquire() as conn:
        rows = await conn.fetch("SELECT id, user_id, amount, comment, created_at FROM p2p_deposits WHERE status = 'pending' ORDER BY created_at ASC LIMIT 10")
    if not rows:
        await callback.message.edit_text("📭 Нет ожидающих P2P-заявок.")
        return
    text = "📋 *Ожидающие P2P-заявки:*\n\n"
    builder = InlineKeyboardBuilder()
    for r in rows:
        text += f"Заявка #{r['id']} | Пользователь: `{r['user_id']}` | Сумма: `{r['amount']}`\n"
        builder.button(text=f"✅ Подтвердить #{r['id']}", callback_data=f"admin_p2p_approve_{r['id']}")
        builder.button(text=f"❌ Отклонить #{r['id']}", callback_data=f"admin_p2p_reject_{r['id']}")
        builder.row()
    builder.button(text="🔙 Назад", callback_data="admin_panel")
    await callback.message.edit_text(text, reply_markup=builder.as_markup(), parse_mode="Markdown")

@dp.callback_query(F.data.startswith("admin_p2p_approve_"))
async def cb_admin_p2p_approve(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        return
    dep_id = int(callback.data.split("_")[3])
    async with db_pool.acquire() as conn:
        dep = await conn.fetchrow("SELECT user_id, amount FROM p2p_deposits WHERE id = $1 AND status = 'pending'", dep_id)
        if not dep:
            await callback.answer("❌ Заявка уже обработана.", show_alert=True)
            return
        async with conn.transaction():
            await conn.execute("UPDATE p2p_deposits SET status = 'approved' WHERE id = $1", dep_id)
            # Начисляем сумму (amount – это сумма игровой валюты, но мы просто начисляем NMP, здесь можно умножить на курс)
            # Для простоты начисляем amount * 1.4 (как в инструкции)
            nmp = float(dep['amount']) * 1.4
            await conn.execute("UPDATE users SET balance = balance + $1 WHERE telegram_id = $2", nmp, dep['user_id'])
            await conn.execute(
                "INSERT INTO transactions (user_id, amount, tx_type, description) VALUES ($1, $2, 'p2p_approved', 'P2P пополнение подтверждено')",
                dep['user_id'], nmp
            )
            await conn.execute(
                "INSERT INTO admin_logs (admin_id, action, target_id, details) VALUES ($1, 'p2p_approve', $2, $3)",
                ADMIN_ID, dep['user_id'], f"Заявка #{dep_id}, начислено {nmp} NMP"
            )
    await callback.answer("✅ Заявка подтверждена, средства начислены.", show_alert=True)
    await cb_admin_p2p_list(callback)

@dp.callback_query(F.data.startswith("admin_p2p_reject_"))
async def cb_admin_p2p_reject(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        return
    dep_id = int(callback.data.split("_")[3])
    async with db_pool.acquire() as conn:
        dep = await conn.fetchrow("SELECT user_id FROM p2p_deposits WHERE id = $1 AND status = 'pending'", dep_id)
        if not dep:
            await callback.answer("❌ Заявка уже обработана.", show_alert=True)
            return
        async with conn.transaction():
            await conn.execute("UPDATE p2p_deposits SET status = 'rejected' WHERE id = $1", dep_id)
            await conn.execute(
                "INSERT INTO admin_logs (admin_id, action, target_id, details) VALUES ($1, 'p2p_reject', $2, $3)",
                ADMIN_ID, dep['user_id'], f"Заявка #{dep_id} отклонена"
            )
    await callback.answer("❌ Заявка отклонена.", show_alert=True)
    await cb_admin_p2p_list(callback)

# Остальные админ-функции (выдача, поиск, промо) остаются без изменений (они уже были)
@dp.callback_query(F.data == "admin_give_coins")
async def cb_admin_give_coins(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        return
    await callback.message.edit_text("Чтобы выдать баланс, используйте команду:\n`/give [ID_получателя] [количество_NMP]`\n\nПример: `/give 123456789 500`")
    await callback.answer()

@dp.callback_query(F.data == "admin_search_user")
async def cb_admin_search_user(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID:
        return
    await callback.answer()
    await state.set_state(Form.waiting_admin_search_user)
    await callback.message.edit_text("🔍 Введите числовой Telegram ID пользователя для просмотра его статистики:")

@dp.message(Form.waiting_admin_search_user)
async def process_admin_search_user(message: types.Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
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
        invited_count = await conn.fetchval("SELECT COUNT(*) FROM users WHERE referrer_id = $1", target_id)
    handle_str = f"🏷 Кастомный никнейм: `{user['custom_handle']}`" if user['custom_handle'] else "Кастомный никнейм: отсутствует"
    text = (
        f"👤 *Информация о пользователе `{target_id}`*\n\n"
        f"📝 Имя пользователя: @{user['username'] or 'User'}\n"
        f"{handle_str}\n"
        f"💵 Баланс: `{user['balance']:.2f} NMP`\n\n"
        f"📊 *Статистика за 30 дней:*\n"
        f"📥 Получено: `+{earned:.2f} NMP`\n"
        f"📤 Списано: `-{spent:.2f} NMP`\n\n"
        f"📈 Стейкинг: `{9.2 + stake_b:.2f}%` в месяц\n"
        f"🛍 Кэшбэк: `{1.0 + cash_b:.2f}%` на все покупки\n\n"
        f"🔒 Активных вкладов: `{active_locks}`\n"
        f"👥 Приглашено друзей: `{invited_count}`"
    )
    await message.reply(text, parse_mode="Markdown")
    await state.clear()

@dp.callback_query(F.data == "admin_create_promo")
async def cb_admin_create_promo(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID:
        return
    await callback.answer()
    await state.set_state(Form.waiting_admin_promo_code)
    await callback.message.edit_text("🎫 Введите кодовое слово для нового промокода (например, VIP50):")

@dp.message(Form.waiting_admin_promo_code)
async def process_admin_promo_code(message: types.Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    await state.update_data(new_promo_code=message.text.strip())
    await state.set_state(Form.waiting_admin_promo_reward)
    await message.reply("💰 Введите размер награды в NMP за активацию этого промокода:")

@dp.message(Form.waiting_admin_promo_reward)
async def process_admin_promo_reward(message: types.Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
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
    if message.from_user.id != ADMIN_ID:
        return
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
        await conn.execute(
            "INSERT INTO admin_logs (admin_id, action, target_id, details) VALUES ($1, 'create_promo', NULL, $2)",
            ADMIN_ID, f"Промокод {code}, награда {reward}, лимит {uses}"
        )
    await message.reply(f"✅ Промокод `{code}` успешно создан!\n🎁 Награда: `{reward:.2f} NMP` | Лимит активаций: `{uses}`")
    await state.clear()

# ---------------------------------------------------------------------------
# CHECKS (с пагинацией)
# ---------------------------------------------------------------------------
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
    builder.button(text="📋 История моих чеков", callback_data="my_checks_history:0")
    builder.button(text="🔙 Назад", callback_data="main_menu")
    builder.adjust(1)
    await callback.message.edit_text(text, reply_markup=builder.as_markup(), parse_mode="Markdown")

@dp.callback_query(F.data.startswith("my_checks_history:"))
async def cb_my_checks_history(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer()
    offset = int(callback.data.split(":")[1]) if ":" in callback.data else 0
    user_id = callback.from_user.id
    limit = 5
    async with db_pool.acquire() as conn:
        checks = await conn.fetch(
            "SELECT * FROM checks WHERE creator_id = $1 ORDER BY created_at DESC OFFSET $2 LIMIT $3",
            user_id, offset, limit
        )
        total = await conn.fetchval("SELECT COUNT(*) FROM checks WHERE creator_id = $1", user_id)
    if not checks:
        await callback.message.edit_text("📭 Вы ещё не создавали чеков.")
        return
    text = "📋 *История ваших чеков:*\n\n"
    bot_info = await bot.get_me()
    for idx, c in enumerate(checks, offset+1):
        text += (
            f"{idx}. Код: `{c['code']}` | Сумма: `{c['amount']:.2f} NMP`\n"
            f"📊 Использовано: `{c['claimed_count']}/{c['max_claims']}`\n"
            f"🔗 Ссылка: `https://t.me/{bot_info.username}?start={c['code']}`\n\n"
        )
    builder = InlineKeyboardBuilder()
    if offset > 0:
        builder.button(text="◀️ Назад", callback_data=f"my_checks_history:{offset-limit}")
    if offset + limit < total:
        builder.button(text="Вперёд ▶️", callback_data=f"my_checks_history:{offset+limit}")
    builder.button(text="🔙 Назад", callback_data="checks_menu")
    builder.adjust(2)
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
            # Достижение
            await check_and_unlock_achievement(conn, user_id, 'check_created')
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
            except Exception as e:
                logging.warning(f"Не удалось уведомить создателя чека: {e}")

# ---------------------------------------------------------------------------
# DEPOSITS (с досрочным закрытием)
# ---------------------------------------------------------------------------
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
        builder = InlineKeyboardBuilder()
        for d in active_deposits:
            text += f"• #{d['id']} Сумма: `{d['amount']:.2f} NMP` | Ставка: `{d['rate']}%` | Конец: `{d['end_date'].strftime('%Y-%m-%d')}`\n"
            builder.button(text=f"Закрыть досрочно #{d['id']}", callback_data=f"deposit_close_{d['id']}")
        builder.button(text="🔙 В меню", callback_data="main_menu")
        builder.adjust(1)
    else:
        text += "У вас нет активных вкладов."
        builder = InlineKeyboardBuilder()
        builder.button(text="💼 Открыть Вклад", callback_data="deposit_open_start")
        builder.button(text="🔙 В меню", callback_data="main_menu")
        builder.adjust(1)
    await callback.message.edit_text(text, reply_markup=builder.as_markup(), parse_mode="Markdown")

@dp.callback_query(F.data.startswith("deposit_close_"))
async def cb_deposit_close(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer()
    dep_id = int(callback.data.split("_")[2])
    user_id = callback.from_user.id
    async with db_pool.acquire() as conn:
        dep = await conn.fetchrow("SELECT * FROM lock_deposits WHERE id = $1 AND user_id = $2 AND is_active = TRUE", dep_id, user_id)
        if not dep:
            await callback.message.reply("❌ Вклад не найден или уже закрыт.")
            return
    await state.update_data(deposit_id=dep_id)
    await state.set_state(Form.confirm_close_deposit)
    builder = InlineKeyboardBuilder()
    builder.button(text="✅ Да, закрыть", callback_data="deposit_confirm_close")
    builder.button(text="❌ Отмена", callback_data="deposits_menu")
    await callback.message.edit_text(
        f"⚠️ *Досрочное закрытие вклада #{dep_id}*\n\n"
        f"Сумма: `{dep['amount']:.2f} NMP`\n"
        f"Проценты будут потеряны, комиссия 5% от тела вклада.\n"
        f"Вы получите: `{dep['amount'] * 0.95:.2f} NMP`.\n\n"
        "Подтвердите действие:",
        reply_markup=builder.as_markup(),
        parse_mode="Markdown"
    )

@dp.callback_query(F.data == "deposit_confirm_close", Form.confirm_close_deposit)
async def cb_deposit_confirm_close(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer()
    user_id = callback.from_user.id
    data = await state.get_data()
    dep_id = data.get('deposit_id')
    if not dep_id:
        await callback.message.reply("Ошибка, попробуйте снова.")
        await state.clear()
        return
    async with db_pool.acquire() as conn:
        async with conn.transaction():
            dep = await conn.fetchrow("SELECT * FROM lock_deposits WHERE id = $1 AND user_id = $2 AND is_active = TRUE FOR UPDATE", dep_id, user_id)
            if not dep:
                await callback.message.reply("❌ Вклад уже закрыт.")
                await state.clear()
                return
            payout = float(dep['amount']) * 0.95  # 5% комиссия
            await conn.execute("UPDATE lock_deposits SET is_active = FALSE WHERE id = $1", dep_id)
            await conn.execute("UPDATE users SET balance = balance + $1 WHERE telegram_id = $2", payout, user_id)
            await conn.execute(
                "INSERT INTO transactions (user_id, amount, tx_type, description) VALUES ($1, $2, 'deposit_early_close', 'Досрочное закрытие вклада')",
                user_id, payout
            )
    await callback.message.edit_text(f"✅ Вклад #{dep_id} закрыт досрочно. Начислено: `{payout:.2f} NMP`.")
    await state.clear()
    await cb_deposits_menu(callback)

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
    if not amount:
        return
    _, m_str, r_str = callback.data.split(":")
    months, rate = int(m_str), float(r_str)
    user_id = callback.from_user.id
    end_date = datetime.now(timezone.utc) + timedelta(days=months * 30)
    
    async with db_pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("UPDATE users SET balance = balance - $1 WHERE telegram_id = $2", amount, user_id)
            await conn.execute("INSERT INTO lock_deposits (user_id, amount, rate, end_date) VALUES ($1, $2, $3, $4)", user_id, amount, rate, end_date)
            await conn.execute("INSERT INTO transactions (user_id, amount, tx_type, description) VALUES ($1, $2, 'deposit_open', $3)", user_id, -amount, f"Заморозка вклада под {rate}%")
            # Достижение
            await check_and_unlock_achievement(conn, user_id, 'first_deposit')
    await callback.message.edit_text(f"🎉 *Вклад открыт!*\nСумма `{amount:.2f} NMP` заморожена под `{rate}% годовых` до `{end_date.strftime('%Y-%m-%d')}`.", parse_mode="Markdown")
    await state.clear()
    await send_main_menu(callback.message.chat.id, "Выберите действие:")

# ---------------------------------------------------------------------------
# NICKNAMES
# ---------------------------------------------------------------------------
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

# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------
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

# ---------------------------------------------------------------------------
# PROMO
# ---------------------------------------------------------------------------
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
            # Достижение
            await check_and_unlock_achievement(conn, user_id, 'promo_used')
    await message.reply(f"🎉 Начислено +{promo['reward']:.2f} NMP!")
    await state.clear()
    await send_main_menu(message.chat.id, "Выберите действие:")

# ---------------------------------------------------------------------------
# BACKGROUND INTEREST SYSTEM
# ---------------------------------------------------------------------------
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
                    try:
                        await bot.send_message(user_id, f"📈 Проценты зачислены: *+{bonus:.2f} NMP*!")
                    except Exception as e:
                        logging.warning(f"Не удалось уведомить о стейкинге {user_id}: {e}")
                
                # Истекшие депозиты
                expired_deposits = await conn.fetch("SELECT * FROM lock_deposits WHERE is_active = TRUE AND end_date <= NOW()")
                for d in expired_deposits:
                    payout = float(d['amount']) * (1 + (float(d['rate']) / 100.0))
                    async with conn.transaction():
                        await conn.execute("UPDATE lock_deposits SET is_active = FALSE WHERE id = $1", d['id'])
                        await conn.execute("UPDATE users SET balance = balance + $1 WHERE telegram_id = $2", payout, d['user_id'])
                        await conn.execute("INSERT INTO transactions (user_id, amount, tx_type, description) VALUES ($1, $2, 'deposit_payout', 'Выплата вклада')", d['user_id'], payout)
                    try:
                        await bot.send_message(d['user_id'], f"📈 Вклад закрыт! Выплачено: *{payout:.2f} NMP*!")
                    except Exception as e:
                        logging.warning(f"Не удалось уведомить о выплате вклада {d['user_id']}: {e}")
        except Exception as e:
            logging.error(f"Шедулер ошибка: {e}", exc_info=True)

# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------
async def main():
    logging.basicConfig(level=logging.INFO)
    await init_db()
    asyncio.create_task(financial_background_scheduler())
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
