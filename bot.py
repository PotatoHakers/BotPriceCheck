import asyncio
import sqlite3
import re
import time
import os
import random
import traceback
from datetime import datetime, timedelta
from dotenv import load_dotenv

import matplotlib
matplotlib.use('Agg') 
import matplotlib.pyplot as plt
import matplotlib.dates as mdates

import undetected_chromedriver as uc
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, FSInputFile, BotCommand
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# --- ЗАГРУЗКА НАСТРОЕК --- #
load_dotenv()
API_TOKEN = os.getenv("BOT_TOKEN")

if not API_TOKEN:
    exit("Ошибка: BOT_TOKEN не найден в файле .env! Проверь название файла и ключа.")

bot = Bot(token=API_TOKEN)
dp = Dispatcher()
scheduler = AsyncIOScheduler()

# Ограничения для стабильности на Windows
browser_semaphore = asyncio.Semaphore(5)
driver_lock = asyncio.Lock()

# --- БАЗА ДАННЫХ --- #
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
            item_id INTEGER, price REAL, timestamp TEXT
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
        driver = uc.Chrome(options=options)
        driver.get(url)
        time.sleep(10) # Озону нужно время
        html = driver.page_source
        
        # Поиск цены в мета-тегах или JSON
        price_match = re.search(r'<meta property="og:price:amount" content="([\d\.,]+)"', html)
        if price_match:
            return float(price_match.group(1).replace(',', '.'))
        
        json_prices = re.findall(r'"price":"?([\d\.,]+)"?', html)
        if json_prices:
            return float(json_prices[0].replace(',', '.'))
        return None
    except Exception as e:
        print(f"Ошибка парсинга: {e}")
        return None
    finally:
        if driver:
            try: driver.quit()
            except: pass

async def bounded_get_ozon_data(url):
    async with browser_semaphore:
        async with driver_lock: # Защита от WinError 183
            await asyncio.sleep(1.5)
            return await asyncio.get_event_loop().run_in_executor(None, get_ozon_data, url)

# --- ЛОГИКА ПРОВЕРКИ --- #
async def check_prices_task(report_id=None):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] Проверка запущена...")
    conn = sqlite3.connect("tracker.db")
    if report_id:
        items = conn.execute("SELECT id, user_id, url, target_price, name FROM items WHERE user_id = ?", (report_id,)).fetchall()
    else:
        items = conn.execute("SELECT id, user_id, url, target_price, name FROM items").fetchall()
    conn.close()

    if not items: return

    async def process_item(iid, uid, url, target, name):
        current = await bounded_get_ozon_data(url)
        if current:
            log_price(iid, current)
            c = sqlite3.connect("tracker.db")
            c.execute("UPDATE items SET last_price = ? WHERE id = ?", (current, iid))
            c.commit()
            c.close()
            if current <= target:
                try: await bot.send_message(uid, f"🎯 **ЦЕЛЬ!**\n📦 [{name}]({url})\n💰 Цена: **{current}**", parse_mode="Markdown")
                except: pass

    await asyncio.gather(*(process_item(*item) for item in items))
    if report_id: await send_list(report_id)

async def send_list(user_id):
    conn = sqlite3.connect("tracker.db")
    rows = conn.execute("SELECT id, name, target_price, last_price, url FROM items WHERE user_id = ?", (user_id,)).fetchall()
    conn.close()
    if not rows:
        await bot.send_message(user_id, "📭 Список пуст.")
        return
    text = "📋 **Ваши товары:**\n\n"
    for r in rows:
        text += f"🔹 `{r[0]}`. [{r[1]}]({r[4]})\n🎯 Цель: `{r[2]}` | 🕒 Сейчас: `{r[3]}`\n\n"
    await bot.send_message(user_id, text, parse_mode="Markdown", disable_web_page_preview=True)

# --- КОМАНДЫ БОТА --- #
@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    await message.answer("🤖 **Бот Ozon запущен!**\n\n/add [URL] [Цена] [Имя]\n/list — показать всё\n/del [ID] — удалить\n/check — обновить цены", parse_mode="Markdown")

@dp.message(Command("add"))
async def cmd_add(message: types.Message):
    try:
        parts = message.text.split(maxsplit=3)
        if len(parts) < 4:
            return await message.answer("⚠️ Формат: `/add [URL] [Цена] [Имя]`")
        
        url, target, name = parts[1], float(parts[2].replace(',', '.')), parts[3]
        msg = await message.answer(f"⏳ Проверяю товар '{name}'...")
        
        current = await bounded_get_ozon_data(url)
        if current:
            conn = sqlite3.connect("tracker.db")
            cursor = conn.cursor()
            cursor.execute("INSERT INTO items (user_id, url, name, target_price, last_price) VALUES (?, ?, ?, ?, ?)",
                         (message.from_user.id, url, name, target, current))
            new_id = cursor.lastrowid
            conn.commit()
            conn.close()
            log_price(new_id, current)
            await msg.edit_text(f"✅ Добавлено! ID товара: `{new_id}`", parse_mode="Markdown")
        else:
            await msg.edit_text("❌ Не удалось получить цену. Проверьте ссылку.")
    except Exception as e:
        await message.answer(f"⚠️ Ошибка: {e}")

@dp.message(Command("list"))
async def cmd_list(message: types.Message):
    await send_list(message.from_user.id)

@dp.message(Command("del"))
async def cmd_del(message: types.Message):
    try:
        iid = message.text.split()[1]
        conn = sqlite3.connect("tracker.db")
        cursor = conn.cursor()
        cursor.execute("DELETE FROM items WHERE id = ? AND user_id = ?", (iid, message.from_user.id))
        if cursor.rowcount > 0:
            await message.answer(f"🗑 Товар `{iid}` удален.")
        else:
            await message.answer("❓ Товар с таким ID не найден.")
        conn.commit()
        conn.close()
    except:
        await message.answer("⚠️ Используй: `/del [ID]`")

@dp.message(Command("check"))
async def cmd_check(message: types.Message):
    await message.answer("🔄 Начинаю обновление цен...")
    asyncio.create_task(check_prices_task(message.from_user.id))

# --- ЗАПУСК --- #
async def main():
    init_db()
    await bot.set_my_commands([
        BotCommand(command="start", description="Инфо"),
        BotCommand(command="add", description="Добавить товар"),
        BotCommand(command="list", description="Мой список"),
        BotCommand(command="check", description="Проверить цены"),
        BotCommand(command="del", description="Удалить по ID")
    ])
    scheduler.add_job(check_prices_task, "interval", minutes=30)
    scheduler.start()
    print("🚀 Бот успешно запущен!")
    await dp.start_polling(bot)

if __name__ == '__main__':
    try:
        asyncio.run(main())
    except:
        print(traceback.format_exc())