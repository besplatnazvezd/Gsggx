import os
import asyncio
import logging
import random
import uuid
import secrets
from datetime import datetime, timedelta
import asyncpg
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.types import InlineQueryResultArticle, InputTextMessageContent
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL") 
ADMIN_ID = int(os.getenv("ADMIN_ID", 0))

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
db_pool = None

# Стейты для FSM ввода данных
class Form(StatesGroup):
    # Промокоды и админка
    waiting_for_promo = State()
    waiting_admin_user_id = State()
    waiting_admin_amount = State()
    waiting_admin_promo_code = State()
    waiting_admin_promo_reward = State()
    # Виртуальные чеки
    waiting_check_amount = State()
    waiting_check_claims = State()
    # Покупка короткого счета (handle)
    waiting_custom_handle = State()
    # Срочные вклады
    waiting_deposit_amount = State()

async def init_db():
    global db_pool
    db_pool = await asyncpg.create_pool(dsn=DATABASE_URL)

# Хелпер: расчет бонусов пользователя от NFT
async def get_user_boosts(conn, user_id: int):
    # Получаем суммарный буст стейкинга и кэшбэка от купленных пользователем NFT
    row = await conn.fetchrow(
        """SELECT COALESCE(SUM(n.boost_staking_pct), 0) as stake_boost, 
                  COALESCE(SUM(n.boost_cashback_pct), 0) as cash_boost 
           FROM user_nfts un 
           JOIN nfts n ON un.nft_id = n.id 
           WHERE un.user_id = $1""", user_id
    )
    return float(row['stake_boost']), float(row['cash_boost'])

# Обработка глубоких ссылок (Рефералы и Чеки)
@dp.message(CommandStart())
async def cmd_start(message: types.Message):
    user_id = message.from_user.id
    username = message.from_user.username or "User"
    
    args = message.text.split()
    referrer_id = None
    check_code_to_claim = None
    
    if len(args) > 1:
        payload = args[1]
        if payload.startswith("chk_"):
            check_code_to_claim = payload.replace("chk_", "")
        elif payload.isdigit():
            referrer_id = int(payload)
            if referrer_id == user_id:
                referrer_id = None

    async with db_pool.acquire() as conn:
        user = await conn.fetchrow("SELECT * FROM users WHERE telegram_id = $1", user_id)
        
        if not user:
            # Создаем нового пользователя (по дефолту дается 100 NMP из настроек БД)
            await conn.execute(
                "INSERT INTO users (telegram_id, username, referrer_id) VALUES ($1, $2, $3)",
                user_id, username, referrer_id
            )
            await conn.execute(
                "INSERT INTO transactions (user_id, amount, tx_type, description) VALUES ($1, 100.00, 'welcome', 'Приветственный бонус')",
                user_id
            )
            
            # Бонус рефереру
            if referrer_id:
                ref_user = await conn.fetchrow("SELECT * FROM users WHERE telegram_id = $1", referrer_id)
                if ref_user:
                    await conn.execute("UPDATE users SET balance = balance + 27 WHERE telegram_id = $1", referrer_id)
                    await conn.execute(
                        "INSERT INTO transactions (user_id, amount, tx_type, description) VALUES ($1, 27.00, 'referral', 'Реферальный бонус за приглашение друга')",
                        referrer_id
                    )
                    try:
                        await bot.send_message(referrer_id, f"🎉 По вашей ссылке зарегистрировался @{username}! Вам зачислено +27 NMP.")
                    except: pass
            
            welcome_text = "🏦 *Добро пожаловать в NMVal Bank!*\n\nЛичный кабинет открыт. На баланс начислено 100.00 NMP!"
        else:
            welcome_text = f"🏦 *С возвращением в NMVal Bank, @{username}!*"

        # Если перешли по ссылке чека
        if check_code_to_claim:
            await claim_check_logic(message, check_code_to_claim)
            return

    await send_main_menu(message.chat.id, welcome_text)

async def send_main_menu(chat_id: int, text: str):
    builder = InlineKeyboardBuilder()
    builder.button(text="💳 Мой Кабинет", callback_data="my_account")
    builder.button(text="🎁 Маркетплейс NFT", callback_data="shop_nfts")
    builder.button(text="💸 Создать Чек", callback_data="checks_menu")
    builder.button(text="📈 Депозиты (Сейвинг)", callback_data="deposits_menu")
    builder.button(text="🎰 Колесо Фортуны", callback_data="wheel_spin")
    builder.button(text="👤 Никнейм Счета (*)", callback_data="custom_handle_menu")
    builder.button(text="👥 Партнерам (API)", callback_data="merchant_api")
    builder.button(text="🎫 Промокод", callback_data="promo_activate")
    
    if chat_id == ADMIN_ID:
        builder.button(text="⚙️ Админка", callback_data="admin_panel")
        
    builder.adjust(2, 2, 2, 2)
    await bot.send_message(chat_id, text, reply_markup=builder.as_markup(), parse_mode="Markdown")

