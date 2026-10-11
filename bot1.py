import asyncio
import html
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
BOT_TOKEN = os.environ["BOT_TOKEN"]
SUPER_ADMIN_ID = int(os.environ.get("SUPER_ADMIN_ID", "8636623905"))
DB_PATH = os.environ.get("DB_PATH", "store.db")
DEFAULT_PHONE = "+996 (555) 00-00-00"
DEFAULT_ADDRESS = "г. Бишкек, Кыргызстан"

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())


def esc(text) -> str:
    return html.escape(str(text if text is not None else ""))


# ============================================================
#  БАЗА ДАННЫХ
# ============================================================
def query(sql, params=(), one=False):
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.row_factory = sqlite3.Row
        cur = conn.execute(sql, params)
        result = cur.fetchone() if one else cur.fetchall()
        conn.commit()
        return result


def execute(sql, params=()):
    with closing(sqlite3.connect(DB_PATH)) as conn:
        cur = conn.execute(sql, params)
        conn.commit()
        return cur.rowcount


def init_db():
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                name TEXT,
                username TEXT,
                is_admin INTEGER DEFAULT 0,
                banned INTEGER DEFAULT 0
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
        conn.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        """)
        # миграция: колонка banned для старых баз
        cols = [r[1] for r in conn.execute("PRAGMA table_info(users)")]
        if "banned" not in cols:
            conn.execute("ALTER TABLE users ADD COLUMN banned INTEGER DEFAULT 0")
        pcols = [r[1] for r in conn.execute("PRAGMA table_info(products)")]
        if "featured" not in pcols:
            conn.execute("ALTER TABLE products ADD COLUMN featured INTEGER DEFAULT 0")
        acols = [r[1] for r in conn.execute("PRAGMA table_info(advertisements)")]
        if "badge" not in acols:
            conn.execute("ALTER TABLE advertisements ADD COLUMN badge TEXT")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created TEXT DEFAULT CURRENT_TIMESTAMP,
                name TEXT,
                phone TEXT,
                address TEXT,
                items TEXT,
                total REAL
            )
        """)
        conn.commit()


init_db()


def get_flag(key, default=True) -> bool:
    row = query("SELECT value FROM settings WHERE key = ?", (key,), one=True)
    if row is None:
        return default
    return row["value"] == "1"


def set_flag(key, value: bool):
    execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, "1" if value else "0"),
    )


def get_text(key, default=""):
    row = query("SELECT value FROM settings WHERE key = ?", (key,), one=True)
    return row["value"] if row else default


def set_text(key, value):
    execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


def insert(sql, params=()):
    with closing(sqlite3.connect(DB_PATH)) as conn:
        cur = conn.execute(sql, params)
        conn.commit()
        return cur.lastrowid


def cur() -> str:
    """Валюта сайта (меняется владельцем в боте)."""
    return get_text("currency", "₽")


# ---------- временные (демо) каталоги и товары ----------
DEMO_CATEGORIES = ["Напитки", "Сладости", "Снеки", "Кофе и чай",
                   "Молочные продукты", "Хлеб и выпечка", "Фрукты и овощи"]

# (каталог, название, единица, цена в ₽, код картинки, эмодзи, цвет фона, скидка, выбор редакции)
DEMO_PRODUCTS = [
    ("Напитки", "Газировка Кола 1.5 л", "шт", 119, "cola", "🥤", "#fde2e2", None, 0),
    ("Напитки", "Сок апельсиновый 1 л", "шт", 109, "orange-juice", "🧃", "#ffe8c7", "10%", 1),
    ("Напитки", "Вода питьевая 1.5 л", "шт", 45, "water", "💧", "#d9efff", None, 0),
    ("Сладости", "Шоколад молочный 90 г", "шт", 89, "chocolate", "🍫", "#f1ddd0", None, 1),
    ("Сладости", "Печенье овсяное 300 г", "шт", 79, "cookies", "🍪", "#f8e8c8", "15%", 0),
    ("Сладости", "Конфеты ассорти", "100 г", 65, "candy", "🍬", "#fbdcec", None, 0),
    ("Снеки", "Чипсы сметана и зелень 140 г", "шт", 129, "chips", "🍟", "#fff1b8", None, 0),
    ("Снеки", "Сухарики ржаные 100 г", "шт", 59, "croutons", "🥖", "#f3e3c3", None, 0),
    ("Снеки", "Орешки солёные 150 г", "шт", 119, "nuts", "🥜", "#efd9bd", "10%", 0),
    ("Кофе и чай", "Кофе молотый 250 г", "шт", 349, "coffee", "☕", "#e8d8cc", None, 1),
    ("Кофе и чай", "Чай чёрный 100 пакетиков", "шт", 189, "black-tea", "🫖", "#e6dccf", None, 0),
    ("Кофе и чай", "Чай зелёный 25 пакетиков", "шт", 99, "green-tea", "🍵", "#dcefd5", None, 0),
    ("Молочные продукты", "Молоко 3.2% 1 л", "шт", 94, "milk", "🥛", "#e4eefb", None, 0),
    ("Молочные продукты", "Кефир 1% 900 мл", "шт", 85, "kefir", "🥛", "#e9f0f7", None, 0),
    ("Молочные продукты", "Сыр Гауда", "100 г", 119, "cheese", "🧀", "#fff0b3", "15%", 1),
    ("Молочные продукты", "Йогурт клубничный 125 г", "шт", 49, "yogurt", "🍓", "#fde0e6", None, 0),
    ("Хлеб и выпечка", "Хлеб пшеничный", "шт", 45, "bread", "🍞", "#f6e3c4", None, 0),
    ("Хлеб и выпечка", "Батон нарезной", "шт", 52, "baguette", "🥖", "#f4e0bd", None, 0),
    ("Хлеб и выпечка", "Круассан", "шт", 69, "croissant", "🥐", "#fbe2b5", "10%", 0),
    ("Фрукты и овощи", "Яблоки", "КГ", 139, "apples", "🍎", "#fddede", None, 1),
    ("Фрукты и овощи", "Бананы", "КГ", 119, "bananas", "🍌", "#fff4b0", None, 0),
    ("Фрукты и овощи", "Картофель", "КГ", 49, "potato", "🥔", "#efe0cb", None, 0),
    ("Фрукты и овощи", "Помидоры", "КГ", 229, "tomatoes", "🍅", "#fdd9d3", None, 0),
    ("Фрукты и овощи", "Огурцы", "КГ", 179, "cucumbers", "🥒", "#dcf0d3", None, 0),
]
DEMO_STYLE = {p[4]: (p[5], p[6]) for p in DEMO_PRODUCTS}


def demo_svg(slug) -> str:
    emoji, bg = DEMO_STYLE.get(slug, ("🛒", "#e3f4ec"))
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 400 400">'
        '<defs><linearGradient id="g" x1="0" y1="0" x2="1" y2="1">'
        f'<stop offset="0" stop-color="{bg}"/><stop offset="1" stop-color="#ffffff"/>'
        '</linearGradient></defs>'
        '<rect width="400" height="400" fill="url(#g)"/>'
        '<circle cx="200" cy="210" r="125" fill="#ffffff" opacity=".6"/>'
        f'<text x="200" y="262" font-size="150" text-anchor="middle">{emoji}</text>'
        '</svg>'
    )


def seed_demo() -> int:
    """Добавляет демо-каталоги и товары. Уже добавленные повторно не создаются."""
    for cat in DEMO_CATEGORIES:
        execute("INSERT OR IGNORE INTO categories (name) VALUES (?)", (cat,))
    added = 0
    for cat, name, unit, price, slug, _emoji, _bg, disc, feat in DEMO_PRODUCTS:
        photo = "demo:" + slug
        if query("SELECT 1 FROM products WHERE photo = ?", (photo,), one=True):
            continue
        execute(
            "INSERT INTO products (name, unit, price, photo, discount, category, featured) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (name, unit, price, photo, disc, cat, feat),
        )
        added += 1
    return added


def get_user(user_id):
    return query("SELECT * FROM users WHERE user_id = ?", (user_id,), one=True)


def is_admin_user(user_id) -> bool:
    if user_id == SUPER_ADMIN_ID:
        return True
    row = get_user(user_id)
    return bool(row and row["is_admin"] == 1 and row["banned"] == 0)


def is_banned(user_id) -> bool:
    row = get_user(user_id)
    return bool(row and row["banned"] == 1)


def register_user(user_id, name, username):
    execute(
        "INSERT INTO users (user_id, name, username, is_admin) VALUES (?, ?, ?, ?) "
        "ON CONFLICT(user_id) DO UPDATE SET name = excluded.name, username = excluded.username",
        (user_id, name, username, 1 if user_id == SUPER_ADMIN_ID else 0),
    )


def fmt_user(row) -> str:
    uname = f" (@{esc(row['username'])})" if row["username"] else ""
    return f"<b>{esc(row['name'])}</b>{uname} — ID: <code>{row['user_id']}</code>"


