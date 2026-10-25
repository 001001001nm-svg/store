import asyncio
import base64
import html
import json
import logging
import mimetypes
import os
import re
import sqlite3
import time
from contextlib import closing
from datetime import datetime, timedelta, timezone

from aiohttp import web
from aiogram import BaseMiddleware, Bot, Dispatcher, F, types
from aiogram.filters import BaseFilter, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage

logging.basicConfig(level=logging.INFO)

# ============================================================
#  НАСТРОЙКИ (задаются в Render -> Environment)
#  BOT_TOKEN      - токен бота (обязательно)
#  SUPER_ADMIN_ID - Telegram ID первого владельца
#  SECOND_OWNER_ID - Telegram ID второго владельца
#  DB_PATH        - путь к базе (при Persistent Disk, напр. /data/store.db)
#  (телефоны и адреса магазина меняются в коде: SHOP_PHONES / SHOP_ADDRESSES)
# ============================================================
BOT_TOKEN = os.environ.get("BOT_TOKEN", "8708493966:AAH__oGZTbNXzP58vD0T3MycH3qYi385PrA")
SUPER_ADMIN_ID = int(os.environ.get("SUPER_ADMIN_ID", "8636623905"))
SECOND_OWNER_ID = int(os.environ.get("SECOND_OWNER_ID", "5312490946"))
DB_PATH = os.environ.get("DB_PATH", "store.db")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

BOT_ACTIVE = True
SITE_ACTIVE = True

def ensure_columns(conn, table, cols):
    existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    for col, typ in cols.items():
        if col not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")

def init_db():
    try:
        with closing(sqlite3.connect(DB_PATH)) as conn:
            with conn:
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS users (
                        user_id INTEGER PRIMARY KEY,
                        name TEXT,
                        username TEXT,
                        is_admin INTEGER DEFAULT 0
                    )
                """)
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS categories (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        name TEXT UNIQUE
                    )
                """)
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS products (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        name TEXT,
                        unit TEXT,
                        price REAL,
                        photo TEXT,
                        discount TEXT,
                        category TEXT,
                        description TEXT,
                        options TEXT,
                        stock INTEGER
                    )
                """)
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS advertisements (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        title TEXT,
                        content TEXT,
                        photo TEXT
                    )
                """)
                # на случай, если таблицы остались от старой версии
                ensure_columns(conn, "products", {"unit": "TEXT", "price": "REAL", "photo": "TEXT", "discount": "TEXT", "category": "TEXT", "description": "TEXT", "options": "TEXT", "stock": "INTEGER"})
                ensure_columns(conn, "advertisements", {"content": "TEXT", "photo": "TEXT"})
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS orders (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        name TEXT,
                        phone TEXT,
                        address TEXT,
                        items TEXT,
                        subtotal REAL,
                        delivery REAL,
                        total REAL,
                        status TEXT DEFAULT 'new',
                        created_at TEXT,
                        done_at TEXT
                    )
                """)
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS order_msgs (
                        order_id INTEGER,
                        chat_id INTEGER,
                        message_id INTEGER
                    )
                """)
    except Exception as e:
        logging.error(f"DB Error: {e}")

init_db()

def db_run(query, params=()):
    try:
        with closing(sqlite3.connect(DB_PATH)) as conn:
            with conn:
                cur = conn.execute(query, params)
                return cur.lastrowid
    except Exception as e:
        logging.error(f"DB run error: {e}")
        return None

def db_all(query, params=()):
    try:
        with closing(sqlite3.connect(DB_PATH)) as conn:
            return conn.execute(query, params).fetchall()
    except Exception as e:
        logging.error(f"DB read error: {e}")
        return []

# ---------- настройки в базе (владелец можно передать) ----------
db_run("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)")

def get_setting(key, default=None):
    rows = db_all("SELECT value FROM settings WHERE key = ?", (key,))
    return rows[0][0] if rows else default

def set_setting(key, value):
    db_run("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, str(value)))

db_run("CREATE TABLE IF NOT EXISTS owners (user_id INTEGER PRIMARY KEY)")

def owner_ids():
    return [r[0] for r in db_all("SELECT user_id FROM owners")]

def is_owner_id(uid):
    return bool(db_all("SELECT 1 FROM owners WHERE user_id = ?", (uid,)))

def init_owners():
    """Владельцы лежат в базе. Начальные владельцы добавляются один раз (или если таблица пуста)."""
    if get_setting("owners_seeded") == "1" and owner_ids():
        return
    try:
        first = int(get_setting("owner_id") or SUPER_ADMIN_ID)
    except (TypeError, ValueError):
        first = SUPER_ADMIN_ID
    for uid in (first, SECOND_OWNER_ID):
        db_run("INSERT OR IGNORE INTO owners (user_id) VALUES (?)", (uid,))
        db_run("INSERT OR IGNORE INTO users (user_id, name, username, is_admin) VALUES (?, ?, ?, 1)", (uid, "Владелец", "нет юзернейма"))
        db_run("UPDATE users SET is_admin = 1 WHERE user_id = ?", (uid,))
    set_setting("owners_seeded", "1")

init_owners()

def fmt_uname(username):
    return "нет юзернейма" if (not username or username == "нет юзернейма") else f"@{str(username).lstrip('@')}"

def user_label(uid):
    rows = db_all("SELECT name, username FROM users WHERE user_id = ?", (uid,))
    if rows:
        name, username = rows[0]
        return f"{name} ({fmt_uname(username)}) — ID {uid}"
    return f"ID {uid}"

def get_user(user_id):
    try:
        with closing(sqlite3.connect(DB_PATH)) as conn:
            cur = conn.cursor()
            cur.execute("SELECT name, is_admin FROM users WHERE user_id = ?", (user_id,))
            return cur.fetchone()
    except:
        return None

def register_user(user_id, name, username):
    try:
        is_admin_val = 1 if is_owner_id(user_id) else 0
        with closing(sqlite3.connect(DB_PATH)) as conn:
            with conn:
                conn.execute("""
                    INSERT OR REPLACE INTO users (user_id, name, username, is_admin) 
                    VALUES (?, ?, ?, COALESCE((SELECT is_admin FROM users WHERE user_id = ?), ?))
                """, (user_id, name, username, user_id, is_admin_val))
                if is_owner_id(user_id):
                    conn.execute("UPDATE users SET is_admin = 1 WHERE user_id = ?", (user_id,))
    except Exception as e:
        logging.error(f"Reg error: {e}")

def set_admin_status(user_id, status):
    try:
        with closing(sqlite3.connect(DB_PATH)) as conn:
            with conn:
                conn.execute("UPDATE users SET is_admin = ? WHERE user_id = ?", (status, user_id))
    except:
        pass

def can_manage(user_id):
    if is_owner_id(user_id):
        return True
    data = get_user(user_id)
    return bool(data and data[1] == 1)

class RegState(StatesGroup):
    waiting_for_name = State()

class OwnerState(StatesGroup):
    waiting_for_admin_input = State()
    waiting_for_transfer_input = State()

class ProductState(StatesGroup):
    waiting_for_category = State()
    waiting_for_name = State()
    waiting_for_unit = State()
    waiting_for_price = State()
    waiting_for_stock = State()
    waiting_for_description = State()
    waiting_for_photo = State()

class CategoryState(StatesGroup):
    waiting_for_name = State()

class DiscountState(StatesGroup):
    waiting_for_value = State()

class PriceState(StatesGroup):
    waiting_for_value = State()

class StockState(StatesGroup):
    waiting_for_value = State()

class AdState(StatesGroup):
    waiting_for_photo = State()

def main_menu_kb(is_owner, is_admin):
    kb = []
    if is_admin or is_owner:
        kb.append([types.KeyboardButton(text="📦 Управление товарами"), types.KeyboardButton(text="📁 Каталоги")])
        kb.append([types.KeyboardButton(text="🏷 Скидки"), types.KeyboardButton(text="📢 Реклама")])
        kb.append([types.KeyboardButton(text="📋 Актуальные заказы"), types.KeyboardButton(text="🗃 Архив заказов")])
    if is_owner:
        kb.append([types.KeyboardButton(text="👑 Панель владельца")])
    return types.ReplyKeyboardMarkup(keyboard=kb, resize_keyboard=True)

@dp.message(CommandStart())
async def cmd_start(message: types.Message, state: FSMContext):
    global BOT_ACTIVE
    if not BOT_ACTIVE and not is_owner_id(message.from_user.id):
        await message.answer("🛠 Бот временно выключен владельцем.")
        return

    user_id = message.from_user.id
    if is_owner_id(user_id):
        set_admin_status(user_id, 1)

    user_data = get_user(user_id)
    if user_data:
        if message.from_user.username:
            db_run("UPDATE users SET username = ? WHERE user_id = ?", (message.from_user.username, user_id))
        db_run("UPDATE users SET name = ? WHERE user_id = ? AND name IN ('Владелец', 'Админ')", (message.from_user.full_name, user_id))
        user_data = get_user(user_id)
    if not user_data:
        await message.answer("Привет! Как тебя зовут? Введи свое имя:")
        await state.set_state(RegState.waiting_for_name)
    else:
        name, is_admin = user_data
        is_owner = (is_owner_id(user_id))
        if not is_owner and is_admin != 1:
            await message.answer("Ожидайте назначения на администратора😇", reply_markup=types.ReplyKeyboardRemove())
            return
        kb = main_menu_kb(is_owner, is_admin == 1)
        await message.answer(f"Привет, {name}! Добро пожаловать.", reply_markup=kb)

async def notify_owner_new_user(user_id, name, username):
    """Владельцу приходит уведомление о новом пользователе с кнопкой «Назначить админом»."""
    uname = username if username == "нет юзернейма" else f"@{username}"
    kb = types.InlineKeyboardMarkup(inline_keyboard=[[
        types.InlineKeyboardButton(text="✅ Назначить админом", callback_data=f"mkadm:{user_id}")
    ]])
    for oid in owner_ids():
        try:
            await bot.send_message(
                oid,
                f"👤 Новый пользователь ждёт назначения:\n{name} ({uname}) — {user_id}",
                reply_markup=kb
            )
        except Exception as e:
            logging.warning(f"Не удалось уведомить владельца {oid} о пользователе {user_id}: {e}")

async def notify_new_admin(uid):
    """Сообщает пользователю, что его назначили админом. True — если сообщение дошло."""
    try:
        await bot.send_message(uid, "Вас назначили на администратора✅", reply_markup=main_menu_kb(False, True))
        return True
    except Exception as e:
        logging.warning(f"Не удалось уведомить нового админа {uid}: {e}")
        return False

@dp.message(RegState.waiting_for_name)
async def process_reg_name(message: types.Message, state: FSMContext):
    if not message.text:
        await message.answer("Введи своё имя текстом:")
        return
    user_id = message.from_user.id
    name = message.text.strip()
    username = message.from_user.username or "нет юзернейма"
    register_user(user_id, name, username)
    await state.clear()
    
    is_owner = (is_owner_id(user_id))
    user_data = get_user(user_id)
    is_admin = user_data[1] == 1 if user_data else 0
    if is_owner or is_admin:
        kb = main_menu_kb(is_owner, is_admin)
        await message.answer(f"Приятно познакомиться, {name}!", reply_markup=kb)
        return
    await message.answer("Ожидайте назначения на администратора😇", reply_markup=types.ReplyKeyboardRemove())
    await notify_owner_new_user(user_id, name, username)

@dp.message(F.text == "👑 Панель владельца")
async def owner_panel(message: types.Message, state: FSMContext = None):
    if not is_owner_id(message.from_user.id):
        return
    if state:
        await state.clear()
    kb = types.ReplyKeyboardMarkup(
        keyboard=[
            [types.KeyboardButton(text="🔴 Выключить бота" if BOT_ACTIVE else "🟢 Включить бота")],
            [types.KeyboardButton(text="🔴 Выключить сайт" if SITE_ACTIVE else "🟢 Включить сайт")],
            [types.KeyboardButton(text="➕ Назначить админа"), types.KeyboardButton(text="👥 Список админов")],
            [types.KeyboardButton(text="🔁 Передать права владельца")],
            [types.KeyboardButton(text="🔙 Главное меню")]
        ],
        resize_keyboard=True
    )
    status_bot = "Включен 🟢" if BOT_ACTIVE else "Выключен 🔴"
    status_site = "Включен 🟢" if SITE_ACTIVE else "Выключен 🔴"
    await message.answer(f"👑 Панель владельца:\n\nСтатус бота: {status_bot}\nСтатус сайта: {status_site}", reply_markup=kb)

@dp.message(F.text.in_(["🔴 Выключить бота", "🟢 Включить бота"]))
async def toggle_bot(message: types.Message):
    global BOT_ACTIVE
    if not is_owner_id(message.from_user.id):
        return
    BOT_ACTIVE = not BOT_ACTIVE
    await owner_panel(message)

@dp.message(F.text.in_(["🔴 Выключить сайт", "🟢 Включить сайт"]))
async def toggle_site(message: types.Message):
    global SITE_ACTIVE
    if not is_owner_id(message.from_user.id):
        return
    SITE_ACTIVE = not SITE_ACTIVE
    await owner_panel(message)

@dp.message(F.text == "🔙 Главное меню")
async def back_to_main(message: types.Message, state: FSMContext):
    await state.clear()
    user_id = message.from_user.id
    user_data = get_user(user_id)
    is_admin = user_data[1] == 1 if user_data else False
    kb = main_menu_kb(is_owner_id(user_id), is_admin)
    await message.answer("Главное меню:", reply_markup=kb)

# ============================================================
#  ВСПОМОГАТЕЛЬНОЕ ДЛЯ СПИСКОВ (с кнопками удаления)
# ============================================================
def build_list(title, items, prefix, empty_text):
    if not items:
        return empty_text, None
    items = items[:50]
    lines, buttons = [], []
    for n, (item_id, line, label) in enumerate(items, 1):
        lines.append(f"{n}. {line}")
        buttons.append([types.InlineKeyboardButton(text=f"❌ {label[:35]}", callback_data=f"{prefix}:{item_id}")])
    return title + "\n\n" + "\n".join(lines), types.InlineKeyboardMarkup(inline_keyboard=buttons)

async def refresh_inline(call: types.CallbackQuery, text, kb):
    try:
        await call.message.edit_text(text, reply_markup=kb)
    except Exception as e:
        logging.info(f"edit skipped: {e}")

def back_row_kb(extra_rows):
    rows = [list(r) for r in extra_rows]
    rows.append([types.KeyboardButton(text="🔙 Главное меню")])
    return types.ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)

# ============================================================
#  📦 УПРАВЛЕНИЕ ТОВАРАМИ
# ============================================================
def products_menu_kb():
    return back_row_kb([
        [types.KeyboardButton(text="➕ Добавить товар"), types.KeyboardButton(text="📋 Список товаров")],
        [types.KeyboardButton(text="💲 Сменить цену"), types.KeyboardButton(text="🔢 Изменить кол-во")],
        [types.KeyboardButton(text="✏️ Изменить инфо о товаре")],
        [types.KeyboardButton(text="🗑 Убрать все товары")],
    ])

def fmt_stock(value):
    return "—" if value is None else f"{int(value)} шт"

def products_view():
    rows = db_all("SELECT id, name, unit, price, discount, category, stock FROM products ORDER BY id")
    items = []
    for pid, name, unit, price, disc, cat, stock in rows:
        line = f"{name} — {price} сом/{unit or 'шт'}"
        if disc:
            line += f" (скидка {disc})"
        if cat:
            line += f" [{cat}]"
        line += f" | в наличии: {fmt_stock(stock)}"
        items.append((pid, line, str(name)))
    return build_list("📋 Товары (нажми ❌, чтобы удалить):", items, "delp", "Товаров пока нет.")

@dp.message(F.text == "📦 Управление товарами")
async def products_menu(message: types.Message, state: FSMContext):
    if not can_manage(message.from_user.id):
        return
    await state.clear()
    await message.answer("📦 Управление товарами:", reply_markup=products_menu_kb())

@dp.message(F.text == "📋 Список товаров")
async def products_list(message: types.Message, state: FSMContext):
    if not can_manage(message.from_user.id):
        return
    await state.clear()
    text, kb = products_view()
    await message.answer(text, reply_markup=kb)

@dp.callback_query(F.data.startswith("delp:"))
async def delete_product(call: types.CallbackQuery):
    if not can_manage(call.from_user.id):
        await call.answer("Нет доступа", show_alert=True)
        return
    db_run("DELETE FROM products WHERE id = ?", (int(call.data.split(":")[1]),))
    await call.answer("Товар удалён")
    text, kb = products_view()
    await refresh_inline(call, text, kb)

