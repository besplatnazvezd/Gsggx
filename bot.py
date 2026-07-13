import os
import sqlite3
import logging
import re
from datetime import datetime
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
# КОНФИГУРАЦИЯ СТУДИИ И ИНТЕГРАЦИЙ
# ---------------------------------------------------------------------------
BOT_TOKEN = "8894416195:AAHZ4i0sTodK5AYKhqZfNIlrFBnlRTOiVR8" # Токен твоего нового бота из @BotFather
ADMIN_ID = 7727345054         # Твой Telegram ID (Администратор студии)

# Юзернеймы официальных ресурсов для оферты
STUDIO_CHANNEL = "@user"      # Канал про кодинг
OWNER_CONTACT = "@SkeletMines" # Твой личный юзернейм для связи

# Файл базы данных SQLite
DB_FILE = "studio_database.db"

# Символические разделители для текстов
DIVIDER = "════════════════════════════════════"

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", 
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# ИНИЦИАЛИЗАЦИЯ И РАБОТА С БАЗОЙ ДАННЫХ SQLite
# ---------------------------------------------------------------------------
def db_init():
    """Создание таблиц для учета пользователей, заказов и конфигурации."""
    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.cursor()
        
        # Таблица клиентов студии
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS clients (
                tg_id INTEGER PRIMARY KEY,
                username TEXT,
                full_name TEXT,
                registered_at TEXT
            )
        """)
        
        # Таблица технических заданий (заказов)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                client_id INTEGER,
                project_name TEXT,
                project_desc TEXT,
                budget TEXT,
                contact_info TEXT,
                status TEXT DEFAULT 'НА РАССМОТРЕНИИ',
                created_at TEXT
            )
        """)
        
        # Системные константы
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS system_config (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        """)
        conn.commit()

def db_register_client(tg_id: int, username: str | None, full_name: str):
    """Регистрация нового клиента в базе данных."""
    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.cursor()
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        cursor.execute("""
            INSERT OR IGNORE INTO clients (tg_id, username, full_name, registered_at)
            VALUES (?, ?, ?, ?)
        """, (tg_id, username, full_name, now))
        conn.commit()

def db_create_order(client_id: int, name: str, desc: str, budget: str, contact: str) -> int:
    """Создание новой заявки на разработку."""
    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.cursor()
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        cursor.execute("""
            INSERT INTO orders (client_id, project_name, project_desc, budget, contact_info, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (client_id, name, desc, budget, contact, now))
        conn.commit()
        return cursor.lastrowid

def db_get_client_orders(client_id: int) -> list:
    """Получение списка всех заказов конкретного пользователя."""
    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT id, project_name, status, created_at FROM orders 
            WHERE client_id = ? ORDER BY id DESC
        """, (client_id,))
        return cursor.fetchall()

def db_get_order_details(order_id: int) -> tuple | None:
    """Получение полных данных о заказе."""
    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT id, client_id, project_name, project_desc, budget, contact_info, status, created_at 
            FROM orders WHERE id = ?
        """, (order_id,))
        return cursor.fetchone()

def db_update_order_status(order_id: int, status: str):
    """Обновление текущего статуса разработки заказа."""
    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.cursor()
        cursor.execute("UPDATE orders SET status = ? WHERE id = ?", (status, order_id))
        conn.commit()

def db_get_all_orders_for_admin() -> list:
    """Выгрузка всех заказов для администратора."""
    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT id, project_name, status FROM orders ORDER BY id DESC")
        return cursor.fetchall()

def db_get_stats() -> dict:
    """Сбор статистики по базе данных."""
    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM clients")
        total_clients = cursor.fetchone()[0]
        cursor.execute("SELECT COUNT(*) FROM orders")
        total_orders = cursor.fetchone()[0]
        return {"clients": total_clients, "orders": total_orders}