def final_price(price, discount):
    """Цена с учётом скидки: '10%' (процент) или '50₽' / '50сом' (сумма)."""
    if not discount:
        return price
    m = re.match(r"^\s*([0-9]+(?:\.[0-9]+)?)\s*(.*)$", str(discount))
    if not m:
        return price
    value, suffix = float(m.group(1)), m.group(2).strip()
    if suffix == "%":
        return round(price * (1 - value / 100), 2)
    return max(round(price - value, 2), 0)


def parse_number(text):
    try:
        value = float(text.strip().replace(",", "."))
    except (ValueError, AttributeError):
        return None
    return value if value > 0 else None


# ============================================================
#  СОСТОЯНИЯ
# ============================================================
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
    waiting_for_badge = State()
    waiting_for_photo = State()


class ContactsState(StatesGroup):
    waiting_for_phone = State()
    waiting_for_address = State()


# ============================================================
#  ФИЛЬТРЫ И ПРОВЕРКА ДОСТУПА
# ============================================================
class IsAdmin(BaseFilter):
    async def __call__(self, event) -> bool:
        user = getattr(event, "from_user", None)
        return bool(user) and is_admin_user(user.id)


class IsOwner(BaseFilter):
    async def __call__(self, event) -> bool:
        user = getattr(event, "from_user", None)
        return bool(user) and user.id == SUPER_ADMIN_ID


class AccessMiddleware(BaseMiddleware):
    """Блокирует забаненных и всех, кроме владельца, когда бот выключен."""

    async def __call__(self, handler, event, data):
        user = data.get("event_from_user")
        if user and user.id != SUPER_ADMIN_ID:
            if is_banned(user.id):
                return
            if not get_flag("bot_active"):
                text = "🛠 Бот временно выключен владельцем."
                if isinstance(event, types.Message):
                    await event.answer(text)
                elif isinstance(event, types.CallbackQuery):
                    await event.answer(text, show_alert=True)
                return
        return await handler(event, data)


dp.message.outer_middleware(AccessMiddleware())
dp.callback_query.outer_middleware(AccessMiddleware())


def main_menu_kb(user_id):
    kb = []
    if is_admin_user(user_id):
        kb.append([types.KeyboardButton(text="📦 Управление товарами"),
                   types.KeyboardButton(text="📁 Каталоги")])
        kb.append([types.KeyboardButton(text="🏷 Скидки"),
                   types.KeyboardButton(text="📢 Реклама")])
    if user_id == SUPER_ADMIN_ID:
        kb.append([types.KeyboardButton(text="👑 Панель владельца")])
    if not kb:
        return types.ReplyKeyboardRemove()
    return types.ReplyKeyboardMarkup(keyboard=kb, resize_keyboard=True)


# ============================================================
#  СТАРТ И РЕГИСТРАЦИЯ
# ============================================================
@dp.message(CommandStart())
async def cmd_start(message: types.Message, state: FSMContext):
    await state.clear()
    user = get_user(message.from_user.id)
    if not user:
        await message.answer("Привет! Как тебя зовут? Введи свое имя:")
        await state.set_state(RegState.waiting_for_name)
        return
    await message.answer(
        f"Привет, {esc(user['name'])}! Добро пожаловать.",
        reply_markup=main_menu_kb(message.from_user.id),
        parse_mode="HTML",
    )


@dp.message(F.text == "🔙 Главное меню")
async def back_to_main(message: types.Message, state: FSMContext):
    await state.clear()
    if not get_user(message.from_user.id):
        await message.answer("Нажмите /start для регистрации.")
        return
    await message.answer("Главное меню:", reply_markup=main_menu_kb(message.from_user.id))


@dp.message(RegState.waiting_for_name, F.text)
async def process_reg_name(message: types.Message, state: FSMContext):
    name = message.text.strip()[:64]
    if not name:
        await message.answer("Введите имя текстом:")
        return
    register_user(message.from_user.id, name, message.from_user.username or "")
    await state.clear()
    await message.answer(
        f"Приятно познакомиться, {esc(name)}!",
        reply_markup=main_menu_kb(message.from_user.id),
        parse_mode="HTML",
    )


# ============================================================
#  ПАНЕЛЬ ВЛАДЕЛЬЦА
# ============================================================
@dp.message(F.text == "👑 Панель владельца", IsOwner())
async def owner_panel(message: types.Message):
    bot_active = get_flag("bot_active")
    site_active = get_flag("site_active")
    kb = types.ReplyKeyboardMarkup(
        keyboard=[
            [types.KeyboardButton(text="🔴 Выключить бота" if bot_active else "🟢 Включить бота")],
            [types.KeyboardButton(text="🔴 Выключить сайт" if site_active else "🟢 Включить сайт")],
            [types.KeyboardButton(text="➕ Назначить админа"),
             types.KeyboardButton(text="👥 Список админов")],
            [types.KeyboardButton(text="🚫 Забаненные"),
             types.KeyboardButton(text="📞 Контакты сайта")],
            [types.KeyboardButton(text="🧪 Загрузить демо-товары"),
             types.KeyboardButton(text="💱 Валюта сайта")],
            [types.KeyboardButton(text="🧹 Убрать все товары")],
            [types.KeyboardButton(text="🔙 Главное меню")],
        ],
        resize_keyboard=True,
    )
    await message.answer(
        "👑 Панель владельца:\n\n"
        f"Статус бота: {'Включен 🟢' if bot_active else 'Выключен 🔴'}\n"
        f"Статус сайта: {'Включен 🟢' if site_active else 'Выключен 🔴'}",
        reply_markup=kb,
    )


@dp.message(F.text.in_(["🔴 Выключить бота", "🟢 Включить бота"]), IsOwner())
async def toggle_bot(message: types.Message):
    set_flag("bot_active", not get_flag("bot_active"))
    await owner_panel(message)


@dp.message(F.text.in_(["🔴 Выключить сайт", "🟢 Включить сайт"]), IsOwner())
async def toggle_site(message: types.Message):
    set_flag("site_active", not get_flag("site_active"))
    await owner_panel(message)


@dp.message(F.text == "➕ Назначить админа", IsOwner())
async def make_admin_button(message: types.Message, state: FSMContext):
    users = query("SELECT * FROM users WHERE banned = 0")
    if not users:
        await message.answer("Пользователей в базе нет.")
        return

    text = "👥 <b>Все участники бота:</b>\n\n"
    for u in users:
        role = "Админ 🟢" if u["is_admin"] == 1 else "Пользователь"
        line = f"• {fmt_user(u)} ({role})\n"
        if len(text) + len(line) > 3800:
            text += "…\n"
            break
        text += line
    text += "\n✍️ <b>Напишите ID или @username для назначения администратором:</b>"
    await message.answer(text, parse_mode="HTML")
    await state.set_state(OwnerState.waiting_for_admin_input)


@dp.message(OwnerState.waiting_for_admin_input, F.text, IsOwner())
async def process_admin_input(message: types.Message, state: FSMContext):
    user_input = message.text.strip().lstrip("@")
    if user_input.isdigit():
        target = get_user(int(user_input))
    else:
        target = query(
            "SELECT * FROM users WHERE LOWER(username) = LOWER(?)", (user_input,), one=True
        )

    if target:
        execute("UPDATE users SET is_admin = 1 WHERE user_id = ?", (target["user_id"],))
        await message.answer(
            f"✅ {fmt_user(target)} назначен администратором!", parse_mode="HTML"
        )
        try:
            await bot.send_message(
                target["user_id"],
                "🎉 Вам выданы права администратора! Нажмите /start для открытия меню.",
            )
        except Exception as e:
            logging.warning("Не удалось уведомить нового админа: %s", e)
    else:
        await message.answer("❌ Пользователь не найден (он должен сначала нажать /start в боте).")

    await state.clear()
    await owner_panel(message)


@dp.message(F.text == "👥 Список админов", IsOwner())
async def manage_admins(message: types.Message):
    admins = query("SELECT * FROM users WHERE is_admin = 1 AND banned = 0")
    text = "👥 <b>Список администраторов:</b>\n\n"
    buttons = []
    for a in admins:
        text += f"• {fmt_user(a)}\n"
        if a["user_id"] != SUPER_ADMIN_ID:
            buttons.append([types.InlineKeyboardButton(
                text=f"Снять админа: {a['name']}", callback_data=f"unadmin_{a['user_id']}")])
            buttons.append([types.InlineKeyboardButton(
                text=f"Забанить: {a['name']}", callback_data=f"ban_{a['user_id']}")])
    markup = types.InlineKeyboardMarkup(inline_keyboard=buttons) if buttons else None
    await message.answer(text, parse_mode="HTML", reply_markup=markup)


@dp.callback_query(F.data.startswith(("unadmin_", "ban_")), IsOwner())
async def admin_actions(callback: types.CallbackQuery):
    action, target_id = callback.data.split("_", 1)
    target_id = int(target_id)
    if target_id == SUPER_ADMIN_ID:
        await callback.answer("Владельца нельзя изменить.", show_alert=True)
        return
    if action == "unadmin":
        execute("UPDATE users SET is_admin = 0 WHERE user_id = ?", (target_id,))
        await callback.answer("Админ снят!")
    else:
        execute("UPDATE users SET banned = 1, is_admin = 0 WHERE user_id = ?", (target_id,))
        await callback.answer("Пользователь забанен!")
    await callback.message.edit_text("Список обновлен.")


