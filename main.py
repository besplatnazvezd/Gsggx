import os
import re
import asyncio
import uuid
from typing import Dict, Any, Optional

from aiogram import Bot, Dispatcher, types
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram import exceptions as aiogram_exceptions

from supabase import create_client
import httpx
from dotenv import load_dotenv

load_dotenv()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")  # вставьте ваш токен
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")      # service_role key предпочтительно для RPC
AUTO_VERIFY_URL = os.getenv("AUTO_VERIFY_URL")  # опционально: URL Edge Function
ADMIN_ID = os.getenv("ADMIN_ID")  # можно использовать для уведомлений, если нужно

PRICE_PER_GMP = 90_000  # мкоин за 1 ГМП

bot = Bot(token=TELEGRAM_TOKEN, parse_mode="HTML")
storage = MemoryStorage()
dp = Dispatcher(storage=storage)

supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

# В памяти храним временные чек-данные: user_id -> {"code":..., "raw_link":...}
pending_checks: Dict[int, Dict[str, str]] = {}


# ---- Утилитарные клавиатуры ----
def main_keyboard() -> InlineKeyboardMarkup:
    kb = InlineKeyboardMarkup(row_width=3)
    kb.add(
        InlineKeyboardButton("🛒 Купить ГМП", callback_data="buy"),
        InlineKeyboardButton("👤 Профиль", callback_data="profile"),
        InlineKeyboardButton("❓ Помощь", callback_data="help")
    )
    return kb


def buy_menu_keyboard() -> InlineKeyboardMarkup:
    kb = InlineKeyboardMarkup(row_width=2)
    kb.add(
        InlineKeyboardButton("1 ГМП (90 000 мкоin)", callback_data="buy_gmp_1"),
        InlineKeyboardButton("10 ГМП (900 000 мкоin)", callback_data="buy_gmp_10"),
        InlineKeyboardButton("Выбрать количество", callback_data="choose_amount"),
        InlineKeyboardButton("Назад", callback_data="main")
    )
    return kb


def profile_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardMarkup(row_width=3)
    kb.add(
        InlineKeyboardButton("Пополнить баланс", callback_data="send_check"),
        InlineKeyboardButton("История транзакций", callback_data="history"),
        InlineKeyboardButton("Назад", callback_data="main")
    )
    return kb


def help_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardMarkup(row_width=2)
    kb.add(
        InlineKeyboardButton("Отправить чек", callback_data="send_check"),
        InlineKeyboardButton("Связаться с админом", callback_data="contact_admin"),
        InlineKeyboardButton("Назад", callback_data="main")
    )
    return kb


# ---- Хелперы Supabase ----
def upsert_user(tg_user: types.User) -> None:
    # Upsert пользователя в таблицу users
    supabase.table("users").upsert({
        "id": tg_user.id,
        "username": tg_user.username or ""
    }).execute()


def fetch_user_row(user_id: int) -> Optional[Dict[str, Any]]:
    res = supabase.table("users").select("*").eq("id", user_id).single().execute()
    if res.error:
        return None
    return res.data


# ---- Хендлеры ----
@dp.message(commands=["start"])
async def cmd_start(message: types.Message):
    user = message.from_user
    upsert_user(user)

    text = (
        "✨ Привет! Я — ваш надёжный помощник по покупке ГМП.\n\n"
        "Почему выбирают именно меня?\n"
        "— Честная цена: 1 ГМП = 90 000 мкоин 💎\n"
        "— Автозачисление: система сама проверяет чек и зачисляет баланс ⚡️\n"
        "— Удобный профиль и прозрачная статистика 📊\n\n"
        "Готовы начать? Нажмите кнопку ниже 👇"
    )
    await message.answer(text, reply_markup=main_keyboard())


@dp.callback_query()
async def process_callback(call: types.CallbackQuery):
    data = call.data
    try:
        if data == "buy":
            await call.message.answer("🛒 Купить ГМП — 1 ГМП = 90 000 мкоin\n\nПочему так дёшево? Прямые каналы поставки и минимальная маржа 😌\n\nВыберите опцию:", reply_markup=buy_menu_keyboard())
        elif data == "profile":
            await show_profile(call.message, call.from_user.id)
        elif data == "help":
            await show_help(call.message)
        elif data == "main":
            await call.message.answer("Главное меню:", reply_markup=main_keyboard())
        elif data == "choose_amount":
            await call.message.answer("Введите количество ГМП (например: 10)\n1 ГМП = 90 000 мкоin")
        elif data.startswith("buy_gmp_"):
            _, _, amt = data.partition("buy_gmp_")
            try:
                gmp_amount = float(amt)
                await handle_buy_gmp(call.message, call.from_user.id, gmp_amount)
            except Exception:
                await call.message.answer("Некорректное количество ГМП.")
        elif data == "send_check":
            await call.message.answer("Отправьте сюда ссылку-чек формата:\nhttps://t.me/gminesbot?start=check_6a51005ebb5ee3ec7e9c0361\n(сумма чека от 5 000 до 1 000 000 мкоin) 📩")
        elif data == "contact_admin":
            admin_note = "Связаться с админом: @" + (os.getenv("ADMIN_USERNAME") or "админ")
            await call.message.answer(admin_note)
        else:
            # возможно другие колбеки: order_status_{id}, history и т.д.
            if data.startswith("order_status_"):
                order_id = data.split("_", 2)[2]
                await send_order_status(call.message, order_id)
            elif data == "history":
                await send_history(call.message, call.from_user.id)
            else:
                await call.message.answer("Неизвестная команда.")
    finally:
        await call.answer()