# ---------------------------------------------------------------------------
# ИНЛАЙН КЛАВИАТУРЫ И ИНТЕРФЕЙСНЫЕ СТРУКТУРЫ
# ---------------------------------------------------------------------------
def get_main_keyboard() -> InlineKeyboardMarkup:
    """Главная навигационная панель ИТ-студии."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📝 Оформить Техническое Задание", callback_data="btn_new_project")],
        [InlineKeyboardButton("💼 Мои текущие заказы", callback_data="btn_my_projects")],
        [InlineKeyboardButton("⚖️ Юридическая Оферта и Тарифы", callback_data="btn_show_tos")],
        [InlineKeyboardButton("📞 Связаться с главным инженером", url=f"https://t.me/{OWNER_CONTACT.replace('@', '')}")]
    ])

def get_back_to_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Вернуться в Главное Меню", callback_data="btn_go_home")]])

def get_cancel_submission_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("❌ Прервать оформление ТЗ", callback_data="btn_go_home")]])

def get_admin_main_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📂 Управление заказами", callback_data="admin_manage_orders")],
        [InlineKeyboardButton("📊 Статистика ИТ-Студии", callback_data="admin_show_stats")],
        [InlineKeyboardButton("📢 Массовая рассылка клиентам", callback_data="admin_start_broadcast")]
    ])

# ---------------------------------------------------------------------------
# ОБРАБОТЧИКИ БАЗОВЫХ КОМАНД TELEGRAM
# ---------------------------------------------------------------------------
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Приветствие пользователя и запуск бота."""
    user = update.effective_user
    db_register_client(user.id, user.username, user.full_name)
    
    # Сброс состояния заполнения ТЗ
    context.user_data.clear()
    
    text = (
        f"ДОБРО ПОЖАЛОВАТЬ В ИНЖЕНЕРНЫЙ ЦЕНТР РАЗРАБОТКИ\n"
        f"{DIVIDER}\n"
        f"Мы рады приветствовать вас в нашей профессиональной студии автоматизации.\n\n"
        f"Здесь вы можете заказать программирование Telegram-ботов, автоматизированных скриптов, "
        f"юзерботов любой сложности, а также интеграцию сложных баз данных под ваши проекты.\n\n"
        f"Для навигации по разделам используйте интерактивные кнопки ниже 👇"
    )
    await update.message.reply_text(text, reply_markup=get_main_inline_keyboard_layout())

def get_main_inline_keyboard_layout() -> InlineKeyboardMarkup:
    return get_main_keyboard()