@dp.message(F.text == "🚫 Забаненные", IsOwner())
async def list_banned(message: types.Message):
    banned = query("SELECT * FROM users WHERE banned = 1")
    if not banned:
        await message.answer("Забаненных нет.")
        return
    text = "🚫 <b>Забаненные:</b>\n\n"
    buttons = []
    for u in banned:
        text += f"• {fmt_user(u)}\n"
        buttons.append([types.InlineKeyboardButton(
            text=f"Разбанить: {u['name']}", callback_data=f"unban_{u['user_id']}")])
    await message.answer(
        text, parse_mode="HTML",
        reply_markup=types.InlineKeyboardMarkup(inline_keyboard=buttons),
    )


@dp.callback_query(F.data.startswith("unban_"), IsOwner())
async def unban_user(callback: types.CallbackQuery):
    target_id = int(callback.data.split("_", 1)[1])
    execute("UPDATE users SET banned = 0 WHERE user_id = ?", (target_id,))
    await callback.answer("Разбанен")
    await callback.message.edit_text("Пользователь разбанен ✅")


@dp.message(F.text == "📞 Контакты сайта", IsOwner())
async def contacts_start(message: types.Message, state: FSMContext):
    await message.answer(
        "Сейчас на сайте:\n"
        f"📞 {esc(get_text('shop_phone', DEFAULT_PHONE))}\n"
        f"📍 {esc(get_text('shop_address', DEFAULT_ADDRESS))}\n\n"
        "Введите новый номер телефона:",
        parse_mode="HTML",
        reply_markup=types.ReplyKeyboardRemove(),
    )
    await state.set_state(ContactsState.waiting_for_phone)


@dp.message(ContactsState.waiting_for_phone, F.text, IsOwner())
async def contacts_phone(message: types.Message, state: FSMContext):
    await state.update_data(phone=message.text.strip()[:32])
    await message.answer("Введите адрес магазина:")
    await state.set_state(ContactsState.waiting_for_address)


@dp.message(ContactsState.waiting_for_address, F.text, IsOwner())
async def contacts_address(message: types.Message, state: FSMContext):
    data = await state.get_data()
    set_text("shop_phone", data["phone"])
    set_text("shop_address", message.text.strip()[:200])
    await state.clear()
    await message.answer("Контакты на сайте обновлены✅")
    await owner_panel(message)


# ============================================================
#  ТОВАРЫ
# ============================================================
@dp.message(F.text == "📦 Управление товарами", IsAdmin())
async def products_menu(message: types.Message):
    kb = types.ReplyKeyboardMarkup(
        keyboard=[
            [types.KeyboardButton(text="➕ Добавить товар"),
             types.KeyboardButton(text="🗑 Снять товар")],
            [types.KeyboardButton(text="⭐ Выбор редакции"),
             types.KeyboardButton(text="📜 Весь список товаров")],
            [types.KeyboardButton(text="🧹 Убрать все товары")],
            [types.KeyboardButton(text="🔙 Главное меню")],
        ],
        resize_keyboard=True,
    )
    await message.answer("📦 Меню управления товарами:", reply_markup=kb)


@dp.message(F.text == "📜 Весь список товаров", IsAdmin())
async def list_all_products(message: types.Message):
    products = query("SELECT * FROM products ORDER BY id")
    if not products:
        await message.answer("В базе пока нет товаров.")
        return

    await message.answer(f"📜 Всего товаров: {len(products)}")
    for p in products:
        disc_text = f"\n🏷 Скидка: {esc(p['discount'])}" if p["discount"] else ""
        cat_text = f"\n📁 Каталог: {esc(p['category'])}" if p["category"] else ""
        caption = (
            f"📦 <b>{esc(p['name'])}</b>{cat_text}\n"
            f"Цена: {p['price']:g} {esc(cur())} / {esc(p['unit'])}{disc_text}"
        )
        if p["photo"] and not p["photo"].startswith("demo:"):
            try:
                await message.answer_photo(photo=p["photo"], caption=caption, parse_mode="HTML")
                continue
            except Exception as e:
                logging.warning("Фото товара %s не отправилось: %s", p["id"], e)
        await message.answer(caption, parse_mode="HTML")


@dp.message(F.text == "➕ Добавить товар", IsAdmin())
async def add_product_start(message: types.Message, state: FSMContext):
    cats = query("SELECT name FROM categories ORDER BY name")
    if not cats:
        await message.answer("❌ Сначала создайте каталог!")
        return
    rows = [[types.KeyboardButton(text=c["name"])] for c in cats]
    rows.append([types.KeyboardButton(text="🔙 Главное меню")])
    await message.answer(
        "📁 Выберите каталог:",
        reply_markup=types.ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True),
    )
    await state.set_state(ProductState.waiting_for_category)


@dp.message(ProductState.waiting_for_category, F.text)
async def process_prod_category(message: types.Message, state: FSMContext):
    category = message.text.strip()
    if not query("SELECT 1 FROM categories WHERE name = ?", (category,), one=True):
        await message.answer("Выберите каталог кнопкой из списка.")
        return
    await state.update_data(category=category)
    await message.answer("Введите название товара:", reply_markup=types.ReplyKeyboardRemove())
    await state.set_state(ProductState.waiting_for_name)


@dp.message(ProductState.waiting_for_name, F.text)
async def process_prod_name(message: types.Message, state: FSMContext):
    await state.update_data(name=message.text.strip())
    kb = types.ReplyKeyboardMarkup(
        keyboard=[[types.KeyboardButton(text="КГ"),
                   types.KeyboardButton(text="100 г"),
                   types.KeyboardButton(text="шт")]],
        resize_keyboard=True,
    )
    await message.answer("Выберите единицу измерения:", reply_markup=kb)
    await state.set_state(ProductState.waiting_for_unit)


@dp.message(ProductState.waiting_for_unit, F.text)
async def process_prod_unit(message: types.Message, state: FSMContext):
    await state.update_data(unit=message.text.strip())
    await message.answer("Введите цену товара (цифрой):", reply_markup=types.ReplyKeyboardRemove())
    await state.set_state(ProductState.waiting_for_price)


@dp.message(ProductState.waiting_for_price, F.text)
async def process_prod_price(message: types.Message, state: FSMContext):
    price = parse_number(message.text)
    if price is None:
        await message.answer("Введите корректную цену (число больше нуля):")
        return
    await state.update_data(price=price)
    await message.answer("Пришлите фото товара:")
    await state.set_state(ProductState.waiting_for_photo)


@dp.message(ProductState.waiting_for_photo, F.photo | F.document)
async def process_prod_photo(message: types.Message, state: FSMContext):
    if message.photo:
        photo_id = message.photo[-1].file_id
    elif message.document and (message.document.mime_type or "").startswith("image/"):
        photo_id = message.document.file_id
    else:
        await message.answer("Пришлите именно изображение.")
        return

    data = await state.get_data()
    execute(
        "INSERT INTO products (name, unit, price, photo, category) VALUES (?, ?, ?, ?, ?)",
        (data["name"], data["unit"], data["price"], photo_id, data["category"]),
    )
    await state.clear()
    await message.answer("Товар успешно добавлен✅")
    await products_menu(message)


@dp.message(ProductState.waiting_for_photo)
async def process_prod_photo_wrong(message: types.Message):
    await message.answer("Пришлите фото товара (как изображение).")


@dp.message(F.text == "🧹 Убрать все товары", IsAdmin())
async def clear_all_ask(message: types.Message):
    products = query("SELECT COUNT(*) AS n FROM products", one=True)["n"]
    cats = query("SELECT COUNT(*) AS n FROM categories", one=True)["n"]
    if not products and not cats:
        await message.answer("Товаров и каталогов нет.")
        return
    kb = types.InlineKeyboardMarkup(inline_keyboard=[
        [types.InlineKeyboardButton(text=f"🗑 Убрать все товары ({products})",
                                    callback_data="clear_products")],
        [types.InlineKeyboardButton(text="🗑 Товары и каталоги", callback_data="clear_all")],
        [types.InlineKeyboardButton(text="Отмена", callback_data="clear_cancel")],
    ])
    await message.answer(
        f"⚠️ Сейчас товаров: {products}, каталогов: {cats}.\n"
        "Что убрать? Это действие нельзя отменить.",
        reply_markup=kb,
    )


@dp.callback_query(F.data.in_(["clear_products", "clear_all", "clear_cancel"]), IsAdmin())
async def clear_all_action(callback: types.CallbackQuery):
    if callback.data == "clear_cancel":
        await callback.message.edit_text("Отменено.")
        await callback.answer()
        return
    execute("DELETE FROM products")
    if callback.data == "clear_all":
        execute("DELETE FROM categories")
        text = "Все товары и каталоги убраны✅"
    else:
        text = "Все товары убраны✅"
    await callback.answer("Готово")
    await callback.message.edit_text(text)


@dp.message(F.text == "🧪 Загрузить демо-товары", IsOwner())
async def load_demo(message: types.Message):
    added = seed_demo()
    set_text("demo_seeded", "1")
    await message.answer(
        f"Демо-каталоги и товары загружены✅ Добавлено товаров: {added}.\n"
        "Убрать их можно кнопкой «🧹 Убрать все товары»."
    )