@dp.message(F.text == "🗑 Убрать все товары")
async def delete_all_products_ask(message: types.Message, state: FSMContext):
    if not can_manage(message.from_user.id):
        return
    await state.clear()
    kb = types.InlineKeyboardMarkup(inline_keyboard=[[
        types.InlineKeyboardButton(text="✅ Да, удалить все", callback_data="delall:yes"),
        types.InlineKeyboardButton(text="Отмена", callback_data="delall:no"),
    ]])
    await message.answer("Точно удалить ВСЕ товары?", reply_markup=kb)

@dp.callback_query(F.data.startswith("delall:"))
async def delete_all_products(call: types.CallbackQuery):
    if not can_manage(call.from_user.id):
        await call.answer("Нет доступа", show_alert=True)
        return
    if call.data.endswith("yes"):
        db_run("DELETE FROM products")
        await call.answer("Готово")
        await refresh_inline(call, "🗑 Все товары удалены.", None)
    else:
        await call.answer("Отменено")
        await refresh_inline(call, "Отменено.", None)

# ---------- смена цены ----------
@dp.message(F.text == "💲 Сменить цену")
async def change_price_menu(message: types.Message, state: FSMContext):
    if not can_manage(message.from_user.id):
        return
    await state.clear()
    rows = db_all("SELECT id, name, price FROM products ORDER BY id")
    if not rows:
        await message.answer("Товаров пока нет. Сначала добавь товары.")
        return
    chunks = [rows[i:i + 50] for i in range(0, len(rows), 50)]
    for n, chunk in enumerate(chunks):
        buttons = [
            [types.InlineKeyboardButton(text=f"{str(name)[:40]} — {fmt_price(price)} сом", callback_data=f"price:{pid}")]
            for pid, name, price in chunk
        ]
        title = "💲 Выбери товар, чтобы сменить цену:" if n == 0 else "…продолжение списка:"
        await message.answer(title, reply_markup=types.InlineKeyboardMarkup(inline_keyboard=buttons))

@dp.callback_query(F.data.startswith("price:"))
async def price_pick(call: types.CallbackQuery, state: FSMContext):
    if not can_manage(call.from_user.id):
        await call.answer("Нет доступа", show_alert=True)
        return
    pid = int(call.data.split(":")[1])
    rows = db_all("SELECT name, price FROM products WHERE id = ?", (pid,))
    if not rows:
        await call.answer("Товар не найден", show_alert=True)
        return
    name, price = rows[0]
    await state.set_state(PriceState.waiting_for_value)
    await state.update_data(product_id=pid)
    await call.answer()
    await call.message.answer(f"Введи новую цену для «{name}» (сейчас {fmt_price(price)} сом):")

@dp.message(PriceState.waiting_for_value, F.text)
async def price_value(message: types.Message, state: FSMContext):
    data = await state.get_data()
    try:
        price = float(message.text.replace(",", ".").replace(" ", ""))
        if price < 0:
            raise ValueError
    except ValueError:
        await message.answer("Введи цену числом, например 120 или 99.5")
        return
    pid = data.get("product_id")
    rows = db_all("SELECT name FROM products WHERE id = ?", (pid,))
    await state.clear()
    if not rows:
        await message.answer("Этот товар уже удалён.", reply_markup=products_menu_kb())
        return
    db_run("UPDATE products SET price = ? WHERE id = ?", (price, pid))
    await message.answer(
        f"✅ Цена товара «{rows[0][0]}» изменена на {fmt_price(price)} сом. Она уже на сайте.",
        reply_markup=products_menu_kb()
    )

# ---------- изменение количества (штук) ----------
@dp.message(F.text == "🔢 Изменить кол-во")
async def change_stock_menu(message: types.Message, state: FSMContext):
    if not can_manage(message.from_user.id):
        return
    await state.clear()
    rows = db_all("SELECT id, name, stock FROM products ORDER BY id")
    if not rows:
        await message.answer("Товаров пока нет. Сначала добавь товары.")
        return
    chunks = [rows[i:i + 50] for i in range(0, len(rows), 50)]
    for n, chunk in enumerate(chunks):
        buttons = [
            [
                types.InlineKeyboardButton(text=f"{str(name)[:25]} — {fmt_stock(stock)}", callback_data="noop"),
                types.InlineKeyboardButton(text="✏️ Изменить кол-во", callback_data=f"stock:{pid}"),
            ]
            for pid, name, stock in chunk
        ]
        title = "🔢 Выбери товар, чтобы изменить количество:" if n == 0 else "…продолжение списка:"
        await message.answer(title, reply_markup=types.InlineKeyboardMarkup(inline_keyboard=buttons))

@dp.callback_query(F.data == "noop")
async def noop_callback(call: types.CallbackQuery):
    await call.answer()

@dp.callback_query(F.data.startswith("stock:"))
async def stock_pick(call: types.CallbackQuery, state: FSMContext):
    if not can_manage(call.from_user.id):
        await call.answer("Нет доступа", show_alert=True)
        return
    pid = int(call.data.split(":")[1])
    rows = db_all("SELECT name, stock FROM products WHERE id = ?", (pid,))
    if not rows:
        await call.answer("Товар не найден", show_alert=True)
        return
    name, stock = rows[0]
    await state.set_state(StockState.waiting_for_value)
    await state.update_data(product_id=pid)
    await call.answer()
    await call.message.answer(f"Введи новое количество штук для «{name}» (сейчас {fmt_stock(stock)}):")

@dp.message(StockState.waiting_for_value, F.text)
async def stock_value(message: types.Message, state: FSMContext):
    data = await state.get_data()
    text = message.text.strip()
    if not text.isdigit() or int(text) > 1000000:
        await message.answer("Введи количество целым числом, например 25")
        return
    pid = data.get("product_id")
    rows = db_all("SELECT name FROM products WHERE id = ?", (pid,))
    await state.clear()
    if not rows:
        await message.answer("Этот товар уже удалён.", reply_markup=products_menu_kb())
        return
    db_run("UPDATE products SET stock = ? WHERE id = ?", (int(text), pid))
    await message.answer(f"✅ Количество «{rows[0][0]}» теперь {int(text)} шт.", reply_markup=products_menu_kb())

# ---------- добавление товара ----------
@dp.message(F.text == "➕ Добавить товар")
async def add_product_start(message: types.Message, state: FSMContext):
    if not can_manage(message.from_user.id):
        return
    await state.clear()
    cats = [r[0] for r in db_all("SELECT name FROM categories ORDER BY name")]
    rows = [[types.KeyboardButton(text=str(c))] for c in cats]
    rows.append([types.KeyboardButton(text="Без каталога")])
    await state.set_state(ProductState.waiting_for_category)
    await message.answer("Выбери каталог для товара:", reply_markup=back_row_kb(rows))

@dp.message(ProductState.waiting_for_category, F.text)
async def add_product_category(message: types.Message, state: FSMContext):
    cats = [r[0] for r in db_all("SELECT name FROM categories")]
    text = message.text.strip()
    if text != "Без каталога" and text not in cats:
        await message.answer("Выбери каталог кнопкой ниже.")
        return
    await state.update_data(category="" if text == "Без каталога" else text)
    await state.set_state(ProductState.waiting_for_name)
    await message.answer("Введи название товара:", reply_markup=back_row_kb([]))

@dp.message(ProductState.waiting_for_name, F.text)
async def add_product_name(message: types.Message, state: FSMContext):
    await state.update_data(name=message.text.strip())
    await state.set_state(ProductState.waiting_for_unit)
    await message.answer("Введи единицу измерения (например: шт, кг, л, упак):")

@dp.message(ProductState.waiting_for_unit, F.text)
async def add_product_unit(message: types.Message, state: FSMContext):
    await state.update_data(unit=message.text.strip())
    await state.set_state(ProductState.waiting_for_price)
    await message.answer("Введи цену (числом, например 120 или 99.5):")

@dp.message(ProductState.waiting_for_price, F.text)
async def add_product_price(message: types.Message, state: FSMContext):
    try:
        price = float(message.text.replace(",", ".").replace(" ", ""))
        if price < 0:
            raise ValueError
    except ValueError:
        await message.answer("Введи цену числом, например 120 или 99.5")
        return
    await state.update_data(price=price)
    await state.set_state(ProductState.waiting_for_stock)
    await message.answer("Сколько всего штук этого товара в наличии? (целое число, например 25)")

@dp.message(ProductState.waiting_for_stock, F.text)
async def add_product_stock(message: types.Message, state: FSMContext):
    text = message.text.strip()
    if not text.isdigit() or int(text) > 1000000:
        await message.answer("Введи количество штук целым числом, например 25")
        return
    await state.update_data(stock=int(text))
    await state.set_state(ProductState.waiting_for_description)
    await message.answer(
        "Введи описание товара (оно будет на сайте) или нажми «Пропустить»:",
        reply_markup=back_row_kb([[types.KeyboardButton(text="Пропустить")]])
    )

@dp.message(ProductState.waiting_for_stock)
async def add_product_stock_wrong(message: types.Message):
    await message.answer("Введи количество штук числом, например 25")

@dp.message(ProductState.waiting_for_description, F.text)
async def add_product_description(message: types.Message, state: FSMContext):
    text = message.text.strip()
    await state.update_data(description="" if text == "Пропустить" else text[:1000])
    await state.set_state(ProductState.waiting_for_photo)
    await message.answer(
        "Отправь фото товара или нажми «Пропустить»:",
        reply_markup=back_row_kb([[types.KeyboardButton(text="Пропустить")]])
    )

@dp.message(ProductState.waiting_for_description)
async def add_product_description_wrong(message: types.Message):
    await message.answer("Отправь описание текстом или нажми «Пропустить».")

async def save_product(message: types.Message, state: FSMContext, photo):
    data = await state.get_data()
    db_run(
        "INSERT INTO products (name, unit, price, photo, discount, category, description, options, stock) VALUES (?, ?, ?, ?, NULL, ?, ?, ?, ?)",
        (data.get("name"), data.get("unit"), data.get("price"), photo, data.get("category", ""),
         data.get("description") or None, None, data.get("stock"))
    )
    await state.clear()
    await message.answer(
        f"✅ Товар «{data.get('name')}» добавлен ({data.get('stock', 0)} шт в наличии)! Он уже на сайте.",
        reply_markup=products_menu_kb()
    )

@dp.message(ProductState.waiting_for_photo, F.photo)
async def add_product_photo(message: types.Message, state: FSMContext):
    await save_product(message, state, message.photo[-1].file_id)

@dp.message(ProductState.waiting_for_photo, F.text == "Пропустить")
async def add_product_skip_photo(message: types.Message, state: FSMContext):
    await save_product(message, state, None)

@dp.message(ProductState.waiting_for_photo)
async def add_product_photo_wrong(message: types.Message):
    await message.answer("Отправь фото или нажми «Пропустить».")

# ============================================================
#  📁 КАТАЛОГИ
# ============================================================
def catalogs_menu_kb():
    return back_row_kb([
        [types.KeyboardButton(text="➕ Добавить каталог"), types.KeyboardButton(text="📋 Список каталогов")],
    ])

def catalogs_view():
    rows = db_all("SELECT id, name FROM categories ORDER BY id")
    items = [(cid, str(name), str(name)) for cid, name in rows]
    return build_list("📁 Каталоги (нажми ❌, чтобы удалить):", items, "delc", "Каталогов пока нет.")

@dp.message(F.text == "📁 Каталоги")
async def catalogs_menu(message: types.Message, state: FSMContext):
    if not can_manage(message.from_user.id):
        return
    await state.clear()
    await message.answer("📁 Каталоги:", reply_markup=catalogs_menu_kb())

@dp.message(F.text == "📋 Список каталогов")
async def catalogs_list(message: types.Message, state: FSMContext):
    if not can_manage(message.from_user.id):
        return
    await state.clear()
    text, kb = catalogs_view()
    await message.answer(text, reply_markup=kb)

@dp.callback_query(F.data.startswith("delc:"))
async def delete_catalog(call: types.CallbackQuery):
    if not can_manage(call.from_user.id):
        await call.answer("Нет доступа", show_alert=True)
        return
    cid = int(call.data.split(":")[1])
    row = db_all("SELECT name FROM categories WHERE id = ?", (cid,))
    if row:
        db_run("UPDATE products SET category = '' WHERE category = ?", (row[0][0],))
    db_run("DELETE FROM categories WHERE id = ?", (cid,))
    await call.answer("Каталог удалён")
    text, kb = catalogs_view()
    await refresh_inline(call, text, kb)

@dp.message(F.text == "➕ Добавить каталог")
async def add_catalog_start(message: types.Message, state: FSMContext):
    if not can_manage(message.from_user.id):
        return
    await state.clear()
    await state.set_state(CategoryState.waiting_for_name)
    await message.answer("Введи название каталога (например: Молочка):", reply_markup=back_row_kb([]))

@dp.message(CategoryState.waiting_for_name, F.text)
async def add_catalog_name(message: types.Message, state: FSMContext):
    name = message.text.strip()
    db_run("INSERT OR IGNORE INTO categories (name) VALUES (?)", (name,))
    await state.clear()
    await message.answer(f"✅ Каталог «{name}» добавлен.", reply_markup=catalogs_menu_kb())

# ============================================================
#  🏷 СКИДКИ
# ============================================================
@dp.message(F.text == "🏷 Скидки")
async def discounts_menu(message: types.Message, state: FSMContext):
    if not can_manage(message.from_user.id):
        return
    await state.clear()
    rows = db_all("SELECT id, name, discount FROM products ORDER BY id LIMIT 50")
    if not rows:
        await message.answer("Сначала добавь товары.")
        return
    buttons = []
    for pid, name, disc in rows:
        label = f"{name} (-{disc})" if disc else str(name)
        buttons.append([types.InlineKeyboardButton(text=label[:45], callback_data=f"disc:{pid}")])
    await message.answer(
        "🏷 Выбери товар, чтобы поставить или убрать скидку:",
        reply_markup=types.InlineKeyboardMarkup(inline_keyboard=buttons)
    )

@dp.callback_query(F.data.startswith("disc:"))
async def discount_pick(call: types.CallbackQuery, state: FSMContext):
    if not can_manage(call.from_user.id):
        await call.answer("Нет доступа", show_alert=True)
        return
    pid = int(call.data.split(":")[1])
    await state.set_state(DiscountState.waiting_for_value)
    await state.update_data(product_id=pid)
    await call.answer()
    await call.message.answer("Введи скидку (например 10 или 10%). Чтобы убрать скидку — отправь 0:")

@dp.message(DiscountState.waiting_for_value, F.text)
async def discount_value(message: types.Message, state: FSMContext):
    data = await state.get_data()
    value = message.text.strip()
    if value in ("0", "-", "нет", "Нет"):
        db_run("UPDATE products SET discount = NULL WHERE id = ?", (data.get("product_id"),))
        await message.answer("✅ Скидка убрана.")
    else:
        if value.replace(".", "", 1).isdigit():
            value += "%"
        db_run("UPDATE products SET discount = ? WHERE id = ?", (value, data.get("product_id")))
        await message.answer(f"✅ Скидка {value} установлена.")
    await state.clear()

# ============================================================
#  📢 РЕКЛАМА
# ============================================================
def ads_menu_kb():
    return back_row_kb([
        [types.KeyboardButton(text="➕ Добавить рекламу"), types.KeyboardButton(text="📋 Список рекламы")],
    ])

def ads_view():
    rows = db_all("SELECT id, title, content, photo FROM advertisements ORDER BY id")
    items = []
    for aid, title, content, photo in rows:
        line = f"🖼 Реклама №{aid} (фото)" if photo else f"{title} — {content}"
        items.append((aid, line, f"Реклама №{aid}"))
    return build_list("📢 Реклама (нажми ❌, чтобы удалить):", items, "dela", "Рекламы пока нет.")

@dp.message(F.text == "📢 Реклама")
async def ads_menu(message: types.Message, state: FSMContext):
    if not can_manage(message.from_user.id):
        return
    await state.clear()
    await message.answer("📢 Реклама:", reply_markup=ads_menu_kb())

@dp.message(F.text == "📋 Список рекламы")
async def ads_list(message: types.Message, state: FSMContext):
    if not can_manage(message.from_user.id):
        return
    await state.clear()
    text, kb = ads_view()
    await message.answer(text, reply_markup=kb)

@dp.callback_query(F.data.startswith("dela:"))
async def delete_ad(call: types.CallbackQuery):
    if not can_manage(call.from_user.id):
        await call.answer("Нет доступа", show_alert=True)
        return
    db_run("DELETE FROM advertisements WHERE id = ?", (int(call.data.split(":")[1]),))
    await call.answer("Реклама удалена")
    text, kb = ads_view()
    await refresh_inline(call, text, kb)

