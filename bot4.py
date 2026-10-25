import asyncio
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
#  ВЕБ-СЕРВЕР И САЙТ «ШВЕЙМАГ KG»
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
SHOP_NAME = "Швеймаг Kg"
SHOP_PHONES = [
    {"label": "+996 556 585 451", "tel": "+996556585451", "wa": "996556585451"},
    {"label": "+996 708 393 975", "tel": "+996708393975", "wa": "996708393975"},
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
.brand{display:flex;align-items:center;gap:10px;font-weight:800;font-size:20px;color:var(--navy);cursor:pointer;text-decoration:none}
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

/* 3D-МАШИНКА */
.stage{position:relative;width:100%;max-width:400px;height:290px;margin:0 auto;perspective:900px;perspective-origin:50% 38%;border-radius:24px;background:radial-gradient(circle at 50% 35%,rgba(255,255,255,.5),rgba(255,255,255,.1) 62%,rgba(10,95,163,.28));border:2px dashed rgba(255,255,255,.75);overflow:hidden;cursor:grab;touch-action:pan-y;user-select:none;-webkit-user-select:none}
.stage:active{cursor:grabbing}
.m-rot,.m-grp,.m-part{position:absolute;left:0;top:0;width:0;height:0;transform-style:preserve-3d}
.m-rot{left:50%;top:52%}
.m-face{position:absolute;backface-visibility:hidden;-webkit-backface-visibility:hidden}
.m-bob{animation:bob 1.4s ease-in-out infinite}
@keyframes bob{0%,100%{transform:translateY(0)}50%{transform:translateY(-16px)}}
.m-spin{animation:spinx 1.4s linear infinite}
@keyframes spinx{to{transform:rotateX(360deg)}}
.m-sspin{animation:spiny 2.2s linear infinite}
@keyframes spiny{to{transform:rotateY(360deg)}}
.m-feed{animation:feed 3s ease-in-out infinite alternate}
@keyframes feed{from{transform:translateX(12px)}to{transform:translateX(-16px)}}
.stage-hint{position:absolute;left:0;right:0;bottom:12px;text-align:center;font-size:12px;font-weight:600;color:rgba(255,255,255,.95);pointer-events:none;z-index:2}
.sparkle{position:absolute;font-size:18px;animation:twinkle 2.8s ease-in-out infinite;pointer-events:none}
@keyframes twinkle{0%,100%{opacity:.25;transform:scale(.75) rotate(-8deg)}50%{opacity:1;transform:scale(1.2) rotate(8deg)}}
@media(prefers-reduced-motion:reduce){.m-bob,.m-spin,.m-sspin,.m-feed,.sparkle,.cloud,.brand .logo span{animation:none}}

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
      <div class="stage" id="stage"></div>
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
    PH.map(p => '<a class="wa-opt" href="' + waLink(p.wa, text) + '" target="_blank" rel="noopener"><span class="wa-ic">' + WA_SVG + '</span><b>' + esc(p.label) + '</b><span>Открыть чат →</span></a>').join(''));
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