CURRENCIES = {"rub": "₽", "som": "сом", "usd": "$", "kzt": "₸"}


@dp.message(F.text == "💱 Валюта сайта", IsOwner())
async def currency_menu(message: types.Message):
    rows = [[types.InlineKeyboardButton(
        text=("✅ " if sym == cur() else "") + sym, callback_data=f"cur_{code}")]
        for code, sym in CURRENCIES.items()]
    await message.answer(
        f"Сейчас цены на сайте и в боте показываются в: {cur()}\nВыберите валюту:",
        reply_markup=types.InlineKeyboardMarkup(inline_keyboard=rows),
    )


@dp.callback_query(F.data.startswith("cur_"), IsOwner())
async def currency_set(callback: types.CallbackQuery):
    sym = CURRENCIES.get(callback.data[4:])
    if not sym:
        await callback.answer()
        return
    set_text("currency", sym)
    await callback.answer("Готово")
    await callback.message.edit_text(
        f"Валюта: {sym} ✅\nСуммы в базе не пересчитываются, меняется только подпись."
    )


def featured_markup():
    products = query("SELECT id, name, featured FROM products ORDER BY id")
    rows = [[types.InlineKeyboardButton(
        text=("⭐ " if p["featured"] else "☆ ") + p["name"],
        callback_data=f"feat_{p['id']}")] for p in products]
    return types.InlineKeyboardMarkup(inline_keyboard=rows) if rows else None


@dp.message(F.text == "⭐ Выбор редакции", IsAdmin())
async def featured_list(message: types.Message):
    markup = featured_markup()
    if not markup:
        await message.answer("Нет товаров.")
        return
    await message.answer(
        "⭐ Нажмите на товар, чтобы добавить его в «Выбор редакции» или убрать оттуда:",
        reply_markup=markup,
    )


@dp.callback_query(F.data.startswith("feat_"), IsAdmin())
async def toggle_featured(callback: types.CallbackQuery):
    execute(
        "UPDATE products SET featured = CASE WHEN featured = 1 THEN 0 ELSE 1 END WHERE id = ?",
        (int(callback.data.split("_", 1)[1]),),
    )
    await callback.answer("Готово")
    await callback.message.edit_reply_markup(reply_markup=featured_markup())


@dp.message(F.text == "🗑 Снять товар", IsAdmin())
async def remove_product_list(message: types.Message):
    products = query("SELECT id, name FROM products")
    if not products:
        await message.answer("Нет товаров.")
        return
    rows = [[types.InlineKeyboardButton(text=p["name"], callback_data=f"delprod_{p['id']}")]
            for p in products]
    await message.answer(
        "Выберите товар для удаления:",
        reply_markup=types.InlineKeyboardMarkup(inline_keyboard=rows),
    )


@dp.callback_query(F.data.startswith("delprod_"), IsAdmin())
async def delete_product(callback: types.CallbackQuery):
    execute("DELETE FROM products WHERE id = ?", (int(callback.data.split("_", 1)[1]),))
    await callback.answer("Товар убран")
    await callback.message.edit_text("Товар успешно убран✅")


# ============================================================
#  КАТАЛОГИ
# ============================================================
@dp.message(F.text == "📁 Каталоги", IsAdmin())
async def categories_menu(message: types.Message):
    kb = types.ReplyKeyboardMarkup(
        keyboard=[
            [types.KeyboardButton(text="➕ Добавить каталог"),
             types.KeyboardButton(text="🗑 Убрать каталог")],
            [types.KeyboardButton(text="📂 Весь список каталогов"),
             types.KeyboardButton(text="🔙 Главное меню")],
        ],
        resize_keyboard=True,
    )
    await message.answer("📁 Управление каталогами:", reply_markup=kb)


@dp.message(F.text == "📂 Весь список каталогов", IsAdmin())
async def list_all_categories(message: types.Message):
    cats = query("SELECT name FROM categories ORDER BY name")
    if not cats:
        await message.answer("Каталогов нет.")
        return
    text = "📂 <b>Каталоги:</b>\n\n" + "\n".join(f"• {esc(c['name'])}" for c in cats)
    await message.answer(text, parse_mode="HTML")


@dp.message(F.text == "➕ Добавить каталог", IsAdmin())
async def add_cat_start(message: types.Message, state: FSMContext):
    await message.answer("Введите название каталога:", reply_markup=types.ReplyKeyboardRemove())
    await state.set_state(CategoryState.waiting_for_name)


@dp.message(CategoryState.waiting_for_name, F.text)
async def process_cat_name(message: types.Message, state: FSMContext):
    cat_name = message.text.strip()
    try:
        execute("INSERT INTO categories (name) VALUES (?)", (cat_name,))
        await message.answer("Каталог добавлен✅")
    except sqlite3.IntegrityError:
        await message.answer("Такой каталог уже есть.")
    await state.clear()
    await categories_menu(message)


@dp.message(F.text == "🗑 Убрать каталог", IsAdmin())
async def remove_cat_list(message: types.Message):
    cats = query("SELECT id, name FROM categories")
    if not cats:
        await message.answer("Нет каталогов.")
        return
    rows = [[types.InlineKeyboardButton(text=c["name"], callback_data=f"delcat_{c['id']}")]
            for c in cats]
    await message.answer(
        "Выберите каталог:",
        reply_markup=types.InlineKeyboardMarkup(inline_keyboard=rows),
    )


@dp.callback_query(F.data.startswith("delcat_"), IsAdmin())
async def delete_catalog(callback: types.CallbackQuery):
    cat_id = int(callback.data.split("_", 1)[1])
    cat = query("SELECT name FROM categories WHERE id = ?", (cat_id,), one=True)
    if not cat:
        await callback.answer("Каталог уже удалён")
        return
    count = query("SELECT COUNT(*) AS n FROM products WHERE category = ?",
                  (cat["name"],), one=True)["n"]
    if count:
        await callback.answer(
            f"В каталоге есть товары ({count} шт.). Сначала снимите их.", show_alert=True)
        return
    execute("DELETE FROM categories WHERE id = ?", (cat_id,))
    await callback.answer("Каталог убран")
    await callback.message.edit_text("Каталог убран✅")


# ============================================================
#  СКИДКИ
# ============================================================
@dp.message(F.text == "🏷 Скидки", IsAdmin())
async def discounts_menu(message: types.Message):
    kb = types.ReplyKeyboardMarkup(
        keyboard=[
            [types.KeyboardButton(text="➕ Сделать скидку"),
             types.KeyboardButton(text="🗑 Убрать скидку")],
            [types.KeyboardButton(text="🔙 Главное меню")],
        ],
        resize_keyboard=True,
    )
    await message.answer("🏷 Скидки:", reply_markup=kb)


@dp.message(F.text == "➕ Сделать скидку", IsAdmin())
async def discount_prod_list(message: types.Message):
    products = query("SELECT id, name FROM products")
    if not products:
        await message.answer("Нет товаров.")
        return
    rows = [[types.InlineKeyboardButton(text=p["name"], callback_data=f"disc_{p['id']}")]
            for p in products]
    await message.answer(
        "Выберите товар:",
        reply_markup=types.InlineKeyboardMarkup(inline_keyboard=rows),
    )


@dp.callback_query(F.data.startswith("disc_"), IsAdmin())
async def select_discount_type(callback: types.CallbackQuery, state: FSMContext):
    await state.update_data(prod_id=callback.data.split("_", 1)[1])
    kb = types.InlineKeyboardMarkup(inline_keyboard=[[
        types.InlineKeyboardButton(text="Скидка %", callback_data="dtype_percent"),
        types.InlineKeyboardButton(text="Скидка в валюте", callback_data="dtype_som"),
    ]])
    await callback.message.edit_text("Тип скидки:", reply_markup=kb)
    await callback.answer()


@dp.callback_query(F.data.startswith("dtype_"), IsAdmin())
async def process_discount_type(callback: types.CallbackQuery, state: FSMContext):
    dtype = "%" if callback.data == "dtype_percent" else cur()
    await state.update_data(dtype=dtype)
    await callback.message.answer(f"Введите значение ({dtype}):")
    await state.set_state(DiscountState.waiting_for_value)
    await callback.answer()


@dp.message(DiscountState.waiting_for_value, F.text)
async def process_discount_value(message: types.Message, state: FSMContext):
    value = parse_number(message.text)
    data = await state.get_data()
    if value is None or (data["dtype"] == "%" and value >= 100):
        await message.answer("Введите корректное число (для % — меньше 100):")
        return
    execute("UPDATE products SET discount = ? WHERE id = ?",
            (f"{value:g}{data['dtype']}", data["prod_id"]))
    await state.clear()
    await message.answer("Скидка добавлена✅")


@dp.message(F.text == "🗑 Убрать скидку", IsAdmin())
async def remove_discount_list(message: types.Message):
    products = query("SELECT id, name FROM products WHERE discount IS NOT NULL")
    if not products:
        await message.answer("Нет скидок.")
        return
    rows = [[types.InlineKeyboardButton(text=p["name"], callback_data=f"rmdisc_{p['id']}")]
            for p in products]
    await message.answer(
        "Выберите товар:",
        reply_markup=types.InlineKeyboardMarkup(inline_keyboard=rows),
    )