@dp.message(F.text == "➕ Добавить рекламу")
async def add_ad_start(message: types.Message, state: FSMContext):
    if not can_manage(message.from_user.id):
        return
    await state.clear()
    await state.set_state(AdState.waiting_for_photo)
    await message.answer(
        "Отправь фото рекламы. Оно займёт всю рекламную панель на сайте.\n"
        "Лучше горизонтальное фото 16:9 (например, 1280×720).",
        reply_markup=back_row_kb([])
    )

async def save_ad(message: types.Message, state: FSMContext, file_id):
    db_run("INSERT INTO advertisements (title, content, photo) VALUES ('', '', ?)", (file_id,))
    await state.clear()
    await message.answer("✅ Реклама добавлена и уже на сайте.", reply_markup=ads_menu_kb())

@dp.message(AdState.waiting_for_photo, F.photo)
async def add_ad_photo(message: types.Message, state: FSMContext):
    await save_ad(message, state, message.photo[-1].file_id)

@dp.message(AdState.waiting_for_photo, F.document.mime_type.startswith("image/"))
async def add_ad_photo_file(message: types.Message, state: FSMContext):
    await save_ad(message, state, message.document.file_id)

@dp.message(AdState.waiting_for_photo)
async def add_ad_wrong(message: types.Message):
    await message.answer("Отправь именно фото рекламы.")

# ============================================================
#  👑 АДМИНЫ (панель владельца)
# ============================================================
def admins_view():
    rows = db_all("SELECT user_id, name, username FROM users WHERE is_admin = 1 AND user_id NOT IN (SELECT user_id FROM owners)")
    items = [(uid, f"{name} ({fmt_uname(username)}) — ID {uid}", str(name)) for uid, name, username in rows]
    return build_list("👥 Админы (нажми ❌, чтобы снять права):", items, "deladm", "Других админов пока нет.")

async def grant_admin(uid):
    """Выдаёт права админа. Если человека нет в базе — пробуем узнать его имя и юзернейм у Telegram."""
    if get_user(uid):
        set_admin_status(uid, 1)
        return
    name, username = "Админ", "нет юзернейма"
    try:
        chat = await bot.get_chat(uid)
        name = chat.full_name or chat.first_name or name
        username = chat.username or username
    except Exception as e:
        logging.info(f"get_chat {uid} skipped: {e}")
    db_run("INSERT OR IGNORE INTO users (user_id, name, username, is_admin) VALUES (?, ?, ?, 1)", (uid, name, username))

@dp.message(F.text == "➕ Назначить админа")
async def add_admin_start(message: types.Message, state: FSMContext):
    if not is_owner_id(message.from_user.id):
        return
    await state.set_state(OwnerState.waiting_for_admin_input)
    await message.answer("Введи Telegram ID нового админа (числом):")

@dp.message(OwnerState.waiting_for_admin_input, F.text)
async def add_admin_input(message: types.Message, state: FSMContext):
    if not is_owner_id(message.from_user.id):
        return
    text = message.text.strip()
    if not text.isdigit():
        await message.answer("ID должен быть числом. Попробуй ещё раз:")
        return
    uid = int(text)
    if is_owner_id(uid):
        await state.clear()
        await message.answer("Этот человек уже владелец — права админа ему не нужны.")
        return
    await grant_admin(uid)
    await state.clear()
    delivered = await notify_new_admin(uid)
    note = "" if delivered else "\n(Не удалось отправить ему уведомление — возможно, он ещё не запускал бота.)"
    await message.answer(f"✅ Назначен админом:\n👤 {user_label(uid)}{note}")

def participants_chunks():
    rows = db_all("SELECT user_id, name, username, is_admin FROM users ORDER BY is_admin DESC, name")
    lines = []
    for uid, name, username, adm in rows:
        role = "👑 владелец" if is_owner_id(uid) else ("🛡 админ" if adm == 1 else "👤 участник")
        lines.append(f"{name} ({fmt_uname(username)}) — ID {uid} · {role}")
    chunks, cur = [], "👥 Участники бота:\n"
    for line in lines:
        if len(cur) + len(line) > 3800:
            chunks.append(cur)
            cur = ""
        cur += "\n" + line
    if cur.strip():
        chunks.append(cur)
    return chunks, len(rows)

@dp.message(F.text == "🔁 Передать права владельца")
async def transfer_start(message: types.Message, state: FSMContext):
    if not is_owner_id(message.from_user.id):
        return
    await state.clear()
    chunks, total = participants_chunks()
    if total <= 1:
        await message.answer("Кроме тебя пока нет участников. Пусть человек сначала запустит бота (/start).")
        return
    for chunk in chunks:
        await message.answer(chunk)
    await state.set_state(OwnerState.waiting_for_transfer_input)
    await message.answer(
        "Напиши @юзернейм или ID того, кому передать права владельца.\n"
        "Ты станешь обычным администратором. Для отмены нажми «🔙 Главное меню»."
    )

@dp.message(OwnerState.waiting_for_transfer_input, F.text)
async def transfer_input(message: types.Message, state: FSMContext):
    if not is_owner_id(message.from_user.id):
        return
    text = message.text.strip()
    if text.isdigit():
        rows = db_all("SELECT user_id, name, username FROM users WHERE user_id = ?", (int(text),))
    else:
        uname = text.replace("https://t.me/", "").replace("t.me/", "").lstrip("@").strip().lower()
        rows = db_all("SELECT user_id, name, username FROM users WHERE LOWER(username) = ?", (uname,)) if uname else []
    if not rows:
        await message.answer("Такого участника нет в списке (он должен был запустить бота). Проверь юзернейм или ID и напиши ещё раз:")
        return
    uid, name, username = rows[0]
    if is_owner_id(uid):
        await message.answer("Этот участник уже владелец. Напиши другого:")
        return
    await state.clear()
    kb = types.InlineKeyboardMarkup(inline_keyboard=[[
        types.InlineKeyboardButton(text="✅ Да, передать", callback_data=f"xfer:yes:{uid}"),
        types.InlineKeyboardButton(text="Отмена", callback_data="xfer:no"),
    ]])
    await message.answer(
        f"Передать права владельца?\n👤 {name} ({fmt_uname(username)}) — ID {uid}\n\n"
        "Ты станешь обычным администратором, вернуть права сможет только новый владелец.",
        reply_markup=kb
    )

@dp.callback_query(F.data.startswith("xfer:"))
async def transfer_confirm(call: types.CallbackQuery):
    if not is_owner_id(call.from_user.id):
        await call.answer("Нет доступа", show_alert=True)
        return
    parts = call.data.split(":")
    if parts[1] != "yes":
        await call.answer("Отменено")
        await refresh_inline(call, "Передача прав отменена.", None)
        return
    new_id, old_id = int(parts[2]), call.from_user.id
    if new_id == old_id or is_owner_id(new_id) or not get_user(new_id):
        await call.answer("Нельзя передать права этому участнику", show_alert=True)
        return
    # передаётся только СВОЯ роль владельца: второй владелец не затрагивается
    db_run("DELETE FROM owners WHERE user_id = ?", (old_id,))
    db_run("INSERT OR IGNORE INTO owners (user_id) VALUES (?)", (new_id,))
    set_admin_status(new_id, 1)
    set_admin_status(old_id, 1)
    await call.answer("Готово")
    await refresh_inline(call, f"✅ Права владельца переданы:\n👤 {user_label(new_id)}\n\nТеперь ты администратор.", None)
    try:
        await bot.send_message(old_id, "Главное меню:", reply_markup=main_menu_kb(False, True))
    except Exception as e:
        logging.info(f"menu for old owner skipped: {e}")
    try:
        await bot.send_message(new_id, f"👑 Тебе переданы права владельца!\nПередал: {user_label(old_id)}", reply_markup=main_menu_kb(True, True))
    except Exception as e:
        logging.warning(f"Не удалось уведомить нового владельца {new_id}: {e}")
    for oid in owner_ids():
        if oid not in (new_id, old_id):
            try:
                await bot.send_message(oid, f"ℹ️ Владелец передал свои права:\n{user_label(old_id)} → {user_label(new_id)}")
            except Exception as e:
                logging.info(f"notify owner {oid} skipped: {e}")

@dp.callback_query(F.data.startswith("mkadm:"))
async def approve_admin(call: types.CallbackQuery):
    if not is_owner_id(call.from_user.id):
        await call.answer("Нет доступа", show_alert=True)
        return
    uid = int(call.data.split(":")[1])
    if is_owner_id(uid):
        await call.answer("Это владелец", show_alert=True)
        return
    await grant_admin(uid)
    delivered = await notify_new_admin(uid)
    await call.answer("Назначен")
    note = "" if delivered else "\n(Не удалось отправить ему уведомление.)"
    await refresh_inline(call, f"✅ Назначен админом:\n👤 {user_label(uid)}{note}", None)

@dp.message(F.text == "👥 Список админов")
async def admins_list(message: types.Message, state: FSMContext):
    if not is_owner_id(message.from_user.id):
        return
    await state.clear()
    text, kb = admins_view()
    await message.answer(text, reply_markup=kb)

@dp.callback_query(F.data.startswith("deladm:"))
async def delete_admin(call: types.CallbackQuery):
    if not is_owner_id(call.from_user.id):
        await call.answer("Нет доступа", show_alert=True)
        return
    uid = int(call.data.split(":")[1])
    if is_owner_id(uid):
        await call.answer("Это владелец — его права снять нельзя", show_alert=True)
        return
    label = user_label(uid)
    set_admin_status(uid, 0)
    await call.answer("Права сняты")
    text, kb = admins_view()
    await refresh_inline(call, text, kb)
    await call.message.answer(f"🚫 Права администратора сняты:\n👤 {label}")
    try:
        await bot.send_message(uid, "Ваши права администратора сняты.", reply_markup=types.ReplyKeyboardRemove())
    except Exception as e:
        logging.info(f"notify removed admin {uid} skipped: {e}")

# ============================================================
#  🛒 ЗАКАЗЫ (приходят с сайта, управление через бота)
# ============================================================
DELIVERY_FEE = 0  # доставка и установка бесплатно
LOCAL_TZ = timezone(timedelta(hours=6))

def now_local():
    return datetime.now(LOCAL_TZ)

def order_text(row):
    oid, name, phone, address, items, delivery, total, created = row
    try:
        dt = datetime.strptime(created, "%Y-%m-%d %H:%M:%S").strftime("%d.%m.%Y %H:%M")
    except Exception:
        dt = created
    return (
        f"🛒 Заказ №{oid}\n🕒 {dt}\n\n"
        f"👤 {name}\n📞 {phone}\n📍 {address}\n\n"
        f"{items}\n\n"
        "🚚 Доставка и установка: бесплатно\n"
        f"💰 Итого: {fmt_price(total)} сом"
    )

def done_kb(order_id):
    return types.InlineKeyboardMarkup(inline_keyboard=[[
        types.InlineKeyboardButton(text="✅ Заказ выполнен", callback_data=f"done:{order_id}")
    ]])

ORDER_FIELDS = "id, name, phone, address, items, delivery, total, created_at"

def track_order_msg(order_id, chat_id, message_id):
    db_run("INSERT INTO order_msgs (order_id, chat_id, message_id) VALUES (?, ?, ?)", (order_id, chat_id, message_id))

async def notify_admins(order_id):
    rows = db_all(f"SELECT {ORDER_FIELDS} FROM orders WHERE id = ?", (order_id,))
    if not rows:
        return
    text = order_text(rows[0])
    ids = set(owner_ids()) | {r[0] for r in db_all("SELECT user_id FROM users WHERE is_admin = 1")}
    for chat_id in ids:
        try:
            sent = await bot.send_message(chat_id, text, reply_markup=done_kb(order_id))
            track_order_msg(order_id, chat_id, sent.message_id)
        except Exception as e:
            logging.warning(f"Не удалось отправить заказ {order_id} в чат {chat_id}: {e}")

@dp.message(F.text == "📋 Актуальные заказы")
async def active_orders(message: types.Message, state: FSMContext):
    if not can_manage(message.from_user.id):
        return
    await state.clear()
    rows = db_all(f"SELECT {ORDER_FIELDS} FROM orders WHERE status = 'new' ORDER BY id")
    if not rows:
        await message.answer("Актуальных заказов нет.")
        return
    for row in rows:
        sent = await message.answer(order_text(row), reply_markup=done_kb(row[0]))
        track_order_msg(row[0], message.chat.id, sent.message_id)
        await asyncio.sleep(0.05)

@dp.callback_query(F.data.startswith("done:"))
async def order_done(call: types.CallbackQuery):
    if not can_manage(call.from_user.id):
        await call.answer("Нет доступа", show_alert=True)
        return
    order_id = int(call.data.split(":")[1])
    db_run(
        "UPDATE orders SET status = 'done', done_at = ? WHERE id = ? AND status = 'new'",
        (now_local().strftime("%Y-%m-%d %H:%M:%S"), order_id)
    )
    msgs = db_all("SELECT chat_id, message_id FROM order_msgs WHERE order_id = ?", (order_id,))
    deleted_current = False
    for chat_id, msg_id in msgs:
        try:
            await bot.delete_message(chat_id, msg_id)
            if call.message and chat_id == call.message.chat.id and msg_id == call.message.message_id:
                deleted_current = True
        except Exception as e:
            logging.info(f"delete skipped: {e}")
    db_run("DELETE FROM order_msgs WHERE order_id = ?", (order_id,))
    if not deleted_current and call.message:
        try:
            await call.message.delete()
        except Exception:
            try:
                await call.message.edit_text(f"✅ Заказ №{order_id} выполнен и перенесён в архив.")
            except Exception:
                pass
    await call.answer("Заказ перенесён в архив")

def archive_chunks(rows):
    chunks, current, last_date = [], "🗃 Архив заказов (последние 3 дня)\n", None
    for oid, name, phone, address, items, delivery, total, created in rows:
        day = created[:10]
        block = ""
        if day != last_date:
            try:
                pretty = datetime.strptime(day, "%Y-%m-%d").strftime("%d.%m.%Y")
            except Exception:
                pretty = day
            block += f"\n📅 {pretty}\n"
            last_date = day
        block += f"\n№{oid} · {created[11:16]}\n👤 {name} · 📞 {phone}\n📍 {address}\n{items}\n💰 {fmt_price(total)} сом\n"
        if len(current) + len(block) > 3800:
            chunks.append(current)
            current, last_date = block, None
        else:
            current += block
    if current:
        chunks.append(current)
    return chunks

@dp.message(F.text == "🗃 Архив заказов")
async def archive_orders(message: types.Message, state: FSMContext):
    if not can_manage(message.from_user.id):
        return
    await state.clear()
    cutoff = (now_local() - timedelta(days=3)).strftime("%Y-%m-%d %H:%M:%S")
    db_run("DELETE FROM orders WHERE status = 'done' AND created_at < ?", (cutoff,))
    rows = db_all(f"SELECT {ORDER_FIELDS} FROM orders WHERE status = 'done' ORDER BY created_at ASC, id ASC")
    if not rows:
        await message.answer("Архив пуст. Здесь хранятся выполненные заказы за последние 3 дня.")
        return
    for chunk in archive_chunks(rows):
        await message.answer(chunk)

# ============================================================
#  ✏️ ИЗМЕНИТЬ ИНФОРМАЦИЮ О ТОВАРЕ
# ============================================================
class EditState(StatesGroup):
    waiting_for_value = State()

EDIT_PROMPTS = {
    "name": "Введи новое название товара:",
    "price": "Введи новую цену (числом, например 120 или 99.5):",
    "unit": "Введи новую единицу измерения (например: шт, кг, л, упак):",
    "stock": "Введи количество штук в наличии (целым числом):",
    "desc": "Отправь новое описание товара. Чтобы удалить описание — отправь «-»:",
    "photo": "Отправь новое фото товара:",
}
EDIT_COLUMNS = {"name": "name", "price": "price", "unit": "unit", "stock": "stock", "desc": "description", "photo": "photo"}

def product_card_text(pid):
    rows = db_all("SELECT name, unit, price, discount, category, description, stock, photo FROM products WHERE id = ?", (pid,))
    if not rows:
        return None
    name, unit, price, disc, cat, desc, stock, photo = rows[0]
    d = (desc or "").strip()
    if len(d) > 300:
        d = d[:300] + "…"
    price_line = f"💲 Цена: {fmt_price(price)} сом / {unit or 'шт'}"
    if disc:
        price_line += f" (скидка {disc})"
    return (
        f"✏️ Товар №{pid}\n\n"
        f"📦 Название: {name}\n"
        f"{price_line}\n"
        f"🔢 В наличии: {fmt_stock(stock)}\n"
        f"📁 Каталог: {cat or 'без каталога'}\n"
        f"🖼 Фото: {'есть' if photo else 'нет'}\n"
        f"📝 Описание: {d or 'нет'}\n\n"
        "Что изменить?"
    )

