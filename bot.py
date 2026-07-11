import asyncio
import logging
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.utils.keyboard import InlineKeyboardBuilder
from config import BOT_TOKEN, ADMIN_ID
import database as db

logging.basicConfig(level=logging.INFO)
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# --- Тексты ---
WELCOME_TEXT = (
    "👋 **Добро пожаловать в G-Master Store!**\n\n"
    "🚀 Почему выбирают именно нас?\n"
    "• **Скорость:** Автоматизированная обработка заявок.\n"
    "• **Безопасность:** Прямые переводы без посредников.\n"
    "• **Репутация:** Нам доверяют более 5000+ игроков.\n\n"
    "💎 Мы — крупнейший хаб по обмену валют. Наш бот поможет тебе получить GMP максимально выгодно!"
)

BUY_TEXT = (
    "💳 **Покупка GMP**\n\n"
    "💰 Курс: **1 GMP = 90,000 Mcoin**\n\n"
    "🤔 *Почему так дешево?*\n"
    "Мы работаем напрямую с крупными пулами ликвидности и закупаем валюту оптом, исключая комиссии бирж. "
    "Это позволяет нам держать цену на 20-30% ниже рыночной!\n\n"
    "⏳ **Сроки доставки:** от 5 до 30 минут после подтверждения.\n\n"
    "💵 **Как пополнить баланс?**\n"
    "Отправьте в этот чат ссылку на чек (GminesBot). Пример:\n"
    "`https://t.me/gminesbot?start=check_...`\n\n"
    "После проверки админом ваш баланс Mcoin пополнится автоматически!"
)

# --- Клавиатуры ---
def main_kb():
    builder = InlineKeyboardBuilder()
    builder.row(types.InlineKeyboardButton(text="💎 Купить GMP", callback_data="buy_gmp"))
    builder.row(
        types.InlineKeyboardButton(text="👤 Профиль", callback_data="profile"),
        types.InlineKeyboardButton(text="❓ Помощь", callback_data="help")
    )
    return builder.as_markup()

# --- Хендлеры ---
@dp.message(Command("start"))
async def start_cmd(message: types.Message):
    await db.create_user(message.from_user.id, message.from_user.username)
    await message.answer(WELCOME_TEXT, reply_markup=main_kb())

@dp.callback_query(F.data == "buy_gmp")
async def buy_gmp(callback: types.CallbackQuery):
    await callback.message.edit_text(BUY_TEXT, reply_markup=main_kb())

@dp.callback_query(F.data == "profile")
async def profile(callback: types.CallbackQuery):
    user = await db.get_user(callback.from_user.id)
    text = (
        f"👤 **Ваш профиль**\n\n"
        f"🆔 ID: `{user[0]}`\n"
        f"💰 Баланс: **{user[1]:,} Mcoin**\n"
        f"📥 Всего пополнено: **{user[2]:,} Mcoin**\n"
        f"📤 Выведено GMP: **{user[3]}**\n\n"
        "Чтобы купить GMP, убедитесь, что на балансе достаточно Mcoin!"
    )
    await callback.message.edit_text(text, reply_markup=main_kb())

@dp.callback_query(F.data == "help")
async def help_cmd(callback: types.CallbackQuery):
    help_text = (
        "📖 **Инструкция по использованию:**\n\n"
        "1. Создайте чек в боте @gminesbot.\n"
        "2. Скопируйте ссылку на чек и отправьте её СЮДА.\n"
        "3. Дождитесь уведомления о зачислении Mcoin.\n"
        "4. Нажмите кнопку 'Купить GMP' и введите количество (команда /buy_gmp_now).\n"
        "5. Ожидайте поступления GMP на ваш аккаунт!"
    )
    await callback.message.edit_text(help_text, reply_markup=main_kb())

# Обработка чека
@dp.message(F.text.contains("t.me/gminesbot?start=check_"))
async def process_check(message: types.Message):
    await message.answer("✅ **Ваш чек принят на проверку!**\nАдминистратор проверит его в ближайшее время.")
    
    # Кнопки для админа
    builder = InlineKeyboardBuilder()
    builder.add(types.InlineKeyboardButton(text="Начислить (Пример: 900к)", callback_data=f"admin_add_{message.from_user.id}"))
    
    await bot.send_message(
        ADMIN_ID, 
        f"📥 **Новый чек от пользователя!**\n\n"
        f"User: @{message.from_user.username} (ID: `{message.from_user.id}`)\n"
        f"Ссылка: {message.text}",
        reply_markup=builder.as_markup()
    )

# Админская команда для начисления (упрощенно через команду)
@dp.message(Command("add_bal"))
async def add_balance_manual(message: types.Message):
    if message.from_user.id != ADMIN_ID: return
    try:
        args = message.text.split()
        user_id = int(args[1])
        amount = int(args[2])
        await db.update_balance(user_id, amount)
        await message.answer(f"✅ Зачислено {amount} Mcoin пользователю {user_id}")
        await bot.send_message(user_id, f"🎉 **Баланс пополнен!**\nВам зачислено: {amount:,} Mcoin")
    except:
        await message.answer("Ошибка! Формат: /add_bal [id] [сумма]")

# Логика покупки (после пополнения)
@dp.message(Command("buy_gmp_now"))
async def process_purchase(message: types.Message):
    try:
        gmp_amount = float(message.text.split()[1])
        mcoin_cost = int(gmp_amount * 90000)
        user = await db.get_user(message.from_user.id)
        
        if user[1] < mcoin_cost:
            return await message.answer("❌ Недостаточно Mcoin на балансе!")
        
        await db.subtract_balance(message.from_user.id, mcoin_cost, gmp_amount)
        
        # Уведомление пользователю
        await message.answer(
            f"✅ **Покупка оформлена!**\n\n"
            f"📦 Товар: {gmp_amount} GMP\n"
            f"💰 Списано: {mcoin_cost:,} Mcoin\n"
            f"⏳ Сроки: 5-30 минут\n\n"
            f"Администратор уже приступил к отправке!"
        )
        
        # Уведомление админу
        await bot.send_message(ADMIN_ID, f"🚨 **ЗАКАЗ!**\nЮзер: `{message.from_user.id}` купил {gmp_amount} GMP за {mcoin_cost} Mcoin.")
        
    except:
        await message.answer("Введите команду так: `/buy_gmp_now 10` (где 10 — количество GMP)")

async def main():
    await db.init_db()
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
