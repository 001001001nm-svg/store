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
#  SUPER_ADMIN_ID - Telegram ID владельца
#  DB_PATH        - путь к базе (при Persistent Disk, напр. /data/store.db)
# ============================================================
BOT_TOKEN = os.environ.get("BOT_TOKEN", "8873651066:AAHT3ma7r3vv1YRWxRwfBz8zJ74rT_Kzm20")
SUPER_ADMIN_ID = int(os.environ.get("SUPER_ADMIN_ID", "8636623905"))
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
                        category TEXT
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
                ensure_columns(conn, "products", {"unit": "TEXT", "price": "REAL", "photo": "TEXT", "discount": "TEXT", "category": "TEXT"})
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
        is_admin_val = 1 if user_id == SUPER_ADMIN_ID else 0
        with closing(sqlite3.connect(DB_PATH)) as conn:
            with conn:
                conn.execute("""
                    INSERT OR REPLACE INTO users (user_id, name, username, is_admin) 
                    VALUES (?, ?, ?, COALESCE((SELECT is_admin FROM users WHERE user_id = ?), ?))
                """, (user_id, name, username, user_id, is_admin_val))
                if user_id == SUPER_ADMIN_ID:
                    conn.execute("UPDATE users SET is_admin = 1 WHERE user_id = ?", (SUPER_ADMIN_ID,))
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
    if user_id == SUPER_ADMIN_ID:
        return True
    data = get_user(user_id)
    return bool(data and data[1] == 1)

class RegState(StatesGroup):
    waiting_for_name = State()

class OwnerState(StatesGroup):
    waiting_for_admin_input = State()

class ProductState(StatesGroup):
    waiting_for_category = State()
    waiting_for_name = State()
    waiting_for_unit = State()
    waiting_for_price = State()
    waiting_for_photo = State()

class CategoryState(StatesGroup):
    waiting_for_name = State()

class DiscountState(StatesGroup):
    waiting_for_value = State()

class AdState(StatesGroup):
    waiting_for_title = State()
    waiting_for_content = State()

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
    if not BOT_ACTIVE and message.from_user.id != SUPER_ADMIN_ID:
        await message.answer("🛠 Бот временно выключен владельцем.")
        return

    user_id = message.from_user.id
    if user_id == SUPER_ADMIN_ID:
        set_admin_status(SUPER_ADMIN_ID, 1)

    user_data = get_user(user_id)
    if not user_data:
        await message.answer("Привет! Как тебя зовут? Введи свое имя:")
        await state.set_state(RegState.waiting_for_name)
    else:
        name, is_admin = user_data
        is_owner = (user_id == SUPER_ADMIN_ID)
        kb = main_menu_kb(is_owner, is_admin == 1)
        await message.answer(f"Привет, {name}! Добро пожаловать.", reply_markup=kb)

@dp.message(RegState.waiting_for_name)
async def process_reg_name(message: types.Message, state: FSMContext):
    user_id = message.from_user.id
    name = message.text.strip()
    username = message.from_user.username or "нет юзернейма"
    register_user(user_id, name, username)
    await state.clear()
    
    is_owner = (user_id == SUPER_ADMIN_ID)
    user_data = get_user(user_id)
    is_admin = user_data[1] == 1 if user_data else 0
    kb = main_menu_kb(is_owner, is_admin)
    await message.answer(f"Приятно познакомиться, {name}!", reply_markup=kb)

@dp.message(F.text == "👑 Панель владельца")
async def owner_panel(message: types.Message, state: FSMContext = None):
    if message.from_user.id != SUPER_ADMIN_ID:
        return
    if state:
        await state.clear()
    kb = types.ReplyKeyboardMarkup(
        keyboard=[
            [types.KeyboardButton(text="🔴 Выключить бота" if BOT_ACTIVE else "🟢 Включить бота")],
            [types.KeyboardButton(text="🔴 Выключить сайт" if SITE_ACTIVE else "🟢 Включить сайт")],
            [types.KeyboardButton(text="➕ Назначить админа"), types.KeyboardButton(text="👥 Список админов")],
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
    if message.from_user.id != SUPER_ADMIN_ID:
        return
    BOT_ACTIVE = not BOT_ACTIVE
    await owner_panel(message)

@dp.message(F.text.in_(["🔴 Выключить сайт", "🟢 Включить сайт"]))
async def toggle_site(message: types.Message):
    global SITE_ACTIVE
    if message.from_user.id != SUPER_ADMIN_ID:
        return
    SITE_ACTIVE = not SITE_ACTIVE
    await owner_panel(message)

@dp.message(F.text == "🔙 Главное меню")
async def back_to_main(message: types.Message, state: FSMContext):
    await state.clear()
    user_id = message.from_user.id
    user_data = get_user(user_id)
    is_admin = user_data[1] == 1 if user_data else False
    kb = main_menu_kb(user_id == SUPER_ADMIN_ID, is_admin)
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
        [types.KeyboardButton(text="🗑 Убрать все товары")],
    ])

def products_view():
    rows = db_all("SELECT id, name, unit, price, discount, category FROM products ORDER BY id")
    items = []
    for pid, name, unit, price, disc, cat in rows:
        line = f"{name} — {price} сом/{unit or 'шт'}"
        if disc:
            line += f" (скидка {disc})"
        if cat:
            line += f" [{cat}]"
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
    await state.set_state(ProductState.waiting_for_photo)
    await message.answer(
        "Отправь фото товара или нажми «Пропустить»:",
        reply_markup=back_row_kb([[types.KeyboardButton(text="Пропустить")]])
    )