def edit_menu_kb(pid):
    def b(text, data):
        return types.InlineKeyboardButton(text=text, callback_data=data)
    return types.InlineKeyboardMarkup(inline_keyboard=[
        [b("✏️ Название", f"ef:name:{pid}"), b("💲 Цена", f"ef:price:{pid}")],
        [b("📏 Ед. измерения", f"ef:unit:{pid}"), b("🔢 Кол-во", f"ef:stock:{pid}")],
        [b("📝 Описание", f"ef:desc:{pid}"), b("🖼 Фото", f"ef:photo:{pid}")],
        [b("📁 Каталог", f"ecatmenu:{pid}")],
        [b("🔙 К списку товаров", "elist")],
    ])

async def send_edit_list(target):
    rows = db_all("SELECT id, name FROM products ORDER BY id")
    if not rows:
        await target.answer("Товаров пока нет.")
        return
    chunks = [rows[i:i + 50] for i in range(0, len(rows), 50)]
    for n, chunk in enumerate(chunks):
        buttons = [[types.InlineKeyboardButton(text=str(name)[:45], callback_data=f"epick:{pid}")] for pid, name in chunk]
        title = "✏️ Выбери товар, информацию о котором нужно изменить:" if n == 0 else "…продолжение списка:"
        await target.answer(title, reply_markup=types.InlineKeyboardMarkup(inline_keyboard=buttons))

@dp.message(F.text == "✏️ Изменить инфо о товаре")
async def edit_menu(message: types.Message, state: FSMContext):
    if not can_manage(message.from_user.id):
        return
    await state.clear()
    await send_edit_list(message)

@dp.callback_query(F.data == "elist")
async def edit_back_to_list(call: types.CallbackQuery, state: FSMContext):
    if not can_manage(call.from_user.id):
        await call.answer("Нет доступа", show_alert=True)
        return
    await state.clear()
    await call.answer()
    await send_edit_list(call.message)

@dp.callback_query(F.data.startswith("epick:"))
async def edit_pick(call: types.CallbackQuery, state: FSMContext):
    if not can_manage(call.from_user.id):
        await call.answer("Нет доступа", show_alert=True)
        return
    pid = int(call.data.split(":")[1])
    text = product_card_text(pid)
    if not text:
        await call.answer("Товар не найден", show_alert=True)
        return
    await state.clear()
    await call.answer()
    await call.message.answer(text, reply_markup=edit_menu_kb(pid))

@dp.callback_query(F.data.startswith("ef:"))
async def edit_field_pick(call: types.CallbackQuery, state: FSMContext):
    if not can_manage(call.from_user.id):
        await call.answer("Нет доступа", show_alert=True)
        return
    _, field, pid = call.data.split(":")
    pid = int(pid)
    if field not in EDIT_PROMPTS or not db_all("SELECT 1 FROM products WHERE id = ?", (pid,)):
        await call.answer("Товар не найден", show_alert=True)
        return
    await state.set_state(EditState.waiting_for_value)
    await state.update_data(product_id=pid, field=field)
    await call.answer()
    await call.message.answer(EDIT_PROMPTS[field], reply_markup=back_row_kb([]))

@dp.callback_query(F.data.startswith("ecatmenu:"))
async def edit_cat_menu(call: types.CallbackQuery):
    if not can_manage(call.from_user.id):
        await call.answer("Нет доступа", show_alert=True)
        return
    pid = int(call.data.split(":")[1])
    cats = db_all("SELECT id, name FROM categories ORDER BY name")
    buttons = [[types.InlineKeyboardButton(text=str(n)[:40], callback_data=f"ecat:{pid}:{cid}")] for cid, n in cats]
    buttons.append([types.InlineKeyboardButton(text="Без каталога", callback_data=f"ecat:{pid}:0")])
    buttons.append([types.InlineKeyboardButton(text="🔙 Назад", callback_data=f"epick:{pid}")])
    await call.answer()
    await call.message.answer("📁 Выбери каталог для товара:", reply_markup=types.InlineKeyboardMarkup(inline_keyboard=buttons))

@dp.callback_query(F.data.startswith("ecat:"))
async def edit_cat_set(call: types.CallbackQuery):
    if not can_manage(call.from_user.id):
        await call.answer("Нет доступа", show_alert=True)
        return
    _, pid, cid = call.data.split(":")
    pid, cid = int(pid), int(cid)
    name = ""
    if cid:
        rows = db_all("SELECT name FROM categories WHERE id = ?", (cid,))
        if not rows:
            await call.answer("Каталог не найден", show_alert=True)
            return
        name = rows[0][0]
    if not db_all("SELECT 1 FROM products WHERE id = ?", (pid,)):
        await call.answer("Товар не найден", show_alert=True)
        return
    db_run("UPDATE products SET category = ? WHERE id = ?", (name, pid))
    await call.answer("Каталог изменён")
    await refresh_inline(call, "✅ Каталог изменён.", None)
    await call.message.answer(product_card_text(pid), reply_markup=edit_menu_kb(pid))

async def apply_edit(message: types.Message, state: FSMContext, field, value):
    data = await state.get_data()
    pid = data.get("product_id")
    await state.clear()
    if not db_all("SELECT 1 FROM products WHERE id = ?", (pid,)):
        await message.answer("Этот товар уже удалён.", reply_markup=products_menu_kb())
        return
    db_run(f"UPDATE products SET {EDIT_COLUMNS[field]} = ? WHERE id = ?", (value, pid))
    await message.answer("✅ Изменено. Уже на сайте.", reply_markup=products_menu_kb())
    await message.answer(product_card_text(pid), reply_markup=edit_menu_kb(pid))

@dp.message(EditState.waiting_for_value, F.photo)
async def edit_value_photo(message: types.Message, state: FSMContext):
    data = await state.get_data()
    field = data.get("field")
    if field != "photo":
        await message.answer("Сейчас нужен текст, а не фото. " + EDIT_PROMPTS.get(field, ""))
        return
    await apply_edit(message, state, "photo", message.photo[-1].file_id)

@dp.message(EditState.waiting_for_value, F.text)
async def edit_value_text(message: types.Message, state: FSMContext):
    data = await state.get_data()
    field = data.get("field")
    text = message.text.strip()
    if field == "photo":
        await message.answer("Отправь именно фото (не текст).")
        return
    if field == "name":
        if not text:
            await message.answer("Название не может быть пустым. " + EDIT_PROMPTS["name"])
            return
        value = text[:200]
    elif field == "unit":
        if not text:
            await message.answer(EDIT_PROMPTS["unit"])
            return
        value = text[:20]
    elif field == "price":
        try:
            value = float(text.replace(",", ".").replace(" ", ""))
            if value < 0:
                raise ValueError
        except ValueError:
            await message.answer("Введи цену числом, например 120 или 99.5")
            return
    elif field == "stock":
        if not text.isdigit() or int(text) > 1000000:
            await message.answer("Введи количество целым числом, например 25")
            return
        value = int(text)
    elif field == "desc":
        value = None if text == "-" else text[:1000]
    else:
        await state.clear()
        return
    await apply_edit(message, state, field, value)

# ============================================================
#  ВЕБ-СЕРВЕР И САЙТ «ШВЕЙМАГ КЫРГЫЗСТАНА»
# ============================================================
def esc(value):
    return html.escape("" if value is None else str(value))