async def show_profile(msg: types.Message, user_id: int):
    row = fetch_user_row(user_id)
    if not row:
        await msg.answer("Профиль не найден. Отправьте /start для регистрации.")
        return
    text = (
        f"👤 Профиль — @{row.get('username') or '—'}\n"
        f"ID: {row.get('id')}\n"
        f"Зарегистрирован: {row.get('registered_at')}\n\n"
        f"💰 Баланс: {row.get('balance_mcoin')} мкоin\n"
        f"🔸 ГМП в наличии: {row.get('gmp_balance')} ГМП\n\n"
        f"📥 Всего пополнено: {row.get('total_deposited')} мкоin\n"
        f"📤 Всего потрачено: {row.get('total_spent')} мкоin\n"
    )
    await msg.answer(text, reply_markup=profile_kb())


async def show_help(msg: types.Message):
    text = (
        "❓ Инструкция — как пополнить и купить ГМП\n\n"
        "1) Пополнение баланса\n"
        "— Создаёте чек в боте-поставщике (сумма от 5 000 до 1 000 000 мкоin) ➕\n"
        "— Получаете ссылку формата: https://t.me/gminesbot?start=check_<код> 📩\n"
        "— Отправляете эту ссылку сюда — бот автоматически проверит и зачислит мкоin на ваш баланс ⚡️\n\n"
        "2) Покупка ГМП\n"
        "— Нажмите «Купить ГМП», выберите количество или введите вручную.\n"
        "— Стоимость списывается с вашего баланса автоматически.\n"
        "— После покупки вы получите уведомление о заказе и сроках.\n\n"
        "Если возникнут вопросы — просто напишите сюда, мы всегда на связи ❤️"
    )
    await msg.answer(text, reply_markup=help_kb())


async def handle_buy_gmp(message: types.Message, user_id: int, gmp_amount: float):
    cost = int(gmp_amount * PRICE_PER_GMP)
    # Вызов RPC create_order: атомарное списание и создание заказа
    try:
        rpc = supabase.rpc("create_order", {
            "p_user_id": user_id,
            "p_gmp_amount": str(gmp_amount),
            "p_cost_mcoin": cost
        }).execute()
        if rpc.error:
            # Если недостаточно средств или другая ошибка
            err_msg = str(rpc.error.get("message") if isinstance(rpc.error, dict) else rpc.error)
            await message.answer(f"❌ Не удалось создать заказ: {err_msg}\nПополните баланс — создайте чек и отправьте ссылку (например: https://t.me/gminesbot?start=check_6a51005ebb5ee3ec7e9c0361) 📩")
            return

        order_result = rpc.data[0] if rpc.data else None
        order_id = order_result.get("order_id") if order_result else None

        text = (
            "✅ Покупка принята!\n\n"
            f"— Покупка: {gmp_amount} ГМП\n"
            f"— Списано: {cost} мкоin 💸\n"
            f"— Номер заказа: {order_id}\n"
            "— Ориентировочный срок получения: 5–24 часа (в редких случаях до 72 часов) ⏳\n\n"
            "Система автоматически выдаст ГМП и уведомит вас сразу после зачисления."
        )
        kb = InlineKeyboardMarkup(row_width=3)
        kb.add(
            InlineKeyboardButton("Статус заказа", callback_data=f"order_status_{order_id}"),
            InlineKeyboardButton("Профиль", callback_data="profile"),
            InlineKeyboardButton("Главное меню", callback_data="main")
        )
        await message.answer(text, reply_markup=kb)
    except Exception as e:
        await message.answer("⚠️ Внутренняя ошибка при создании заказа. Попробуйте чуть позже.")


async def send_order_status(message: types.Message, order_id: str):
    res = supabase.table("orders").select("*").eq("id", order_id).single().execute()
    if res.error or not res.data:
        await message.answer("Заказ не найден.")
        return
    o = res.data
    text = (
        f"📦 Статус заказа: {o.get('status')}\n"
        f"Номер: {o.get('id')}\n"
        f"ГМП: {o.get('gmp_amount')}\n"
        f"Списано: {o.get('cost_mcoin')} мкоin\n"
        f"Создан: {o.get('created_at')}\n"
    )
    await message.answer(text)