@dp.callback_query(F.data.startswith("rmdisc_"), IsAdmin())
async def remove_discount_action(callback: types.CallbackQuery):
    execute("UPDATE products SET discount = NULL WHERE id = ?",
            (int(callback.data.split("_", 1)[1]),))
    await callback.answer("Скидка убрана")
    await callback.message.edit_text("Скидка убрана✅")


# ============================================================
#  РЕКЛАМА
# ============================================================
@dp.message(F.text == "📢 Реклама", IsAdmin())
async def adv_menu(message: types.Message):
    kb = types.ReplyKeyboardMarkup(
        keyboard=[
            [types.KeyboardButton(text="➕ Поставить рекламу"),
             types.KeyboardButton(text="🗑 Снять рекламу")],
            [types.KeyboardButton(text="🔙 Главное меню")],
        ],
        resize_keyboard=True,
    )
    await message.answer("📢 Реклама:", reply_markup=kb)


@dp.message(F.text == "➕ Поставить рекламу", IsAdmin())
async def add_ad_start(message: types.Message, state: FSMContext):
    await message.answer(
        "Заголовок рекламы (крупный текст на баннере), например:\nСкидки до 30% на все соки и напитки!",
        reply_markup=types.ReplyKeyboardRemove(),
    )
    await state.set_state(AdState.waiting_for_title)


@dp.message(AdState.waiting_for_title, F.text)
async def process_ad_title(message: types.Message, state: FSMContext):
    await state.update_data(title=message.text.strip()[:120])
    await message.answer("Подзаголовок (мелкий текст под заголовком) или «-» чтобы пропустить:")
    await state.set_state(AdState.waiting_for_content)


@dp.message(AdState.waiting_for_content, F.text)
async def process_ad_content(message: types.Message, state: FSMContext):
    text = message.text.strip()
    await state.update_data(content="" if text == "-" else text[:250])
    await message.answer("Метка над заголовком (например: АКЦИЯ НЕДЕЛИ) или «-» чтобы пропустить:")
    await state.set_state(AdState.waiting_for_badge)


@dp.message(AdState.waiting_for_badge, F.text)
async def process_ad_badge(message: types.Message, state: FSMContext):
    text = message.text.strip()
    await state.update_data(badge=None if text == "-" else text[:30].upper())
    await message.answer("Пришлите картинку для баннера (она будет справа) или «-» чтобы пропустить:")
    await state.set_state(AdState.waiting_for_photo)


@dp.message(AdState.waiting_for_photo)
async def process_ad_photo(message: types.Message, state: FSMContext):
    photo_id = None
    if message.photo:
        photo_id = message.photo[-1].file_id
    elif not (message.text and message.text.strip() == "-"):
        await message.answer("Пришлите картинку или «-» чтобы пропустить.")
        return
    data = await state.get_data()
    execute(
        "INSERT INTO advertisements (title, content, photo, badge) VALUES (?, ?, ?, ?)",
        (data["title"], data["content"], photo_id, data["badge"]),
    )
    await state.clear()
    await message.answer("Реклама добавлена✅ Она уже на сайте.")
    await adv_menu(message)


@dp.message(F.text == "🗑 Снять рекламу", IsAdmin())
async def remove_ad_list(message: types.Message):
    ads = query("SELECT id, title FROM advertisements")
    if not ads:
        await message.answer("Нет рекламы.")
        return
    rows = [[types.InlineKeyboardButton(text=a["title"], callback_data=f"delad_{a['id']}")]
            for a in ads]
    await message.answer(
        "Выберите рекламу:",
        reply_markup=types.InlineKeyboardMarkup(inline_keyboard=rows),
    )


@dp.callback_query(F.data.startswith("delad_"), IsAdmin())
async def delete_ad(callback: types.CallbackQuery):
    execute("DELETE FROM advertisements WHERE id = ?", (int(callback.data.split("_", 1)[1]),))
    await callback.answer("Реклама убрана")
    await callback.message.edit_text("Реклама убрана✅")