/* ---------- 3D-ШВЕЙНАЯ МАШИНКА (собрана из CSS-граней) ---------- */
const hsl = (c, dl) => 'hsl(' + c[0] + ',' + c[1] + '%,' + Math.max(5, Math.min(97, c[2] + dl)) + '%)';
function mkFace(w, h, tf, bg, round) {
  const f = document.createElement('div'); f.className = 'm-face';
  f.style.cssText = 'width:' + w + 'px;height:' + h + 'px;left:' + (-w / 2) + 'px;top:' + (-h / 2) + 'px;transform:' + tf + ';background:' + bg + ';' + (round ? 'border-radius:50%;' : '');
  return f;
}
function mkPart(x, y, z) { const p = document.createElement('div'); p.className = 'm-part'; p.style.transform = 'translate3d(' + x + 'px,' + y + 'px,' + z + 'px)'; return p; }
function mkGrp(cls, tf) { const g = document.createElement('div'); g.className = 'm-grp ' + (cls || ''); if (tf) g.style.transform = tf; return g; }
function mkBox(w, h, d, x, y, z, c) {
  const p = mkPart(x, y, z);
  [[w, h, 'translateZ(' + d / 2 + 'px)', 0], [w, h, 'rotateY(180deg) translateZ(' + d / 2 + 'px)', -10], [d, h, 'rotateY(90deg) translateZ(' + w / 2 + 'px)', -5],
   [d, h, 'rotateY(-90deg) translateZ(' + w / 2 + 'px)', -13], [w, d, 'rotateX(90deg) translateZ(' + h / 2 + 'px)', 9], [w, d, 'rotateX(-90deg) translateZ(' + h / 2 + 'px)', -20]
  ].forEach(f => p.appendChild(mkFace(f[0], f[1], f[2], hsl(c, f[3]))));
  return p;
}
function mkCylY(r, h, x, y, z, c, N) {
  N = N || 20; const p = mkPart(x, y, z); const w = Math.ceil(2 * Math.PI * r / N) + 1;
  for (let i = 0; i < N; i++) { const a = i * 360 / N; p.appendChild(mkFace(w, h, 'rotateY(' + a + 'deg) translateZ(' + r + 'px)', hsl(c, 14 * Math.cos((a - 40) * Math.PI / 180) - 4))); }
  p.appendChild(mkFace(2 * r, 2 * r, 'rotateX(90deg) translateZ(' + h / 2 + 'px)', hsl(c, 10), true));
  p.appendChild(mkFace(2 * r, 2 * r, 'rotateX(-90deg) translateZ(' + h / 2 + 'px)', hsl(c, -16), true));
  return p;
}
function buildMachine() {
  const stage = $('stage'); if (!stage) return;
  const BODY = [203, 80, 58], BODYD = [207, 85, 40], BASE = [205, 35, 92], STEEL = [210, 10, 82], DARK = [215, 25, 26], CORAL = [10, 85, 60], CREAM = [45, 60, 92], FAB = [350, 70, 76];
  const rot = document.createElement('div'); rot.className = 'm-rot';
  const root = mkGrp(''); rot.appendChild(root);
  const add = n => root.appendChild(n);

  const shadow = mkPart(5, 70, 0);
  shadow.appendChild(mkFace(250, 110, 'rotateX(90deg)', 'radial-gradient(rgba(5,40,80,.38),transparent 70%)'));
  shadow.appendChild(mkFace(250, 110, 'rotateX(-90deg)', 'radial-gradient(rgba(5,40,80,.38),transparent 70%)'));
  add(shadow);

  add(mkBox(210, 14, 78, 5, 60, 0, BASE));          // основание
  add(mkBox(70, 3, 50, -75, 51.5, 0, STEEL));        // игольная пластина
  add(mkBox(38, 110, 60, 78, -2, 0, BODY));          // стойка
  add(mkBox(205, 38, 60, 5, -62, 0, BODY));          // рукав
  add(mkBox(150, 4, 62, 10, -62, 0, BODYD));         // полоса на рукаве
  add(mkBox(40, 74, 54, -80, -44, 0, BODY));         // головка
  add(mkBox(10, 10, 4, 78, -28, 32, CORAL));         // регуляторы
  add(mkBox(10, 10, 4, 78, -28, -32, CORAL));
  add(mkBox(14, 4, 12, -80, 46, 0, STEEL));          // лапка

  // игла и игловодитель ходят вверх-вниз
  const needlePos = mkPart(-80, 0, 0), bob = mkGrp('m-bob');
  bob.appendChild(mkCylY(2.6, 40, 0, -10, 0, STEEL, 12));
  bob.appendChild(mkBox(9, 8, 9, 0, 6, 0, DARK));
  bob.appendChild(mkCylY(1.1, 36, 0, 28, 0, STEEL, 8));
  needlePos.appendChild(bob); add(needlePos);

  // ткань скользит под лапкой
  const fabPos = mkPart(-65, 49, 0), feed = mkGrp('m-feed');
  feed.appendChild(mkBox(80, 2, 46, 0, 0, 0, FAB));
  fabPos.appendChild(feed); add(fabPos);

  // маховик крутится
  const wheelPos = mkPart(112, -62, 0), spin = mkGrp('m-spin'), axis = mkGrp('', 'rotateZ(90deg)');
  axis.appendChild(mkCylY(22, 10, 0, 0, 0, DARK, 24));
  axis.appendChild(mkCylY(8, 12, 0, 0, 0, STEEL, 12));
  spin.appendChild(axis); spin.appendChild(mkBox(6, 6, 6, 7, -15, 0, CORAL));
  wheelPos.appendChild(spin); add(wheelPos);

  // катушка с ниткой
  add(mkCylY(2, 12, 20, -85, 0, STEEL, 8));
  const spoolPos = mkPart(20, -97, 0), sspin = mkGrp('m-sspin');
  sspin.appendChild(mkCylY(9, 22, 0, 0, 0, CORAL, 16));
  sspin.appendChild(mkCylY(12, 2, 0, -11, 0, CREAM, 16));
  sspin.appendChild(mkCylY(12, 2, 0, 11, 0, CREAM, 16));
  spoolPos.appendChild(sspin); add(spoolPos);
  const th = mkBox(102, 1.6, 1.6, -30, -87, 0, CORAL);
  th.style.transform = 'translate3d(-30px,-87px,0) rotateZ(-12.4deg)'; add(th);
  add(mkBox(1.6, 108, 1.6, -87, -18, 0, CORAL));

  stage.appendChild(rot);
  [['9%', '12%', '0s', '🧵'], ['84%', '14%', '.9s', '🪡'], ['8%', '70%', '1.6s', '✂️'], ['86%', '64%', '.4s', '✨']].forEach(a => {
    const s = document.createElement('span'); s.className = 'sparkle'; s.textContent = a[3]; s.style.left = a[0]; s.style.top = a[1]; s.style.animationDelay = a[2]; stage.appendChild(s);
  });
  const hint = document.createElement('div'); hint.className = 'stage-hint'; hint.textContent = '👆 Потяните, чтобы повернуть машинку'; stage.appendChild(hint);

  const sc = Math.min(1.25, Math.max(0.7, stage.clientWidth / 340));
  root.style.transform = 'scale(' + sc + ') translate3d(-9px,18px,0)';
  let angle = -28, tilt = -14, drag = false, lx = 0, ly = 0;
  const reduce = window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  const apply = () => { rot.style.transform = 'rotateX(' + tilt + 'deg) rotateY(' + angle + 'deg)'; };
  const tick = () => { if (!drag && !reduce) angle += 0.35; apply(); requestAnimationFrame(tick); };
  apply(); requestAnimationFrame(tick);
  stage.addEventListener('pointerdown', e => { drag = true; lx = e.clientX; ly = e.clientY; try { stage.setPointerCapture(e.pointerId); } catch (x) {} });
  stage.addEventListener('pointermove', e => { if (!drag) return; angle += (e.clientX - lx) * 0.7; tilt = Math.max(-38, Math.min(10, tilt + (e.clientY - ly) * 0.2)); lx = e.clientX; ly = e.clientY; });
  ['pointerup', 'pointercancel', 'pointerleave'].forEach(ev => stage.addEventListener(ev, () => { drag = false; }));
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
  renderAds(); renderCats(); renderShops(); saveCart(); buildMachine();
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
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.environ.get("PORT", 10000))
    site = web.TCPSite(runner, '0.0.0.0', port)
    await site.start()

async def main():
    await run_web()
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