# Назад в главное меню
@dp.callback_query(F.data == "main_menu")
async def cb_main_menu(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.delete()
    await send_main_menu(callback.message.chat.id, "🏦 *Главный экран NMVal Bank:*")

# --- МОЙ КАБИНЕТ (Счета, кэшбэк, стейкинг) ---
@dp.callback_query(F.data == "my_account")
async def cb_my_account(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    async with db_pool.acquire() as conn:
        user = await conn.fetchrow("SELECT balance, custom_handle FROM users WHERE telegram_id = $1", user_id)
        stake_b, cash_b = await get_user_boosts(conn, user_id)
        
    handle_str = f"🏷 Счет: `{user['custom_handle']}`" if user['custom_handle'] else f"💳 ID Счета: `{user_id}`"
    base_stake = 9.2
    base_cashback = 1.0
    
    text = (
        f"🏦 *Ваш Личный Кабинет*\n\n"
        f"{handle_str}\n"
        f"💵 Баланс: `{user['balance']:.2f} NMP`\n\n"
        f"📊 *Пассивные показатели:*\n"
        f"📈 Стейкинг: `{base_stake + stake_b:.2f}%` в месяц (База: {base_stake}% + Буст: {stake_b:.2f}%)\n"
        f"🛍 Кэшбэк: `{base_cashback + cash_b:.2f}%` на все покупки (База: {base_cashback}% + Буст: {cash_b:.2f}%)"
    )
    
    builder = InlineKeyboardBuilder()
    builder.button(text="📥 Купить NMP (Stars)", callback_data="buy_stars")
    builder.button(text="🔙 В меню", callback_data="main_menu")
    builder.adjust(1)
    await callback.message.edit_text(text, reply_markup=builder.as_markup(), parse_mode="Markdown")

# --- ВИРТУАЛЬНЫЕ ЧЕКИ ---
@dp.callback_query(F.data == "checks_menu")
async def cb_checks_menu(callback: types.CallbackQuery, state: FSMContext):
    text = (
        "💸 *Виртуальные Чеки*\n\n"
        "Вы можете создать чек на любую сумму NMP и поделиться им с друзьями в личке или чатах. "
        "Активировавший чек мгновенно получит средства на баланс."
    )
    builder = InlineKeyboardBuilder()
    builder.button(text="🎫 Создать Чек", callback_data="create_check_start")
    builder.button(text="🔙 Назад", callback_data="main_menu")
    builder.adjust(1)
    await callback.message.edit_text(text, reply_markup=builder.as_markup(), parse_mode="Markdown")

@dp.callback_query(F.data == "create_check_start")
async def cb_create_check(callback: types.CallbackQuery, state: FSMContext):
    await state.set_state(Form.waiting_check_amount)
    await callback.message.edit_text("💸 Введите общую сумму NMP для обеспечения чека:")

@dp.message(Form.waiting_check_amount)
async def process_check_amount(message: types.Message, state: FSMContext):
    try:
        amount = float(message.text)
        if amount <= 0: raise ValueError()
    except ValueError:
        await message.reply("❌ Введите корректное положительное число:")
        return
        
    async with db_pool.acquire() as conn:
        user_bal = await conn.fetchval("SELECT balance FROM users WHERE telegram_id = $1", message.from_user.id)
        if user_bal < amount:
            await message.reply("❌ Недостаточно средств на балансе. Введите другую сумму:")
            return
            
    await state.update_data(check_amount=amount)
    await state.set_state(Form.waiting_check_claims)
    await message.reply("👥 На сколько человек рассчитать этот чек? (Введите число от 1 до 100):")

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
    check_code = secrets.token_hex(6) # 12 символов
    is_multi = claims > 1
    
    async with db_pool.acquire() as conn:
        async with conn.transaction():
            # Забираем средства с баланса создателя
            await conn.execute("UPDATE users SET balance = balance - $1 WHERE telegram_id = $2", amount, user_id)
            # Создаем чек
            await conn.execute(
                "INSERT INTO checks (code, creator_id, amount, max_claims, is_multi, amount_per_claim) VALUES ($1, $2, $3, $4, $5, $6)",
                check_code, user_id, amount, claims, is_multi, amount_per_claim
            )
            # Пишем транзакцию
            await conn.execute(
                "INSERT INTO transactions (user_id, amount, tx_type, description) VALUES ($1, $2, 'check_create', $3)",
                user_id, -amount, f"Создание чека {check_code}"
            )
            
    bot_info = await bot.get_me()
    check_link = f"https://t.me/{bot_info.username}?start=chk_{check_code}"
    
    text = (
        f"✅ *Чек успешно создан!*\n\n"
        f"💰 Общая сумма: `{amount:.2f} NMP`\n"
        f"👥 Количество активаций: `{claims}`\n"
        f"💵 Получение на человека: `{amount_per_claim:.2f} NMP`\n\n"
        f"🔗 Ссылка на получение:\n{check_link}"
    )
    await state.clear()
    await send_main_menu(message.chat.id, text)

# Логика обналичивания чека (через deep-link)
async def claim_check_logic(message: types.Message, code: str):
    user_id = message.from_user.id
    async with db_pool.acquire() as conn:
        async with conn.transaction():
            check = await conn.fetchrow("SELECT * FROM checks WHERE code = $1 FOR UPDATE", code)
            if not check:
                await message.reply("❌ Чек не существует или уже недействителен.")
                await send_main_menu(message.chat.id, "Выберите действие:")
                return
                
            if check['claimed_count'] >= check['max_claims']:
                await message.reply("❌ К сожалению, этот чек уже полностью обналичен другими пользователями.")
                await send_main_menu(message.chat.id, "Выберите действие:")
                return
                
            # Проверка, забирал ли этот юзер уже этот конкретный чек
            claimed = await conn.fetchrow("SELECT * FROM check_claims WHERE check_code = $1 AND user_id = $2", code, user_id)
            if claimed:
                await message.reply("❌ Вы уже забирали средства из этого чека!")
                await send_main_menu(message.chat.id, "Выберите действие:")
                return
                
            # Переводим средства
            amount_to_pay = check['amount_per_claim']
            await conn.execute("UPDATE users SET balance = balance + $1 WHERE telegram_id = $2", amount_to_pay, user_id)
            await conn.execute("UPDATE checks SET claimed_count = claimed_count + 1 WHERE code = $1", code)
            await conn.execute("INSERT INTO check_claims (check_code, user_id) VALUES ($1, $2)", code, user_id)
            await conn.execute(
                "INSERT INTO transactions (user_id, amount, tx_type, description) VALUES ($1, $2, 'check_claim', $3)",
                user_id, amount_to_pay, f"Активация чека {code}"
            )
            
            await message.reply(f"🎉 Вы успешно активировали чек! На ваш баланс начислено *+{amount_to_pay:.2f} NMP*!")
            
            # Уведомляем создателя
            try:
                await bot.send_message(check['creator_id'], f"💸 Твой чек `{code}` был активирован пользователем @{message.from_user.username or user_id}. Начислено: {amount_to_pay:.2f} NMP.")
            except: pass
            
    await send_main_menu(message.chat.id, "Выберите действие:")

# --- СРОЧНЫЕ ВКЛАДЫ (Lock-up Сейвинг) ---
@dp.callback_query(F.data == "deposits_menu")
async def cb_deposits_menu(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    async with db_pool.acquire() as conn:
        active_deposits = await conn.fetch("SELECT * FROM lock_deposits WHERE user_id = $1 AND is_active = TRUE", user_id)
        
    text = "📈 *Срочные вклады (Lock-up Сейвинг)*\n\nЗаморозьте свободные NMP на определенный срок и получите высокий гарантированный процент:\n"
    text += "• 3 месяца: *12% годовых*\n• 6 месяцев: *15% годовых*\n• 12 месяцев: *20% годовых*\n\n"
    
    if active_deposits:
        text += "💼 *Ваши активные вклады:*\n"
        for d in active_deposits:
            text += f"• Сумма: `{d['amount']:.2f} NMP` | Ставка: `{d['rate']}%` | Конец: `{d['end_date'].strftime('%Y-%m-%d')}`\n"
    else:
        text += "У вас пока нет активных вкладов."
        
    builder = InlineKeyboardBuilder()
    builder.button(text="💼 Открыть Вклад", callback_data="deposit_open_start")
    builder.button(text="🔙 В меню", callback_data="main_menu")
    builder.adjust(1)
    await callback.message.edit_text(text, reply_markup=builder.as_markup(), parse_mode="Markdown")

@dp.callback_query(F.data == "deposit_open_start")
async def cb_open_deposit_start(callback: types.CallbackQuery, state: FSMContext):
    await state.set_state(Form.waiting_deposit_amount)
    await callback.message.edit_text("📈 Введите количество NMP для открытия вклада:")

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
            await message.reply("❌ Недостаточно средств на балансе.")
            return
            
    await state.update_data(dep_amount=amount)
    
    # Выбор тарифа
    builder = InlineKeyboardBuilder()
    builder.button(text="3 мес (12% APR)", callback_data="dep_plan:3:12.0")
    builder.button(text="6 мес (15% APR)", callback_data="dep_plan:6:15.0")
    builder.button(text="12 мес (20% APR)", callback_data="dep_plan:12:20.0")
    builder.button(text="❌ Отмена", callback_data="main_menu")
    builder.adjust(1)
    
    await message.reply("📅 Выберите срок и процентную ставку:", reply_markup=builder.as_markup())

@dp.callback_query(F.data.startswith("dep_plan:"))
async def cb_select_dep_plan(callback: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    amount = data.get("dep_amount")
    if not amount:
        await callback.answer("Ошибка сессии.")
        return
        
    _, months_str, rate_str = callback.data.split(":")
    months = int(months_str)
    rate = float(rate_str)
    user_id = callback.from_user.id
    
    end_date = datetime.now() + timedelta(days=months * 30) # упрощенный месяц в 30 дней
    
    async with db_pool.acquire() as conn:
        async with conn.transaction():
            # Списание средств
            await conn.execute("UPDATE users SET balance = balance - $1 WHERE telegram_id = $2", amount, user_id)
            # Создание записи вклада
            await conn.execute(
                "INSERT INTO lock_deposits (user_id, amount, rate, end_date) VALUES ($1, $2, $3, $4)",
                user_id, amount, rate, end_date
            )
            # Транзакция
            await conn.execute(
                "INSERT INTO transactions (user_id, amount, tx_type, description) VALUES ($1, $2, 'deposit_open', $3)",
                user_id, -amount, f"Заморозка вклада под {rate}% на {months} мес."
            )
            
    await callback.message.edit_text(
        f"🎉 *Вклад успешно открыт!*\n\n"
        f"💵 Сумма: `{amount:.2f} NMP`\n"
        f"📈 Ставка: `{rate}% годовых`\n"
        f"📅 Дата разблокировки: `{end_date.strftime('%Y-%m-%d')}`\n\n"
        f"Сумма вместе с процентами вернется на счет автоматически.",
        parse_mode="Markdown"
    )
    await state.clear()
    await send_main_menu(callback.message.chat.id, "Выберите действие:")

# --- КОЛЕСО ФОРТУНЫ (Daily Spin) ---
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
        await callback.answer(f"❌ Колесо уже крутилось! Следующая попытка через {hours}ч {minutes}м.", show_alert=True)
        return
        
    # Розыгрыш призов
    prizes = [
        ("0.50 NMP", 0.50),
        ("1.00 NMP", 1.00),
        ("5.00 NMP", 5.00),
        ("10.00 NMP", 10.00),
        ("27.00 NMP", 27.00),
        ("СУПЕР КЭШБЭК (+50.00 NMP)", 50.0)
    ]
    
    prize_name, prize_val = random.choices(prizes, weights=[50, 30, 12, 5, 2.5, 0.5], k=1)[0]
    
    async with db_pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                "UPDATE users SET balance = balance + $1, daily_spin_last_used = NOW() WHERE telegram_id = $2",
                prize_val, user_id
            )
            await conn.execute(
                "INSERT INTO transactions (user_id, amount, tx_type, description) VALUES ($1, $2, 'spin', 'Выигрыш в Колесе Фортуны')",
                user_id, prize_val
            )
            
    await callback.message.edit_text(
        f"🎰 *Колесо Фортуны крутится...*\n\n"
        f"🌀 Барабан останавливается...\n\n"
        f"🎉 Поздравляем! Ваш выигрыш: *{prize_name}*! Деньги зачислены.",
        reply_markup=InlineKeyboardBuilder().button(text="🔙 В меню", callback_data="main_menu").as_markup(),
        parse_mode="Markdown"
    )

# --- НИКНЕЙМ СЧЕТА (*) ---
@dp.callback_query(F.data == "custom_handle_menu")
async def cb_custom_handle(callback: types.CallbackQuery, state: FSMContext):
    user_id = callback.from_user.id
    async with db_pool.acquire() as conn:
        handle = await conn.fetchval("SELECT custom_handle FROM users WHERE telegram_id = $1", user_id)
        
    if handle:
        text = f"✨ У вас уже зарегистрирован уникальный банковский никнейм счета: `{handle}`\n\nВы можете использовать его для переводов!"
        builder = InlineKeyboardBuilder().button(text="🔙 Назад", callback_data="main_menu")
    else:
        text = (
            "👤 *Уникальный Никнейм Счета*\n\n"
            "Вы можете купить красивый буквенный никнейм для вашего счета (например, `*DUROV`, `*BILLIONAIRE`) вместо обычного числового ID.\n\n"
            "💰 Стоимость услуги: *500.00 NMP*"
        )
        builder = InlineKeyboardBuilder()
        builder.button(text="✍️ Зарегистрировать", callback_data="custom_handle_buy_start")
        builder.button(text="🔙 Назад", callback_data="main_menu")
        builder.adjust(1)
        
    await callback.message.edit_text(text, reply_markup=builder.as_markup(), parse_mode="Markdown")

@dp.callback_query(F.data == "custom_handle_buy_start")
async def cb_handle_buy_start(callback: types.CallbackQuery, state: FSMContext):
    await state.set_state(Form.waiting_custom_handle)
    await callback.message.edit_text("👤 Введите желаемый никнейм (должен начинаться со знака `*`, без пробелов, от 4 до 15 символов):")

@dp.message(Form.waiting_custom_handle)
async def process_custom_handle(message: types.Message, state: FSMContext):
    handle = message.text.strip().upper()
    user_id = message.from_user.id
    
    if not handle.startswith("*") or len(handle) < 4 or len(handle) > 16:
        await message.reply("❌ Некорректный формат. Никнейм должен начинаться со знака `*` и содержать от 4 до 15 символов:")
        return
        
    async with db_pool.acquire() as conn:
        async with conn.transaction():
            # Проверка занятости
            exists = await conn.fetchrow("SELECT telegram_id FROM users WHERE custom_handle = $1", handle)
            if exists:
                await message.reply("❌ Данный банковский никнейм уже занят. Попробуйте другой:")
                return
                
            # Проверка баланса
            user_bal = await conn.fetchval("SELECT balance FROM users WHERE telegram_id = $1 FOR UPDATE", user_id)
            if user_bal < 500.00:
                await message.reply("❌ Недостаточно средств на балансе. Требуется 500.00 NMP.")
                await state.clear()
                return
                
            # Применение изменений
            await conn.execute("UPDATE users SET balance = balance - 500.00, custom_handle = $1 WHERE telegram_id = $2", handle, user_id)
            await conn.execute(
                "INSERT INTO transactions (user_id, amount, tx_type, description) VALUES ($1, -500.00, 'handle_buy', $2)",
                user_id, f"Покупка никнейма {handle}"
            )
            
    await message.reply(f"🎉 Поздравляем! Ваш новый банковский счет `{handle}` успешно зарегистрирован!")
    await state.clear()
    await send_main_menu(message.chat.id, "Выберите действие:")

# --- NFT-МАРКЕТПЛЕЙС (С учетом индивидуальных бустов!) ---
@dp.callback_query(F.data == "shop_nfts")
async def cb_shop_nfts(callback: types.CallbackQuery):
    async with db_pool.acquire() as conn:
        nfts = await conn.fetch("SELECT * FROM nfts WHERE remaining_supply > 0 ORDER BY id LIMIT 5")
        
    text = "🎁 *NFT-Маркетплейс NMVal Bank*\n\nПокупайте лимитированные NFT-подарки. Каждый NFT увеличивает ваш стейкинг и кэшбэк!\n\nТовары в наличии:"
    builder = InlineKeyboardBuilder()
    
    for n in nfts:
        builder.button(
            text=f"{n['name']} | 🪙 {n['price']} NMP | Стейкинг: +{n['boost_staking_pct']}% | Кэшбэк: +{n['boost_cashback_pct']}%", 
            callback_data=f"buy_nft_pro:{n['id']}"
        )
    builder.button(text="🔙 Назад", callback_data="main_menu")
    builder.adjust(1)
    await callback.message.edit_text(text, reply_markup=builder.as_markup(), parse_mode="Markdown")

@dp.callback_query(F.data.startswith("buy_nft_pro:"))
async def cb_buy_nft_pro(callback: types.CallbackQuery):
    nft_id = int(callback.data.split(":")[2])
    user_id = callback.from_user.id
    
    async with db_pool.acquire() as conn:
        async with conn.transaction():
            user = await conn.fetchrow("SELECT balance FROM users WHERE telegram_id = $1 FOR UPDATE", user_id)
            nft = await conn.fetchrow("SELECT * FROM nfts WHERE id = $1 FOR UPDATE", nft_id)
            
            if not nft or nft['remaining_supply'] <= 0:
                await callback.answer("❌ Товар закончился!", show_alert=True)
                return
                
            if user['balance'] < nft['price']:
                await callback.answer("❌ Недостаточно средств!", show_alert=True)
                return
                
            price = float(nft['price'])
            # Расчет кэшбэка (Базовый 1% + Буст пользователя от его старых NFT)
            stake_b, cash_b = await get_user_boosts(conn, user_id)
            total_cashback_rate = 0.01 + (cash_b / 100.0)
            cashback_reward = price * total_cashback_rate
            
            new_balance = float(user['balance']) - price + cashback_reward
            
            await conn.execute("UPDATE users SET balance = $1 WHERE telegram_id = $2", new_balance, user_id)
            await conn.execute("UPDATE nfts SET remaining_supply = remaining_supply - 1 WHERE id = $1", nft_id)
            await conn.execute("INSERT INTO user_nfts (user_id, nft_id) VALUES ($1, $2)", user_id, nft_id)
            
            # Записи в транзакции
            await conn.execute(
                "INSERT INTO transactions (user_id, amount, tx_type, description) VALUES ($1, $2, 'purchase', $3)",
                user_id, -price, f"Покупка NFT {nft['name']}"
            )
            await conn.execute(
                "INSERT INTO transactions (user_id, amount, tx_type, description) VALUES ($1, $2, 'cashback', $3)",
                user_id, cashback_reward, f"Кэшбэк ({total_cashback_rate*100:.2f}%) за {nft['name']}"
            )
            
    await callback.answer(f"🎉 Вы приобрели {nft['name']}!\nПолучен кэшбэк: {cashback_reward:.2f} NMP!", show_alert=True)
    await cb_shop_nfts(callback)

# --- ИНТЕГРАЦИЯ С ПУБЛИЧНЫМ API (Мерчанты) ---
@dp.callback_query(F.data == "merchant_api")
async def cb_merchant_api(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    async with db_pool.acquire() as conn:
        key = await conn.fetchrow("SELECT api_key FROM merchant_api_keys WHERE user_id = $1", user_id)
        if not key:
            await conn.execute("INSERT INTO merchant_api_keys (user_id, service_name) VALUES ($1, 'По умолчанию')", user_id)
            key = await conn.fetchrow("SELECT api_key FROM merchant_api_keys WHERE user_id = $1", user_id)
            
    text = (
        "🔗 *NMVal Merchant API*\n\n"
        "Вы можете подключить прием оплаты в монете NMP в свои Telegram-боты или на личные сайты!\n\n"
        f"🔑 Ваш токен интеграции:\n`{key['api_key']}`\n\n"
        "Отправляйте API запросы к платежному шлюзу. Документация будет доступна на нашем официальном сайте."
    )
    builder = InlineKeyboardBuilder().button(text="🔙 В меню", callback_data="main_menu")
    await callback.message.edit_text(text, reply_markup=builder.as_markup(), parse_mode="Markdown")

# --- АКТИВАЦИЯ ПРОМОКОДОВ ---
@dp.callback_query(F.data == "promo_activate")
async def cb_promo_activate(callback: types.CallbackQuery, state: FSMContext):
    await state.set_state(Form.waiting_for_promo)
    await callback.message.edit_text("🎫 Введите секретный промокод:")

@dp.message(Form.waiting_for_promo)
async def process_promo(message: types.Message, state: FSMContext):
    code = message.text.strip()
    user_id = message.from_user.id
    
    async with db_pool.acquire() as conn:
        promo = await conn.fetchrow("SELECT * FROM promo_codes WHERE code = $1", code)
        if not promo:
            await message.reply("❌ Промокод не существует.")
            await state.clear()
            return
            
        used = await conn.fetchrow("SELECT id FROM promo_uses WHERE user_id = $1 AND code = $2", user_id, code)
        if used:
            await message.reply("❌ Вы уже активировали этот промокод!")
            await state.clear()
            return
            
        if promo['used_count'] >= promo['max_uses']:
            await message.reply("❌ Промокод исчерпан по количеству активаций.")
            await state.clear()
            return
            
        async with conn.transaction():
            await conn.execute("UPDATE users SET balance = balance + $1 WHERE telegram_id = $2", promo['reward'], user_id)
            await conn.execute("UPDATE promo_codes SET used_count = used_count + 1 WHERE code = $1", code)
            await conn.execute("INSERT INTO promo_uses (user_id, code) VALUES ($1, $2)", user_id, code)
            await conn.execute(
                "INSERT INTO transactions (user_id, amount, tx_type, description) VALUES ($1, $2, 'promo', $3)",
                user_id, promo['reward'], f"Активация промокода {code}"
            )
            
    await message.reply(f"🎉 Промокод успешно применен! Баланс пополнен на *+{promo['reward']:.2f} NMP*.")
    await state.clear()
    await send_main_menu(message.chat.id, "Выберите действие:")

# --- ПОДПИСКА НА ИНЛАЙН ПЕРЕВОДЫ (Перевод в любом чате!) ---
@dp.inline_query()
async def inline_transfer_handler(inline_query: types.InlineQuery):
    query_text = inline_query.query.strip()
    if not query_text or not query_text.isdigit():
        return
        
    amount = float(query_text)
    if amount <= 0:
        return
        
    # Формируем чек "на лету"
    check_code = f"inline_{secrets.token_hex(4)}"
    
    # Создаем красивую интерактивную карточку
    results = [
        InlineQueryResultArticle(
            id=str(uuid.uuid4()),
            title=f"💳 Отправить {amount:.2f} NMP",
            description="Создайте мгновенный чек для любого пользователя в этом чате!",
            input_message_content=InputTextMessageContent(
                message_text=f"🎁 *Денежный подарок от @{inline_query.from_user.username or 'пользователя'}!*\n\n"
                             f"Сумма перевода: `{amount:.2f} NMP`\n\n"
                             f"Кто первый нажмет кнопку снизу, тот и заберет перевод!",
                parse_mode="Markdown"
            ),
            reply_markup=types.InlineKeyboardMarkup(
                inline_keyboard=[
                    [types.InlineKeyboardButton(text="📥 Забрать переведенные NMP", url=f"https://t.me/{(await bot.get_me()).username}?start=chk_{check_code}")]
                ]
            )
        )
    ]
    
    # Резервируем этот чек в БД за счет отправителя
    async with db_pool.acquire() as conn:
        user_bal = await conn.fetchval("SELECT balance FROM users WHERE telegram_id = $1", inline_query.from_user.id)
        if user_bal >= amount:
            async with conn.transaction():
                await conn.execute("UPDATE users SET balance = balance - $1 WHERE telegram_id = $2", amount, inline_query.from_user.id)
                await conn.execute(
                    "INSERT INTO checks (code, creator_id, amount, max_claims, is_multi, amount_per_claim) VALUES ($1, $2, $3, 1, FALSE, $3)",
                    check_code, inline_query.from_user.id, amount
                )
                await conn.execute(
                    "INSERT INTO transactions (user_id, amount, tx_type, description) VALUES ($1, $2, 'check_create', $3)",
                    inline_query.from_user.id, -amount, f"Быстрый инлайн чек {check_code}"
                )
                # Отправляем результаты только если денег хватает
                await inline_query.answer(results, is_personal=True, cache_time=0)

# --- АДМИН-ПАНЕЛЬ ---
@dp.callback_query(F.data == "admin_panel")
async def cb_admin_panel(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID: return
    
    async with db_pool.acquire() as conn:
        total_users = await conn.fetchval("SELECT COUNT(*) FROM users")
        total_balance = await conn.fetchval("SELECT SUM(balance) FROM users")
        
    text = (
        "⚙️ *Админ-Панель NMVal*\n\n"
        f"👥 Зарегистрировано участников: `{total_users}`\n"
        f"🪙 Валюта в обороте: `{total_balance:.2f} NMP`"
    )
    builder = InlineKeyboardBuilder()
    builder.button(text="✍️ Выдать Валюту", callback_data="admin_give_coins")
    builder.button(text="🎫 Создать Промокод", callback_data="admin_create_promo")
    builder.button(text="🔙 Главный экран", callback_data="main_menu")
    builder.adjust(1)
    await callback.message.edit_text(text, reply_markup=builder.as_markup(), parse_mode="Markdown")

@dp.callback_query(F.data == "admin_give_coins")
async def cb_admin_give_coins(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID: return
    await state.set_state(Form.waiting_admin_user_id)
    await callback.message.edit_text("⚙️ Введите Telegram ID пользователя:")

@dp.message(Form.waiting_admin_user_id)
async def process_admin_uid(message: types.Message, state: FSMContext):
    await state.update_data(target_id=int(message.text))
    await state.set_state(Form.waiting_admin_amount)
    await message.reply("⚙️ Введите сумму начисления NMP:")

@dp.message(Form.waiting_admin_amount)
async def process_admin_val(message: types.Message, state: FSMContext):
    data = await state.get_data()
    target_id = data['target_id']
    amount = float(message.text)
    
    async with db_pool.acquire() as conn:
        await conn.execute("UPDATE users SET balance = balance + $1 WHERE telegram_id = $2", amount, target_id)
        await conn.execute(
            "INSERT INTO transactions (user_id, amount, tx_type, description) VALUES ($1, $2, 'admin', 'Пополнение от администратора')",
            target_id, amount
        )
        
    await message.reply(f"✅ Баланс аккаунта `{target_id}` пополнен на `+{amount:.2f} NMP`!")
    try:
        await bot.send_message(target_id, f"🏦 На ваш счет от Администрации зачислено *+{amount:.2f} NMP*!")
    except: pass
    await state.clear()

@dp.callback_query(F.data == "admin_create_promo")
async def cb_admin_create_promo(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID: return
    await state.set_state(Form.waiting_admin_promo_code)
    await callback.message.edit_text("⚙️ Введите имя нового промокода:")

@dp.message(Form.waiting_admin_promo_code)
async def admin_promo_code(message: types.Message, state: FSMContext):
    await state.update_data(promo_code=message.text.strip())
    await state.set_state(Form.waiting_admin_promo_reward)
    await message.reply("⚙️ Введите сумму награды за активацию:")

@dp.message(Form.waiting_admin_promo_reward)
async def admin_promo_reward(message: types.Message, state: FSMContext):
    data = await state.get_data()
    p_code = data['promo_code']
    reward = float(message.text)
    
    async with db_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO promo_codes (code, reward, max_uses) VALUES ($1, $2, 10000)", # по дефолту на 10.000 активаций
            p_code, reward
        )
        
    await message.reply(f"✅ Промокод `{p_code}` на `{reward:.2f} NMP` успешно запущен!")
    await state.clear()

# --- ФОНОВЫЕ СЕРВИСНЫЕ ЗАДАЧИ (Стейкинг + Сейвинг выплаты) ---
async def financial_background_scheduler():
    while True:
        await asyncio.sleep(60) # Проверка каждую минуту (для точных тестов)
        try:
            async with db_pool.acquire() as conn:
                # 1. Стейкинг 9.2% на свободный баланс (раз в 30 дней)
                users_to_pay = await conn.fetch(
                    "SELECT telegram_id, balance FROM users WHERE last_interest_accrued < NOW() - INTERVAL '30 days'"
                )
                for u in users_to_pay:
                    user_id = u['telegram_id']
                    # Получаем индивидуальные бусты NFT
                    stake_b, _ = await get_user_boosts(conn, user_id)
                    total_rate = 0.092 + (stake_b / 100.0)
                    bonus = float(u['balance']) * total_rate
                    
                    async with conn.transaction():
                        await conn.execute(
                            "UPDATE users SET balance = balance + $1, last_interest_accrued = NOW() WHERE telegram_id = $2",
                            bonus, user_id
                        )
                        await conn.execute(
                            "INSERT INTO transactions (user_id, amount, tx_type, description) VALUES ($1, $2, 'staking', $3)",
                            user_id, bonus, f"Начисление стейкинга ({total_rate*100:.2f}%)"
                        )
                    try:
                        await bot.send_message(user_id, f"📈 Банк начислил проценты по вашему счету: *+{bonus:.2f} NMP*!")
                    except: pass
                    
                # 2. Выплаты по срочным Сейвинг вкладам (Lock-up)
                expired_deposits = await conn.fetch(
                    "SELECT * FROM lock_deposits WHERE is_active = TRUE AND end_date <= NOW()"
                )
                for d in expired_deposits:
                    dep_id = d['id']
                    user_id = d['user_id']
                    principal = float(d['amount'])
                    rate = float(d['rate'])
                    
                    # Математика выплаты: сумма + процент прибыли
                    payout_amount = principal * (1 + (rate / 100.0))
                    
                    async with conn.transaction():
                        await conn.execute("UPDATE lock_deposits SET is_active = FALSE WHERE id = $1", dep_id)
                        await conn.execute("UPDATE users SET balance = balance + $1 WHERE telegram_id = $2", payout_amount, user_id)
                        await conn.execute(
                            "INSERT INTO transactions (user_id, amount, tx_type, description) VALUES ($1, $2, 'deposit_payout', $3)",
                            user_id, payout_amount, f"Закрытие вклада. Возврат {principal} NMP + доход"
                        )
                        
                    try:
                        await bot.send_message(
                            user_id, 
                            f"📈 *Ваш Lock-up вклад разблокирован!*\n\n"
                            f"💰 Сумма возврата с процентами: *{payout_amount:.2f} NMP* зачислена на основной баланс!"
                        )
                    except: pass
        except Exception as e:
            logging.error(f"Ошибка в финансовом шедулере: {e}")

async def main():
    logging.basicConfig(level=logging.INFO)
    await init_db()
    asyncio.create_task(financial_background_scheduler())
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