# ============================================================
#  САЙТ
# ============================================================
INDEX_HTML = """<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Продуктовый Магазин</title>
<style>
  :root { --green:#2f9e6c; --green-d:#1f7a52; --green-l:#e3f4ec; --text:#1f2d27; --muted:#7a8a82;
          --bg:#f4f7f5; --card:#ffffff; --red:#ef4444; --yellow:#facc15; }
  * { box-sizing:border-box; }
  html { scroll-behavior:smooth; }
  body { margin:0; font-family:"Segoe UI",system-ui,-apple-system,Roboto,sans-serif; background:var(--bg); color:var(--text); }
  button { font-family:inherit; cursor:pointer; }
  a { color:inherit; }

  .topbar { background:var(--green); color:#fff; font-size:13px; font-weight:600; }
  .topbar .in { max-width:1200px; margin:0 auto; padding:9px 20px; display:flex; justify-content:flex-end; gap:26px; }
  .topbar a { color:#fff; text-decoration:none; opacity:.9; cursor:pointer; }
  .topbar a:hover, .topbar a.active { opacity:1; text-decoration:underline; }

  header { background:#fff; box-shadow:0 1px 0 rgba(0,0,0,.06); position:sticky; top:0; z-index:20; }
  header .in { max-width:1200px; margin:0 auto; padding:14px 20px; display:flex; align-items:center; gap:24px; }
  .logo { display:flex; align-items:center; gap:10px; font-weight:800; font-size:22px; color:var(--green-d); white-space:nowrap; cursor:pointer; }
  .search { flex:1; position:relative; }
  .search input { width:100%; border:0; background:#eef2f0; border-radius:999px; padding:13px 48px 13px 20px; font-size:15px; outline:none; color:var(--text); }
  .search input:focus { box-shadow:0 0 0 2px var(--green); }
  .search svg { position:absolute; right:18px; top:50%; transform:translateY(-50%); color:var(--muted); }
  .cartbtn { position:relative; background:none; border:0; color:var(--text); padding:6px; }
  .cartbtn .count { position:absolute; top:-4px; right:-6px; background:var(--green); color:#fff; font-size:11px; font-weight:700;
                    min-width:20px; height:20px; border-radius:999px; display:flex; align-items:center; justify-content:center; }

  main { max-width:1200px; margin:0 auto; padding:22px 20px 40px; }

  .slider { position:relative; border-radius:26px; overflow:hidden; margin-bottom:30px; }
  .track { display:flex; transition:transform .5s ease; }
  .slide { min-width:100%; min-height:270px; padding:44px 64px; display:flex; align-items:center; justify-content:space-between; gap:20px; color:#fff; }
  .slide.c0 { background:linear-gradient(120deg,#2d7f6e,#2f8f8a); }
  .slide.c1 { background:linear-gradient(120deg,#2f9e55,#1f6b47); }
  .slide.c2 { background:linear-gradient(120deg,#3b8f4f,#2a6f6b); }
  .slide.c3 { background:linear-gradient(120deg,#2a7a5a,#3d9a7a); }
  .slide .txt { max-width:580px; }
  .tag { display:inline-block; font-size:12px; font-weight:800; letter-spacing:.3px; padding:6px 12px; border-radius:6px; margin-bottom:14px; background:var(--yellow); color:#4a3b00; }
  .tag.alt { background:var(--red); color:#fff; }
  .slide h2 { margin:0 0 12px; font-size:36px; line-height:1.15; font-weight:800; }
  .slide p { margin:0; font-size:17px; color:#c8f1dc; }
  .slide img { max-height:190px; max-width:260px; object-fit:contain; filter:drop-shadow(0 8px 14px rgba(0,0,0,.25)); }
  .arrow { position:absolute; top:50%; transform:translateY(-50%); width:36px; height:36px; border-radius:50%; border:0;
           background:rgba(0,0,0,.28); color:#fff; font-size:20px; display:flex; align-items:center; justify-content:center; }
  .arrow:hover { background:rgba(0,0,0,.45); }
  .arrow.l { left:14px; } .arrow.r { right:14px; }

  h3.title { font-size:28px; margin:0 0 16px; font-weight:800; color:#2c3e36; }
  .chips { display:flex; gap:8px; overflow-x:auto; padding:2px 0 16px; }
  .chip { border:1px solid #d3dcd7; background:#fff; color:var(--text); border-radius:999px; padding:8px 16px; white-space:nowrap; font-size:14px; font-weight:600; }
  .chip.active { background:var(--green); border-color:var(--green); color:#fff; }

  .grid { display:grid; grid-template-columns:repeat(auto-fill,minmax(220px,1fr)); gap:20px; }
  .card { background:var(--card); border-radius:20px; padding:14px; box-shadow:0 2px 10px rgba(20,60,40,.06); display:flex; flex-direction:column; position:relative; transition:transform .15s; }
  .card:hover { transform:translateY(-3px); }
  .thumb { height:170px; border-radius:14px; background:#f1f5f3; display:flex; align-items:center; justify-content:center; overflow:hidden; margin-bottom:12px; font-size:54px; }
  .thumb img { width:100%; height:100%; object-fit:cover; }
  .cat { align-self:flex-start; background:var(--green-l); color:var(--green-d); font-size:11px; font-weight:700; text-transform:uppercase; letter-spacing:.4px; padding:3px 8px; border-radius:6px; margin-bottom:8px; }
  .name { font-weight:600; font-size:15px; line-height:1.3; min-height:40px; }
  .bottom { display:flex; align-items:center; justify-content:space-between; border-top:1px solid #edf1ef; margin-top:12px; padding-top:12px; }
  .price { font-weight:800; font-size:17px; }
  .price small { display:block; color:var(--muted); font-weight:500; font-size:12px; }
  .price .old { color:var(--muted); text-decoration:line-through; font-weight:500; font-size:13px; margin-left:6px; }
  .add { width:36px; height:36px; border-radius:10px; border:0; background:var(--green); color:#fff; font-size:22px; font-weight:700; line-height:1; }
  .add:hover { background:var(--green-d); }
  .badge { position:absolute; top:22px; left:22px; background:var(--red); color:#fff; font-size:12px; font-weight:800; padding:3px 8px; border-radius:8px; }
  .empty { color:var(--muted); padding:30px 0; grid-column:1/-1; }

  footer { background:linear-gradient(120deg,#2d9462,#1f7a52); color:#fff; margin-top:20px; }
  footer .in { max-width:1200px; margin:0 auto; padding:44px 20px 20px; display:grid; grid-template-columns:2fr 1.3fr 1.3fr; gap:40px; }
  footer h4 { margin:0 0 14px; font-size:18px; }
  footer .brand { display:flex; align-items:center; gap:10px; font-size:22px; font-weight:800; margin-bottom:12px; }
  footer p { margin:0; line-height:1.6; opacity:.92; }
  footer ul { list-style:none; padding:0; margin:0; display:grid; gap:10px; }
  footer li a { text-decoration:none; opacity:.92; cursor:pointer; }
  footer li a:hover { opacity:1; text-decoration:underline; }
  .contact { display:flex; align-items:center; gap:10px; margin-bottom:10px; opacity:.95; }
  .contact a { text-decoration:none; }
  .copy { text-align:center; font-size:13px; opacity:.85; padding:18px 20px 26px; }

  .overlay { position:fixed; inset:0; background:rgba(0,0,0,.4); opacity:0; pointer-events:none; transition:.2s; z-index:50; }
  .overlay.open { opacity:1; pointer-events:auto; }
  .drawer { position:fixed; top:0; right:0; height:100%; width:min(430px,100%); background:#fff; z-index:60; transform:translateX(100%); transition:transform .25s; display:flex; flex-direction:column; }
  .drawer.open { transform:none; }
  .dhead { display:flex; align-items:center; justify-content:space-between; padding:18px 20px; border-bottom:1px solid #edf1ef; font-size:20px; font-weight:800; }
  .x { background:none; border:0; font-size:26px; color:var(--muted); line-height:1; }
  .items { flex:1; overflow-y:auto; padding:8px 20px; }
  .item { display:flex; align-items:center; gap:12px; padding:12px 0; border-bottom:1px solid #f0f3f1; }
  .item .nm { flex:1; font-weight:600; font-size:14px; }
  .item .nm small { display:block; color:var(--muted); font-weight:500; margin-top:3px; }
  .qty { display:flex; align-items:center; gap:8px; }
  .qty button { width:28px; height:28px; border-radius:8px; border:1px solid #d3dcd7; background:#fff; font-size:16px; }
  .dfoot { padding:16px 20px 20px; border-top:1px solid #edf1ef; display:grid; gap:10px; }
  .total { display:flex; justify-content:space-between; font-size:18px; font-weight:800; }
  .dfoot input { border:1px solid #d3dcd7; border-radius:10px; padding:11px 12px; font-size:15px; outline:none; width:100%; font-family:inherit; }
  .dfoot input:focus { border-color:var(--green); }
  .primary { background:var(--green); color:#fff; border:0; border-radius:12px; padding:14px; font-size:16px; font-weight:700; }
  .primary:disabled { opacity:.6; }
  .msg { font-size:14px; min-height:18px; }
  .msg.err { color:var(--red); } .msg.ok { color:var(--green-d); font-weight:600; }

  .modal { position:fixed; inset:0; z-index:70; display:none; align-items:center; justify-content:center; padding:20px; background:rgba(0,0,0,.45); }
  .modal.open { display:flex; }
  .mbox { background:#fff; border-radius:18px; padding:26px; max-width:460px; width:100%; }
  .mbox h4 { margin:0 0 12px; font-size:20px; }
  .mbox p { margin:0 0 18px; line-height:1.6; color:#3b4a43; }
  .toast { position:fixed; left:50%; bottom:26px; transform:translateX(-50%) translateY(20px); background:#1f2d27; color:#fff; padding:11px 18px; border-radius:999px; font-size:14px; opacity:0; pointer-events:none; transition:.25s; z-index:80; }
  .toast.show { opacity:1; transform:translateX(-50%); }

  @media (max-width:760px) {
    .topbar .in { justify-content:center; gap:16px; font-size:12px; padding:8px 12px; }
    header .in { flex-wrap:wrap; gap:12px; padding:12px 14px; }
    .logo { font-size:18px; }
    .search { order:3; flex-basis:100%; }
    .cartbtn { margin-left:auto; }
    main { padding:16px 14px 30px; }
    .slide { padding:28px 26px; min-height:210px; }
    .slide h2 { font-size:24px; }
    .slide p { font-size:14px; }
    .slide img { display:none; }
    .arrow { display:none; }
    h3.title { font-size:22px; }
    .grid { grid-template-columns:repeat(2,1fr); gap:12px; }
    .thumb { height:130px; }
    footer .in { grid-template-columns:1fr; gap:26px; }
  }
</style>
</head>
<body>

<div class="topbar"><div class="in">
  <a id="m-new">Новинки</a>
  <a id="m-featured">Выбор редакции</a>
  <a id="m-sale">Товары со скидкой</a>
</div></div>

<header><div class="in">
  <div class="logo" id="logo">
    <svg width="30" height="30" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M5 9h14l-1.5 9a2 2 0 0 1-2 1.7H8.5a2 2 0 0 1-2-1.7L5 9z"/><path d="M9 9l3-5 3 5"/></svg>
    <span>Продуктовый Магазин</span>
  </div>
  <div class="search">
    <input id="search" type="search" placeholder="Поиск в Продуктовом Магазине...">
    <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><circle cx="11" cy="11" r="7"/><path d="M20 20l-3.5-3.5"/></svg>
  </div>
  <button class="cartbtn" id="cartbtn" aria-label="Корзина">
    <svg width="30" height="30" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 4h2l2.4 10.2a2 2 0 0 0 2 1.6h7.3a2 2 0 0 0 2-1.5L20.5 8H6"/><circle cx="9.5" cy="20" r="1.5"/><circle cx="17.5" cy="20" r="1.5"/></svg>
    <span class="count" id="count">0</span>
  </button>
</div></header>

<main>
  <div class="slider" id="slider" style="display:none">
    <div class="track" id="track"></div>
    <button class="arrow l" id="prev" aria-label="Назад">&#8249;</button>
    <button class="arrow r" id="next" aria-label="Вперёд">&#8250;</button>
  </div>
  <div class="chips" id="chips"></div>
  <h3 class="title" id="title">Популярные товары</h3>
  <div class="grid" id="grid"></div>
</main>

<footer>
  <div class="in">
    <div>
      <div class="brand">
        <svg width="28" height="28" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M5 9h14l-1.5 9a2 2 0 0 1-2 1.7H8.5a2 2 0 0 1-2-1.7L5 9z"/><path d="M9 9l3-5 3 5"/></svg>
        Продуктовый Магазин
      </div>
      <p>Продуктовый Магазин — Все свежие продукты прямо к вашей двери. Быстрая доставка, высокое качество и лучший выбор каждый день.</p>
    </div>
    <div>
      <h4>Покупателям</h4>
      <ul>
        <li><a data-info="about">О нас</a></li>
        <li><a data-info="order">Как сделать заказ</a></li>
        <li><a data-info="delivery">Доставка и оплата</a></li>
        <li><a data-info="faq">Частые вопросы (FAQ)</a></li>
      </ul>
    </div>
    <div>
      <h4>Контакты</h4>
      <div class="contact">&#9742; <a id="phone" href="#">...</a></div>
      <div class="contact">&#9679; <span id="address">...</span></div>
    </div>
  </div>
  <div class="copy" id="copy"></div>
</footer>

<div class="overlay" id="overlay"></div>
<aside class="drawer" id="drawer">
  <div class="dhead"><span>Корзина</span><button class="x" id="closecart" aria-label="Закрыть">&times;</button></div>
  <div class="items" id="items"></div>
  <div class="dfoot" id="dfoot">
    <div class="total"><span>Итого</span><span id="total">0 ₽</span></div>
    <input id="f-name" placeholder="Ваше имя" maxlength="64" autocomplete="name">
    <input id="f-phone" placeholder="Телефон" maxlength="32" inputmode="tel" autocomplete="tel">
    <input id="f-address" placeholder="Адрес доставки" maxlength="200" autocomplete="street-address">
    <div class="msg" id="msg"></div>
    <button class="primary" id="send">Оформить заказ</button>
  </div>
</aside>

<div class="modal" id="modal"><div class="mbox">
  <h4 id="mtitle"></h4><p id="mtext"></p>
  <button class="primary" id="mclose" style="width:100%">Понятно</button>
</div></div>
<div class="toast" id="toast"></div>

<script>
  let CURRENCY = "₽";
  const INFO = {
    about: ["О нас", "Продуктовый Магазин — свежие продукты с доставкой до двери. Мы работаем каждый день, чтобы вы всегда находили хорошие цены и качество."],
    order: ["Как сделать заказ", "1. Выберите товары и нажмите «+». 2. Откройте корзину и укажите имя, телефон и адрес. 3. Нажмите «Оформить заказ» — мы свяжемся с вами для подтверждения."],
    delivery: ["Доставка и оплата", "Доставка по городу. Оплата при получении. Условия доставки и акции смотрите в баннерах на главной странице."],
    faq: ["Частые вопросы", "Как быстро привезут? Время доставки уточнит оператор при подтверждении заказа. Можно ли изменить заказ? Да, позвоните нам по номеру из контактов."]
  };
  const TITLES = { all: "Популярные товары", new: "Новинки", featured: "Выбор редакции", sale: "Товары со скидкой" };

  let products = [], ads = [], cart = {}, mode = "all", category = "all", q = "", slide = 0, timer = null, sending = false;
  const $ = s => document.querySelector(s);

  function el(tag, cls, text) {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text !== undefined) e.textContent = text;
    return e;
  }
  function money(n) { return Number(n).toLocaleString("ru-RU", { maximumFractionDigits: 2 }) + " " + CURRENCY; }
  function discText(d) { return String(d).endsWith("%") ? d : parseFloat(d) + " " + CURRENCY; }
  function toast(t) {
    const x = $("#toast"); x.textContent = t; x.classList.add("show");
    clearTimeout(x._t); x._t = setTimeout(() => x.classList.remove("show"), 1600);
  }

  function loadCart() { try { cart = JSON.parse(localStorage.getItem("cart") || "{}") || {}; } catch (e) { cart = {}; } }
  function saveCart() { try { localStorage.setItem("cart", JSON.stringify(cart)); } catch (e) {} }
  function find(id) { return products.find(p => p.id === Number(id)); }
  function cartCount() { return Object.values(cart).reduce((a, b) => a + b, 0); }

  function addToCart(id) { cart[id] = Math.min((cart[id] || 0) + 1, 99); saveCart(); renderCart(); toast("Добавлено в корзину"); }
  function changeQty(id, d) {
    cart[id] = (cart[id] || 0) + d;
    if (cart[id] <= 0) delete cart[id];
    if (cart[id] > 99) cart[id] = 99;
    saveCart(); renderCart();
  }

  function renderCart() {
    $("#count").textContent = cartCount();
    const box = $("#items"); box.innerHTML = "";
    let total = 0;
    const ids = Object.keys(cart).filter(id => find(id));
    ids.forEach(id => {
      const p = find(id), qty = cart[id];
      total += p.final_price * qty;
      const row = el("div", "item");
      const nm = el("div", "nm", p.name);
      nm.appendChild(el("small", "", money(p.final_price) + " / " + p.unit));
      const qb = el("div", "qty");
      const minus = el("button", "", "−"); minus.onclick = () => changeQty(id, -1);
      const plus = el("button", "", "+"); plus.onclick = () => changeQty(id, 1);
      qb.append(minus, el("span", "", String(qty)), plus);
      row.append(nm, qb);
      box.appendChild(row);
    });
    if (!ids.length) box.appendChild(el("div", "empty", "Корзина пуста"));
    $("#total").textContent = money(total);
    $("#dfoot").style.display = ids.length ? "grid" : "none";
  }

  function openCart(open) {
    $("#drawer").classList.toggle("open", open);
    $("#overlay").classList.toggle("open", open);
    if (open) { $("#msg").textContent = ""; $("#msg").className = "msg"; renderCart(); }
  }

  async function sendOrder() {
    if (sending) return;
    const msg = $("#msg");
    const name = $("#f-name").value.trim(), phone = $("#f-phone").value.trim(), address = $("#f-address").value.trim();
    msg.className = "msg err";
    if (!name) { msg.textContent = "Введите имя"; return; }
    if (phone.replace(/[^0-9]/g, "").length < 7) { msg.textContent = "Введите корректный телефон"; return; }
    if (!address) { msg.textContent = "Введите адрес доставки"; return; }
    const items = Object.keys(cart).filter(id => find(id)).map(id => ({ id: Number(id), qty: cart[id] }));
    if (!items.length) { msg.textContent = "Корзина пуста"; return; }
    sending = true; $("#send").disabled = true; msg.textContent = "";
    try {
      const r = await fetch("/api/order", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ name, phone, address, items }) });
      const d = await r.json();
      if (!r.ok) throw new Error(d.error || "Не удалось отправить заказ");
      cart = {}; saveCart(); renderCart();
      $("#dfoot").style.display = "none";
      $("#items").innerHTML = "";
      $("#items").appendChild(el("div", "msg ok", "Заказ №" + d.id + " принят! Мы скоро свяжемся с вами."));
    } catch (e) {
      msg.className = "msg err"; msg.textContent = e.message || "Ошибка отправки";
    }
    sending = false; $("#send").disabled = false;
  }

  function visible() {
    let list = products.slice();
    if (mode === "new") list = list.slice().sort((a, b) => b.id - a.id).slice(0, 8);
    else if (mode === "featured") list = list.filter(p => p.featured);
    else if (mode === "sale") list = list.filter(p => p.discount);
    if (category !== "all") list = list.filter(p => p.category === category);
    if (q) list = list.filter(p => p.name.toLowerCase().includes(q));
    return list;
  }

  function renderChips() {
    const box = $("#chips"); box.innerHTML = "";
    const cats = ["all"].concat(Array.from(new Set(products.map(p => p.category).filter(Boolean))));
    if (cats.length < 2) return;
    cats.forEach(c => {
      const b = el("button", "chip" + (c === category ? " active" : ""), c === "all" ? "Все" : c);
      b.onclick = () => { category = c; renderChips(); renderProducts(); };
      box.appendChild(b);
    });
  }

  function renderProducts() {
    $("#title").textContent = q ? "Результаты поиска" : TITLES[mode];
    ["new", "featured", "sale"].forEach(m => $("#m-" + m).classList.toggle("active", mode === m));
    const grid = $("#grid"); grid.innerHTML = "";
    const list = visible();
    if (!list.length) { grid.appendChild(el("div", "empty", "Товаров не найдено")); return; }
    list.forEach(p => {
      const card = el("div", "card");
      const th = el("div", "thumb");
      if (p.photo) { const img = el("img"); img.src = p.photo; img.alt = p.name; img.loading = "lazy"; th.appendChild(img); }
      else th.textContent = "🛒";
      card.appendChild(th);
      if (p.discount) card.appendChild(el("div", "badge", "-" + discText(p.discount)));
      if (p.category) card.appendChild(el("div", "cat", p.category));
      card.appendChild(el("div", "name", p.name));
      const bottom = el("div", "bottom");
      const price = el("div", "price", money(p.final_price));
      if (p.final_price !== p.price) price.appendChild(el("span", "old", money(p.price)));
      price.appendChild(el("small", "", "за " + p.unit));
      const add = el("button", "add", "+"); add.setAttribute("aria-label", "В корзину");
      add.onclick = () => addToCart(p.id);
      bottom.append(price, add);
      card.appendChild(bottom);
      $("#grid").appendChild(card);
    });
  }

  function renderSlider() {
    const sl = $("#slider"), tr = $("#track");
    tr.innerHTML = "";
    if (!ads.length) { sl.style.display = "none"; return; }
    sl.style.display = "block";
    ads.forEach((a, i) => {
      const s = el("div", "slide c" + (i % 4));
      const t = el("div", "txt");
      if (a.badge) t.appendChild(el("span", "tag" + (i % 2 ? " alt" : ""), a.badge));
      t.appendChild(el("h2", "", a.title));
      if (a.content) t.appendChild(el("p", "", a.content));
      s.appendChild(t);
      if (a.photo) { const img = el("img"); img.src = a.photo; img.alt = ""; s.appendChild(img); }
      tr.appendChild(s);
    });
    const many = ads.length > 1;
    $("#prev").style.display = many ? "flex" : "none";
    $("#next").style.display = many ? "flex" : "none";
    slide = 0; goSlide(0);
    clearInterval(timer);
    if (many) timer = setInterval(() => goSlide(slide + 1), 5500);
  }
  function goSlide(i) {
    slide = (i + ads.length) % ads.length;
    $("#track").style.transform = "translateX(-" + (slide * 100) + "%)";
  }

  function setMode(m) { mode = (mode === m) ? "all" : m; category = "all"; q = ""; $("#search").value = ""; renderChips(); renderProducts(); }
  function showInfo(key) { $("#mtitle").textContent = INFO[key][0]; $("#mtext").textContent = INFO[key][1]; $("#modal").classList.add("open"); }

  async function load() {
    try {
      const [p, a, inf] = await Promise.all([
        fetch("/api/products").then(r => r.json()),
        fetch("/api/ads").then(r => r.json()),
        fetch("/api/info").then(r => r.json())
      ]);
      if (!Array.isArray(p) || !Array.isArray(a)) throw new Error("bad");
      products = p; ads = a;
      CURRENCY = inf.currency || CURRENCY;
      const digits = (inf.phone || "").replace(/[^0-9+]/g, "");
      $("#phone").textContent = inf.phone || ""; $("#phone").href = "tel:" + digits;
      $("#address").textContent = inf.address || "";
      Object.keys(cart).forEach(id => { if (!find(id)) delete cart[id]; });
      saveCart();
      renderSlider(); renderChips(); renderProducts(); renderCart();
    } catch (e) {
      $("#grid").innerHTML = ""; $("#grid").appendChild(el("div", "empty", "Не удалось загрузить данные. Обновите страницу."));
    }
  }

  $("#m-new").onclick = () => setMode("new");
  $("#m-featured").onclick = () => setMode("featured");
  $("#m-sale").onclick = () => setMode("sale");
  $("#logo").onclick = () => { mode = "all"; category = "all"; q = ""; $("#search").value = ""; renderChips(); renderProducts(); };
  $("#search").oninput = e => { q = e.target.value.trim().toLowerCase(); renderProducts(); };
  $("#prev").onclick = () => { goSlide(slide - 1); };
  $("#next").onclick = () => { goSlide(slide + 1); };
  $("#cartbtn").onclick = () => openCart(true);
  $("#closecart").onclick = () => openCart(false);
  $("#overlay").onclick = () => openCart(false);
  $("#send").onclick = sendOrder;
  $("#mclose").onclick = () => $("#modal").classList.remove("open");
  $("#modal").onclick = e => { if (e.target.id === "modal") $("#modal").classList.remove("open"); };
  document.querySelectorAll("[data-info]").forEach(a => a.onclick = () => showInfo(a.dataset.info));
  $("#copy").textContent = "© " + new Date().getFullYear() + " Продуктовый Магазин. Все права защищены.";

  loadCart(); renderCart(); load();
</script>
</body>
</html>
"""