async def send_history(message: types.Message, user_id: int):
    topups = supabase.table("topups").select("*").eq("user_id", user_id).order("created_at", desc=True).limit(10).execute()
    orders = supabase.table("orders").select("*").eq("user_id", user_id).order("created_at", desc=True).limit(10).execute()

    text = "📚 Последние операции:\n\nПополнения:\n"
    if topups.data:
        for t in topups.data:
            text += f"- {t.get('created_at')}: {t.get('amount')} мкоin — {t.get('status')}\n"
    else:
        text += "- нет записей\n"

    text += "\nЗаказы:\n"
    if orders.data:
        for o in orders.data:
            text += f"- {o.get('created_at')}: {o.get('gmp_amount')} ГМП — {o.get('status')}\n"
    else:
        text += "- нет записей\n"

    await message.answer(text)


# ---- Обработка входящих текстов: чек-ссылка и сумма ----
@dp.message()
async def catch_text_messages(message: types.Message):
    user_id = message.from_user.id
    text = (message.text or "").strip()

    # Если у пользователя есть ожидаемый чек (we use pending_checks), то это — сумма
    if user_id in pending_checks and pending_checks[user_id].get("awaiting_amount"):
        # парсим сумму
        digits = re.sub(r"[^\d]", "", text)
        if not digits:
            await message.answer("❗ Введите сумму в мкоin цифрами (например: 900000).")
            return
        amount = int(digits)
        if amount < 5000 or amount > 1_000_000:
            await message.answer("❗ Сумма должна быть от 5 000 до 1 000 000 мкоin. Попробуйте ещё раз.")
            return

        code = pending_checks[user_id]["code"]
        raw_link = pending_checks[user_id]["raw_link"]

        # Создаём запись топапа в Supabase
        try:
            ins = supabase.table("topups").insert({
                "user_id": user_id,
                "code": code,
                "raw_link": raw_link,
                "amount": amount,
                "status": "pending"
            }).select("*").execute()
            if ins.error:
                await message.answer("⚠️ Не удалось сохранить чек. Попробуйте позже.")
                pending_checks.pop(user_id, None)
                return

            topup_row = ins.data[0]
            topup_id = topup_row.get("id")

            # очищаем pending
            pending_checks.pop(user_id, None)

            await message.answer("📩 Чек получен — система автоматически проверяет и зачислит средства. Это займёт несколько секунд ⚡️")

            # Вызываем Edge Function для автоматической проверки, если есть
            if AUTO_VERIFY_URL:
                async with httpx.AsyncClient(timeout=10) as client:
                    try:
                        r = await client.post(AUTO_VERIFY_URL, json={"topup_id": topup_id})
                        if r.status_code == 200:
                            await message.answer("✅ Чек верифицирован и средства зачислены автоматически. Проверьте профиль.")
                        else:
                            await message.answer("ℹ️ Чек принят на верификацию. Зачисление может занять немного времени.")
                    except Exception:
                        await message.answer("⚠️ Внутренняя ошибка верификации — чек принят, зачёт будет выполнен чуть позже.")
            else:
                # Локальная простая проверка и прямой вызов RPC verify_and_credit
                verified = await simple_verify_check(raw_link)
                if verified:
                    try:
                        supabase.rpc("verify_and_credit", {"p_topup_id": topup_id}).execute()
                        await message.answer("✅ Чек автоматически подтверждён и средства зачислены. Проверьте профиль.")
                    except Exception:
                        await message.answer("ℹ️ Чек принят, скоро средства будут зачислены.")
                else:
                    await message.answer("❌ Не удалось автоматически верифицировать чек. Проверьте ссылку или свяжитесь с поддержкой.")

        except Exception:
            await message.answer("⚠️ Ошибка при обработке чека. Попробуйте позже.")
        return

    # Иначе — пытаемся найти ссылку-чек
    m = re.search(r"https?://t\.me/gminesbot\?start=check_([A-Za-z0-9]+)", text)
    if m:
        code = m.group(1)
        raw_link = m.group(0)
        # сохраняем в pending и просим сумму
        pending_checks[user_id] = {"code": code, "raw_link": raw_link, "awaiting_amount": True}
        await message.answer("✅ Ссылка-чек принята. Укажите сумму в мкоin (например: 900000). Сумма должна быть от 5 000 до 1 000 000 мкоin.")
        return

    # Если сообщение не распознано
    # Мягкий ответ, не перебивая UX
    await message.answer("Я вас понял — отправьте ссылку-чек формата https://t.me/gminesbot?start=check_<код> для пополнения или используйте меню.", reply_markup=main_keyboard())


# Простая локальная проверка доступности ссылки (опционально)
async def simple_verify_check(raw_link: str) -> bool:
    if not re.search(r"check_[A-Za-z0-9]+", raw_link):
        return False
    try:
        async with httpx.AsyncClient(timeout=8) as client:
            r = await client.get(raw_link, follow_redirects=True)
            return r.status_code == 200
    except Exception:
        return False


# ---- Запуск бота ----
async def main():
    try:
        print("Bot starting...")
        await dp.start_polling(bot)
    except (KeyboardInterrupt, SystemExit):
        print("Shutting down...")
    finally:
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