async def save_product(message: types.Message, state: FSMContext, photo):
    data = await state.get_data()
    db_run(
        "INSERT INTO products (name, unit, price, photo, discount, category) VALUES (?, ?, ?, ?, NULL, ?)",
        (data.get("name"), data.get("unit"), data.get("price"), photo, data.get("category", ""))
    )
    await state.clear()
    await message.answer(
        f"✅ Товар «{data.get('name')}» добавлен! Он уже на сайте.",
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
    rows = db_all("SELECT id, title, content FROM advertisements ORDER BY id")
    items = [(aid, f"{title} — {content}", str(title)) for aid, title, content in rows]
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
    await state.set_state(AdState.waiting_for_title)
    await message.answer("Введи заголовок рекламы:", reply_markup=back_row_kb([]))

@dp.message(AdState.waiting_for_title, F.text)
async def add_ad_title(message: types.Message, state: FSMContext):
    await state.update_data(title=message.text.strip())
    await state.set_state(AdState.waiting_for_content)
    await message.answer("Введи текст рекламы:")

@dp.message(AdState.waiting_for_content, F.text)
async def add_ad_content(message: types.Message, state: FSMContext):
    data = await state.get_data()
    db_run(
        "INSERT INTO advertisements (title, content, photo) VALUES (?, ?, NULL)",
        (data.get("title"), message.text.strip())
    )
    await state.clear()
    await message.answer("✅ Реклама добавлена и уже на сайте.", reply_markup=ads_menu_kb())

# ============================================================
#  👑 АДМИНЫ (панель владельца)
# ============================================================
def admins_view():
    rows = db_all("SELECT user_id, name, username FROM users WHERE is_admin = 1 AND user_id != ?", (SUPER_ADMIN_ID,))
    items = [(uid, f"{name} (@{username}) — {uid}", str(name)) for uid, name, username in rows]
    return build_list("👥 Админы (нажми ❌, чтобы снять права):", items, "deladm", "Других админов пока нет.")

@dp.message(F.text == "➕ Назначить админа")
async def add_admin_start(message: types.Message, state: FSMContext):
    if message.from_user.id != SUPER_ADMIN_ID:
        return
    await state.set_state(OwnerState.waiting_for_admin_input)
    await message.answer("Введи Telegram ID нового админа (числом):")

@dp.message(OwnerState.waiting_for_admin_input, F.text)
async def add_admin_input(message: types.Message, state: FSMContext):
    if message.from_user.id != SUPER_ADMIN_ID:
        return
    text = message.text.strip()
    if not text.isdigit():
        await message.answer("ID должен быть числом. Попробуй ещё раз:")
        return
    uid = int(text)
    if get_user(uid):
        set_admin_status(uid, 1)
    else:
        db_run("INSERT OR IGNORE INTO users (user_id, name, username, is_admin) VALUES (?, ?, ?, 1)", (uid, "Админ", "нет юзернейма"))
    await state.clear()
    await message.answer(f"✅ Пользователь {uid} назначен админом.")

@dp.message(F.text == "👥 Список админов")
async def admins_list(message: types.Message, state: FSMContext):
    if message.from_user.id != SUPER_ADMIN_ID:
        return
    await state.clear()
    text, kb = admins_view()
    await message.answer(text, reply_markup=kb)

@dp.callback_query(F.data.startswith("deladm:"))
async def delete_admin(call: types.CallbackQuery):
    if call.from_user.id != SUPER_ADMIN_ID:
        await call.answer("Нет доступа", show_alert=True)
        return
    set_admin_status(int(call.data.split(":")[1]), 0)
    await call.answer("Права сняты")
    text, kb = admins_view()
    await refresh_inline(call, text, kb)

# ============================================================
#  🛒 ЗАКАЗЫ (приходят с сайта, управление через бота)
# ============================================================
DELIVERY_FEE = 150
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
        f"Доставка: {fmt_price(delivery)} сом\n"
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
    ids = {SUPER_ADMIN_ID} | {r[0] for r in db_all("SELECT user_id FROM users WHERE is_admin = 1")}
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
#  ВЕБ-СЕРВЕР И КРАСИВАЯ ВИТРИНА С КОРЗИНОЙ И ДОСТАВКОЙ
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

PAGE_HEAD = """<!DOCTYPE html>
<html lang="ru">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Магазин косметики</title>
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
    <style>
        :root { --candy: #ecb4c2; --candy-soft: #fdf0f4; --candy-mid: #f6d4de; --rose: #c95b7c; --rose-dark: #a8415f; --plum: #4a2331; }
        * { box-sizing: border-box; }
        body { font-family: 'Inter', sans-serif; background-color: #fff8fa; margin: 0; padding: 0; color: var(--plum); }
        
        header { background: var(--candy); color: var(--plum); padding: 15px 30px; display: flex; justify-content: space-between; align-items: center; box-shadow: 0 4px 14px rgba(168,65,95,0.15); position: sticky; top: 0; z-index: 100; }
        .logo { font-size: 22px; font-weight: 700; display: flex; align-items: center; gap: 10px; }
        .cart-btn-header { background: #ffffff; color: var(--rose-dark); border: none; padding: 10px 18px; border-radius: 25px; font-weight: 600; cursor: pointer; display: flex; align-items: center; gap: 8px; transition: all 0.2s ease; box-shadow: 0 2px 6px rgba(0,0,0,0.08); }
        .cart-btn-header:hover { background: var(--candy-soft); transform: translateY(-1px); }
        .cart-badge { background: var(--rose); color: white; border-radius: 50%; padding: 2px 7px; font-size: 12px; font-weight: 700; display: inline-block; }
        .cart-badge.bump { animation: bump 0.35s ease; }
        @keyframes bump { 0% { transform: scale(1); } 50% { transform: scale(1.5); } 100% { transform: scale(1); } }

        .search-bar { background: #fff; padding: 15px 20px; display: flex; justify-content: center; border-bottom: 1px solid #f3dce3; }
        .search-input { width: 100%; max-width: 600px; padding: 12px 20px; border-radius: 25px; border: 1px solid #ecc9d3; outline: none; font-size: 15px; transition: border 0.2s; }
        .search-input:focus { border-color: var(--rose); }

        .container { max-width: 1200px; margin: 25px auto; padding: 0 20px; }

        /* ГЛАВНЫЙ БЛОК: 3D-ПОМАДА СЛЕВА + БАННЕРЫ */
        .hero { display: flex; gap: 20px; align-items: stretch; margin-bottom: 30px; }
        .banners { flex: 1; min-width: 0; display: flex; flex-direction: column; gap: 16px; }
        .banner { background: linear-gradient(135deg, var(--candy), #f8d9e2); color: var(--plum); border-radius: 16px; padding: 28px 30px; box-shadow: 0 6px 18px rgba(201,91,124,0.2); flex: 1; display: flex; flex-direction: column; justify-content: center; }
        .banner h2 { margin: 0 0 10px 0; font-size: 24px; font-weight: 700; }
        .banner p { margin: 0; font-size: 15px; opacity: 0.9; }

        .stage3d { flex: 0 0 200px; min-height: 260px; position: relative; perspective: 800px; border-radius: 16px; background: radial-gradient(circle at 50% 40%, #fff 0%, var(--candy-mid) 60%, var(--candy) 100%); box-shadow: 0 6px 18px rgba(201,91,124,0.2); overflow: hidden; }
        .lip-rot { position: absolute; left: 50%; top: 50%; width: 0; transform-style: preserve-3d; animation: lipspin 9s linear infinite; }
        .lip-seg { position: absolute; left: 0; width: 0; transform-style: preserve-3d; }
        .lip-face { position: absolute; top: 0; left: 0; backface-visibility: hidden; }
        .lip-cap { position: absolute; left: 0; top: 0; border-radius: 50%; transform: rotateX(90deg); }
        @keyframes lipspin { from { transform: rotateX(-16deg) rotateY(0deg); } to { transform: rotateX(-16deg) rotateY(360deg); } }
        .lip-shadow { position: absolute; left: 50%; bottom: 24px; width: 110px; height: 18px; margin-left: -55px; border-radius: 50%; background: radial-gradient(ellipse, rgba(74,35,49,0.28), rgba(74,35,49,0) 70%); }
        .sparkle { position: absolute; font-size: 16px; animation: twinkle 2.6s ease-in-out infinite; }
        @keyframes twinkle { 0%, 100% { opacity: 0.2; transform: scale(0.7); } 50% { opacity: 1; transform: scale(1.2); } }
        @media (max-width: 700px) { .hero { flex-direction: column; } .stage3d { flex: 0 0 240px; } }
        @media (prefers-reduced-motion: reduce) { .lip-rot, .sparkle { animation: none; } }

        .categories-container { display: flex; gap: 10px; margin-bottom: 26px; overflow-x: auto; padding-bottom: 8px; scrollbar-width: none; }
        .cat-pill { background: var(--candy-soft); color: var(--rose-dark); padding: 8px 18px; border-radius: 20px; font-weight: 600; font-size: 14px; white-space: nowrap; border: 1px solid var(--candy); cursor: pointer; transition: all 0.2s; user-select: none; }
        .cat-pill:hover, .cat-pill.active { background: var(--candy); color: var(--plum); }
        .cat-pill.active { box-shadow: 0 2px 8px rgba(201,91,124,0.3); }

        .section-head { display: flex; justify-content: space-between; align-items: center; margin-bottom: 20px; gap: 12px; flex-wrap: wrap; }
        h3 { font-size: 22px; color: var(--plum); margin: 0; font-weight: 700; }
        .sort-select { padding: 9px 14px; border-radius: 12px; border: 1px solid var(--candy); background: #fff; color: var(--plum); font-size: 14px; font-family: inherit; outline: none; cursor: pointer; }
        .products-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(240px, 1fr)); gap: 22px; margin-bottom: 40px; perspective: 1000px; }
        .no-results { display: none; text-align: center; color: #9a6b7a; margin: -10px 0 40px; }
        .product-card { position: relative; background: white; border-radius: 16px; padding: 18px; box-shadow: 0 4px 12px rgba(168,65,95,0.06); border: 1px solid #f8e4ea; display: flex; flex-direction: column; justify-content: space-between; transition: transform 0.25s, box-shadow 0.25s; transform-style: preserve-3d; }
        .product-card:hover { transform: translateY(-6px) rotateX(4deg) rotateY(-4deg); box-shadow: 0 14px 26px rgba(201,91,124,0.22); }
        .fav-btn { position: absolute; top: 12px; right: 12px; width: 34px; height: 34px; border-radius: 50%; border: none; background: var(--candy-soft); color: var(--rose); font-size: 18px; cursor: pointer; line-height: 1; transition: transform 0.15s; }
        .fav-btn:hover { transform: scale(1.15); }
        .fav-btn.on { background: var(--rose); color: white; }
        .product-cat { font-size: 11px; color: var(--rose-dark); background: var(--candy-soft); padding: 4px 8px; border-radius: 6px; display: inline-block; margin-bottom: 10px; font-weight: 700; text-transform: uppercase; letter-spacing: 0.5px; }
        .product-title { font-size: 16px; font-weight: 600; margin-bottom: 8px; color: var(--plum); line-height: 1.3; padding-right: 30px; }
        .product-price { font-size: 20px; color: var(--rose-dark); font-weight: 700; margin-bottom: 15px; display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
        .old-price { font-size: 14px; color: #a58a93; font-weight: 500; text-decoration: line-through; }
        .product-discount { background: var(--rose); color: white; padding: 2px 6px; border-radius: 6px; font-size: 12px; font-weight: 700; }
        .add-btn { background: var(--rose); color: white; border: none; padding: 10px; border-radius: 10px; cursor: pointer; font-weight: 600; width: 100%; transition: background 0.2s; }
        .add-btn:hover { background: var(--rose-dark); }

        /* КОРЗИНА И САЙДБАР */
        .cart-overlay { position: fixed; top: 0; left: 0; width: 100%; height: 100%; background: rgba(74,35,49,0.4); display: none; z-index: 1000; backdrop-filter: blur(2px); }
        .cart-sidebar { position: fixed; top: 0; right: -420px; width: 100%; max-width: 420px; height: 100%; background: white; z-index: 1001; transition: right 0.3s ease; display: flex; flex-direction: column; box-shadow: -6px 0 20px rgba(0,0,0,0.15); }
        .cart-sidebar.open { right: 0; }
        .cart-overlay.open { display: block; }

        .cart-header { padding: 20px; border-bottom: 1px solid #f8e4ea; display: flex; justify-content: space-between; align-items: center; background: var(--candy-soft); }
        .cart-header h3 { margin: 0; font-size: 20px; }
        .close-cart { background: none; border: none; font-size: 24px; cursor: pointer; color: var(--rose-dark); }

        .cart-items { flex: 1; overflow-y: auto; padding: 15px 20px; min-height: 80px; }
        .cart-item { display: flex; justify-content: space-between; align-items: center; margin-bottom: 15px; padding-bottom: 15px; border-bottom: 1px solid #f8e4ea; }
        .cart-item-title { font-weight: 600; font-size: 14px; margin-bottom: 4px; color: var(--plum); }
        .cart-item-price { color: var(--rose-dark); font-weight: 700; font-size: 14px; }
        .cart-controls { display: flex; align-items: center; gap: 8px; }
        .qty-btn { background: var(--candy-soft); color: var(--rose-dark); border: none; width: 26px; height: 26px; border-radius: 6px; font-weight: bold; cursor: pointer; }

        .cart-footer { padding: 20px; border-top: 1px solid #f8e4ea; background: #fffafb; max-height: 70vh; overflow-y: auto; }
        .cart-summary-line { display: flex; justify-content: space-between; margin-bottom: 8px; font-size: 14px; color: #8a6573; }
        .cart-summary-total { display: flex; justify-content: space-between; margin-top: 12px; font-size: 18px; font-weight: 700; color: var(--plum); border-top: 1px solid #f3dce3; padding-top: 10px; }
        .form-title { font-size: 13px; font-weight: 700; margin: 14px 0 8px; color: var(--plum); }
        .form-input { width: 100%; padding: 10px 12px; border: 1px solid #ecc9d3; border-radius: 10px; font-size: 14px; margin-bottom: 8px; outline: none; font-family: inherit; background: white; }
        .form-input:focus { border-color: var(--rose); }
        .form-error { color: #d93a5f; font-size: 13px; margin-top: 4px; }
        .checkout-btn { background: var(--rose); color: white; border: none; width: 100%; padding: 14px; border-radius: 12px; font-weight: 700; font-size: 16px; margin-top: 12px; cursor: pointer; transition: background 0.2s; }
        .checkout-btn:hover { background: var(--rose-dark); }
        .checkout-btn:disabled { opacity: 0.6; cursor: wait; }

        /* ОКНО С ИНФОРМАЦИЕЙ */
        .modal-overlay { position: fixed; inset: 0; background: rgba(74,35,49,0.5); display: none; align-items: center; justify-content: center; z-index: 2000; padding: 20px; }
        .modal-overlay.open { display: flex; }
        .modal { background: white; color: var(--plum); max-width: 520px; width: 100%; border-radius: 16px; padding: 26px; max-height: 85vh; overflow-y: auto; box-shadow: 0 20px 40px rgba(0,0,0,0.25); }
        .modal h3 { margin: 0 0 14px 0; }
        .modal p { line-height: 1.6; font-size: 15px; color: #6b4a57; white-space: pre-line; margin: 0 0 18px 0; }
        .modal button { background: var(--rose); color: white; border: none; padding: 10px 22px; border-radius: 10px; font-weight: 600; cursor: pointer; }

        .toast { position: fixed; bottom: 24px; left: 50%; transform: translateX(-50%) translateY(20px); background: var(--plum); color: white; padding: 12px 20px; border-radius: 14px; font-size: 14px; opacity: 0; transition: all 0.3s; z-index: 3000; pointer-events: none; max-width: 90%; text-align: center; }
        .toast.show { opacity: 1; transform: translateX(-50%) translateY(0); }

        footer { background: var(--candy); color: var(--plum); padding: 40px 20px; margin-top: 60px; }
        .footer-content { max-width: 1200px; margin: 0 auto; display: flex; justify-content: space-between; flex-wrap: wrap; gap: 30px; }
        .footer-col h4 { margin-top: 0; margin-bottom: 15px; font-size: 16px; font-weight: 700; }
        .footer-col p, .footer-col a { font-size: 14px; color: var(--plum); text-decoration: none; display: block; margin-bottom: 8px; }
        .footer-col a { cursor: pointer; }
        .footer-col a:hover { text-decoration: underline; }
        .footer-bottom { text-align: center; margin-top: 30px; font-size: 13px; opacity: 0.8; border-top: 1px solid rgba(74,35,49,0.2); padding-top: 15px; }
    </style>
</head>
<body>
    <header>
        <div class="logo">💄 Магазин косметики</div>
        <button class="cart-btn-header" onclick="openCart()">
            🛍 Корзина <span class="cart-badge" id="cartBadge">0</span>
        </button>
    </header>

    <div class="search-bar">
        <input type="text" class="search-input" placeholder="Поиск косметики..." onkeyup="filterProducts(this.value)">
    </div>

    <div class="container">
"""

PAGE_MIDDLE = r"""
        </div>
        <p class="no-results" id="noResults">Ничего не найдено 💄</p>
    </div>

    <!-- КОРЗИНА -->
    <div class="cart-overlay" id="cartOverlay" onclick="closeCart()"></div>
    <div class="cart-sidebar" id="cartSidebar">
        <div class="cart-header">
            <h3>Корзина</h3>
            <button class="close-cart" onclick="closeCart()">&times;</button>
        </div>

        <div class="cart-items" id="cartItems">
            <p style="text-align: center; color: #b595a1; margin-top: 40px;">Корзина пуста</p>
        </div>

        <div class="cart-footer">
            <div class="cart-summary-line">
                <span>Товары:</span>
                <span id="subtotalPrice">0 сом</span>
            </div>
            <div class="cart-summary-line">
                <span>Доставка:</span>
                <span id="deliveryPrice">0 сом</span>
            </div>
            <div class="cart-summary-total">
                <span>Итого:</span>
                <span id="totalPrice">0 сом</span>
            </div>

            <div class="form-title">Данные для доставки</div>
            <input class="form-input" id="custName" type="text" placeholder="Ваше имя" maxlength="50" autocomplete="name">
            <input class="form-input" id="custPhone" type="tel" placeholder="Телефон: +996 700 245 781" maxlength="20" autocomplete="tel">
            <input class="form-input" id="custAddress" type="text" placeholder="Адрес доставки (улица, дом, квартира)" maxlength="200" autocomplete="street-address">
            <div class="form-error" id="formError"></div>

            <button class="checkout-btn" id="checkoutBtn" onclick="checkout()">Оформить заказ</button>
        </div>
    </div>

    <!-- ОКНО С ИНФОРМАЦИЕЙ -->
    <div class="modal-overlay" id="infoModal" onclick="if(event.target===this) closeModal()">
        <div class="modal">
            <h3 id="modalTitle"></h3>
            <p id="modalText"></p>
            <button onclick="closeModal()">Закрыть</button>
        </div>
    </div>

    <div class="toast" id="toast"></div>

    <footer>
        <div class="footer-content">
            <div class="footer-col">
                <h4>Магазин косметики</h4>
                <p>Всё для красоты и ухода: косметика, уход за кожей и волосами, парфюмерия и аксессуары. Быстрая доставка прямо к вашей двери.</p>
            </div>
            <div class="footer-col">
                <h4>Информация</h4>
                <a onclick="showInfo('about')">О нас</a>
                <a onclick="showInfo('order')">Как сделать заказ</a>
                <a onclick="showInfo('delivery')">Доставка и оплата</a>
            </div>
            <div class="footer-col">
                <h4>Контакты</h4>
                <p>📞 +7 (900) 000-00-00</p>
                <p>📍 Доставка по городу</p>
            </div>
        </div>
        <div class="footer-bottom">
            © 2026 Магазин косметики. Все права защищены.
        </div>
    </footer>

    <script>
        const DELIVERY_FEE = __DELIVERY__;
        let cart = [];
        const state = { q: '', cat: '', fav: false };

        function loadJSON(key, fallback) {
            try { return JSON.parse(localStorage.getItem(key)) || fallback; } catch (e) { return fallback; }
        }
        function saveJSON(key, value) {
            try { localStorage.setItem(key, JSON.stringify(value)); } catch (e) {}
        }
        let favs = loadJSON('favs', []);

        const INFO = {
            about: ['О нас', 'Магазин косметики — это место, где собрано всё для красоты и ухода: средства для лица, тела и волос, декоративная косметика, парфюмерия и приятные аксессуары.\n\nМы подбираем товары, которые нужны каждый день и которые приятно дарить, чтобы вам не приходилось искать их по разным магазинам. Ассортимент постоянно пополняется, а заказ мы привозим прямо к вашей двери.'],
            order: ['Как сделать заказ', '1. Найдите нужный товар через поиск или выберите каталог.\n2. Нажмите «В корзину» на карточке товара. Понравившееся можно отметить сердечком ♡.\n3. Откройте корзину, проверьте количество и укажите имя, номер телефона и адрес доставки.\n4. Нажмите «Оформить заказ» — мы получим его сразу и свяжемся с вами для подтверждения.'],
            delivery: ['Доставка и оплата', `Мы доставляем заказы по городу прямо к двери. Стоимость доставки — ${DELIVERY_FEE} сом.\n\nОплата производится при получении заказа. Номер телефона нужен, чтобы курьер мог с вами связаться, поэтому указывайте действующий номер.`]
        };

        function showModal(title, text) {
            document.getElementById('modalTitle').innerText = title;
            document.getElementById('modalText').innerText = text;
            document.getElementById('infoModal').classList.add('open');
        }
        function closeModal() { document.getElementById('infoModal').classList.remove('open'); }
        function showInfo(key) { showModal(INFO[key][0], INFO[key][1]); }

        let toastTimer = null;
        function showToast(text) {
            const t = document.getElementById('toast');
            t.innerText = text;
            t.classList.add('show');
            clearTimeout(toastTimer);
            toastTimer = setTimeout(() => t.classList.remove('show'), 1800);
        }

        function escapeHtml(s) {
            return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
        }
        function money(x) { return Math.round(x * 100) / 100; }

        // Проверка номера телефона Кыргызстана: +996 XXX XXX XXX
        const KG_PREFIXES = new Set(['755', '880', '990', '995', '999', '222']);
        for (let i = 500; i <= 509; i++) KG_PREFIXES.add(String(i));
        for (let i = 550; i <= 559; i++) KG_PREFIXES.add(String(i));
        for (let i = 700; i <= 709; i++) KG_PREFIXES.add(String(i));
        for (let i = 770; i <= 779; i++) KG_PREFIXES.add(String(i));

        function normalizePhone(raw) {
            let d = (raw || '').replace(/\D/g, '');
            let nine = null;
            if (d.length === 12 && d.startsWith('996')) nine = d.slice(3);
            else if (d.length === 10 && d.startsWith('0')) nine = d.slice(1);
            else if (d.length === 9) nine = d;
            if (!nine) return null;
            if (!KG_PREFIXES.has(nine.slice(0, 3))) return null;
            let rest = nine.slice(3);
            if (/^(\d)\1+$/.test(rest)) return null;
            if ('0123456789'.includes(rest) || '9876543210'.includes(rest)) return null;
            return '+996' + nine;
        }

        // ---------- 3D-ПОМАДА ----------
        function buildLipstick() {
            const stage = document.getElementById('lipstick');
            if (!stage) return;
            const N = 28;
            const parts = [
                { r: 30, h: 80, c: [335, 28, 24] },
                { r: 31, h: 9,  c: [43, 70, 62] },
                { r: 27, h: 36, c: [43, 62, 52] },
                { r: 22, h: 44, c: [346, 62, 70] },
                { r: 19, h: 7,  c: [346, 62, 72] },
                { r: 15, h: 6,  c: [346, 62, 74] },
                { r: 9,  h: 5,  c: [346, 62, 76] }
            ];
            const total = parts.reduce((s, p) => s + p.h, 0);
            const rot = document.createElement('div');
            rot.className = 'lip-rot';
            rot.style.height = total + 'px';
            rot.style.marginTop = (-total / 2) + 'px';
            let bottom = 0;
            parts.forEach(p => {
                const seg = document.createElement('div');
                seg.className = 'lip-seg';
                seg.style.bottom = bottom + 'px';
                seg.style.height = p.h + 'px';
                const w = Math.ceil(2 * Math.PI * p.r / N) + 1;
                for (let i = 0; i < N; i++) {
                    const a = i * 360 / N;
                    const light = Math.max(8, Math.min(92, p.c[2] + 16 * Math.cos((a - 50) * Math.PI / 180) - 4));
                    const f = document.createElement('div');
                    f.className = 'lip-face';
                    f.style.width = w + 'px';
                    f.style.height = p.h + 'px';
                    f.style.marginLeft = (-w / 2) + 'px';
                    f.style.background = `hsl(${p.c[0]}, ${p.c[1]}%, ${light}%)`;
                    f.style.transform = `rotateY(${a}deg) translateZ(${p.r}px)`;
                    seg.appendChild(f);
                }
                const cap = document.createElement('div');
                cap.className = 'lip-cap';
                cap.style.width = (2 * p.r) + 'px';
                cap.style.height = (2 * p.r) + 'px';
                cap.style.marginLeft = (-p.r) + 'px';
                cap.style.marginTop = (-p.r) + 'px';
                cap.style.background = `hsl(${p.c[0]}, ${p.c[1]}%, ${Math.min(90, p.c[2] + 10)}%)`;
                seg.appendChild(cap);
                rot.appendChild(seg);
                bottom += p.h;
            });
            const shadow = document.createElement('div');
            shadow.className = 'lip-shadow';
            stage.appendChild(shadow);
            stage.appendChild(rot);
            [['12%', '14%', '0s'], ['78%', '22%', '0.9s'], ['18%', '70%', '1.6s'], ['80%', '64%', '0.4s']].forEach(([l, t, d]) => {
                const s = document.createElement('span');
                s.className = 'sparkle';
                s.innerText = '✨';
                s.style.left = l; s.style.top = t; s.style.animationDelay = d;
                stage.appendChild(s);
            });
        }

        // ---------- ФИЛЬТРЫ, ПОИСК, СОРТИРОВКА, ИЗБРАННОЕ ----------
        function applyFilters() {
            const grid = document.getElementById('productsGrid');
            if (!grid) return;
            const cards = Array.from(grid.querySelectorAll('.product-card'));
            const sortSel = document.getElementById('sortSel');
            const sort = sortSel ? sortSel.value : 'default';
            let visible = 0;
            cards.forEach(card => {
                const okQ = !state.q || card.dataset.title.includes(state.q);
                const okC = !state.cat || card.dataset.cat === state.cat;
                const okF = !state.fav || favs.includes(Number(card.dataset.id));
                const show = okQ && okC && okF;
                card.style.display = show ? 'flex' : 'none';
                if (show) visible++;
            });
            cards.sort((a, b) => {
                if (sort === 'cheap') return a.dataset.price - b.dataset.price;
                if (sort === 'expensive') return b.dataset.price - a.dataset.price;
                return a.dataset.order - b.dataset.order;
            }).forEach(c => grid.appendChild(c));
            const nr = document.getElementById('noResults');
            if (nr) nr.style.display = (cards.length && !visible) ? 'block' : 'none';
            document.querySelectorAll('.cat-pill').forEach(p => {
                const isFav = p.dataset.kind === 'fav';
                p.classList.toggle('active', isFav ? state.fav : (!state.fav && p.dataset.cat === state.cat));
            });
            const fc = document.getElementById('favCount');
            if (fc) fc.innerText = favs.length ? '(' + favs.length + ')' : '';
        }

        function filterProducts(query) { state.q = (query || '').trim().toLowerCase(); applyFilters(); }
        function filterCat(cat) { state.cat = cat || ''; state.fav = false; applyFilters(); }
        function filterFav() { state.fav = true; state.cat = ''; applyFilters(); }

        function toggleFav(id, btn) {
            const i = favs.indexOf(id);
            if (i >= 0) favs.splice(i, 1); else favs.push(id);
            saveJSON('favs', favs);
            btn.classList.toggle('on', favs.includes(id));
            btn.innerText = favs.includes(id) ? '♥' : '♡';
            applyFilters();
        }

        function initFavs() {
            document.querySelectorAll('.fav-btn').forEach(btn => {
                const on = favs.includes(Number(btn.dataset.id));
                btn.classList.toggle('on', on);
                btn.innerText = on ? '♥' : '♡';
            });
        }

        // ---------- КОРЗИНА ----------
        function syncCartWithPage() {
            cart = loadJSON('cart', []).map(i => {
                if (!i || !Number.isInteger(i.id) || !(i.qty > 0)) return null;
                const card = document.querySelector('.product-card[data-id="' + i.id + '"]');
                if (!card) return null;
                return { id: i.id, name: card.dataset.name, price: parseFloat(card.dataset.price), unit: card.dataset.unit, qty: Math.min(99, i.qty) };
            }).filter(Boolean);
        }

        function addToCart(id, name, price, unit) {
            let item = cart.find(i => i.id === id);
            if (item) {
                if (item.qty < 99) item.qty++;
            } else {
                cart.push({ id, name, price, unit, qty: 1 });
            }
            updateCart();
            showToast('Добавлено в корзину: ' + name);
            const badge = document.getElementById('cartBadge');
            badge.classList.remove('bump');
            void badge.offsetWidth;
            badge.classList.add('bump');
        }

        function changeQty(id, delta) {
            let item = cart.find(i => i.id === id);
            if (item) {
                item.qty = Math.min(99, item.qty + delta);
                if (item.qty <= 0) {
                    cart = cart.filter(i => i.id !== id);
                }
            }
            updateCart();
        }

        function updateCart() {
            let cartItemsDiv = document.getElementById('cartItems');
            let badge = document.getElementById('cartBadge');
            let totalQty = cart.reduce((sum, item) => sum + item.qty, 0);
            let subtotal = money(cart.reduce((sum, item) => sum + (item.price * item.qty), 0));
            let deliveryFee = subtotal === 0 ? 0 : DELIVERY_FEE;

            saveJSON('cart', cart);
            badge.innerText = totalQty;
            document.getElementById('subtotalPrice').innerText = subtotal + ' сом';
            document.getElementById('deliveryPrice').innerText = deliveryFee + ' сом';
            document.getElementById('totalPrice').innerText = money(subtotal + deliveryFee) + ' сом';

            if (cart.length === 0) {
                cartItemsDiv.innerHTML = '<p style="text-align: center; color: #b595a1; margin-top: 40px;">Корзина пуста</p>';
                return;
            }

            cartItemsDiv.innerHTML = '';
            cart.forEach(item => {
                cartItemsDiv.innerHTML += `
                    <div class="cart-item">
                        <div>
                            <div class="cart-item-title">${escapeHtml(item.name)}</div>
                            <div class="cart-item-price">${money(item.price * item.qty)} сом</div>
                        </div>
                        <div class="cart-controls">
                            <button class="qty-btn" onclick="changeQty(${item.id}, -1)">-</button>
                            <span>${item.qty}</span>
                            <button class="qty-btn" onclick="changeQty(${item.id}, 1)">+</button>
                        </div>
                    </div>
                `;
            });
        }

        function openCart() {
            document.getElementById('cartSidebar').classList.add('open');
            document.getElementById('cartOverlay').classList.add('open');
        }

        function closeCart() {
            document.getElementById('cartSidebar').classList.remove('open');
            document.getElementById('cartOverlay').classList.remove('open');
        }

        async function checkout() {
            const err = document.getElementById('formError');
            const btn = document.getElementById('checkoutBtn');
            err.innerText = '';

            if (cart.length === 0) { err.innerText = 'Корзина пуста!'; return; }

            const name = document.getElementById('custName').value.trim();
            const phone = normalizePhone(document.getElementById('custPhone').value);
            const address = document.getElementById('custAddress').value.trim();

            if (name.length < 2 || !/[A-Za-zА-Яа-яЁёҢңӨөҮү]/.test(name)) { err.innerText = 'Введите ваше имя.'; return; }
            if (!phone) { err.innerText = 'Введите настоящий номер телефона, например +996 700 245 781.'; return; }
            if (address.length < 5) { err.innerText = 'Введите адрес доставки.'; return; }

            btn.disabled = true;
            try {
                const res = await fetch('/order', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ name, phone, address, items: cart.map(i => ({ id: i.id, qty: i.qty })) })
                });
                const data = await res.json();
                if (data.ok) {
                    cart = [];
                    updateCart();
                    document.getElementById('custName').value = '';
                    document.getElementById('custPhone').value = '';
                    document.getElementById('custAddress').value = '';
                    closeCart();
                    showModal('Спасибо за заказ!', 'Ваш заказ №' + data.id + ' принят. Мы свяжемся с вами по номеру ' + phone + ' для подтверждения.');
                } else {
                    err.innerText = data.error || 'Не удалось оформить заказ.';
                }
            } catch (e) {
                err.innerText = 'Не удалось отправить заказ. Попробуйте ещё раз.';
            } finally {
                btn.disabled = false;
            }
        }

        buildLipstick();
        initFavs();
        syncCartWithPage();
        updateCart();
        applyFilters();
    </script>
</body>
</html>""".replace("__DELIVERY__", str(DELIVERY_FEE))

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
    global SITE_ACTIVE
    if not SITE_ACTIVE:
        # ВАЖНО: в aiohttp charset нельзя писать внутри content_type — это и давало ошибку 500
        return web.Response(text="🛠 Сайт временно отключен владельцем.", status=503, content_type="text/plain", charset="utf-8")

    ads = []
    categories = []
    products = []

    try:
        with closing(sqlite3.connect(DB_PATH)) as conn:
            cur = conn.cursor()
            try:
                cur.execute("SELECT title, content FROM advertisements")
                ads = cur.fetchall()
            except: pass
            try:
                cur.execute("SELECT name FROM categories")
                categories = [row[0] for row in cur.fetchall()]
            except: pass
            try:
                cur.execute("SELECT id, name, unit, price, photo, discount, category FROM products")
                products = cur.fetchall()
            except: pass
    except Exception as e:
        logging.error(f"Error reading DB for site: {e}")

    parts = [PAGE_HEAD]

    # 3D-помада слева + баннеры справа
    parts.append('<div class="hero"><div class="stage3d" id="lipstick"></div><div class="banners">')
    if ads:
        for title, content in ads:
            parts.append(f"""
            <div class="banner">
                <h2>💖 {esc(title)}</h2>
                <p>{esc(content)}</p>
            </div>
            """)
    else:
        parts.append("""
        <div class="banner">
            <h2>💖 Красота начинается здесь</h2>
            <p>Косметика и уход для лица, волос и тела. Заказывайте онлайн — привезём прямо к вам домой.</p>
        </div>
        """)
    parts.append('</div></div>')

    parts.append('<div class="categories-container"><div class="cat-pill" data-cat="" onclick="filterCat(\'\')">Все товары</div>')
    parts.append('<div class="cat-pill" data-kind="fav" onclick="filterFav()">♥ Избранное <span id="favCount"></span></div>')
    for cat in categories:
        parts.append(f'<div class="cat-pill" data-cat="{esc(cat)}" onclick="filterCat(this.dataset.cat)">{esc(cat)}</div>')
    parts.append('</div>')

    parts.append("""<div class="section-head"><h3>Популярные товары</h3>
        <select class="sort-select" id="sortSel" onchange="applyFilters()">
            <option value="default">Сортировка: по умолчанию</option>
            <option value="cheap">Сначала дешевле</option>
            <option value="expensive">Сначала дороже</option>
        </select></div>""")
    parts.append('<div class="products-grid" id="productsGrid">')
    if products:
        for order_no, (p_id, name, unit, price, photo, discount, cat) in enumerate(products):
            final_price = effective_price(price, discount)
            has_discount = final_price < round(float(price or 0), 2)
            disc_badge = f'<span class="product-discount">-{esc(discount)}</span>' if (discount and has_discount) else ''
            old_price = f'<span class="old-price">{fmt_price(price)} сом</span>' if has_discount else ''
            cat_label = f'<div class="product-cat">{esc(cat or "Общее")}</div>'
            unit_text = unit or "шт"
            price_text = fmt_price(final_price)
            parts.append(f"""
                <div class="product-card" data-id="{int(p_id)}" data-order="{order_no}" data-price="{price_text}" data-name="{esc(name)}" data-unit="{esc(unit_text)}" data-title="{esc(str(name or '').lower())}" data-cat="{esc(cat or '')}">
                    <button class="fav-btn" data-id="{int(p_id)}" onclick="toggleFav({int(p_id)}, this)" title="В избранное">♡</button>
                    <div>
                        {cat_label}
                        <div class="product-title">{esc(name)}</div>
                        <div class="product-price">{price_text} сом {old_price} {disc_badge}</div>
                    </div>
                    <button class="add-btn" onclick="addToCart({int(p_id)}, {js_str(name)}, {price_text}, {js_str(unit_text)})">В корзину ({esc(unit_text)})</button>
                </div>
            """)
    else:
        parts.append('<p style="grid-column: 1/-1; text-align: center; color: #9a6b7a;">В магазине пока нет товаров. Добавьте их через Telegram-бота.</p>')

    parts.append(PAGE_MIDDLE)

    return web.Response(text="".join(parts), content_type="text/html", charset="utf-8")

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