MAINTENANCE_HTML = """<!doctype html>
<html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Сайт отключён</title></head>
<body style="font-family:system-ui,sans-serif;text-align:center;padding:60px 16px">
<h1>🛠 Сайт временно недоступен</h1><p>Загляните позже.</p></body></html>
"""


def site_off_response():
    return web.Response(text=MAINTENANCE_HTML, content_type="text/html",
                        charset="utf-8", status=503)


async def handle_health(request):
    # Для проверки Render (Health Check Path = /health). Всегда 200.
    return web.Response(text="OK", content_type="text/plain", charset="utf-8")


async def handle_index(request):
    if not get_flag("site_active"):
        return site_off_response()
    return web.Response(text=INDEX_HTML, content_type="text/html", charset="utf-8")


async def handle_api_products(request):
    if not get_flag("site_active"):
        return web.json_response({"error": "site_off"}, status=503)
    rows = query("SELECT * FROM products ORDER BY id DESC")
    data = [{
        "id": r["id"],
        "name": r["name"],
        "unit": r["unit"],
        "price": r["price"],
        "discount": r["discount"],
        "final_price": final_price(r["price"], r["discount"]),
        "category": r["category"],
        "featured": bool(r["featured"]),
        "photo": f"/photo/{r['photo']}" if r["photo"] else None,
    } for r in rows]
    return web.json_response(data)


