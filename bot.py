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
from aiogram.types import InlineQueryResultArticle, InputTextMessageContent, LabeledPrice
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL") 
ADMIN_ID = int(os.getenv("ADMIN_ID", 0))

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
    waiting_check_amount = State()
    waiting_check_claims = State()
    waiting_custom_handle = State()
    waiting_deposit_amount = State()
    # Состояние ожидания ввода суммы начисления для P2P-заявки
    waiting_admin_p2p_nmp_amount = State()

async def init_db():
    global db_pool
    db_pool = await asyncpg.create_pool(dsn=DATABASE_URL)
    
    # Автоматическое создание таблицы P2P заявок, если она отсутствует
    async with db_pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS p2p_deposits (
                id SERIAL PRIMARY KEY,
                user_id BIGINT NOT NULL,
                game VARCHAR(50) NOT NULL,
                amount NUMERIC NOT NULL,
                comment TEXT,
                status VARCHAR(20) DEFAULT 'pending',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

# Получение бустов от NFT
async def get_user_boosts(conn, user_id: int):
    row = await conn.fetchrow(
        """SELECT COALESCE(SUM(n.boost_staking_pct), 0) as stake_boost, 
                  COALESCE(SUM(n.boost_cashback_pct), 0) as cash_boost 
           FROM user_nfts un 
           JOIN nfts n ON un.nft_id = n.id 
           WHERE un.user_id = $1""", user_id
    )
    return float(row['stake_boost']), float(row['cash_boost'])

# Статистика заработка и трат пользователя за месяц
async def get_user_monthly_stats(conn, user_id: int):
    # Потрачено (сумма отрицательных транзакций за 30 дней)
    spent = await conn.fetchval(
        "SELECT ABS(COALESCE(SUM(amount), 0)) FROM transactions WHERE user_id = $1 AND amount < 0 AND created_at >= NOW() - INTERVAL '30 days'",
        user_id
    )
    # Заработано (сумма положительных транзакций за 30 дней, кроме приветственного бонуса)
    earned = await conn.fetchval(
        "SELECT COALESCE(SUM(amount), 0) FROM transactions WHERE user_id = $1 AND amount > 0 AND tx_type != 'welcome' AND created_at >= NOW() - INTERVAL '30 days'",
        user_id
    )
    return float(earned), float(spent)

# СТАРТ / ОБРАБОТКА ССЫЛОК ПЛАТЕЖЕЙ И P2P ДЕПОЗИТОВ
@dp.message(CommandStart())
async def cmd_start(message: types.Message):
    user_id = message.from_user.id
    username = message.from_user.username or "User"
    
    args = message.text.split()
    referrer_id = None
    check_code_to_claim = None
    
    if len(args) > 1:
        payload = args[1]
        
        # 🎮 ОБРАБОТКА ИГРОВЫХ P2P ДЕПОЗИТОВ ИЗ MINI APP
        if payload.startswith("dep_p2p_"):
            try:
                # Шаблон ссылки: dep_p2p_{игра}_a_{кол-во}_c_{base64}
                parts = payload.split("_")
                game = parts[2]
                amount = float(parts[4])
                encoded_comment = parts[6]
                
                # Безопасно декодируем комментарий из Base64
                encoded_comment += "=" * ((4 - len(encoded_comment) % 4) % 4) # Восстановление паддинга
                decoded_bytes = base64.b64decode(encoded_comment)
                comment = urllib.parse.unquote(decoded_bytes.decode('utf-8'))
            except Exception as e:
                logging.error(f"Ошибка парсинга P2P ссылки: {e}")
                await message.reply("❌ Ошибка обработки ссылки на пополнение. Попробуйте еще раз из Mini App.")
                return

            async with db_pool.acquire() as conn:
                # Проверяем регистрацию пользователя
                user = await conn.fetchrow("SELECT * FROM users WHERE telegram_id = $1", user_id)
                if not user:
                    await conn.execute(
                        "INSERT INTO users (telegram_id, username) VALUES ($1, $2)",
                        user_id, username
                    )
                    await conn.execute(
                        "INSERT INTO transactions (user_id, amount, tx_type, description) VALUES ($1, 100.00, 'welcome', 'Приветственный бонус')",
                        user_id
                    )
                
                # Создаем запись о пополнении со статусом 'pending'
                dep_id = await conn.fetchval(
                    "INSERT INTO p2p_deposits (user_id, game, amount, comment) VALUES ($1, $2, $3, $4) RETURNING id",
                    user_id, game, amount, comment
                )

            # Формируем интерактивное уведомление для админа
            admin_text = (
                f"🎮 *Новая заявка на P2P-пополнение!* (Заявка #{dep_id})\n\n"
                f"👤 *Отправитель:* @{username} (ID: `{user_id}`)\n"
                f"🎲 *Игра:* `{game.upper()}`\n"
                f"💰 *Количество валюты:* `{amount}`\n"
                f"📝 *Реквизиты/Ник/Ссылка:* `{comment}`\n\n"
                f"⚠️ *Формула расчёта:* 1 Star (NMP) = `[Валюта] * [Курс в боте] * 1.40`\n"
                f"Проверьте перевод на свои реквизиты и выберите действие:"
            )
            
            builder = InlineKeyboardBuilder()
            builder.button(text="✅ Одобрить", callback_data=f"adm_p2p_approve:{dep_id}:{user_id}")
            builder.button(text="❌ Отклонить", callback_data=f"adm_p2p_reject:{dep_id}:{user_id}")
            builder.adjust(2)
            
            try:
                await bot.send_message(ADMIN_ID, admin_text, reply_markup=builder.as_markup(), parse_mode="Markdown")
                await message.reply("✅ *Заявка на пополнение успешно отправлена админу!*\nОжидайте проверки и начисления средств.")
            except Exception as e:
                logging.error(f"Не удалось уведомить админа: {e}")
                await message.reply("❌ Произошел сбой уведомления администрации. Свяжитесь напрямую с техподдержкой.")
            return

        # Обработка глубоких переходов на оплату с сайта через Telegram Stars
        elif payload.startswith("pay_"):
            parts = payload.split("_")
            pay_type = parts[1] # "stars"
            amount = float(parts[2])
            
            if pay_type == "stars":
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
            return

        elif payload.startswith("c_"):
            check_code_to_claim = payload
        elif payload.isdigit():
            referrer_id = int(payload)
            if referrer_id == user_id:
                referrer_id = None

    async with db_pool.acquire() as conn:
        user = await conn.fetchrow("SELECT * FROM users WHERE telegram_id = $1", user_id)
        
        if not user:
            await conn.execute(
                "INSERT INTO users (telegram_id, username, referrer_id) VALUES ($1, $2, $3)",
                user_id, username, referrer_id
            )
            await conn.execute(
                "INSERT INTO transactions (user_id, amount, tx_type, description) VALUES ($1, 100.00, 'welcome', 'Приветственный бонус')",
                user_id
            )
            
            if referrer_id:
                ref_user = await conn.fetchrow("SELECT * FROM users WHERE telegram_id = $1", referrer_id)
                if ref_user:
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

        if check_code_to_claim:
            await claim_check_logic(message, check_code_to_claim)
            return

    await send_main_menu(message.chat.id, welcome_text)

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

@dp.callback_query(F.data == "main_menu")
async def cb_main_menu(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.delete()
    await send_main_menu(callback.message.chat.id, "🏦 *Главный экран NMVal Bank:*")

# --- МОЙ КАБИНЕТ (С добавлением статистики за месяц) ---
@dp.callback_query(F.data == "my_account")
async def cb_my_account(callback: types.CallbackQuery):
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

# Меню пополнения через бота
@dp.callback_query(F.data == "buy_stars_menu")
async def cb_buy_stars_menu(callback: types.CallbackQuery):
    text = (
        "📥 *Пополнение баланса NMP*\n\n"
        "Выберите удобный способ оплаты пакета валюты:"
    )
    builder = InlineKeyboardBuilder()
    builder.button(text="⭐️ Telegram Stars (Курс 1=3)", callback_data="stars_list")
    builder.button(text="🎮 Игровая валюта P2P (Без ограничений)", callback_data="p2p_info_menu")
    builder.button(text="🔙 Назад", callback_data="my_account")
    builder.adjust(1)
    await callback.message.edit_text(text, reply_markup=builder.as_markup(), parse_mode="Markdown")

# Информация о пополнении игровой валютой (P2P)
@dp.callback_query(F.data == "p2p_info_menu")
async def cb_p2p_info_menu(callback: types.CallbackQuery):
    text = (
        "🎮 *Пополнение через Игровую валюту (P2P)*\n\n"
        "Вы можете обменять свою игровую валюту (скины, золото, робуксы, алмазы) на внутреннюю валюту банка NMP!\n\n"
        "📌 *Как это сделать:*\n"
        "1. Зайдите в наш Mini App на сайте.\n"
        "2. Нажмите кнопку **Пополнить** -> пролистайте до блока **Игровой перевод (P2P)**.\n"
        "3. Выберите вашу игру, укажите сумму перевода и ссылку на трейд/ваш ник в игре.\n"
        "4. Нажмите «Отправить заявку». Администрация проверит перевод и начислит средства!\n\n"
        "💡 *Курс начисления:* вся переданная игровая валюта пересчитывается админом по курсу и умножается на **1.40** за 1 Telegram Star (NMP)!"
    )
    builder = InlineKeyboardBuilder()
    builder.button(text="🔙 Назад", callback_data="buy_stars_menu")
    await callback.message.edit_text(text, reply_markup=builder.as_markup(), parse_mode="Markdown")

@dp.callback_query(F.data == "stars_list")
async def cb_stars_list(callback: types.CallbackQuery):
    builder = InlineKeyboardBuilder()
    builder.button(text="🪙 100 NMP (за 33 Stars)", callback_data="buy_pack:100:33")
    builder.button(text="🪙 300 NMP (за 100 Stars)", callback_data="buy_pack:300:100")
    builder.button(text="🪙 900 NMP (за 300 Stars)", callback_data="buy_pack:900:300")
    builder.button(text="🔙 Назад", callback_data="buy_stars_menu")
    builder.adjust(1)
    await callback.message.edit_text("Выберите пакет пополнения за Stars:", reply_markup=builder.as_markup())

@dp.callback_query(F.data.startswith("buy_pack:"))
async def cb_buy_pack(callback: types.CallbackQuery):
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
    await callback.answer()

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

# --- ЧЕКИ ---
@dp.callback_query(F.data == "checks_menu")
async def cb_checks_menu(callback: types.CallbackQuery):
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

# Обналичивание чеков
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
    async with db_pool.acquire() as conn:
        nfts = await conn.fetch("SELECT id, name, price FROM nfts ORDER BY id LIMIT 10")
        
    text = "🛍 *NFT-Маркетплейс NMVal Bank*\n\nВыберите интересующий NFT:"
    builder = InlineKeyboardBuilder()
    
    for n in nfts:
        builder.button(text=f"🖼 {n['name']} — {n['price']:.0f} NMP", callback_data=f"view_nft:{n['id']}")
    builder.button(text="🔙 Назад", callback_data="main_menu")
    builder.adjust(1)
    await callback.message.edit_text(text, reply_markup=builder.as_markup(), parse_mode="Markdown")

@dp.callback_query(F.data.startswith("view_nft:"))
async def cb_view_nft(callback: types.CallbackQuery):
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

# Колесо фортуны
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
        await callback.answer(f"❌ Колесо можно крутить только раз в 24 часа! Доступно через {hours}ч {minutes}м.", show_alert=True)
        return
        
    prizes = [("0.50 NMP", 0.5), ("1.00 NMP", 1.0), ("5.00 NMP", 5.0), ("10.00 NMP", 10.0), ("27.00 NMP", 27.0)]
    prize_name, prize_val = random.choices(prizes, weights=[50, 30, 14, 5, 1], k=1)[0]
    
    async with db_pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("UPDATE users SET balance = balance + $1, daily_spin_last_used = NOW() WHERE telegram_id = $2", prize_val, user_id)
            await conn.execute("INSERT INTO transactions (user_id, amount, tx_type, description) VALUES ($1, $2, 'spin', 'Колесо Фортуны')", user_id, prize_val)
            
    await callback.message.edit_text(f"🎰 *Колесо Фортуны!*\n\nВы выиграли: **{prize_name}**! Баланс обновлен.", reply_markup=InlineKeyboardBuilder().button(text="🔙 В меню", callback_data="main_menu").as_markup(), parse_mode="Markdown")

# Кастомный никнейм
@dp.callback_query(F.data == "custom_handle_menu")
async def cb_custom_handle(callback: types.CallbackQuery):
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

# API
@dp.callback_query(F.data == "merchant_api")
async def cb_merchant_api(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    async with db_pool.acquire() as conn:
        key = await conn.fetchrow("SELECT api_key FROM merchant_api_keys WHERE user_id = $1", user_id)
        if not key:
            await conn.execute("INSERT INTO merchant_api_keys (user_id, service_name) VALUES ($1, 'По умолчанию')", user_id)
            key = await conn.fetchrow("SELECT api_key FROM merchant_api_keys WHERE user_id = $1", user_id)
    text = f"🔗 *NMVal Merchant API*\n\n🔑 Токен:\n`{key['api_key']}`"
    builder = InlineKeyboardBuilder().button(text="🔙 В меню", callback_data="main_menu")
    await callback.message.edit_text(text, reply_markup=builder.as_markup(), parse_mode="Markdown")

# Промокоды
@dp.callback_query(F.data == "promo_activate")
async def cb_promo_activate(callback: types.CallbackQuery, state: FSMContext):
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

# --- АДМИНКА ---
@dp.callback_query(F.data == "admin_panel")
async def cb_admin_panel(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID: return
    async with db_pool.acquire() as conn:
        total_users = await conn.fetchval("SELECT COUNT(*) FROM users")
        total_balance = await conn.fetchval("SELECT SUM(balance) FROM users")
    text = f"⚙️ *Админка*\n\nЮзеров: `{total_users}`\nБалансы: `{total_balance:.2f} NMP`"
    builder = InlineKeyboardBuilder()
    builder.button(text="✍️ Выдать баланс (FSM)", callback_data="admin_give_coins")
    builder.button(text="🎫 Создать Промо", callback_data="admin_create_promo")
    builder.button(text="🔙 В меню", callback_data="main_menu")
    builder.adjust(1)
    await callback.message.edit_text(text, reply_markup=builder.as_markup(), parse_mode="Markdown")

@dp.callback_query(F.data == "admin_give_coins")
async def cb_admin_give_coins(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID: return
    await state.set_state(Form.waiting_admin_user_id)
    await callback.message.edit_text("Введите ID получателя:")

@dp.message(Form.waiting_admin_user_id)
async def process_admin_uid(message: types.Message, state: FSMContext):
    await state.update_data(target_id=int(message.text))
    await state.set_state(Form.waiting_admin_amount)
    await message.reply("Введите сумму:")

@dp.message(Form.waiting_admin_amount)
async def process_admin_val(message: types.Message, state: FSMContext):
    data = await state.get_data()
    target_id, amount = data['target_id'], float(message.text)
    async with db_pool.acquire() as conn:
        await conn.execute("UPDATE users SET balance = balance + $1 WHERE telegram_id = $2", amount, target_id)
        await conn.execute("INSERT INTO transactions (user_id, amount, tx_type, description) VALUES ($1, $2, 'admin', 'Админ-начисление')", target_id, amount)
    await message.reply(f"Успешно зачислено {amount} NMP юзеру {target_id}!")
    await state.clear()

# --- ОБРАБОТЧИКИ МОДЕРАЦИИ ИГРОВЫХ P2P ПЛАТЕЖЕЙ ДЛЯ АДМИНИСТРАТОРА ---

# 1. Отклонение заявки
@dp.callback_query(F.data.startswith("adm_p2p_reject:"))
async def cb_adm_p2p_reject(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID: return
    
    _, dep_id_str, target_uid_str = callback.data.split(":")
    dep_id = int(dep_id_str)
    target_uid = int(target_uid_str)
    
    async with db_pool.acquire() as conn:
        status = await conn.fetchval("SELECT status FROM p2p_deposits WHERE id = $1", dep_id)
        if status != 'pending':
            await callback.answer("⚠️ Эта заявка уже была обработана ранее!", show_alert=True)
            return
            
        await conn.execute("UPDATE p2p_deposits SET status = 'rejected' WHERE id = $1", dep_id)
        
    await callback.message.edit_text(
        f"❌ *Заявка #{dep_id} отклонена.*\nПользователь ID: `{target_uid}` уведомлен.",
        parse_mode="Markdown"
    )
    
    try:
        await bot.send_message(target_uid, "❌ Ваша заявка на P2P пополнение счета игровой валютой была отклонена администратором.")
    except Exception:
        pass
    await callback.answer()

# 2. Переход к одобрению и вводу NMP
@dp.callback_query(F.data.startswith("adm_p2p_approve:"))
async def cb_adm_p2p_approve(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID: return
    
    _, dep_id_str, target_uid_str = callback.data.split(":")
    dep_id = int(dep_id_str)
    target_uid = int(target_uid_str)
    
    async with db_pool.acquire() as conn:
        status = await conn.fetchval("SELECT status FROM p2p_deposits WHERE id = $1", dep_id)
        if status != 'pending':
            await callback.answer("⚠️ Эта заявка уже была обработана ранее!", show_alert=True)
            return
            
    # Задаем FSM context данные
    await state.set_state(Form.waiting_admin_p2p_nmp_amount)
    await state.update_data(
        p2p_dep_id=dep_id, 
        p2p_target_uid=target_uid, 
        p2p_admin_msg_id=callback.message.message_id
    )
    
    await callback.message.reply(
        f"Введите количество **NMP**, которое нужно зачислить пользователю `{target_uid}` за заявку #{dep_id}:\n"
        f"*(Напоминание: умножайте количество переданных игровых ресурсов на коэффициент 1.40)*"
    )
    await callback.answer()

# 3. Фиксация ввода админом и зачисление монет пользователю
@dp.message(Form.waiting_admin_p2p_nmp_amount)
async def process_admin_p2p_amount(message: types.Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID: return
    
    try:
        nmp_amount = float(message.text)
        if nmp_amount <= 0: raise ValueError()
    except ValueError:
        await message.reply("❌ Введите корректное положительное число:")
        return
        
    data = await state.get_data()
    dep_id = data['p2p_dep_id']
    target_uid = data['p2p_target_uid']
    admin_msg_id = data['p2p_admin_msg_id']
    
    async with db_pool.acquire() as conn:
        async with conn.transaction():
            # Блокировка от двойного начисления
            status = await conn.fetchval("SELECT status FROM p2p_deposits WHERE id = $1 FOR UPDATE", dep_id)
            if status != 'pending':
                await message.reply("❌ Эта заявка уже была обработана другим процессом!")
                await state.clear()
                return
                
            # Обновляем статус заявки и баланс
            await conn.execute("UPDATE p2p_deposits SET status = 'approved' WHERE id = $1", dep_id)
            await conn.execute("UPDATE users SET balance = balance + $1 WHERE telegram_id = $2", nmp_amount, target_uid)
            await conn.execute(
                "INSERT INTO transactions (user_id, amount, tx_type, description) VALUES ($1, $2, 'p2p_deposit', $3)",
                target_uid, nmp_amount, f"P2P пополнение #{dep_id}"
            )
            
    # Меняем оригинальное сообщение админки
    try:
        await bot.edit_message_text(
            chat_id=ADMIN_ID,
            message_id=admin_msg_id,
            text=f"✅ *Заявка #{dep_id} одобрена!*\nНачислено: `{nmp_amount:.2f} NMP` пользователю `{target_uid}`.",
            parse_mode="Markdown"
        )
    except Exception:
        pass
        
    await message.reply(f"🎉 Успешно начислено **{nmp_amount:.2f} NMP** пользователю `{target_uid}`!")
    
    # Уведомляем счастливого пользователя
    try:
        await bot.send_message(
            target_uid, 
            f"🎉 *Баланс пополнен!*\nВаша заявка #{dep_id} на P2P-пополнение игровой валютой успешно одобрена администратором!\n"
            f"На ваш баланс зачислено: **+{nmp_amount:.2f} NMP**!"
        )
    except Exception:
        pass
        
    await state.clear()

# --- ПРЯМАЯ УПРАВЛЯЮЩАЯ КОМАНДА ДЛЯ БЫСТРОГО НАЧИСЛЕНИЯ ПРИ ОБЩЕНИИ С АДМИНОМ ---
@dp.message(Command("give"))
async def cmd_give_shortcut(message: types.Message):
    if message.from_user.id != ADMIN_ID: return
    
    args = message.text.split()
    if len(args) != 3:
        await message.reply("📖 Справка использования:\n`/give [ID_получателя] [количество_NMP]`", parse_mode="Markdown")
        return
        
    try:
        target_uid = int(args[1])
        amount = float(args[2])
        if amount <= 0: raise ValueError()
    except ValueError:
        await message.reply("❌ Ошибка ввода. ID должен быть целым числом, а сумма начисления - положительным числом.")
        return
        
    async with db_pool.acquire() as conn:
        async with conn.transaction():
            # Проверяем существование
            user_exists = await conn.fetchval("SELECT telegram_id FROM users WHERE telegram_id = $1", target_uid)
            if not user_exists:
                await message.reply("❌ Пользователь с таким ID не зарегистрирован в базе данных банка!")
                return
                
            await conn.execute("UPDATE users SET balance = balance + $1 WHERE telegram_id = $2", amount, target_uid)
            await conn.execute(
                "INSERT INTO transactions (user_id, amount, tx_type, description) VALUES ($1, $2, 'admin_direct', 'Прямое начисление от администрации')",
                target_uid, amount
            )
            
    await message.reply(f"✅ Успешно начислено **{amount:.2f} NMP** пользователю `{target_uid}` напрямую!")
    
    try:
        await bot.send_message(
            target_uid, 
            f"🎁 Администратор напрямую начислил на ваш личный баланс **+{amount:.2f} NMP**!"
        )
    except Exception:
        pass

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
            logging.error(f"Шедулер ошибка: {e}")

async def main():
    logging.basicConfig(level=logging.INFO)
    await init_db()
    asyncio.create_task(financial_background_scheduler())
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
