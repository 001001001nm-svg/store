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
        line = f"{name} — {price} руб./{unit or 'шт'}"
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
    <title>Магазин</title>
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
    <style>
        * { box-sizing: border-box; }
        body { font-family: 'Inter', sans-serif; background-color: #f5f5fb; margin: 0; padding: 0; color: #2c3e50; }
        
        header { background: #4f46e5; color: white; padding: 15px 30px; display: flex; justify-content: space-between; align-items: center; box-shadow: 0 4px 12px rgba(0,0,0,0.1); position: sticky; top: 0; z-index: 100; }
        .logo { font-size: 22px; font-weight: 700; display: flex; align-items: center; gap: 10px; }
        .cart-btn-header { background: #ffffff; color: #4f46e5; border: none; padding: 10px 18px; border-radius: 25px; font-weight: 600; cursor: pointer; display: flex; align-items: center; gap: 8px; transition: all 0.2s ease; box-shadow: 0 2px 6px rgba(0,0,0,0.1); }
        .cart-btn-header:hover { background: #eef2ff; transform: translateY(-1px); }
        .cart-badge { background: #e74c3c; color: white; border-radius: 50%; padding: 2px 7px; font-size: 12px; font-weight: 700; }

        .search-bar { background: #fff; padding: 15px 20px; display: flex; justify-content: center; border-bottom: 1px solid #e2e8f0; }
        .search-input { width: 100%; max-width: 600px; padding: 12px 20px; border-radius: 25px; border: 1px solid #cbd5e1; outline: none; font-size: 15px; transition: border 0.2s; }
        .search-input:focus { border-color: #4f46e5; }

        .container { max-width: 1200px; margin: 25px auto; padding: 0 20px; }

        .banner { position: relative; overflow: hidden; background: linear-gradient(135deg, #4f46e5, #7c3aed); color: white; border-radius: 16px; padding: 25px 150px 25px 30px; margin-bottom: 30px; box-shadow: 0 6px 18px rgba(79,70,229,0.3); }
        .banner h2 { margin: 0 0 10px 0; font-size: 24px; font-weight: 700; }
        .banner p { margin: 0; font-size: 15px; opacity: 0.95; }

        /* 3D-КУБ В БАННЕРЕ */
        .cube-scene { position: absolute; right: 45px; top: 50%; margin-top: -45px; width: 90px; height: 90px; perspective: 500px; }
        .cube { width: 90px; height: 90px; position: relative; transform-style: preserve-3d; animation: spin 9s infinite linear; }
        .cube div { position: absolute; width: 90px; height: 90px; border: 2px solid rgba(255,255,255,0.75); background: rgba(255,255,255,0.18); display: flex; align-items: center; justify-content: center; font-size: 34px; }
        .cube .f1 { transform: translateZ(45px); }
        .cube .f2 { transform: rotateY(90deg) translateZ(45px); }
        .cube .f3 { transform: rotateY(180deg) translateZ(45px); }
        .cube .f4 { transform: rotateY(-90deg) translateZ(45px); }
        .cube .f5 { transform: rotateX(90deg) translateZ(45px); }
        .cube .f6 { transform: rotateX(-90deg) translateZ(45px); }
        @keyframes spin { from { transform: rotateX(-20deg) rotateY(0deg); } to { transform: rotateX(-20deg) rotateY(360deg); } }
        @media (max-width: 600px) { .cube-scene { display: none; } .banner { padding-right: 30px; } }

        .categories-container { display: flex; gap: 10px; margin-bottom: 30px; overflow-x: auto; padding-bottom: 8px; scrollbar-width: none; }
        .cat-pill { background: #eef2ff; color: #4f46e5; padding: 8px 18px; border-radius: 20px; font-weight: 600; font-size: 14px; white-space: nowrap; border: 1px solid #c7d2fe; cursor: pointer; transition: all 0.2s; }
        .cat-pill:hover { background: #4f46e5; color: white; }

        h3 { font-size: 22px; color: #1e293b; margin-bottom: 20px; font-weight: 700; }
        .products-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(240px, 1fr)); gap: 22px; margin-bottom: 40px; perspective: 1000px; }
        .product-card { background: white; border-radius: 16px; padding: 18px; box-shadow: 0 4px 12px rgba(0,0,0,0.03); border: 1px solid #f1f5f9; display: flex; flex-direction: column; justify-content: space-between; transition: transform 0.25s, box-shadow 0.25s; transform-style: preserve-3d; }
        .product-card:hover { transform: translateY(-6px) rotateX(4deg) rotateY(-4deg); box-shadow: 0 14px 26px rgba(79,70,229,0.18); }
        .product-cat { font-size: 11px; color: #4f46e5; background: #eef2ff; padding: 4px 8px; border-radius: 6px; display: inline-block; margin-bottom: 10px; font-weight: 700; text-transform: uppercase; letter-spacing: 0.5px; }
        .product-title { font-size: 16px; font-weight: 600; margin-bottom: 8px; color: #1e293b; line-height: 1.3; }
        .product-price { font-size: 20px; color: #4f46e5; font-weight: 700; margin-bottom: 15px; display: flex; align-items: center; gap: 8px; }
        .product-discount { background: #e67e22; color: white; padding: 2px 6px; border-radius: 6px; font-size: 12px; font-weight: 700; }
        .add-btn { background: #4f46e5; color: white; border: none; padding: 10px; border-radius: 10px; cursor: pointer; font-weight: 600; width: 100%; transition: background 0.2s; }
        .add-btn:hover { background: #3730a3; }

        /* КОРЗИНА И САЙДБАР */
        .cart-overlay { position: fixed; top: 0; left: 0; width: 100%; height: 100%; background: rgba(0,0,0,0.4); display: none; z-index: 1000; backdrop-filter: blur(2px); }
        .cart-sidebar { position: fixed; top: 0; right: -420px; width: 100%; max-width: 420px; height: 100%; background: white; z-index: 1001; transition: right 0.3s ease; display: flex; flex-direction: column; box-shadow: -6px 0 20px rgba(0,0,0,0.15); }
        .cart-sidebar.open { right: 0; }
        .cart-overlay.open { display: block; }

        .cart-header { padding: 20px; border-bottom: 1px solid #f1f5f9; display: flex; justify-content: space-between; align-items: center; }
        .cart-header h3 { margin: 0; font-size: 20px; }
        .close-cart { background: none; border: none; font-size: 24px; cursor: pointer; color: #64748b; }

        /* БАННЕР БЕСПЛАТНОЙ ДОСТАВКИ В КОРЗИНЕ */
        .delivery-promo { background: linear-gradient(135deg, #fff3cd, #ffe8a1); border: 1px solid #ffe082; padding: 14px 18px; margin: 15px 20px 5px 20px; border-radius: 12px; font-size: 13px; color: #856404; font-weight: 600; box-shadow: 0 2px 6px rgba(0,0,0,0.03); }
        .delivery-progress-bar { background: #ffe082; height: 6px; border-radius: 3px; margin-top: 8px; overflow: hidden; }
        .delivery-progress-fill { background: #27ae60; height: 100%; width: 0%; transition: width 0.3s; }

        .cart-items { flex: 1; overflow-y: auto; padding: 15px 20px; }
        .cart-item { display: flex; justify-content: space-between; align-items: center; margin-bottom: 15px; padding-bottom: 15px; border-bottom: 1px solid #f1f5f9; }
        .cart-item-title { font-weight: 600; font-size: 14px; margin-bottom: 4px; color: #1e293b; }
        .cart-item-price { color: #4f46e5; font-weight: 700; font-size: 14px; }
        .cart-controls { display: flex; align-items: center; gap: 8px; }
        .qty-btn { background: #f1f5f9; border: none; width: 26px; height: 26px; border-radius: 6px; font-weight: bold; cursor: pointer; }

        .cart-footer { padding: 20px; border-top: 1px solid #f1f5f9; background: #fafafa; }
        .cart-summary-line { display: flex; justify-content: space-between; margin-bottom: 8px; font-size: 14px; color: #64748b; }
        .cart-summary-total { display: flex; justify-content: space-between; margin-top: 12px; font-size: 18px; font-weight: 700; color: #1e293b; border-top: 1px solid #e2e8f0; padding-top: 10px; }
        .checkout-btn { background: #4f46e5; color: white; border: none; width: 100%; padding: 14px; border-radius: 12px; font-weight: 700; font-size: 16px; margin-top: 15px; cursor: pointer; transition: background 0.2s; }
        .checkout-btn:hover { background: #3730a3; }

        footer { background: #4f46e5; color: white; padding: 40px 20px; margin-top: 60px; }
        .footer-content { max-width: 1200px; margin: 0 auto; display: flex; justify-content: space-between; flex-wrap: wrap; gap: 30px; }
        .footer-col h4 { margin-top: 0; margin-bottom: 15px; font-size: 16px; font-weight: 700; }
        .footer-col p, .footer-col a { font-size: 14px; opacity: 0.9; color: white; text-decoration: none; display: block; margin-bottom: 8px; }
        .footer-bottom { text-align: center; margin-top: 30px; font-size: 13px; opacity: 0.8; border-top: 1px solid rgba(255,255,255,0.2); padding-top: 15px; }
    </style>
</head>
<body>
    <header>
        <div class="logo">🛍 Магазин</div>
        <button class="cart-btn-header" onclick="openCart()">
            🛒 Корзина <span class="cart-badge" id="cartBadge">0</span>
        </button>
    </header>

    <div class="search-bar">
        <input type="text" class="search-input" placeholder="Поиск в магазине..." onkeyup="filterProducts(this.value)">
    </div>

    <div class="container">
"""

CUBE_HTML = """<div class="cube-scene"><div class="cube">
<div class="f1">🛍</div><div class="f2">📦</div><div class="f3">🎁</div>
<div class="f4">🛒</div><div class="f5">⭐</div><div class="f6">🚚</div>
</div></div>"""

PAGE_MIDDLE = """
        </div>
    </div>

    <!-- КОРЗИНА -->
    <div class="cart-overlay" id="cartOverlay" onclick="closeCart()"></div>
    <div class="cart-sidebar" id="cartSidebar">
        <div class="cart-header">
            <h3>Корзина</h3>
            <button class="close-cart" onclick="closeCart()">&times;</button>
        </div>

        <!-- БАННЕР БЕСПЛАТНОЙ ДОСТАВКИ -->
        <div class="delivery-promo" id="deliveryPromo">
            🚚 Заказ от <b>5000 руб.</b> — доставка <span style="color: #27ae60; font-weight:700;">БЕСПЛАТНО!</span>
            <div class="delivery-progress-bar">
                <div class="delivery-progress-fill" id="deliveryProgressFill"></div>
            </div>
            <div id="deliveryPromoText" style="margin-top: 5px; font-size: 11px; opacity: 0.9;">Добавьте товаров для бесплатной доставки!</div>
        </div>

        <div class="cart-items" id="cartItems">
            <p style="text-align: center; color: #94a3b8; margin-top: 40px;">Корзина пуста</p>
        </div>

        <div class="cart-footer">
            <div class="cart-summary-line">
                <span>Товары:</span>
                <span id="subtotalPrice">0 руб.</span>
            </div>
            <div class="cart-summary-line">
                <span>Доставка:</span>
                <span id="deliveryPrice">150 руб.</span>
            </div>
            <div class="cart-summary-total">
                <span>Итого:</span>
                <span id="totalPrice">150 руб.</span>
            </div>
            <button class="checkout-btn" onclick="checkout()">Оформить заказ</button>
        </div>
    </div>

    <footer>
        <div class="footer-content">
            <div class="footer-col">
                <h4>Магазин</h4>
                <p>Все товары прямо к вашей двери. Быстрая доставка и лучшая цена.</p>
            </div>
            <div class="footer-col">
                <h4>Покупателям</h4>
                <a href="#">О нас</a>
                <a href="#">Как сделать заказ</a>
                <a href="#">Доставка и оплата</a>
            </div>
            <div class="footer-col">
                <h4>Контакты</h4>
                <p>📞 +7 (900) 000-00-00</p>
                <p>📍 Доставка по всей РФ</p>
            </div>
        </div>
        <div class="footer-bottom">
            © 2026 Магазин. Все права защищены.
        </div>
    </footer>

    <script>
        let cart = [];

        function addToCart(id, name, price, unit) {
            let item = cart.find(i => i.id === id);
            if (item) {
                item.qty++;
            } else {
                cart.push({ id, name, price, unit, qty: 1 });
            }
            updateCart();
            openCart();
        }

        function changeQty(id, delta) {
            let item = cart.find(i => i.id === id);
            if (item) {
                item.qty += delta;
                if (item.qty <= 0) {
                    cart = cart.filter(i => i.id !== id);
                }
            }
            updateCart();
        }

        function updateCart() {
            let cartItemsDiv = document.getElementById('cartItems');
            let badge = document.getElementById('cartBadge');
            let subtotalSpan = document.getElementById('subtotalPrice');
            let deliverySpan = document.getElementById('deliveryPrice');
            let totalSpan = document.getElementById('totalPrice');
            let progressFill = document.getElementById('deliveryProgressFill');
            let promoText = document.getElementById('deliveryPromoText');

            let totalQty = cart.reduce((sum, item) => sum + item.qty, 0);
            let subtotal = cart.reduce((sum, item) => sum + (item.price * item.qty), 0);
            subtotal = Math.round(subtotal * 100) / 100;
            
            badge.innerText = totalQty;

            // Расчет доставки +150 руб. если менее 5000 руб.
            let deliveryFee = subtotal >= 5000 || subtotal === 0 ? 0 : 150;
            let total = subtotal + deliveryFee;

            // Обновление прогресс-бара доставки
            let progress = Math.min((subtotal / 5000) * 100, 100);
            progressFill.style.width = progress + '%';

            if (subtotal >= 5000) {
                promoText.innerHTML = "🎉 У вас <b>БЕСПЛАТНАЯ ДОСТАВКА!</b>";
                deliverySpan.innerHTML = "<span style='color:#27ae60; font-weight:bold;'>Бесплатно</span>";
            } else if (subtotal > 0) {
                let diff = Math.round((5000 - subtotal) * 100) / 100;
                promoText.innerHTML = `Добавьте товаров еще на <b>${diff} руб.</b> до бесплатной доставки!`;
                deliverySpan.innerText = "+150 руб.";
            } else {
                promoText.innerText = "Добавьте товаров для бесплатной доставки!";
                deliverySpan.innerText = "150 руб.";
            }

            subtotalSpan.innerText = subtotal + " руб.";
            totalSpan.innerText = (subtotal === 0 ? 0 : total) + " руб.";

            if (cart.length === 0) {
                cartItemsDiv.innerHTML = '<p style="text-align: center; color: #94a3b8; margin-top: 40px;">Корзина пуста</p>';
                return;
            }

            cartItemsDiv.innerHTML = '';
            cart.forEach(item => {
                cartItemsDiv.innerHTML += `
                    <div class="cart-item">
                        <div>
                            <div class="cart-item-title">${item.name}</div>
                            <div class="cart-item-price">${Math.round(item.price * item.qty * 100) / 100} руб.</div>
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

        function filterProducts(query) {
            let cards = document.querySelectorAll('.product-card');
            cards.forEach(card => {
                let title = card.getAttribute('data-title');
                if (title.includes(query.toLowerCase())) {
                    card.style.display = 'flex';
                } else {
                    card.style.display = 'none';
                }
            });
        }

        function filterCat(cat) {
            let cards = document.querySelectorAll('.product-card');
            cards.forEach(card => {
                let cardCat = card.getAttribute('data-cat');
                if (!cat || cardCat === cat) {
                    card.style.display = 'flex';
                } else {
                    card.style.display = 'none';
                }
            });
        }

        function checkout() {
            if (cart.length === 0) {
                alert("Корзина пуста!");
                return;
            }
            alert("Спасибо за заказ! Наш менеджер свяжется с вами.");
        }
    </script>
</body>
</html>"""

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

    if ads:
        for title, content in ads:
            parts.append(f"""
            <div class="banner">
                {CUBE_HTML}
                <h2>🔥 {esc(title)}</h2>
                <p>{esc(content)}</p>
            </div>
            """)
    else:
        parts.append(f"""
        <div class="banner">
            {CUBE_HTML}
            <h2>🎉 Товары с доставкой!</h2>
            <p>Заказывайте любимые товары онлайн. Бесплатная доставка при заказе от 5000 руб.!</p>
        </div>
        """)

    if categories:
        parts.append('<div class="categories-container"><div class="cat-pill" data-cat="" onclick="filterCat(\'\')">Все товары</div>')
        for cat in categories:
            parts.append(f'<div class="cat-pill" data-cat="{esc(cat)}" onclick="filterCat(this.dataset.cat)">{esc(cat)}</div>')
        parts.append('</div>')

    parts.append('<h3>Популярные товары</h3><div class="products-grid" id="productsGrid">')
    if products:
        for p_id, name, unit, price, photo, discount, cat in products:
            disc_badge = f'<span class="product-discount">-{esc(discount)}</span>' if discount else ''
            cat_label = f'<div class="product-cat">{esc(cat or "Общее")}</div>'
            unit_text = unit or "шт"
            price_text = fmt_price(price)
            parts.append(f"""
                <div class="product-card" data-title="{esc(str(name or '').lower())}" data-cat="{esc(cat or '')}">
                    <div>
                        {cat_label}
                        <div class="product-title">{esc(name)}</div>
                        <div class="product-price">{price_text} руб. {disc_badge}</div>
                    </div>
                    <button class="add-btn" onclick="addToCart({int(p_id)}, {js_str(name)}, {price_text}, {js_str(unit_text)})">В корзину ({esc(unit_text)})</button>
                </div>
            """)
    else:
        parts.append('<p style="grid-column: 1/-1; text-align: center; color: #64748b;">В магазине пока нет товаров. Добавьте их через Telegram-бота.</p>')

    parts.append(PAGE_MIDDLE)

    return web.Response(text="".join(parts), content_type="text/html", charset="utf-8")

async def run_web():
    app = web.Application()
    app.router.add_get('/', handle_website)
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