async def handle_api_ads(request):
    if not get_flag("site_active"):
        return web.json_response({"error": "site_off"}, status=503)
    rows = query("SELECT * FROM advertisements ORDER BY id DESC")
    data = [{
        "id": r["id"],
        "title": r["title"],
        "content": r["content"],
        "badge": r["badge"],
        "photo": f"/photo/{r['photo']}" if r["photo"] else None,
    } for r in rows]
    return web.json_response(data)


async def handle_info(request):
    return web.json_response({
        "phone": get_text("shop_phone", DEFAULT_PHONE),
        "address": get_text("shop_address", DEFAULT_ADDRESS),
        "currency": cur(),
    })


async def handle_photo(request):
    """Отдаёт фото товара или рекламы из Telegram (file_id -> картинка)."""
    if not get_flag("site_active"):
        raise web.HTTPServiceUnavailable()
    file_id = request.match_info["file_id"]
    known = query(
        "SELECT 1 FROM products WHERE photo = ? UNION SELECT 1 FROM advertisements WHERE photo = ?",
        (file_id, file_id), one=True,
    )
    if not known:
        raise web.HTTPNotFound()
    if file_id.startswith("demo:"):
        return web.Response(text=demo_svg(file_id[5:]), content_type="image/svg+xml",
                            charset="utf-8", headers={"Cache-Control": "public, max-age=3600"})
    try:
        tg_file = await bot.get_file(file_id)
        buf = await bot.download_file(tg_file.file_path)
    except Exception as e:
        logging.warning("Фото не загрузилось: %s", e)
        raise web.HTTPNotFound()
    ctype = mimetypes.guess_type(tg_file.file_path)[0] or "image/jpeg"
    return web.Response(body=buf.read(), content_type=ctype,
                        headers={"Cache-Control": "public, max-age=86400"})


async def notify_admins(text):
    ids = {SUPER_ADMIN_ID}
    for r in query("SELECT user_id FROM users WHERE is_admin = 1 AND banned = 0"):
        ids.add(r["user_id"])
    for uid in ids:
        try:
            await bot.send_message(uid, text, parse_mode="HTML")
        except Exception as e:
            logging.warning("Не удалось отправить заказ админу %s: %s", uid, e)


ORDER_LAST = {}


def order_error(text, status=400):
    return web.json_response({"error": text}, status=status)


async def handle_order(request):
    if not get_flag("site_active"):
        return order_error("Сайт временно недоступен", 503)

    ip = (request.headers.get("X-Forwarded-For") or request.remote or "").split(",")[0].strip()
    now = time.time()
    if now - ORDER_LAST.get(ip, 0) < 30:
        return order_error("Подождите немного перед следующим заказом", 429)

    try:
        data = await request.json()
    except Exception:
        return order_error("Неверный запрос")
    if not isinstance(data, dict):
        return order_error("Неверный запрос")

    name = str(data.get("name", "")).strip()[:64]
    phone = str(data.get("phone", "")).strip()[:32]
    address = str(data.get("address", "")).strip()[:200]
    if not name or not address or len(re.sub(r"[^0-9]", "", phone)) < 7:
        return order_error("Заполните имя, телефон и адрес")

    items = data.get("items")
    if not isinstance(items, list) or not items or len(items) > 50:
        return order_error("Корзина пуста")

    lines, total = [], 0.0
    for it in items:
        try:
            pid, qty = int(it["id"]), int(it["qty"])
        except (KeyError, TypeError, ValueError):
            continue
        if qty < 1 or qty > 99:
            continue
        p = query("SELECT * FROM products WHERE id = ?", (pid,), one=True)
        if not p:
            continue
        price = final_price(p["price"], p["discount"])
        subtotal = round(price * qty, 2)
        total += subtotal
        lines.append(f"• {esc(p['name'])} × {qty} {esc(p['unit'])} = {subtotal:g} {esc(cur())}")
    if not lines:
        return order_error("Товары из корзины больше недоступны")

    order_id = insert(
        "INSERT INTO orders (name, phone, address, items, total) VALUES (?, ?, ?, ?, ?)",
        (name, phone, address, "; ".join(lines), round(total, 2)),
    )
    ORDER_LAST[ip] = now
    await notify_admins(
        f"🛒 <b>Новый заказ №{order_id}</b>\n\n"
        f"👤 {esc(name)}\n📞 {esc(phone)}\n📍 {esc(address)}\n\n"
        + "\n".join(lines)
        + f"\n\n💰 <b>Итого: {round(total, 2):g} {esc(cur())}</b>"
    )
    return web.json_response({"ok": True, "id": order_id})


async def run_web():
    app = web.Application()
    app.router.add_get("/", handle_index)
    app.router.add_get("/health", handle_health)
    app.router.add_get("/api/products", handle_api_products)
    app.router.add_get("/api/ads", handle_api_ads)
    app.router.add_get("/api/info", handle_info)
    app.router.add_post("/api/order", handle_order)
    app.router.add_get("/photo/{file_id}", handle_photo)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.environ.get("PORT", 10000))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()


async def main():
    if is_admin_user(SUPER_ADMIN_ID) and not get_user(SUPER_ADMIN_ID):
        register_user(SUPER_ADMIN_ID, "Владелец", "")
    if get_text("demo_seeded") != "1" and not query("SELECT 1 FROM products LIMIT 1", one=True):
        seed_demo()
        set_text("demo_seeded", "1")
    await run_web()
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
