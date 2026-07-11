import asyncio
import logging
import re
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.utils.keyboard import InlineKeyboardBuilder
from config import BOT_TOKEN, ADMIN_ID
import database as db

logging.basicConfig(level=logging.INFO)
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# Вспомогательная функция для перевода "20к" / "20k" в 20000
def parse_amount(text: str) -> int:
    clean_text = text.strip().lower().replace(" ", "")
    multiplier = 1
    if clean_text.endswith(('k', 'к')):
        multiplier = 1000
        clean_text = clean_text[:-1]
    elif clean_text.endswith(('m', 'м')):
        multiplier = 1000000
        clean_text = clean_text[:-1]
    return int(float(clean_text) * multiplier)

# --- Тексты ---
WELCOME_TEXT = (
    "👋 **Добро пожаловать в GMP от salfika Store!**\n\n"
    "🚀 Почему выбирают именно нас?\n"
    "• **Скорость:** Автоматизированная обработка заявок.\n"
    "• **Безопасность:** Прямые переводы без посредников.\n"
    "• **Репутация:** Нам доверяют более 500+ игроков.\n\n"
    "💎 Мы — крупнейший хаб по обмену валют. Наш бот поможет тебе получить GMP максимально выгодно!"
)

BUY_TEXT = (
    "💳 **Покупка GMP**\n\n"
    "💰 Курс: **1 GMP = 90,000 Mcoin**\n\n"
    "🤔 *Почему так дешево?*\n"
    "Мы работаем напрямую с крупными пулами ликвидности и закупаем валюту оптом, исключая комиссии бирж. "
    "Это позволяет нам держать цену  в 4,44 раза ниже рыночной!\n\n"
    "⏳ **Сроки доставки:** от 5 до 30 минут после подтверждения, максимум до 24 часов.\n\n"
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

# --- Прием чека от пользователя ---
@dp.message(F.text.contains("t.me/gminesbot?start=check_"))
async def process_check(message: types.Message):
    await message.answer("✅ **Ваш чек принят на проверку!**\nАдминистратор проверит его в ближайшее время.")
    
    await bot.send_message(
        ADMIN_ID, 
        f"📥 **Новый чек от пользователя!**\n\n"
        f"User: @{message.from_user.username or 'None'} (ID: `{message.from_user.id}`)\n"
        f"Ссылка: {message.text}\n\n"
        f"👉 _Вы можете ответить на это сообщение суммой (например, '20к') или ввести команду:_ `/add_bal {message.from_user.id} 20к`"
    )

# --- СПОСОБ 1: НАЧИСЛЕНИЕ ПО КОМАНДЕ С ID (/add_bal ID сумма) ---
@dp.message(Command("add_bal"), F.from_user.id == ADMIN_ID)
async def add_balance_by_id(message: types.Message):
    try:
        args = message.text.split()
        if len(args) < 3:
            return await message.answer("❌ Неверный формат!\nИспользуйте: `/add_bal [ID] [сумма]`\nПример: `/add_bal 8491563038 20к`")
        
        user_id = int(args[1])
        amount = parse_amount(args[2])
        
        # Проверяем, есть ли юзер в нашей БД
        user = await db.get_user(user_id)
        if not user:
            return await message.answer(f"❌ Пользователь с ID `{user_id}` еще ни разу не запускал этого бота!")
            
        await db.update_balance(user_id, amount)
        await message.answer(f"✅ Успешно начислено **{amount:,} Mcoin** пользователю `{user_id}`.")
        
        # Оповещаем пользователя
        try:
            await bot.send_message(
                user_id, 
                f"🎉 **Баланс пополнен!**\n\n"
                f"Вам зачислено: **{amount:,} Mcoin**.\n"
                f"Теперь вы можете купить GMP! Для этого напишите команду:\n"
                f"`/buy_gmp_now [количество]` (например: `/buy_gmp_now 10`)"
            )
        except Exception:
            await message.answer("⚠️ Сообщение пользователю не доставлено (возможно, бот заблокирован).")
            
    except Exception as e:
        await message.answer(f"❌ Ошибка: {str(e)}")

# --- СПОСОБ 2: БЫСТРЫЙ ОТВЕТ НА СООБЩЕНИЕ ---
@dp.message(F.reply_to_message & (F.from_user.id == ADMIN_ID))
async def process_admin_reply(message: types.Message):
    reply = message.reply_to_message
    if "📥 Новый чек от пользователя!" in reply.text:
        match_id = re.search(r"ID:\s*`?(\d+)`?", reply.text)
        if not match_id:
            return await message.answer("❌ Не удалось извлечь ID пользователя из сообщения.")
        
        user_id = int(match_id.group(1))
        
        try:
            amount = parse_amount(message.text)
        except Exception:
            return await message.answer("❌ Напишите сумму числом (например: `20k`, `900к` или `900000`).")
            
        await db.update_balance(user_id, amount)
        await message.answer(f"✅ Успешно начислено **{amount:,} Mcoin** пользователю `{user_id}`.")
        
        try:
            await bot.send_message(
                user_id, 
                f"🎉 **Баланс пополнен!**\n\n"
                f"Вам зачислено: **{amount:,} Mcoin**.\n"
                f"Теперь вы можете купить GMP! Для этого напишите команду:\n"
                f"`/buy_gmp_now [количество]` (например: `/buy_gmp_now 10`)"
            )
        except Exception:
            await message.answer("⚠️ Сообщение пользователю не доставлено (возможно, бот заблокирован).")

# --- Покупка GMP пользователем ---
@dp.message(Command("buy_gmp_now"))
async def process_purchase(message: types.Message):
    try:
        gmp_amount = float(message.text.split()[1])
        mcoin_cost = int(gmp_amount * 90000)
        user = await db.get_user(message.from_user.id)
        
        if not user or user[1] < mcoin_cost:
            return await message.answer("❌ Недостаточно Mcoin на балансе!")
        
        await db.subtract_balance(message.from_user.id, mcoin_cost, gmp_amount)
        
        await message.answer(
            f"✅ **Покупка оформлена!**\n\n"
            f"📦 Товар: **{gmp_amount} GMP**\n"
            f"💰 Списано: **{mcoin_cost:,} Mcoin**\n"
            f"⏳ Сроки: **5-30 минут, максимум до 24 часов**\n\n"
            f"Администратор уже приступил к отправке!"
        )
        
        await bot.send_message(
            ADMIN_ID, 
            f"🚨 **НОВЫЙ ЗАКАЗ на GMP!**\n\n"
            f"Юзер: @{message.from_user.username or 'None'} (ID: `{message.from_user.id}`)\n"
            f"Купил: **{gmp_amount} GMP**\n"
            f"Списано: **{mcoin_cost:,} Mcoin**\n\n"
            f"⚠️ Отправьте ему товар!"
        )
        
    except:
        await message.answer("Введите команду так: `/buy_gmp_now 10` (где 10 — количество GMP)")

async def main():
    await db.init_db()
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