async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Принудительная отмена любых операций заполнения."""
    context.user_data.clear()
    await update.message.reply_text(
        "Действие успешно отменено. Вы возвращены на главный экран управления студией.",
        reply_markup=get_main_keyboard()
    )

# ---------------------------------------------------------------------------
# ОБРАБОТКА ИНЛАЙН-НАЖАТИЙ (MENU & FLOW NAVIGATION)
# ---------------------------------------------------------------------------
async def inline_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    
    user_id = query.from_user.id
    data = query.data
    
    # Сценарий возвращения Домой
    if data == "btn_go_home":
        context.user_data.clear() # Сбрасываем кэш
        text = (
            f"ГЛАВНЫЙ ЭКРАН СТУДИИ РАЗРАБОТКИ\n"
            f"{DIVIDER}\n"
            f"Вы находитесь в меню управления заказами. Выберите нужную операцию на кнопках ниже:"
        )
        await query.edit_message_text(text, reply_markup=get_main_keyboard(), parse_mode="HTML")
        return

    # Показ юридической оферты и условий оплаты
    elif data == "btn_show_tos":
        tos_text = (
            f"ЮРИДИЧЕСКИЙ РЕГЛАМЕНТ И УСЛОВИЯ ОКАЗАНИЯ УСЛУГ\n"
            f"{DIVIDER}\n"
            f"Настоящий документ является публичным соглашением сторон. Отправка заявки на разработку "
            f"означает согласие со следующими пунктами:\n\n"
            f"1. СПОСОБЫ РАСЧЕТА И ВАЛЮТЫ\n"
            f"Мы принимаем оплату исключительно в трех активах:\n"
            f"• mCoin (m¢) — перевод через официального игрового бота @gminesbot.\n"
            f"• Валюта GMP (второй системный токен проекта) — переводы через @gminesbot.\n"
            f"• Telegram Stars (официальные Звезды) — удобная оплата картой внутри мессенджера.\n\n"
            f"2. ПОРЯДОК ВНЕСЕНИЯ ИЗМЕНЕНИЙ И ПРАВОК\n"
            f"• Вся разработка осуществляется строго по утвержденному ТЗ.\n"
            f"• Любые переделки, доработки кода, добавление кнопок, изменение функционала или "
            f"интеграция дополнительных API после утверждения ТЗ оплачиваются строго пакетами.\n"
            f"• Стоимость Пакета Модификаций составляет 50,000 mCoin (или эквивалент в GMP/Stars).\n"
            f"• В один Пакет Модификаций входит ровно 4 (четыре) отдельных изменения средней сложности.\n"
            f"• Отдельные мелкие правки вне пакета не выполняются.\n\n"
            f"3. ПРИЕМКА ПРОЕКТОВ\n"
            f"После демонстрации проекта Заказчику предоставляется 24 часа на полное тестирование. "
            f"По истечении 24 часов проект считается успешно сданным."
        )
        await query.edit_message_text(tos_text, reply_markup=get_back_to_menu_keyboard(), parse_mode="HTML")
        return

    # Просмотр списка своих проектов
    elif data == "btn_my_projects":
        orders = db_get_client_orders(user_id)
        if not orders:
            text = (
                f"СПИСОК ВАШИХ ЗАКАЗОВ ПУСТ\n"
                f"{DIVIDER}\n"
                f"Вы еще не оформили ни одного технического задания через наш автоматизированный конструктор.\n"
                f"Желаете создать проект прямо сейчас?"
            )
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("📝 Написать новое ТЗ", callback_data="btn_new_project")],
                [InlineKeyboardButton("◀️ Вернуться в меню", callback_data="btn_go_home")]
            ])
            await query.edit_message_text(text, reply_markup=keyboard, parse_mode="HTML")
        else:
            text = f"СПИСОК ВАШИХ ТЕКУЩИХ ПРОЕКТОВ:\n{DIVIDER}\n\n"
            keyboard_buttons = []
            for idx, order in enumerate(orders, 1):
                order_id, name, status, created = order
                text += f"{idx}. ID {order_id} — {name}\n• Статус: {status}\n• Дата: {created}\n\n"
                keyboard_buttons.append([InlineKeyboardButton(f"Посмотреть детали ID {order_id}", callback_data=f"view_order_{order_id}")])
            
            keyboard_buttons.append([InlineKeyboardButton("◀️ Вернуться в меню", callback_data="btn_go_home")])
            await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard_buttons), parse_mode="HTML")
        return

    # Детали конкретного заказа пользователя
    elif data.startswith("view_order_"):
        order_id = int(data.split("_")[2])
        order = db_get_order_details(order_id)
        if not order:
            await query.message.reply_text("Проект не найден.", reply_markup=get_back_to_menu_keyboard())
            return
            
        _, _, name, desc, budget, contact, status, created = order
        text = (
            f"ДЕТАЛЬНАЯ ИНФОРМАЦИЯ О ПРОЕКТЕ ID {order_id}\n"
            f"{DIVIDER}\n\n"
            f"• НАЗВАНИЕ: {name}\n"
            f"• СТАТУС: {status}\n"
            f"• ДАТА ЗАЯВКИ: {created}\n\n"
            f"• ОПИСАНИЕ И ТЗ:\n<i>{desc}</i>\n\n"
            f"• ВЫБРАННЫЙ БЮДЖЕТ: {budget}\n"
            f"• КОНТАКТ ДЛЯ СВЯЗИ: {contact}"
        )
        await query.edit_message_text(text, reply_markup=get_back_to_menu_keyboard(), parse_mode="HTML")
        return

    # Инициализация пошагового оформления нового ТЗ
    elif data == "btn_new_project":
        context.user_data["step"] = "AWAITING_PROJECT_NAME"
        text = (
            f"ЗАПУСК КОНСТРУКТОРА ТЕХНИЧЕСКОГО ЗАДАНИЯ\n"
            f"{DIVIDER}\n"
            f"Мы поможем вам правильно составить ТЗ. Напишите первым сообщением НАЗВАНИЕ вашего "
            f"будущего программного продукта (например: Торговый бот для магазинов, Юзербот-автовыплатчик и т.д.):"
        )
        await query.edit_message_text(text, reply_markup=get_cancel_submission_keyboard(), parse_mode="HTML")
        return

    # ---------------------------------------------------------------------------
    # АДМИН-КОЛБЭКИ УПРАВЛЕНИЯ СТУДИЕЙ
    # ---------------------------------------------------------------------------
    elif data == "admin_show_stats" and user_id == ADMIN_ID:
        stats = db_get_stats()
        text = (
            f"📊 СТАТИСТИКА ИТ-ЛАБОРАТОРИИ\n"
            f"{DIVIDER}\n"
            f"• Общее число клиентов в базе данных: {stats['clients']}\n"
            f"• Общее количество поданных заявок (ТЗ): {stats['orders']}"
        )
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ В Админку", callback_data="admin_go_back")]]))
        return

    elif data == "admin_go_back" and user_id == ADMIN_ID:
        await query.edit_message_text("ПАНЕЛЬ АДМИНИСТРАТОРА ИТ-СТУДИИ", reply_markup=get_admin_main_keyboard())
        return

    elif data == "admin_manage_orders" and user_id == ADMIN_ID:
        orders = db_get_all_orders_for_admin()
        if not orders:
            await query.edit_message_text("В базе данных пока нет ни одного заказа.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ В Админку", callback_data="admin_go_back")]]))
        else:
            text = "СПИСОК ВСЕХ ЗАКАЗОВ В СИСТЕМЕ:\n\n"
            keyboard_buttons = []
            for order in orders:
                oid, name, status = order
                text += f"ID {oid}: {name} (Статус: {status})\n"
                keyboard_buttons.append([InlineKeyboardButton(f"Управлять ID {oid}", callback_data=f"adm_view_{oid}")])
            keyboard_buttons.append([InlineKeyboardButton("◀️ В Админку", callback_data="admin_go_back")])
            await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard_buttons))
        return

    elif data.startswith("adm_view_") and user_id == ADMIN_ID:
        oid = int(data.split("_")[2])
        order = db_get_order_details(oid)
        if not order:
            await query.message.reply_text("Заказ не найден.")
            return
        _, client_id, name, desc, budget, contact, status, created = order
        text = (
            f"УПРАВЛЕНИЕ ЗАКАЗОМ ID {oid}\n"
            f"Клиент ID: {client_id}\n"
            f"Имя проекта: {name}\n"
            f"Создан: {created}\n"
            f"Бюджет: {budget}\n"
            f"Контакты: {contact}\n"
            f"Статус: {status}\n\n"
            f"Описание проекта:\n{desc}"
        )
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("Одобрить и запустить", callback_data=f"adm_status_{oid}_В РАБОТЕ")],
            [InlineKeyboardButton("Завершить разработку", callback_data=f"adm_status_{oid}_ВЫПОЛНЕН")],
            [InlineKeyboardButton("Отклонить проект", callback_data=f"adm_status_{oid}_ОТКЛОНЕН")],
            [InlineKeyboardButton("◀️ К заказам", callback_data="admin_manage_orders")]
        ])
        await query.edit_message_text(text, reply_markup=keyboard)
        return

    elif data.startswith("adm_status_") and user_id == ADMIN_ID:
        parts = data.split("_")
        oid = int(parts[2])
        new_status = parts[3]
        db_update_order_status(oid, new_status)
        
        # Получаем данные заказа, чтобы узнать ID клиента для отправки уведомления
        order = db_get_order_details(oid)
        if order:
            client_id = order[1]
            try:
                await context.bot.send_message(
                    chat_id=client_id,
                    text=f"⚙️ <b>Обновление статуса вашего проекта ID {oid}!</b>\nНовый статус: <u>{new_status}</u>",
                    parse_mode="HTML"
                )
            except Exception:
                pass
                
        await query.edit_message_text(f"Статус заказа ID {oid} изменен на: {new_status}", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ К заказам", callback_data="admin_manage_orders")]]))
        return

    elif data == "admin_start_broadcast" and user_id == ADMIN_ID:
        context.user_data["admin_broadcast_state"] = "AWAITING_TEXT"
        await query.edit_message_text(
            "Введите текст рекламного или сервисного сообщения для массовой рассылки по всем клиентам:",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Отмена рассылки", callback_data="admin_go_back")]])
        )
        return

# ---------------------------------------------------------------------------
# ОБРАБОТЧИК ПОШАГОВОГО ТЕКСТОВОГО ВВОДА (STATE MACHINE)
# ---------------------------------------------------------------------------
async def handle_text_messages(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    text = update.message.text.strip()
    step = context.user_data.get("step")

    # Сценарий создания ТЗ
    if step == "AWAITING_PROJECT_NAME":
        context.user_data["p_name"] = text
        context.user_data["step"] = "AWAITING_PROJECT_DESC"
        await update.message.reply_text(
            "Отлично! Название записано.\n\n"
            "Теперь максимально подробно опишите весь необходимый функционал бота или скрипта. "
            "Какая логика должна выполняться, какие будут кнопки, нужны ли интеграции, базы данных? "
            "Отправьте детальное описание одним сообщением:",
            reply_markup=get_cancel_submission_keyboard()
        )
        return

    elif step == "AWAITING_PROJECT_DESC":
        context.user_data["p_desc"] = text
        context.user_data["step"] = "AWAITING_PROJECT_BUDGET"
        await update.message.reply_text(
            "Техническое задание зафиксировано.\n\n"
            "Напишите желаемый бюджет проекта и валюту, в которой планируете производить оплату "
            "(Например: 150k mCoin, 2 GMP или 350 Stars Telegram):",
            reply_markup=get_cancel_submission_keyboard()
        )
        return

    elif step == "AWAITING_PROJECT_BUDGET":
        context.user_data["p_budget"] = text
        context.user_data["step"] = "AWAITING_PROJECT_CONTACT"
        await update.message.reply_text(
            "Условия бюджета зафиксированы.\n\n"
            "Пожалуйста, укажите ваш точный юзернейм в Telegram или другие контактные данные для "
            "связи (например: @SkeletMines):",
            reply_markup=get_cancel_submission_keyboard()
        )
        return

    elif step == "AWAITING_PROJECT_CONTACT":
        p_name = context.user_data.get("p_name")
        p_desc = context.user_data.get("p_desc")
        p_budget = context.user_data.get("p_budget")
        p_contact = text
        
        # Записываем проект в базу данных SQLite
        order_id = db_create_order(user_id, p_name, p_desc, p_budget, p_contact)
        
        # Сбрасываем шаги
        context.user_data.clear()
        
        # Отправляем подтверждение пользователю
        confirmation_text = (
            f"🎉 ВАШЕ ТЕХНИЧЕСКОЕ ЗАДАНИЕ УСПЕШНО ОФОРМЛЕНО!\n"
            f"{DIVIDER}\n"
            f"• Проекту присвоен уникальный номер: ID {order_id}\n"
            f"• Название: {p_name}\n"
            f"• Указанный бюджет: {p_budget}\n"
            f"• Контакт для связи: {p_contact}\n\n"
            f"Главный разработчик рассмотрит ваше ТЗ в ближайшее время и свяжется с вами для уточнения деталей. "
            f"Вы можете отслеживать статус выполнения в разделе 'Мои заказы'."
        )
        await update.message.reply_text(confirmation_text, reply_markup=get_main_keyboard(), parse_mode="HTML")
        
        # Оповещаем администратора студии о новом заказе
        admin_alert_text = (
            f"🔔 ПОСТУПИЛО НОВОЕ ТЕХНИЧЕСКОЕ ЗАДАНИЕ!\n"
            f"═" * 20 + "\n"
            f"• Номер заказа: ID {order_id}\n"
            f"• Проект: {p_name}\n"
            f"• Бюджет: {p_budget}\n"
            f"• Клиент: {p_contact} (ID {user_id})\n\n"
            f"Для управления заказом перейдите в панель управления /admin"
        )
        try:
            await context.bot.send_message(chat_id=ADMIN_ID, text=admin_alert_text)
        except Exception as e:
            logger.error(f"Не удалось отправить уведомление админу: {e}")
        return

    # Админская рассылка
    if context.user_data.get("admin_broadcast_state") == "AWAITING_TEXT" and user_id == ADMIN_ID:
        context.user_data.clear()
        
        # Выгружаем список всех клиентов
        with sqlite3.connect(DB_FILE) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT tg_id FROM clients")
            clients = cursor.fetchall()
            
        if not clients:
            await update.message.reply_text("В базе данных еще нет зарегистрированных клиентов для рассылки.")
            return
            
        await update.message.reply_text("Запуск массовой рассылки... Пожалуйста, подождите.")
        
        sent_count = 0
        fail_count = 0
        for client in clients:
            try:
                await context.bot.send_message(chat_id=client[0], text=text, parse_mode="HTML")
                sent_count += 1
                await asyncio.sleep(0.05) # Небольшой лимит для избежания спам-блока Telegram API
            except Exception:
                fail_count += 1
                
        await update.message.reply_text(
            f"Рассылка полностью завершена!\n"
            f"• Успешно доставлено: {sent_count} клиентам\n"
            f"• Не удалось отправить: {fail_count} пользователям"
        )
        return

# ---------------------------------------------------------------------------
# АДМИНИСТРАТИВНЫЕ СЛУЖЕБНЫЕ КОМАНДЫ
# ---------------------------------------------------------------------------
async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Вход в панель администрирования студии."""
    if update.effective_user.id != ADMIN_ID:
        return
    await update.message.reply_text(
        "ПАНЕЛЬ АДМИНИСТРАТОРА ИТ-СТУДИИ",
        reply_markup=get_admin_main_keyboard()
    )

# ---------------------------------------------------------------------------
# ЗАПУСК И ИНИЦИАЛИЗАЦИЯ ПРИЛОЖЕНИЯ
# ---------------------------------------------------------------------------
def main() -> None:
    # Инициализация базы данных SQLite
    db_init()
    
    # Создание Telegram-приложения
    app = Application.builder().token(BOT_TOKEN).build()
    
    # Регистрация системных команд
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("cancel", cancel_command))
    app.add_handler(CommandHandler("admin", admin_command))
    
    # Интерактивные нажатия кнопок (инлайн меню)
    app.add_handler(CallbackQueryHandler(inline_callback_handler))
    
    # Текстовые сообщения пользователей (Шаги ТЗ)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_messages))
    
    # Конфигурация меню синей кнопки
    app.bot.set_my_commands([
        BotCommand("start", "🏠 В главное меню"),
        BotCommand("cancel", "❌ Прервать заполнение ТЗ")
    ])
    
    logger.info("Бот новой ИТ-Студии кодинга успешно запущен на SQLite!")
    app.run_polling()

if __name__ == "__main__":
    main()
