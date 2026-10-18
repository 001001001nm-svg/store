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
#  SHOP_PHONE     - телефон магазина для футера сайта
# ============================================================
BOT_TOKEN = os.environ.get("BOT_TOKEN", "8708493966:AAFfGohPh6jmyuWopT57LQVn-bQ_NNMt_7E")
SUPER_ADMIN_ID = int(os.environ.get("SUPER_ADMIN_ID", "8636623905"))
DB_PATH = os.environ.get("DB_PATH", "store.db")
SHOP_PHONE = os.environ.get("SHOP_PHONE", "+996 708 393 975")

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
    
# Наши соц сети
# instagram : @shveimag_kyrgyzstan
