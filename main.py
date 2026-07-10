import os
import re
import asyncio
from datetime import datetime, timezone
from telethon import TelegramClient, events, types
from telethon.sessions import StringSession
from telethon.tl.functions.users import GetUsersRequest
from telethon.tl.types import UserStatusOnline, UserStatusOffline

# Попытка импортировать библиотеку для перевода
try:
    from deep_translator import GoogleTranslator
    HAS_TRANSLATOR = True
except ImportError:
    HAS_TRANSLATOR = False

# ============ КОНФИГУРАЦИЯ И НАСТРОЙКИ ============
API_ID = 2040
API_HASH = 'b18441a1ff607e10a989891a5462e627'

# Railway будет брать сессию из переменных окружения.
# Если запускаете локально — будет использоваться обычный файл 'userbot_session'
SESSION_STRING = os.environ.get("TELEGRAM_SESSION")

if SESSION_STRING:
    client = TelegramClient(StringSession(SESSION_STRING), API_ID, API_HASH)
else:
    client = TelegramClient('userbot_session', API_ID, API_HASH)

# Настройки для автоматического сбора чеков
TARGET_CHAT = 'gmineschat' # Чат, где бот ловит ссылки
TARGET_BOT = 'gminesbot'   # Бот, в котором активируются чеки

# Глобальные списки для слежки
edit_watch_list = []
watch_list = []
me_id = None

# ============ ОБРАБОТЧИКИ ДЛЯ МОМЕНТАЛЬНОЙ ЛОВЛИ И АКТИВАЦИИ ЧЕКОВ ============

# 1. Быстро ловит ссылки на чеки и отвечает на ФК
@client.on(events.NewMessage(chats=TARGET_CHAT))
async def auto_check_catcher(event):
    text_to_search = event.raw_text
    text_lower = text_to_search.lower()

    # --- ЛОГИКА ФК ---
    if text_lower.startswith("фк"):
        content = re.sub(r'^фк\s*', '', text_lower)
        content = re.sub(r'^\d+(?:\.\d*)?[кkмm]*\s*', '', content)
        content = content.strip()
        
        if not content:
            answer = "а"
        else:
            match_keyword = re.search(r'(?:пароль|капча)[:\s]*([^\s]+)', content)
            if match_keyword:
                answer = match_keyword.group(1)
            else:
                words = content.split()
                answer = words[0] if words else "а"
        
        try:
            await event.reply(answer)
            print(f"[ФК] Моментальный ответ: {answer}")
        except Exception as e:
            print(f"[Ошибка ФК] Не удалось ответить: {e}")
        return

    # --- ПОИСК ОБЫЧНОЙ ССЫЛКИ В ТЕКСТЕ ---
    check_code = None
    match = re.search(r'start=(check_[a-zA-Z0-9_]+)', text_to_search)
    if match:
        check_code = match.group(1)

    # Если код не найден в тексте, ищем в кнопках
    if not check_code and event.reply_markup:
        for row in event.reply_markup.rows:
            for button in row.buttons:
                if hasattr(button, 'url') and TARGET_BOT in button.url:
                    match = re.search(r'start=(check_[a-zA-Z0-9_]+)', button.url)
                    if match:
                        check_code = match.group(1)
                        break

    if check_code:
        print(f"[{TARGET_CHAT}] Обнаружен чек: {check_code}! Отправляю команду {TARGET_BOT}...")
        try:
            await client.send_message(TARGET_BOT, f"/start {check_code}")
        except Exception as e:
            print(f"[Ошибка] Не удалось отправить команду боту {TARGET_BOT}: {e}")

# 2. Мгновенно нажимает кнопку "Получить" в боте (TARGET_BOT)
@client.on(events.NewMessage(chats=TARGET_BOT))
@client.on(events.MessageEdited(chats=TARGET_BOT))
async def get_money_button_clicker(event):
    if event.reply_markup:
        for i, row in enumerate(event.reply_markup.rows):
            for j, button in enumerate(row.buttons):
                btn_text = button.text.lower()
                if "получить" in btn_text or "забрать" in btn_text:
                    print(f"[+] Найдена кнопка '{button.text}'! Кликаю...")
                    try:
                        await event.click(i, j)
                        print("[+] Успешно кликнул по кнопке!")
                        return
                    except Exception as e:
                        print(f"[Ошибка] Не удалось кликнуть на кнопку '{button.text}': {e}")

# ============ ЗАПУСК ЮЗЕРБОТА ============
async def main():
    global me_id
    await client.start()
    me = await client.get_me()
    me_id = me.id
    
    print(f"Бот запущен под аккаунтом: ID {me_id}!")
    print(f"--- Настройки ---")
    print(f"Целевой чат: @{TARGET_CHAT}")
    print(f"Целевой бот для чеков: @{TARGET_BOT}")
    print(f"Логика ФК: Активна")
    
    await client.run_until_disconnected()

if __name__ == '__main__':
    client.loop.run_until_complete(main())
