import asyncio
import sqlite3
import re
import time
import os
import sys
import traceback
import random
import matplotlib.dates as mdates
from datetime import datetime, timedelta

import matplotlib
matplotlib.use('Agg') 
import matplotlib.pyplot as plt

import undetected_chromedriver as uc
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, FSInputFile, BotCommand
from apscheduler.schedulers.asyncio import AsyncIOScheduler

import os
from dotenv import load_dotenv

load_dotenv() # Загружает данные из файла .env
API_TOKEN = os.getenv("BOT_TOKEN")

# Если токен не найден, бот выдаст понятную ошибку
if not API_TOKEN:
    exit("Ошибка: Токен не найден в файле .env")

# --- НАСТРОЙКИ --- #
API_TOKEN = "ТВОЙ_ТОКЕН"
IMAGE_PATH = "image_db2d24.png"

bot = Bot(token=API_TOKEN)
dp = Dispatcher()
scheduler = AsyncIOScheduler()

# Семафор ограничивает общее число открытых браузеров (чтобы не съели RAM)
browser_semaphore = asyncio.Semaphore(5)
# Лок нужен специально, чтобы избежать ошибки WinError 183 при создании драйвера
driver_lock = asyncio.Lock()

# --- БАЗА ДАННЫХ (без изменений) --- #
def init_db():
    conn = sqlite3.connect("tracker.db")
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            url TEXT,
            name TEXT,
            target_price REAL,
            last_price REAL
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS price_history (
            item_id INTEGER, 
            price REAL, 
            timestamp TEXT
        )
    """)
    conn.commit()
    conn.close()

def log_price(item_id, price):
    conn = sqlite3.connect("tracker.db")
    conn.execute("INSERT INTO price_history (item_id, price, timestamp) VALUES (?, ?, ?)",
                 (item_id, price, datetime.now().isoformat()))
    conn.commit()
    conn.close()

# --- ПАРСЕР --- #
def get_ozon_data(url):
    options = uc.ChromeOptions()
    options.add_argument("--headless") 
    driver = None
    try:
        # Здесь происходит магия: сам запуск Chrome вынесен во внешнюю функцию под замок
        driver = uc.Chrome(options=options)
        driver.get(url)
        time.sleep(10)
        html = driver.page_source
        
        price_match = re.search(r'<meta property="og:price:amount" content="([\d\.,]+)"', html)
        if price_match:
            return float(price_match.group(1).replace(',', '.'))
        
        json_prices = re.findall(r'"price":"?([\d\.,]+)"?', html)
        if json_prices:
            return float(json_prices[0].replace(',', '.'))
        return None
    except Exception as e:
        print(f"Ошибка внутри парсера: {e}")
        return None
    finally:
        if driver: 
            try: driver.quit()
            except: pass

async def bounded_get_ozon_data(url):
    async with browser_semaphore:
        # ЗАМОК: Только один поток за раз создает объект драйвера
        # Это исключает WinError 183
        async with driver_lock:
            # Небольшая пауза, чтобы Windows успела "отпустить" файлы
            await asyncio.sleep(1)
            result = await asyncio.get_event_loop().run_in_executor(None, get_ozon_data, url)
        return result

# --- ПРОВЕРКА ЦЕН --- #
async def check_prices_task(report_id=None):
    print(f"\n [{datetime.now().strftime('%H:%M:%S')}] Начало массовой проверки...")
    conn = sqlite3.connect("tracker.db")
    if report_id:
        items = conn.execute("SELECT id, user_id, url, target_price, name FROM items WHERE user_id = ?", (report_id,)).fetchall()
    else:
        items = conn.execute("SELECT id, user_id, url, target_price, name FROM items").fetchall()
    conn.close()

    if not items:
        if report_id: await bot.send_message(report_id, "📭 Список пуст.")
        return

    async def process_item(iid, uid, url, target, name):
        current = await bounded_get_ozon_data(url)
        if current:
            log_price(iid, current)
            c = sqlite3.connect("tracker.db")
            c.execute("UPDATE items SET last_price = ? WHERE id = ?", (current, iid))
            c.commit()
            c.close()
            if current <= target:
                try: await bot.send_message(uid, f"🎯 **ЦЕЛЬ ДОСТИГНУТА!**\n📦 [{name}]({url})\n💰 Цена: **{current}**", parse_mode="Markdown")
                except: pass
            return True
        return False

    tasks = [process_item(*item) for item in items]
    await asyncio.gather(*tasks)
    
    if report_id:
        # Автоматический вызов списка после проверки
        await cmd_list_internal(report_id)

async def cmd_list_internal(user_id):
    conn = sqlite3.connect("tracker.db")
    rows = conn.execute("SELECT id, name, target_price, last_price, url FROM items WHERE user_id = ?", (user_id,)).fetchall()
    conn.close()
    if rows:
        text = "📋 **Результаты проверки:**\n\n"
        for r in rows:
            text += f"🔹 {r[0]}. [{r[1]}]({r[4]})\n🎯 Цель: `{r[2]}` | 🕒 Сейчас: `{r[3]}`\n\n"
        await bot.send_message(user_id, text, parse_mode="Markdown", disable_web_page_preview=True)

# --- ТГ КОМАНДЫ (сокращенно) --- #
@dp.message(Command("check"))
async def cmd_check(message: types.Message):
    await message.answer("🔄 Запущено. Ожидайте обновления списка...")
    asyncio.create_task(check_prices_task(message.from_user.id))

@dp.message(Command("list"))
async def cmd_list(message: types.Message):
    await cmd_list_internal(message.from_user.id)

# ОСТАЛЬНЫЕ КОМАНДЫ (add, del, graph) оставь как были

async def main():
    init_db()
    scheduler.add_job(check_prices_task, "interval", minutes=30)
    scheduler.start()
    await dp.start_polling(bot)

if __name__ == '__main__':
    asyncio.run(main())