def fmt_price(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        number = 0.0
    return str(int(number)) if number == int(number) else str(number)

def js_str(value):
    return html.escape(json.dumps("" if value is None else str(value), ensure_ascii=False), quote=True)

# ------------------------------------------------------------
#  КОНТАКТЫ МАГАЗИНА (меняются здесь)
# ------------------------------------------------------------
SHOP_NAME = "Швеймаг Кыргызстана"
SHOP_PHONES = [
    {"label": "+996 556 585 451", "tel": "+996556585451", "wa": "996556585451"},
    {"label": "+996 708 393 975", "tel": "+996708393975", "wa": "996708393975"},
    {"label": "+996 228 878 848", "tel": "+996228878848", "wa": ""},  # только звонки (без WhatsApp)
]
SHOP_ADDRESSES = [
    {"city": "Бишкек шаары", "addr": "Елебесова 280г/1"},
    {"city": "Манас шаары", "addr": "Чехов 2/2 (Бишкек аялдамасынын жанында)"},
    {"city": "Ош шаары", "addr": "ХБК Касымбекова 8г/1 МК Гранд Соода борбору"},
]

PAGE = r"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, viewport-fit=cover">
<meta name="theme-color" content="#0a5fa3">
<title>__NAME__ — швейные машины, оверлоки и аксессуары</title>
<link rel="icon" href="data:image/svg+xml,<svg xmlns=%22http://www.w3.org/2000/svg%22 viewBox=%220 0 100 100%22><text y=%22.9em%22 font-size=%2290%22>🧵</text></svg>">
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap" rel="stylesheet">
<style>
:root{--sky-50:#f0f9ff;--sky-100:#dff2ff;--sky-200:#bde3ff;--sky-300:#8fd0ff;--sky-400:#4db8ff;--sky-500:#1a9bf0;--sky-600:#0b7ccf;--sky-700:#0a5fa3;--navy:#0b3257;--ink:#0f2a43;--muted:#5d7a94;--line:#d3e8f7;--coral:#ff6b4a;--coral-d:#e8512f;--wa:#25d366}
*{box-sizing:border-box;-webkit-tap-highlight-color:transparent}
html{scroll-behavior:smooth}
body{margin:0;font-family:'Inter',system-ui,sans-serif;color:var(--ink);background:radial-gradient(#d4eafa 1.2px,transparent 1.2px) 0 0/22px 22px,#f1f9ff;padding-bottom:84px;-webkit-text-size-adjust:100%}
a{color:inherit}
button{font-family:inherit}
.muted{color:var(--muted)}

/* ШАПКА */
.hdr{position:sticky;top:0;z-index:100;background:rgba(255,255,255,.96);backdrop-filter:blur(8px);border-bottom:1px solid var(--line);padding-top:env(safe-area-inset-top)}
.hdr-in{max-width:1200px;margin:0 auto;padding:10px 18px;display:flex;align-items:center;gap:14px}
.brand{display:flex;align-items:center;gap:10px;font-weight:800;font-size:clamp(15px,4.8vw,20px);color:var(--navy);cursor:pointer;text-decoration:none}
.brand .logo{width:42px;height:42px;border-radius:14px;background:linear-gradient(135deg,var(--sky-400),var(--sky-700));display:grid;place-items:center;font-size:22px;box-shadow:0 4px 10px rgba(10,95,163,.35)}
.brand .logo span{display:inline-block;animation:wobble 3.2s ease-in-out infinite}
@keyframes wobble{0%,100%{transform:rotate(-14deg)}50%{transform:rotate(14deg)}}
.burger{margin-left:auto;width:50px;height:46px;border:1.5px solid var(--line);background:#fff;border-radius:14px;font-size:22px;color:var(--navy);cursor:pointer}
.desk-nav{display:none;margin-left:auto;gap:6px;align-items:center}
.desk-nav a{padding:9px 14px;border-radius:12px;font-weight:600;color:var(--navy);cursor:pointer;text-decoration:none;position:relative}
.desk-nav a:hover,.desk-nav a.on{background:var(--sky-100);color:var(--sky-700)}
.cnt{background:var(--coral);color:#fff;border-radius:10px;font-size:11px;font-weight:700;padding:1px 6px;margin-left:4px}
.cnt:empty,.cnt.zero{display:none}
.stitch{position:absolute;left:0;bottom:-2px;height:3px;width:0;background:repeating-linear-gradient(90deg,var(--coral) 0 9px,transparent 9px 15px);transition:width .1s}

.ov{position:fixed;inset:0;background:rgba(11,50,87,.5);opacity:0;pointer-events:none;transition:.25s;z-index:900}
.ov.on{opacity:1;pointer-events:auto}
.drawer{position:fixed;top:0;right:0;height:100%;width:min(86vw,340px);background:#fff;z-index:901;transform:translateX(100%);transition:transform .28s;padding:78px 22px 22px;overflow:auto}
.drawer.on{transform:none}
.drawer .x{position:absolute;top:16px;right:16px;width:44px;height:44px;border-radius:12px;border:1.5px solid var(--line);background:#fff;font-size:20px;cursor:pointer}
.drawer h6{margin:0 0 10px;font-size:13px;letter-spacing:1.2px;color:var(--muted)}
.drawer a.dl{display:flex;align-items:center;gap:14px;padding:14px 4px;font-size:18px;font-weight:600;color:var(--navy);text-decoration:none;cursor:pointer}
.drawer a.dl i{font-style:normal;width:28px;text-align:center}
.drawer .phones a{display:block;text-align:center;font-weight:800;font-size:18px;color:var(--navy);padding:8px;text-decoration:none}

.view{display:none}
.view.on{display:block}

/* КНОПКИ */
.btn{display:inline-flex;align-items:center;justify-content:center;gap:8px;border:0;border-radius:999px;padding:15px 24px;font-weight:700;font-size:16px;cursor:pointer;text-decoration:none;transition:.2s;font-family:inherit}
.btn:hover{transform:translateY(-2px)}
.btn-primary{background:var(--coral);color:#fff;box-shadow:0 8px 18px rgba(255,107,74,.35)}
.btn-primary:hover{background:var(--coral-d)}
.btn-ghost{background:transparent;color:#fff;border:2px solid rgba(255,255,255,.6)}
.btn-sky{background:var(--sky-600);color:#fff}
.btn-wa{background:var(--wa);color:#fff}
.btn-line{background:#fff;color:var(--navy);border:1.5px solid var(--line)}
.btn-block{width:100%}

/* ГЕРОЙ */
.hero{position:relative;background:linear-gradient(165deg,#0a5fa3 0%,#1a9bf0 58%,#86d3ff 100%);color:#fff;overflow:hidden}
.cloud{position:absolute;width:150px;height:44px;border-radius:50px;background:rgba(255,255,255,.3);animation:drift 46s linear infinite}
.cloud::before,.cloud::after{content:'';position:absolute;background:inherit;border-radius:50%}
.cloud::before{width:62px;height:62px;top:-28px;left:22px}
.cloud::after{width:48px;height:48px;top:-18px;left:70px}
.cloud.c1{top:36px;left:-180px}
.cloud.c2{top:210px;left:-180px;animation-duration:70s;animation-delay:-30s;transform:scale(.7)}
@keyframes drift{to{transform:translateX(calc(100vw + 360px))}}
.hero-in{position:relative;z-index:2;max-width:1200px;margin:0 auto;padding:34px 18px 28px;display:grid;gap:22px}
.hero h1{font-size:clamp(30px,6.5vw,48px);line-height:1.1;margin:0 0 12px;font-weight:800}
.hero .lead{font-size:17px;line-height:1.5;opacity:.96;margin:0 0 20px}
.hero-btns{display:flex;flex-direction:column;gap:12px;max-width:440px}
.consult{display:flex;flex-wrap:wrap;gap:6px 14px;align-items:center;margin-top:18px;font-size:17px}
.consult a{font-weight:800;text-decoration:none}
@media(min-width:900px){.hero-in{grid-template-columns:1.1fr .9fr;align-items:center;padding:60px 18px 36px}.hero-ads{grid-column:1/-1}.hero-btns{flex-direction:row;max-width:none}}

/* ВИТРИНА С ФОТО МАШИНКИ */
.showcase{position:relative;width:100%;max-width:420px;margin:0 auto;background:#fff;border-radius:26px;padding:14px;box-shadow:0 18px 40px rgba(5,40,80,.35);border:2px dashed #bfe0f5;outline:4px solid rgba(255,255,255,.55);animation:floaty 5s ease-in-out infinite}
.showcase img{display:block;width:100%;height:auto;border-radius:16px}
@keyframes floaty{0%,100%{transform:translateY(0)}50%{transform:translateY(-8px)}}
@media(prefers-reduced-motion:reduce){.showcase,.cloud,.brand .logo span{animation:none}}

/* РЕКЛАМА (карусель) */
.ads{position:relative;border-radius:24px;overflow:hidden;background:#fff;padding:8px;box-shadow:0 12px 28px rgba(5,40,80,.25);touch-action:pan-y}
.ad-track{display:flex;transition:transform .5s ease;border-radius:18px}
.slide{flex:0 0 100%;min-width:100%;border-radius:18px;color:#fff;position:relative;overflow:hidden}
.slide-in{padding:26px 26px 30px;min-height:176px;position:relative;z-index:2;display:flex;flex-direction:column;justify-content:center}
.slide::after{content:'';position:absolute;inset:8px;border:2px dashed rgba(255,255,255,.55);border-radius:13px;pointer-events:none}
.slide::before{content:'🧵';position:absolute;right:16px;bottom:-6px;font-size:84px;opacity:.2;transform:rotate(14deg)}
.slide.photo{aspect-ratio:16/9;background:#0b3257}
.slide.photo::before{content:'';position:absolute;inset:-24px;background:var(--img) center/cover;filter:blur(20px) brightness(.92);opacity:1;transform:none}
.slide.photo::after{display:none}
.slide.photo img{position:absolute;inset:0;z-index:2;width:100%;height:100%;object-fit:contain;display:block}
.g0{background:linear-gradient(135deg,#0a5fa3,#1a9bf0)}
.g1{background:linear-gradient(135deg,#ff6b4a,#ffa24c)}
.g2{background:linear-gradient(135deg,#0b3257,#0b7ccf)}
.g3{background:linear-gradient(135deg,#119a8a,#4db8ff)}
.slide-tag{display:inline-block;align-self:flex-start;background:rgba(255,255,255,.22);padding:4px 12px;border-radius:20px;font-size:12px;font-weight:700;margin-bottom:10px}
.slide h3{margin:0 0 8px;font-size:24px;line-height:1.2}
.slide p{margin:0;font-size:16px;line-height:1.5;opacity:.97;max-width:620px}
.ad-arrow{position:absolute;top:50%;transform:translateY(-50%);width:44px;height:44px;border-radius:50%;border:0;background:rgba(255,255,255,.92);font-size:22px;cursor:pointer;z-index:5;color:var(--navy);box-shadow:0 4px 12px rgba(0,0,0,.2)}
.ad-arrow.l{left:14px}.ad-arrow.r{right:14px}
.dots{position:absolute;left:0;right:0;bottom:16px;display:flex;justify-content:center;gap:7px;z-index:5}
.dots i{width:8px;height:8px;border-radius:50%;background:rgba(255,255,255,.55);cursor:pointer;transition:.2s}
.dots i.on{background:#fff;width:22px;border-radius:8px}

/* СЕКЦИИ */
.sec{max-width:1200px;margin:0 auto;padding:34px 18px 0}
.sec-head{display:flex;flex-wrap:wrap;justify-content:space-between;align-items:flex-end;gap:6px 14px;margin-bottom:18px}
.sec-head h2{font-size:28px;margin:0;font-weight:800}
.sec-head .sub{color:var(--muted);margin-top:4px}
.more{color:var(--coral-d);font-weight:700;cursor:pointer;text-decoration:none;white-space:nowrap}
.perks{display:grid;grid-template-columns:repeat(2,1fr);gap:14px}
.perk{display:flex;gap:12px;align-items:flex-start;background:#fff;border:1.5px dashed #9fd1ee;border-radius:18px;padding:14px}
.perk .pi{flex:0 0 52px;height:52px;border-radius:15px;background:var(--sky-100);display:grid;place-items:center;font-size:24px}
.perk b{display:block;font-size:16px;margin-bottom:2px}
.perk span{color:var(--muted);font-size:13px;line-height:1.35}
@media(min-width:900px){.perks{grid-template-columns:repeat(4,1fr)}}
.free-banner{position:relative;display:flex;align-items:center;justify-content:center;gap:18px;background:linear-gradient(135deg,#ff6b4a,#ffa24c);color:#fff;border-radius:22px;padding:20px 26px;text-align:center;box-shadow:0 10px 24px rgba(255,107,74,.32)}
.free-banner::after{content:'';position:absolute;inset:7px;border:2px dashed rgba(255,255,255,.8);border-radius:16px;pointer-events:none}
.free-banner b{display:block;font-size:22px;margin-bottom:4px}
.free-banner span.t{font-size:14px;opacity:.96}
.free-banner .fb{font-size:38px;animation:wobble 3.2s ease-in-out infinite}
@media(max-width:700px){.free-banner{padding:16px 12px;gap:10px}.free-banner b{font-size:17px}.free-banner .fb{font-size:28px}}
.cats{display:grid;grid-template-columns:repeat(2,1fr);gap:14px}
@media(min-width:700px){.cats{grid-template-columns:repeat(4,1fr)}}
.cat-card{background:#fff;border:1.5px dashed #9fd1ee;border-radius:22px;padding:22px 12px;text-align:center;font-weight:700;font-size:17px;line-height:1.25;cursor:pointer;transition:.2s}
.cat-card:hover{transform:translateY(-4px);box-shadow:0 12px 24px rgba(10,95,163,.16);border-style:solid}
.cat-ic{width:68px;height:68px;margin:0 auto 12px;border-radius:20px;background:linear-gradient(135deg,var(--sky-700),var(--sky-500));display:grid;place-items:center;font-size:30px}
.shops{display:grid;gap:14px;grid-template-columns:1fr}
@media(min-width:800px){.shops{grid-template-columns:repeat(3,1fr)}}
.shop{background:#fff;border:1px solid var(--line);border-radius:22px;padding:20px;position:relative}
.shop::after{content:'';position:absolute;inset:5px;border:1.5px dashed #bfe0f5;border-radius:18px;pointer-events:none}
.shop h4{margin:0 0 8px;font-size:18px}
.shop p{margin:0 0 14px;color:#2a4a66;line-height:1.5}
.shop a{position:relative;z-index:2}
.tip{display:flex;gap:16px;align-items:center;background:linear-gradient(135deg,var(--sky-100),#fff);border:1.5px dashed #8fcdf2;border-radius:22px;padding:20px}
.tip .ti{font-size:40px}
.tip b{display:block;margin-bottom:4px}
.tip button{margin-top:10px;border:0;background:var(--sky-600);color:#fff;border-radius:20px;padding:8px 16px;font-weight:600;cursor:pointer}

/* ТОВАРЫ */
.grid{display:grid;grid-template-columns:repeat(2,1fr);gap:14px}
@media(min-width:700px){.grid{grid-template-columns:repeat(3,1fr);gap:18px}}
@media(min-width:1000px){.grid{grid-template-columns:repeat(4,1fr)}}
.card{position:relative;background:#fff;border-radius:20px;border:1px solid var(--line);box-shadow:0 4px 14px rgba(10,95,163,.08);display:flex;flex-direction:column;overflow:hidden;transition:transform .25s,box-shadow .25s}
.card:hover{transform:translateY(-4px);box-shadow:0 14px 28px rgba(10,95,163,.18)}
.card::after{content:'';position:absolute;inset:5px;border:1.5px dashed #bfe0f5;border-radius:16px;pointer-events:none}
.c-img{aspect-ratio:1/1;background:var(--sky-50);display:grid;place-items:center;cursor:pointer;overflow:hidden}
.c-img img{width:100%;height:100%;object-fit:contain;padding:10px}
.noimg{font-size:56px;opacity:.45}
.c-body{padding:12px 14px 14px;display:flex;flex-direction:column;gap:6px;flex:1}
.c-cat{font-size:11px;font-weight:700;letter-spacing:.7px;text-transform:uppercase;color:var(--muted)}
.c-name{font-size:15px;font-weight:700;line-height:1.3;cursor:pointer;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
.c-price{margin-top:auto;padding-top:4px;font-size:21px;font-weight:800;color:var(--navy);display:flex;align-items:baseline;gap:6px;flex-wrap:wrap}
.c-price small{font-size:13px;font-weight:600;color:var(--muted)}
.c-old{font-size:13px;color:#8aa3b8;text-decoration:line-through;font-weight:500}
.c-act{display:flex;gap:8px;margin-top:8px;position:relative;z-index:2}
.btn-cart{flex:1;background:var(--coral);color:#fff;border:0;border-radius:12px;padding:11px 8px;font-weight:700;cursor:pointer;font-size:14px}
.btn-cart:hover{background:var(--coral-d)}
.btn-ico{width:42px;border:1.5px solid var(--line);background:#fff;border-radius:12px;cursor:pointer;font-size:15px}
.btn-ico:hover{background:var(--sky-100)}
.badge{position:absolute;top:12px;left:12px;z-index:3;background:var(--coral);color:#fff;font-weight:800;font-size:12px;padding:6px 12px;border-radius:20px}
.heart{position:absolute;top:10px;right:10px;z-index:3;width:36px;height:36px;border-radius:50%;border:0;background:rgba(255,255,255,.95);color:var(--sky-600);font-size:19px;cursor:pointer;box-shadow:0 2px 8px rgba(0,0,0,.12)}
.heart.on{background:var(--coral);color:#fff}
.empty{text-align:center;color:var(--muted);padding:34px 10px;grid-column:1/-1}
.reveal{opacity:0}
.reveal.in{opacity:1;animation:rise .5s backwards}
@keyframes rise{from{opacity:0;transform:translateY(16px)}to{opacity:1;transform:none}}

/* КАТАЛОГ */
.cat-hero{background:linear-gradient(135deg,#0b3257,#0b7ccf);color:#fff;padding:34px 18px}
.cat-hero div{max-width:1200px;margin:0 auto}
.cat-hero h1{margin:0 0 6px;font-size:34px}
.cat-hero p{margin:0;opacity:.92;font-size:17px}
.searchbox{display:flex;background:#fff;border-radius:999px;box-shadow:0 4px 14px rgba(10,95,163,.12);overflow:hidden;border:1px solid var(--line)}
.searchbox input{flex:1;border:0;outline:none;padding:16px 22px;font:inherit;font-size:16px;min-width:0;background:transparent}
.searchbox button{border:0;background:var(--coral);color:#fff;padding:0 24px;font-size:18px;cursor:pointer}
.chips{display:flex;gap:8px;overflow-x:auto;padding:14px 0 4px;scrollbar-width:none}
.chip{white-space:nowrap;border:1.5px dashed #8fcdf2;background:var(--sky-100);color:var(--sky-700);border-radius:20px;padding:8px 16px;font-weight:600;cursor:pointer;font-size:14px}
.chip.on{background:var(--sky-600);color:#fff;border-style:solid;border-color:var(--sky-600)}
.filters{display:none;background:#fff;border:1px solid var(--line);border-radius:20px;padding:16px;margin-top:12px;gap:12px;grid-template-columns:1fr 1fr}
.filters.on{display:grid}
.filters label{font-size:13px;font-weight:600;color:var(--muted);display:block;margin-bottom:4px}
.filters .full{grid-column:1/-1}
.found{text-align:right;color:var(--muted);margin:18px 0 12px}
.found b{color:var(--ink);font-size:20px}
.inp{width:100%;padding:13px 15px;border:1.5px solid var(--line);border-radius:14px;font:inherit;font-size:15px;background:#fff;outline:none}
.inp:focus{border-color:var(--sky-500)}
.ck{display:flex;align-items:center;gap:8px;font-weight:600;font-size:14px}
.ck input{width:18px;height:18px}

/* КОРЗИНА / КАБИНЕТ */
.page{max-width:1100px;margin:0 auto;padding:26px 18px 0}
.page h1{font-size:30px;margin:0 0 18px}
.box{background:#fff;border:1px solid var(--line);border-radius:24px;padding:22px;position:relative}
.box.dash::after{content:'';position:absolute;inset:6px;border:1.5px dashed #bfe0f5;border-radius:19px;pointer-events:none}
.center{text-align:center;padding:48px 18px}
.center .big{font-size:64px;opacity:.7}
.center h2{margin:10px 0 6px;font-size:26px}
.cart-grid{display:grid;gap:18px}
@media(min-width:900px){.cart-grid{grid-template-columns:1.3fr .9fr;align-items:start}}
.ci{display:flex;gap:12px;align-items:center;padding:12px 0;border-bottom:1px solid var(--line)}
.ci:last-child{border-bottom:0}
.ci-img{flex:0 0 64px;height:64px;border-radius:14px;background:var(--sky-50);overflow:hidden;display:grid;place-items:center;font-size:26px}
.ci-img img{width:100%;height:100%;object-fit:contain}
.ci-info{flex:1;min-width:0}
.ci-name{font-weight:700;font-size:15px;line-height:1.3}
.ci-price{color:var(--sky-700);font-weight:700;margin-top:3px}
.qty{display:flex;align-items:center;gap:8px}
.qty button{width:32px;height:32px;border-radius:10px;border:0;background:var(--sky-100);color:var(--sky-700);font-size:18px;font-weight:700;cursor:pointer}
.qty .rm{background:#ffe9e4;color:var(--coral-d);font-size:14px}
.line{display:flex;justify-content:space-between;margin:8px 0;color:#2a4a66}
.line.total{font-size:20px;font-weight:800;color:var(--ink);border-top:1px solid var(--line);padding-top:12px;margin-top:12px}
.free-tag{color:#119a6b;font-weight:700}
.ftitle{font-weight:700;margin:16px 0 8px}
.ferr{color:#d93a5f;font-size:14px;min-height:20px;margin-top:4px}
.fld{display:grid;gap:8px}
.orders .o{display:flex;justify-content:space-between;gap:10px;padding:10px 0;border-bottom:1px dashed var(--line)}

/* ПОДВАЛ */
.ftr{background:var(--navy);color:#cfe3f5;margin-top:46px;padding:38px 18px 26px}
.ftr-in{max-width:1200px;margin:0 auto;display:grid;gap:26px}
@media(min-width:800px){.ftr-in{grid-template-columns:1.3fr 1fr 1.3fr}}
.ftr h5{margin:0 0 12px;color:#fff;font-size:15px;letter-spacing:1.2px;text-transform:uppercase}
.ftr a{display:block;color:#cfe3f5;text-decoration:none;padding:5px 0;cursor:pointer}
.ftr a:hover{color:#fff}
.ftr p{margin:0 0 8px;line-height:1.55}
.ftr-bot{max-width:1200px;margin:24px auto 0;padding-top:16px;border-top:1px solid rgba(255,255,255,.18);text-align:center;font-size:13px;opacity:.85}

/* ТАББАР */
.tabbar{position:fixed;left:0;right:0;bottom:0;z-index:120;background:#fff;border-top:1px solid var(--line);display:flex;padding-bottom:env(safe-area-inset-bottom)}
.tab{flex:1;text-align:center;padding:9px 4px 7px;font-size:12px;font-weight:600;color:var(--sky-700);cursor:pointer;position:relative}
.tab i{font-style:normal;font-size:22px;display:block;line-height:1.2}
.tab.on{background:var(--sky-50);color:var(--sky-600)}
.tab .cnt{position:absolute;top:4px;left:calc(50% + 8px);margin:0}

/* WHATSAPP */
.fab{position:fixed;right:16px;bottom:96px;z-index:130;width:62px;height:62px;border-radius:50%;background:var(--wa);display:grid;place-items:center;box-shadow:0 8px 20px rgba(37,211,102,.5);cursor:pointer;border:0}
.fab::before{content:'';position:absolute;inset:0;border-radius:50%;border:3px solid var(--wa);animation:ping 2.2s ease-out infinite}
@keyframes ping{0%{transform:scale(1);opacity:.7}100%{transform:scale(1.55);opacity:0}}
.fab svg{width:34px;height:34px;fill:#fff}

/* МОДАЛКА */
.modal-ov{position:fixed;inset:0;background:rgba(11,50,87,.55);display:none;align-items:flex-end;justify-content:center;z-index:1000}
.modal-ov.on{display:flex}
.modal-card{background:#fff;width:100%;max-width:600px;max-height:92vh;overflow:auto;border-radius:24px 24px 0 0;padding:24px 22px calc(24px + env(safe-area-inset-bottom));position:relative;animation:up .25s}
@keyframes up{from{transform:translateY(40px);opacity:0}to{transform:none;opacity:1}}
.m-x{position:absolute;top:12px;right:12px;width:38px;height:38px;border-radius:50%;border:0;background:var(--sky-100);font-size:18px;cursor:pointer;z-index:3}
.m-title{margin:0 0 12px;font-size:22px;padding-right:40px}
.m-text{white-space:pre-line;line-height:1.65;color:#2a4a66;margin-bottom:16px}
.m-sub{margin:0 0 12px;color:var(--muted)}
@media(min-width:700px){.modal-ov{align-items:center;padding:20px}.modal-card{border-radius:24px}}
.wa-opt,.call-opt{display:flex;align-items:center;gap:12px;padding:14px;border:1.5px solid var(--line);border-radius:16px;margin-bottom:10px;text-decoration:none;color:var(--ink)}
.wa-opt:hover,.call-opt:hover{background:var(--sky-50)}
.wa-opt .wa-ic{width:42px;height:42px;border-radius:50%;background:var(--wa);display:grid;place-items:center}
.wa-opt .wa-ic svg{width:24px;height:24px;fill:#fff}
.wa-opt span:last-child{margin-left:auto;color:var(--muted);font-size:13px}
.pm{display:grid;gap:16px}
@media(min-width:600px){.pm{grid-template-columns:1fr 1fr}}
.pm-img{aspect-ratio:1/1;background:var(--sky-50);border-radius:18px;display:grid;place-items:center;overflow:hidden}
.pm-img img{width:100%;height:100%;object-fit:contain}
.pm-act{display:grid;gap:8px;margin-top:6px}
.pm .c-price{font-size:28px}
.fd{background:#e8fbf1;color:#0f7a4f;border-radius:12px;padding:8px 12px;font-size:14px;font-weight:600;margin:6px 0 12px}
.toast{position:fixed;bottom:92px;left:50%;transform:translateX(-50%) translateY(20px);background:var(--navy);color:#fff;padding:12px 20px;border-radius:14px;font-size:14px;opacity:0;transition:.3s;z-index:3000;pointer-events:none;max-width:90%;text-align:center}
.toast.show{opacity:1;transform:translateX(-50%) translateY(0)}

@media(min-width:900px){
  body{padding-bottom:0}
  .burger,.tabbar{display:none}
  .desk-nav{display:flex}
  .fab{bottom:26px}
  .toast{bottom:26px}
  .modal-card{max-width:720px}
}
</style>
</head>
<body>

<header class="hdr">
  <div class="hdr-in">
    <a class="brand" onclick="go('home')"><div class="logo"><span>🧵</span></div><span id="brandName"></span></a>
    <nav class="desk-nav">
      <a data-tab="home" onclick="go('home')">Главная</a>
      <a data-tab="catalog" onclick="go('catalog')">Каталог</a>
      <a data-tab="cart" onclick="go('cart')">Корзина<span class="cnt" data-cnt></span></a>
      <a data-tab="cabinet" onclick="go('cabinet')">Кабинет</a>
    </nav>
    <button class="burger" onclick="openDrawer()" aria-label="Меню">☰</button>
  </div>
  <div class="stitch" id="stitch"></div>
</header>

<div class="ov" id="ov" onclick="closeDrawer()"></div>
<aside class="drawer" id="drawer">
  <button class="x" onclick="closeDrawer()">✕</button>
  <h6>МАГАЗИН</h6>
  <a class="dl" onclick="go('home')"><i>🏠</i>Главная</a>
  <a class="dl" onclick="go('catalog')"><i>🗂</i>Каталог</a>
  <a class="dl" onclick="go('cart')"><i>🛍</i>Корзина<span class="cnt" data-cnt></span></a>
  <a class="dl" onclick="go('cabinet')"><i>👤</i>Личный кабинет</a>
  <button class="btn btn-primary btn-block" style="margin:16px 0 8px" onclick="go('catalog')">🔍 Смотреть каталог</button>
  <button class="btn btn-wa btn-block" style="margin:0 0 8px" onclick="closeDrawer();openWA()">💬 Написать в WhatsApp</button>
  <div class="phones" id="drawerPhones"></div>
</aside>

<!-- ============ ГЛАВНАЯ ============ -->
<main class="view on" id="view-home">
  <section class="hero">
    <span class="cloud c1"></span><span class="cloud c2"></span>
    <div class="hero-in">
      <div>
        <h1>Швейные машины, оверлоки и аксессуары</h1>
        <p class="lead">Оборудование для дома и ателье. Доставка и установка — бесплатно, гарантия на всё оборудование.</p>
        <div class="hero-btns">
          <button class="btn btn-primary" onclick="go('catalog')">🔍 Смотреть каталог</button>
          <button class="btn btn-ghost" onclick="openCall()">📞 Позвонить</button>
        </div>
        <div class="consult">🎧 Консультация: <span id="heroPhones"></span></div>
      </div>
      <div class="showcase"><img src="/machine.jpg" alt="Швейная машина JACK" width="960" height="752"></div>
      <div class="hero-ads">
        <div class="ads" id="adBox">
          <div class="ad-track" id="adTrack"></div>
          <button class="ad-arrow l" onclick="adPrev()" aria-label="Назад">‹</button>
          <button class="ad-arrow r" onclick="adNext()" aria-label="Вперёд">›</button>
          <div class="dots" id="adDots"></div>
        </div>
      </div>
    </div>
  </section>

  <section class="sec">
    <div class="perks">
      <div class="perk"><div class="pi">🚚</div><div><b>Доставка</b><span>Бесплатно, вместе с установкой</span></div></div>
      <div class="perk"><div class="pi">🛡</div><div><b>Гарантия</b><span>На всё оборудование</span></div></div>
      <div class="perk"><div class="pi">🔧</div><div><b>Ремонт и настройка</b><span>Поможем и подскажем</span></div></div>
      <div class="perk"><div class="pi">🤝</div><div><b>Прямые поставки</b><span>Честные цены без наценок</span></div></div>
    </div>
  </section>

  <section class="sec">
    <div class="free-banner">
      <span class="fb">🚚</span>
      <div><b>Доставка и установка — бесплатно!</b><span class="t">Привезём заказ к двери и установим. Вы платите только за сам товар.</span></div>
      <span class="fb">🎁</span>
    </div>
  </section>

  <section class="sec" id="catsSec">
    <div class="sec-head"><div><h2>Категории</h2><div class="sub">Выберите нужный раздел</div></div><a class="more" onclick="openCat(-1)">Все товары →</a></div>
    <div class="cats" id="catGrid"></div>
  </section>

  <section class="sec">
    <div class="sec-head"><div><h2>Популярные товары</h2><div class="sub">Скидки и новинки магазина</div></div><a class="more" onclick="openCat(-1)">Смотреть все →</a></div>
    <div class="grid" id="popGrid"></div>
  </section>

  <section class="sec">
    <div class="tip"><div class="ti">🪡</div><div><b>Совет швее</b><span id="tipText"></span><br><button onclick="nextTip()">Другой совет</button></div></div>
  </section>

  <section class="sec">
    <div class="sec-head"><div><h2>Наши магазины</h2><div class="sub">Приезжайте — покажем и подберём</div></div></div>
    <div class="shops" id="shops"></div>
  </section>
</main>

<!-- ============ КАТАЛОГ ============ -->
<main class="view" id="view-catalog">
  <div class="cat-hero"><div><h1>Каталог товаров</h1><p>Швейные машины, оверлоки и аксессуары — найдите нужную модель</p></div></div>
  <div class="page" style="padding-top:22px">
    <div class="searchbox"><input id="q" type="search" placeholder="Поиск по названию..." oninput="onSearch(this.value)"><button onclick="document.getElementById('q').focus()">🔍</button></div>
    <button class="btn btn-line btn-block" style="margin-top:12px" onclick="toggleFilters()">⚙️ Фильтры</button>
    <div class="filters" id="filters">
      <div><label>Сортировка</label>
        <select class="inp" id="fSort" onchange="onFilter()">
          <option value="new">Сначала новые</option><option value="cheap">Сначала дешевле</option><option value="expensive">Сначала дороже</option><option value="name">По названию</option>
        </select></div>
      <div><label>Цена от — до, сом</label>
        <div style="display:flex;gap:6px"><input class="inp" id="fMin" type="number" min="0" placeholder="от" oninput="onFilter()"><input class="inp" id="fMax" type="number" min="0" placeholder="до" oninput="onFilter()"></div></div>
      <label class="ck"><input type="checkbox" id="fSale" onchange="onFilter()"> Только со скидкой</label>
      <label class="ck"><input type="checkbox" id="fFav" onchange="onFilter()"> Только избранное</label>
      <div class="full"><button class="btn btn-line" onclick="resetFilters()">Сбросить фильтры</button></div>
    </div>
    <div class="chips" id="chips"></div>
    <div class="found" id="found"></div>
    <div class="grid" id="catalogGrid"></div>
  </div>
</main>

<!-- ============ КОРЗИНА ============ -->
<main class="view" id="view-cart">
  <div class="page">
    <h1>Корзина</h1>
    <div class="box dash center" id="cartEmpty">
      <div class="big">🛍</div><h2>Корзина пуста</h2>
      <p class="muted">Добавьте товары из каталога, чтобы оформить заказ</p>
      <button class="btn btn-primary" onclick="go('catalog')">Перейти в каталог</button>
    </div>
    <div class="cart-grid" id="cartFull" style="display:none">
      <div class="box dash"><div id="cartList"></div></div>
      <aside class="box dash">
        <div class="line"><span>Товары:</span><span id="sumSub">0 сом</span></div>
        <div class="line"><span>Доставка и установка:</span><span class="free-tag">Бесплатно 🎁</span></div>
        <div class="line total"><span>Итого:</span><span id="sumTotal">0 сом</span></div>
        <div class="ftitle">Данные для доставки</div>
        <div class="fld">
          <input class="inp" id="custName" type="text" placeholder="Ваше имя" maxlength="50" autocomplete="name">
          <input class="inp" id="custPhone" type="tel" placeholder="Телефон: +996 700 245 781" maxlength="20" autocomplete="tel">
          <input class="inp" id="custAddress" type="text" placeholder="Адрес доставки (улица, дом, квартира)" maxlength="200" autocomplete="street-address">
        </div>
        <div class="ferr" id="formError"></div>
        <button class="btn btn-primary btn-block" id="checkoutBtn" onclick="checkout()">Оформить заказ</button>
        <button class="btn btn-wa btn-block" style="margin-top:10px" onclick="waCart()">💬 Заказать через WhatsApp</button>
      </aside>
    </div>
  </div>
</main>

<!-- ============ КАБИНЕТ ============ -->
<main class="view" id="view-cabinet">
  <div class="page">
    <h1>Личный кабинет</h1>
    <div class="box dash">
      <b>Мои данные</b>
      <p class="muted" style="margin:6px 0 12px">Хранятся только на вашем устройстве и подставляются при оформлении заказа.</p>
      <div class="fld">
        <input class="inp" id="pfName" type="text" placeholder="Имя" maxlength="50">
        <input class="inp" id="pfPhone" type="tel" placeholder="Телефон" maxlength="20">
        <input class="inp" id="pfAddress" type="text" placeholder="Адрес доставки" maxlength="200">
      </div>
      <div class="ferr" id="pfErr"></div>
      <button class="btn btn-sky" onclick="saveProfile()">Сохранить</button>
    </div>
    <div class="sec-head" style="margin-top:28px"><h2>♥ Избранное</h2></div>
    <div class="grid" id="favGrid"></div>
    <div class="sec-head" style="margin-top:28px"><h2>Мои заказы</h2></div>
    <div class="box orders" id="ordersBox"></div>
    <button class="btn btn-line" style="margin-top:20px" onclick="clearMyData()">Очистить данные на устройстве</button>
  </div>
</main>

<footer class="ftr">
  <div class="ftr-in">
    <div><h5 id="ftName"></h5><p>Магазин швейных машин, оверлоков и аксессуаров. Бишкек · Манас · Ош.</p><p>🚚 Доставка и установка бесплатно</p></div>
    <div><h5>Навигация</h5>
      <a onclick="go('catalog')">Каталог</a><a onclick="go('cart')">Корзина</a><a onclick="go('cabinet')">Личный кабинет</a>
      <a onclick="showInfo('about')">О нас</a><a onclick="showInfo('order')">Как сделать заказ</a><a onclick="showInfo('delivery')">Доставка и оплата</a><a onclick="showInfo('tips')">Советы швее 🪡</a>
    </div>
    <div><h5>Контакты</h5><div id="ftContacts"></div></div>
  </div>
  <div class="ftr-bot">© 2026 <span id="ftName2"></span>. Все права защищены.</div>
</footer>

<nav class="tabbar">
  <div class="tab on" data-tab="home" onclick="go('home')"><i>🏠</i>Главная</div>
  <div class="tab" data-tab="catalog" onclick="go('catalog')"><i>🗂</i>Каталог</div>
  <div class="tab" data-tab="cart" onclick="go('cart')"><i>🛍</i>Корзина<span class="cnt" data-cnt></span></div>
  <div class="tab" data-tab="cabinet" onclick="go('cabinet')"><i>👤</i>Кабинет</div>
</nav>

<button class="fab" onclick="openWA()" aria-label="WhatsApp">
  <svg viewBox="0 0 24 24"><path d="M17.472 14.382c-.297-.149-1.758-.867-2.03-.967-.273-.099-.471-.148-.67.15-.197.297-.767.966-.94 1.164-.173.199-.347.223-.644.075-.297-.15-1.255-.463-2.39-1.475-.883-.788-1.48-1.761-1.653-2.059-.173-.297-.018-.458.13-.606.134-.133.298-.347.446-.52.149-.174.198-.298.298-.497.099-.198.05-.371-.025-.52-.075-.149-.669-1.612-.916-2.207-.242-.579-.487-.5-.669-.51-.173-.008-.371-.01-.57-.01-.198 0-.52.074-.792.372-.272.297-1.04 1.016-1.04 2.479 0 1.462 1.065 2.875 1.213 3.074.149.198 2.096 3.2 5.077 4.487.709.306 1.262.489 1.694.625.712.227 1.36.195 1.871.118.571-.085 1.758-.719 2.006-1.413.248-.694.248-1.289.173-1.413-.074-.124-.272-.198-.57-.347m-5.421 7.403h-.004a9.87 9.87 0 01-5.031-1.378l-.361-.214-3.741.982.998-3.648-.235-.374a9.86 9.86 0 01-1.51-5.26c.001-5.45 4.436-9.884 9.888-9.884 2.64 0 5.122 1.03 6.988 2.898a9.825 9.825 0 012.893 6.994c-.003 5.45-4.437 9.884-9.885 9.884m8.413-18.297A11.815 11.815 0 0012.05 0C5.495 0 .16 5.335.157 11.892c0 2.096.547 4.142 1.588 5.945L.057 24l6.305-1.654a11.882 11.882 0 005.683 1.448h.005c6.554 0 11.89-5.335 11.893-11.893a11.821 11.821 0 00-3.48-8.413Z"/></svg>
</button>

<div class="modal-ov" id="modal" onclick="if(event.target===this)closeModal()">
  <div class="modal-card"><button class="m-x" onclick="closeModal()">✕</button><div id="modalBody"></div></div>
</div>
<div class="toast" id="toast"></div>

<script type="application/json" id="appdata">__DATA__</script>
<script>
const D = JSON.parse(document.getElementById('appdata').textContent);
const P = D.products, PH = D.phones, ADDR = D.addrs;
const byId = {}; P.forEach(p => { byId[p.id] = p; });
const S = { q: '', cat: '', sort: 'new', min: '', max: '', sale: false, fav: false };
let view = 'home', cart = [], favs = [], orders = [], profile = {};
let inited = false;

const $ = id => document.getElementById(id);
function esc(s) { return String(s == null ? '' : s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;').replace(/'/g, '&#39;'); }
function load(k, f) { try { const v = JSON.parse(localStorage.getItem(k)); return v == null ? f : v; } catch (e) { return f; } }
function save(k, v) { try { localStorage.setItem(k, JSON.stringify(v)); } catch (e) {} }
function money(n) { return (Math.round(n * 100) / 100).toLocaleString('ru-RU'); }
function plural(n, a, b, c) { n = Math.abs(n) % 100; const m = n % 10; if (n > 10 && n < 20) return c; if (m > 1 && m < 5) return b; if (m === 1) return a; return c; }

let toastTimer = null;
function toast(t) { const el = $('toast'); el.textContent = t; el.classList.add('show'); clearTimeout(toastTimer); toastTimer = setTimeout(() => el.classList.remove('show'), 2000); }

/* ---------- модалки ---------- */
function openModal(html) { $('modalBody').innerHTML = html; $('modal').classList.add('on'); document.body.style.overflow = 'hidden'; }
function closeModal() {
  $('modal').classList.remove('on'); document.body.style.overflow = '';
  if (/^#\/p\//.test(location.hash)) history.replaceState(null, '', view === 'home' ? '#/' : '#/' + view);
}
const INFO = {
  about: ['О нас', D.name + ' — магазин швейных машин, оверлоков и аксессуаров. У нас есть всё, чтобы начать шить и продолжать: техника для дома и ателье, запчасти и мелочи, без которых не обойтись.\n\nМы работаем в Бишкеке, Манасе и Оше. Выбирайте товары на сайте, оформляйте заказ — доставка и установка бесплатно. А если нужна консультация, звоните или пишите в WhatsApp.'],
  order: ['Как сделать заказ', '1. Найдите товар через поиск или выберите категорию.\n2. Нажмите «В корзину». Понравившееся можно отметить сердечком ♡.\n3. Откройте корзину, проверьте количество и укажите имя, телефон и адрес.\n4. Нажмите «Оформить заказ» — мы получим его сразу и позвоним для подтверждения.\n\nМожно также заказать через WhatsApp — кнопка есть в корзине.'],
  delivery: ['Доставка и оплата', 'Доставка и установка у нас бесплатные: привезём заказ прямо к двери, вы платите только за сам товар.\n\nОплата при получении заказа. Номер телефона нужен, чтобы мы могли с вами связаться, поэтому указывайте действующий номер.'],
  tips: ['Советы швее 🪡', null]
};
const TIPS = [
  'Подбирайте иглу под ткань: тонкие номера (60–70) — для шёлка и тонкого хлопка, средние (80–90) — для обычных тканей, толстые (100–110) — для джинсы и плотных материалов.',
  'Нитки лучше брать на тон темнее ткани: на самой ткани стежки выглядят светлее, чем на катушке.',
  'Перед раскроем постирайте и прогладьте ткань, чтобы готовое изделие потом не село.',
  'Меняйте иглу примерно через 6–8 часов шитья или если она задела булавку: тупая игла портит ткань и пропускает стежки.',
  'Для трикотажа берите иглу для джерси с закруглённым кончиком — она раздвигает нити, а не прокалывает их.',
  'Регулярно чистите машинку от пыли и ниточного пуха в районе челнока — это продлевает ей жизнь.',
  'Перед шитьём проверьте строчку на обрезке той же ткани: так сразу видно, нужно ли менять натяжение нити.'
];
INFO.tips[1] = TIPS.map((t, i) => (i + 1) + '. ' + t).join('\n');
function showInfo(k) { openModal('<h3 class="m-title">' + esc(INFO[k][0]) + '</h3><div class="m-text">' + esc(INFO[k][1]) + '</div><button class="btn btn-sky" onclick="closeModal()">Закрыть</button>'); }
let tipI = Math.floor(Math.random() * TIPS.length);
function nextTip() { tipI = (tipI + 1) % TIPS.length; $('tipText').textContent = TIPS[tipI]; }

const WA_SVG = '<svg viewBox="0 0 24 24"><path d="M17.472 14.382c-.297-.149-1.758-.867-2.03-.967-.273-.099-.471-.148-.67.15-.197.297-.767.966-.94 1.164-.173.199-.347.223-.644.075-.297-.15-1.255-.463-2.39-1.475-.883-.788-1.48-1.761-1.653-2.059-.173-.297-.018-.458.13-.606.134-.133.298-.347.446-.52.149-.174.198-.298.298-.497.099-.198.05-.371-.025-.52-.075-.149-.669-1.612-.916-2.207-.242-.579-.487-.5-.669-.51-.173-.008-.371-.01-.57-.01-.198 0-.52.074-.792.372-.272.297-1.04 1.016-1.04 2.479 0 1.462 1.065 2.875 1.213 3.074.149.198 2.096 3.2 5.077 4.487.709.306 1.262.489 1.694.625.712.227 1.36.195 1.871.118.571-.085 1.758-.719 2.006-1.413.248-.694.248-1.289.173-1.413-.074-.124-.272-.198-.57-.347m-5.421 7.403h-.004a9.87 9.87 0 01-5.031-1.378l-.361-.214-3.741.982.998-3.648-.235-.374a9.86 9.86 0 01-1.51-5.26c.001-5.45 4.436-9.884 9.888-9.884 2.64 0 5.122 1.03 6.988 2.898a9.825 9.825 0 012.893 6.994c-.003 5.45-4.437 9.884-9.885 9.884m8.413-18.297A11.815 11.815 0 0012.05 0C5.495 0 .16 5.335.157 11.892c0 2.096.547 4.142 1.588 5.945L.057 24l6.305-1.654a11.882 11.882 0 005.683 1.448h.005c6.554 0 11.89-5.335 11.893-11.893a11.821 11.821 0 00-3.48-8.413Z"/></svg>';
function waLink(wa, text) { return 'https://wa.me/' + wa + '?text=' + encodeURIComponent(text); }
function openWA(text) {
  text = text || ('Здравствуйте! Пишу с сайта ' + D.name + '.');
  openModal('<h3 class="m-title">Написать в WhatsApp</h3><p class="m-sub">Выберите номер:</p>' +
    PH.filter(p => p.wa).map(p => '<a class="wa-opt" href="' + waLink(p.wa, text) + '" target="_blank" rel="noopener"><span class="wa-ic">' + WA_SVG + '</span><b>' + esc(p.label) + '</b><span>Открыть чат →</span></a>').join(''));
}
function openCall() {
  openModal('<h3 class="m-title">Позвонить</h3><p class="m-sub">Выберите номер:</p>' + PH.map(p => '<a class="call-opt" href="tel:' + p.tel + '">📞 <b>' + esc(p.label) + '</b></a>').join(''));
}
function mapLink(a) { return 'https://www.google.com/maps/search/?api=1&query=' + encodeURIComponent(a.city.replace(' шаары', '') + ' ' + a.addr.replace(/\s*\(.*\)\s*/, ' ')); }
function showContacts() {
  openModal('<h3 class="m-title">Контакты</h3>' + PH.map(p => '<a class="call-opt" href="tel:' + p.tel + '">📞 <b>' + esc(p.label) + '</b></a>').join('') +
    ADDR.map(a => '<p class="m-text" style="margin:12px 0 0">📌 <b>' + esc(a.city) + '</b>\n' + esc(a.addr) + '</p>').join(''));
}

/* ---------- телефон ---------- */
const KG = new Set(['755', '880', '990', '995', '999', '222']);
for (let i = 500; i <= 509; i++) KG.add(String(i));
for (let i = 550; i <= 559; i++) KG.add(String(i));
for (let i = 700; i <= 709; i++) KG.add(String(i));
for (let i = 770; i <= 779; i++) KG.add(String(i));
function normalizePhone(raw) {
  const d = (raw || '').replace(/\D/g, ''); let nine = null;
  if (d.length === 12 && d.startsWith('996')) nine = d.slice(3);
  else if (d.length === 10 && d.startsWith('0')) nine = d.slice(1);
  else if (d.length === 9) nine = d;
  if (!nine || !KG.has(nine.slice(0, 3))) return null;
  const rest = nine.slice(3);
  if (/^(\d)\1+$/.test(rest) || '0123456789'.includes(rest) || '9876543210'.includes(rest)) return null;
  return '+996' + nine;
}

/* ---------- карточки ---------- */
function imgHtml(p) { return p.photo ? '<img src="/photo/' + p.id + '" alt="' + esc(p.name) + '" loading="lazy" onerror="this.parentNode.innerHTML=\'<div class=noimg>🧵</div>\'">' : '<div class="noimg">🧵</div>'; }
function cardHtml(p) {
  const fav = favs.includes(p.id);
  return '<article class="card reveal">' +
    (p.disc ? '<span class="badge">-' + esc(p.disc) + '</span>' : '') +
    '<button class="heart ' + (fav ? 'on' : '') + '" onclick="toggleFav(' + p.id + ')" aria-label="В избранное">' + (fav ? '♥' : '♡') + '</button>' +
    '<div class="c-img" onclick="openProduct(' + p.id + ')">' + imgHtml(p) + '</div>' +
    '<div class="c-body"><div class="c-cat">' + esc(p.cat || 'Общее') + '</div>' +
    '<div class="c-name" onclick="openProduct(' + p.id + ')">' + esc(p.name) + '</div>' +
    '<div class="c-price">' + money(p.price) + ' <small>сом</small>' + (p.old ? '<span class="c-old">' + money(p.old) + '</span>' : '') + '</div>' +
    '<div class="c-act"><button class="btn-cart" onclick="addToCart(' + p.id + ')">🛒 В корзину</button>' +
    '<button class="btn-ico" onclick="openProduct(' + p.id + ')" title="Подробнее">👁</button>' +
    '<button class="btn-ico" onclick="shareProduct(' + p.id + ')" title="Поделиться">🔗</button></div></div></article>';
}
function toggleFav(id) {
  const i = favs.indexOf(id);
  if (i >= 0) favs.splice(i, 1); else favs.push(id);
  save('favs', favs); refreshViews();
  toast(i >= 0 ? 'Убрано из избранного' : 'Добавлено в избранное ♥');
}
function shareProduct(id) {
  const p = byId[id], url = location.origin + '/#/p/' + id;
  if (navigator.share) navigator.share({ title: p.name, text: p.name + ' — ' + money(p.price) + ' сом', url: url }).catch(() => {});
  else if (navigator.clipboard) navigator.clipboard.writeText(url).then(() => toast('Ссылка скопирована')).catch(() => toast(url));
  else toast(url);
}
function askWA(id) { const p = byId[id]; openWA('Здравствуйте! Интересует: ' + p.name + ' (' + money(p.price) + ' сом).'); }
function openProduct(id) {
  const p = byId[id]; if (!p) return;
  if (location.hash !== '#/p/' + id) history.replaceState(null, '', '#/p/' + id);
  const fav = favs.includes(id);
  openModal('<div class="pm"><div class="pm-img">' + imgHtml(p) + '</div><div>' +
    '<div class="c-cat">' + esc(p.cat || 'Общее') + '</div><h3 class="m-title">' + esc(p.name) + '</h3>' +
    '<div class="c-price">' + money(p.price) + ' <small>сом / ' + esc(p.unit) + '</small>' + (p.old ? '<span class="c-old">' + money(p.old) + '</span>' : '') + '</div>' +
    '<div class="fd">🚚 Доставка и установка бесплатно</div>' +
    (p.desc ? '<div class="m-text">' + esc(p.desc) + '</div>' : '') +
    '<div class="pm-act"><button class="btn btn-primary" onclick="addToCart(' + id + ');closeModal()">🛒 В корзину</button>' +
    '<button class="btn btn-wa" onclick="askWA(' + id + ')">💬 Спросить в WhatsApp</button>' +
    '<button class="btn btn-line" onclick="toggleFav(' + id + ');openProduct(' + id + ')">' + (fav ? '♥ В избранном' : '♡ В избранное') + '</button>' +
    '<button class="btn btn-line" onclick="shareProduct(' + id + ')">🔗 Поделиться</button></div></div></div>');
}

/* ---------- реклама (карусель) ---------- */
let slide = 0, nSlides = 0, slideTimer = null;
function renderAds() {
  const ads = D.ads.length ? D.ads : [
    { title: 'Доставка и установка — бесплатно!', content: 'Привезём вашу швейную машину к двери и установим без доплат.' },
    { title: 'Нужна консультация?', content: 'Позвоните или напишите в WhatsApp — поможем выбрать машинку под ваши задачи.' }
  ];
  nSlides = ads.length;
  $('adTrack').innerHTML = ads.map((a, i) => a.photo
    ? '<div class="slide photo" style="--img:url(/adphoto/' + a.id + ')"><img src="/adphoto/' + a.id + '" alt="Реклама" loading="' + (i ? 'lazy' : 'eager') + '"></div>'
    : '<div class="slide g' + (i % 4) + '"><div class="slide-in"><span class="slide-tag">📢 Объявление</span><h3>' + esc(a.title) + '</h3><p>' + esc(a.content) + '</p></div></div>').join('');
  $('adDots').innerHTML = ads.map((a, i) => '<i onclick="goSlide(' + i + ')"></i>').join('');
  document.querySelectorAll('.ad-arrow').forEach(b => { b.style.display = nSlides > 1 ? '' : 'none'; });
  goSlide(0); startAds();
}
function goSlide(i) {
  slide = (i + nSlides) % nSlides;
  $('adTrack').style.transform = 'translateX(-' + (slide * 100) + '%)';
  document.querySelectorAll('#adDots i').forEach((d, k) => d.classList.toggle('on', k === slide));
}
function adNext() { goSlide(slide + 1); startAds(); }
function adPrev() { goSlide(slide - 1); startAds(); }
function startAds() { clearInterval(slideTimer); if (nSlides > 1) slideTimer = setInterval(() => goSlide(slide + 1), 5500); }
(function () {
  const box = $('adBox'); let sx = null;
  box.addEventListener('pointerdown', e => { sx = e.clientX; });
  box.addEventListener('pointerup', e => { if (sx == null) return; const dx = e.clientX - sx; sx = null; if (Math.abs(dx) > 45 && nSlides > 1) { goSlide(slide + (dx < 0 ? 1 : -1)); startAds(); } });
  box.addEventListener('pointercancel', () => { sx = null; });
})();

/* ---------- главная ---------- */
const CAT_ICONS = ['🧵', '🪡', '✂️', '⚙️', '🧶', '📏', '🔧', '🪢'];
function renderCats() {
  $('catGrid').innerHTML = D.cats.length ? D.cats.map((c, i) => '<div class="cat-card reveal" onclick="openCat(' + i + ')"><div class="cat-ic">' + CAT_ICONS[i % CAT_ICONS.length] + '</div>' + esc(c) + '</div>').join('')
    : '';
  $('catsSec').style.display = D.cats.length ? '' : 'none';
}
function renderPopular() {
  const list = P.slice().sort((a, b) => ((b.old ? 1 : 0) - (a.old ? 1 : 0)) || (b.id - a.id)).slice(0, 8);
  $('popGrid').innerHTML = list.length ? list.map(cardHtml).join('') : '<div class="empty">В магазине пока нет товаров. Загляните позже 🧵</div>';
}
function renderShops() {
  $('shops').innerHTML = ADDR.map(a => '<div class="shop reveal"><h4>📌 ' + esc(a.city) + '</h4><p>' + esc(a.addr) + '</p><a class="btn btn-line" href="' + mapLink(a) + '" target="_blank" rel="noopener">🗺 Маршрут</a></div>').join('');
}
function openCat(i) { S.cat = i >= 0 ? D.cats[i] : ''; S.q = ''; go('catalog'); }

/* ---------- каталог ---------- */
function setCat(i) { S.cat = i >= 0 ? D.cats[i] : ''; renderCatalogChips(); renderCatalogGrid(); }
function onSearch(v) { S.q = (v || '').trim().toLowerCase(); renderCatalogGrid(); }
function toggleFilters() { $('filters').classList.toggle('on'); }
function onFilter() { S.sort = $('fSort').value; S.min = $('fMin').value; S.max = $('fMax').value; S.sale = $('fSale').checked; S.fav = $('fFav').checked; renderCatalogGrid(); }
function resetFilters() { S.sort = 'new'; S.min = ''; S.max = ''; S.sale = false; S.fav = false; S.cat = ''; S.q = ''; $('q').value = ''; syncFilterInputs(); renderCatalogChips(); renderCatalogGrid(); }
function syncFilterInputs() { $('fSort').value = S.sort; $('fMin').value = S.min; $('fMax').value = S.max; $('fSale').checked = S.sale; $('fFav').checked = S.fav; $('q').value = S.q; }
function renderCatalogChips() {
  $('chips').innerHTML = '<button class="chip ' + (!S.cat ? 'on' : '') + '" onclick="setCat(-1)">Все товары</button>' +
    D.cats.map((c, i) => '<button class="chip ' + (S.cat === c ? 'on' : '') + '" onclick="setCat(' + i + ')">' + esc(c) + '</button>').join('');
}
function filtered() {
  let list = P.filter(p => {
    if (S.q && !p.name.toLowerCase().includes(S.q)) return false;
    if (S.cat && p.cat !== S.cat) return false;
    if (S.sale && !p.old) return false;
    if (S.fav && !favs.includes(p.id)) return false;
    if (S.min !== '' && p.price < Number(S.min)) return false;
    if (S.max !== '' && p.price > Number(S.max)) return false;
    return true;
  });
  list.sort((a, b) => S.sort === 'cheap' ? a.price - b.price : S.sort === 'expensive' ? b.price - a.price : S.sort === 'name' ? a.name.localeCompare(b.name, 'ru') : b.id - a.id);
  return list;
}
function renderCatalogGrid() {
  const list = filtered();
  $('found').innerHTML = 'Найдено: <b>' + list.length + '</b> ' + plural(list.length, 'товар', 'товара', 'товаров');
  $('catalogGrid').innerHTML = list.length ? list.map(cardHtml).join('') : '<div class="empty">Ничего не найдено 🪡</div>';
  observeReveal();
}

/* ---------- корзина ---------- */
function saveCart() {
  save('cart', cart);
  const n = cart.reduce((s, i) => s + i.qty, 0);
  document.querySelectorAll('[data-cnt]').forEach(el => { el.textContent = n || ''; });
  document.querySelectorAll('.tab .cnt,.cnt').forEach(el => el.classList.toggle('zero', !n));
}
function addToCart(id) {
  const it = cart.find(i => i.id === id);
  if (it) { if (it.qty < 99) it.qty++; } else cart.push({ id: id, qty: 1 });
  saveCart(); toast('Добавлено в корзину: ' + byId[id].name);
  if (view === 'cart') renderCart();
}
function changeQty(id, d) {
  const it = cart.find(i => i.id === id); if (!it) return;
  it.qty = Math.min(99, it.qty + d);
  if (it.qty <= 0) cart = cart.filter(i => i.id !== id);
  saveCart(); renderCart();
}
function removeItem(id) { cart = cart.filter(i => i.id !== id); saveCart(); renderCart(); }
function cartTotal() { return cart.reduce((s, i) => s + byId[i.id].price * i.qty, 0); }
function renderCart() {
  cart = cart.filter(i => byId[i.id]);
  const empty = cart.length === 0;
  $('cartEmpty').style.display = empty ? '' : 'none';
  $('cartFull').style.display = empty ? 'none' : '';
  if (empty) return;
  $('cartList').innerHTML = cart.map(i => {
    const p = byId[i.id];
    return '<div class="ci"><div class="ci-img">' + (p.photo ? '<img src="/photo/' + p.id + '" alt="" onerror="this.remove()">' : '🧵') + '</div>' +
      '<div class="ci-info"><div class="ci-name">' + esc(p.name) + '</div><div class="ci-price">' + money(p.price * i.qty) + ' сом</div></div>' +
      '<div class="qty"><button onclick="changeQty(' + p.id + ',-1)">−</button><span>' + i.qty + '</span><button onclick="changeQty(' + p.id + ',1)">+</button><button class="rm" onclick="removeItem(' + p.id + ')">✕</button></div></div>';
  }).join('');
  const t = cartTotal();
  $('sumSub').textContent = money(t) + ' сом'; $('sumTotal').textContent = money(t) + ' сом';
  if (!$('custName').value && profile.name) $('custName').value = profile.name;
  if (!$('custPhone').value && profile.phone) $('custPhone').value = profile.phone;
  if (!$('custAddress').value && profile.address) $('custAddress').value = profile.address;
}
function waCart() {
  if (!cart.length) return;
  const lines = cart.map(i => '• ' + byId[i.id].name + ' × ' + i.qty + ' = ' + money(byId[i.id].price * i.qty) + ' сом').join('\n');
  openWA('Здравствуйте! Хочу заказать:\n' + lines + '\nИтого: ' + money(cartTotal()) + ' сом (доставка и установка бесплатно).');
}
async function checkout() {
  const err = $('formError'), btn = $('checkoutBtn'); err.textContent = '';
  if (!cart.length) { err.textContent = 'Корзина пуста!'; return; }
  const name = $('custName').value.trim(), phone = normalizePhone($('custPhone').value), address = $('custAddress').value.trim();
  if (name.length < 2 || !/[A-Za-zА-Яа-яЁёҢңӨөҮү]/.test(name)) { err.textContent = 'Введите ваше имя.'; return; }
  if (!phone) { err.textContent = 'Введите настоящий номер телефона, например +996 700 245 781.'; return; }
  if (address.length < 5) { err.textContent = 'Введите адрес доставки.'; return; }
  btn.disabled = true;
  try {
    const total = cartTotal();
    const res = await fetch('/order', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ name: name, phone: phone, address: address, items: cart.map(i => ({ id: i.id, qty: i.qty })) }) });
    const data = await res.json();
    if (data.ok) {
      orders.unshift({ id: data.id, total: total, date: new Date().toLocaleDateString('ru-RU') }); save('orders', orders.slice(0, 30));
      profile = { name: name, phone: phone, address: address }; save('profile', profile);
      cart = []; saveCart(); renderCart();
      $('custName').value = ''; $('custPhone').value = ''; $('custAddress').value = '';
      openModal('<h3 class="m-title">Спасибо за заказ! 🎉</h3><div class="m-text">Ваш заказ №' + data.id + ' принят. Мы свяжемся с вами по номеру ' + esc(phone) + ' для подтверждения.\n\nДоставка и установка — бесплатно.</div><button class="btn btn-sky" onclick="closeModal();go(\'home\')">Отлично</button>');
    } else err.textContent = data.error || 'Не удалось оформить заказ.';
  } catch (e) { err.textContent = 'Не удалось отправить заказ. Попробуйте ещё раз.'; }
  finally { btn.disabled = false; }
}

/* ---------- кабинет ---------- */
function fillProfile() { $('pfName').value = profile.name || ''; $('pfPhone').value = profile.phone || ''; $('pfAddress').value = profile.address || ''; $('pfErr').textContent = ''; }
function saveProfile() {
  const name = $('pfName').value.trim(), raw = $('pfPhone').value.trim(), address = $('pfAddress').value.trim(), err = $('pfErr');
  const phone = raw ? normalizePhone(raw) : '';
  if (raw && !phone) { err.textContent = 'Проверьте номер телефона.'; return; }
  profile = { name: name, phone: phone || '', address: address }; save('profile', profile); err.textContent = ''; toast('Данные сохранены ✓');
}
function renderCabinet() {
  const fl = favs.map(id => byId[id]).filter(Boolean);
  $('favGrid').innerHTML = fl.length ? fl.map(cardHtml).join('') : '<div class="empty">Пока пусто. Нажимайте ♡ на товарах.</div>';
  $('ordersBox').innerHTML = orders.length ? orders.map(o => '<div class="o"><span>Заказ №' + o.id + ' · ' + esc(o.date) + '</span><b>' + money(o.total) + ' сом</b></div>').join('') : '<span class="muted">Заказов с этого устройства пока нет.</span>';
  observeReveal();
}
function clearMyData() { if (!confirm('Удалить избранное, историю заказов и сохранённые данные на этом устройстве?')) return; favs = []; orders = []; profile = {}; save('favs', favs); save('orders', orders); save('profile', profile); fillProfile(); refreshViews(); toast('Данные очищены'); }

/* ---------- навигация ---------- */
function openDrawer() { $('ov').classList.add('on'); $('drawer').classList.add('on'); }
function closeDrawer() { $('ov').classList.remove('on'); $('drawer').classList.remove('on'); }
function refreshViews() { if (view === 'home') renderPopular(); if (view === 'catalog') renderCatalogGrid(); if (view === 'cabinet') renderCabinet(); observeReveal(); }
function show(v) {
  view = v;
  document.querySelectorAll('.view').forEach(el => el.classList.toggle('on', el.id === 'view-' + v));
  document.querySelectorAll('[data-tab]').forEach(el => el.classList.toggle('on', el.dataset.tab === v));
  closeDrawer();
  if (v === 'home') renderPopular();
  if (v === 'catalog') { syncFilterInputs(); renderCatalogChips(); renderCatalogGrid(); }
  if (v === 'cart') renderCart();
  if (v === 'cabinet') { fillProfile(); renderCabinet(); }
  window.scrollTo(0, 0); observeReveal();
}
function go(v) {
  closeModal(); closeDrawer();
  const h = v === 'home' ? '#/' : '#/' + v;
  const cur = (location.hash === '' ? '#/' : location.hash);
  if (cur === h) show(v); else location.hash = h;
}
function route() {
  const parts = location.hash.replace(/^#\/?/, '').split('/');
  const v = parts[0];
  if (v === 'p' && byId[parts[1]]) { if (!inited) show('home'); inited = true; openProduct(Number(parts[1])); return; }
  inited = true;
  closeModal();
  show(['home', 'catalog', 'cart', 'cabinet'].includes(v) ? v : 'home');
}
window.addEventListener('hashchange', route);
window.addEventListener('scroll', () => {
  const h = document.documentElement; const max = h.scrollHeight - h.clientHeight;
  $('stitch').style.width = (max > 0 ? Math.min(100, h.scrollTop / max * 100) : 0) + '%';
}, { passive: true });
document.addEventListener('keydown', e => { if (e.key === 'Escape') { closeModal(); closeDrawer(); } });

let io = null;
function observeReveal() {
  const els = document.querySelectorAll('.reveal:not(.in)');
  if (!('IntersectionObserver' in window)) { els.forEach(e => e.classList.add('in')); return; }
  if (!io) io = new IntersectionObserver(es => es.forEach(en => { if (en.isIntersecting) { en.target.classList.add('in'); io.unobserve(en.target); } }), { threshold: 0.08 });
  els.forEach(e => io.observe(e));
}

/* ---------- старт ---------- */
(function init() {
  $('brandName').textContent = D.name; $('ftName').textContent = '🧵 ' + D.name; $('ftName2').textContent = D.name;
  $('heroPhones').innerHTML = PH.map(p => '<a href="tel:' + p.tel + '">' + esc(p.label) + '</a>').join(' · ');
  $('drawerPhones').innerHTML = PH.map(p => '<a href="tel:' + p.tel + '">📞 ' + esc(p.label) + '</a>').join('');
  $('ftContacts').innerHTML = PH.map(p => '<a href="tel:' + p.tel + '">📞 ' + esc(p.label) + '</a>').join('') +
    ADDR.map(a => '<p style="margin-top:10px">📌 <b style="color:#fff">' + esc(a.city) + '</b><br>' + esc(a.addr) + '</p>').join('');
  favs = load('favs', []).filter(id => byId[id]);
  cart = load('cart', []).filter(i => i && byId[i.id] && i.qty > 0).map(i => ({ id: i.id, qty: Math.min(99, i.qty) }));
  orders = load('orders', []); profile = load('profile', {});
  $('tipText').textContent = TIPS[tipI];
  renderAds(); renderCats(); renderShops(); saveCart();
  route();
})();
</script>
</body>
</html>"""


def effective_price(price, discount):
    try:
        value = float(price or 0)
    except (TypeError, ValueError):
        value = 0.0
    if discount:
        m = re.fullmatch(r"\s*-?\s*(\d+(?:[.,]\d+)?)\s*%?\s*", str(discount))
        if m:
            pct = float(m.group(1).replace(",", "."))
            if 0 < pct < 100:
                value = value * (1 - pct / 100)
    return round(value, 2)

async def handle_website(request):
    if not SITE_ACTIVE:
        # ВАЖНО: в aiohttp charset нельзя писать внутри content_type — это и давало ошибку 500
        return web.Response(text="🛠 Сайт временно отключен владельцем.", status=503, content_type="text/plain", charset="utf-8")

    products, categories, ads = [], [], []
    for pid, name, unit, price, photo, discount, cat, desc in db_all(
        "SELECT id, name, unit, price, photo, discount, category, description FROM products ORDER BY id"
    ):
        final_price = effective_price(price, discount)
        try:
            base_price = round(float(price or 0), 2)
        except (TypeError, ValueError):
            base_price = 0.0
        has_discount = final_price < base_price
        products.append({
            "id": int(pid),
            "name": str(name or ""),
            "unit": unit or "шт",
            "price": final_price,
            "old": base_price if has_discount else 0,
            "disc": str(discount) if (discount and has_discount) else "",
            "cat": cat or "",
            "desc": str(desc or "").strip(),
            "photo": bool(photo),
        })
    categories = [r[0] for r in db_all("SELECT name FROM categories ORDER BY id")]
    ads = [
        {"id": int(i), "title": str(t or ""), "content": str(c or ""), "photo": bool(ph)}
        for i, t, c, ph in db_all("SELECT id, title, content, photo FROM advertisements ORDER BY id")
    ]

    data = {
        "name": SHOP_NAME,
        "products": products,
        "cats": categories,
        "ads": ads,
        "phones": SHOP_PHONES,
        "addrs": SHOP_ADDRESSES,
    }
    payload = json.dumps(data, ensure_ascii=False).replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    page = PAGE.replace("__NAME__", esc(SHOP_NAME)).replace("__DATA__", payload)
    return web.Response(text=page, content_type="text/html", charset="utf-8")


# ---------- ФОТО ТОВАРОВ (берём из Telegram и кэшируем) ----------
PHOTO_DIR = os.path.join(os.path.dirname(os.path.abspath(DB_PATH)), "photos")

async def serve_tg_file(file_id):
    if not file_id:
        return web.Response(status=404)
    safe_name = re.sub(r"[^A-Za-z0-9_-]", "", str(file_id))[:150]
    if not safe_name:
        return web.Response(status=404)
    try:
        os.makedirs(PHOTO_DIR, exist_ok=True)
        path = os.path.join(PHOTO_DIR, safe_name + ".jpg")
        if not os.path.exists(path):
            tmp_path = path + ".tmp"
            await bot.download(file_id, destination=tmp_path)
            os.replace(tmp_path, path)
    except Exception as e:
        logging.error(f"Photo error for {safe_name}: {e}")
        return web.Response(status=404)
    return web.FileResponse(path, headers={"Cache-Control": "public, max-age=86400"})

async def handle_photo(request):
    try:
        pid = int(request.match_info["pid"])
    except (KeyError, ValueError):
        return web.Response(status=404)
    rows = db_all("SELECT photo FROM products WHERE id = ?", (pid,))
    return await serve_tg_file(rows[0][0] if rows else None)

async def handle_adphoto(request):
    try:
        aid = int(request.match_info["aid"])
    except (KeyError, ValueError):
        return web.Response(status=404)
    rows = db_all("SELECT photo FROM advertisements WHERE id = ?", (aid,))
    return await serve_tg_file(rows[0][0] if rows else None)

async def handle_machine(request):
    try:
        body = base64.b64decode("".join(MACHINE_B64.split()))
    except Exception as e:
        logging.error(f"machine image error: {e}")
        return web.Response(status=404)
    return web.Response(body=body, content_type="image/jpeg", headers={"Cache-Control": "public, max-age=604800"})

# ---------- ПРИЁМ ЗАКАЗОВ С САЙТА ----------
KG_PREFIXES = {"755", "880", "990", "995", "999", "222"}
for _start, _end in ((500, 509), (550, 559), (700, 709), (770, 779)):
    for _code in range(_start, _end + 1):
        KG_PREFIXES.add(str(_code))

def normalize_phone(raw):
    digits = re.sub(r"\D", "", str(raw or ""))
    if len(digits) == 12 and digits.startswith("996"):
        nine = digits[3:]
    elif len(digits) == 10 and digits.startswith("0"):
        nine = digits[1:]
    elif len(digits) == 9:
        nine = digits
    else:
        return None
    if nine[:3] not in KG_PREFIXES:
        return None
    rest = nine[3:]
    if len(set(rest)) == 1 or rest in "0123456789" or rest in "9876543210":
        return None
    return "+996" + nine

LAST_ORDER_TIME = {}

def order_error(text, status=400):
    return web.json_response({"ok": False, "error": text}, status=status)

async def handle_order(request):
    if not SITE_ACTIVE:
        return order_error("Сайт временно отключен.", 503)
    try:
        data = await request.json()
    except Exception:
        return order_error("Неверные данные заказа.")

    ip = (request.headers.get("X-Forwarded-For") or request.remote or "").split(",")[0].strip()
    if time.time() - LAST_ORDER_TIME.get(ip, 0) < 15:
        return order_error("Подождите немного перед новым заказом.", 429)

    name = str(data.get("name", "")).strip()
    address = str(data.get("address", "")).strip()
    phone = normalize_phone(data.get("phone"))

    if len(name) < 2 or len(name) > 50 or not re.search(r"[A-Za-zА-Яа-яЁёҢңӨөҮү]", name):
        return order_error("Введите ваше имя.")
    if not phone:
        return order_error("Введите настоящий номер телефона, например +996 700 245 781.")
    if len(address) < 5 or len(address) > 200:
        return order_error("Введите адрес доставки.")

    raw_items = data.get("items")
    if not isinstance(raw_items, list) or not raw_items or len(raw_items) > 50:
        return order_error("Корзина пуста.")

    lines, subtotal = [], 0.0
    for entry in raw_items:
        try:
            pid = int(entry.get("id"))
            qty = int(entry.get("qty"))
        except Exception:
            return order_error("Неверные данные заказа.")
        if qty < 1 or qty > 99:
            return order_error("Неверное количество товара.")
        rows = db_all("SELECT name, unit, price, discount FROM products WHERE id = ?", (pid,))
        if not rows:
            return order_error("Один из товаров больше недоступен. Обновите страницу.")
        pname, unit, price, disc = rows[0]
        price = effective_price(price, disc)
        line_total = price * qty
        subtotal += line_total
        lines.append(f"• {pname} × {qty} {unit or 'шт'} = {fmt_price(line_total)} сом")

    delivery = DELIVERY_FEE
    total = subtotal + delivery
    created = now_local().strftime("%Y-%m-%d %H:%M:%S")
    order_id = db_run(
        "INSERT INTO orders (name, phone, address, items, subtotal, delivery, total, status, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, 'new', ?)",
        (name, phone, address, "\n".join(lines), subtotal, delivery, total, created)
    )
    if not order_id:
        return order_error("Не удалось сохранить заказ. Попробуйте ещё раз.", 500)

    LAST_ORDER_TIME[ip] = time.time()
    await notify_admins(order_id)
    return web.json_response({"ok": True, "id": order_id})

async def run_web():
    app = web.Application()
    app.router.add_get('/', handle_website)
    app.router.add_post('/order', handle_order)
    app.router.add_get('/photo/{pid}', handle_photo)
    app.router.add_get('/adphoto/{aid}', handle_adphoto)
    app.router.add_get('/machine.jpg', handle_machine)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.environ.get("PORT", 10000))
    site = web.TCPSite(runner, '0.0.0.0', port)
    await site.start()

async def main():
    await run_web()
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)

# ---------- ФОТО ШВЕЙНОЙ МАШИНКИ НА ГЛАВНОЙ (JPEG в base64) ----------
MACHINE_B64 = """
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
"""

if __name__ == "__main__":
    asyncio.run(main())
