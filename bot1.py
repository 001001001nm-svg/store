import asyncio
import html
import logging
import mimetypes
import os
import sqlite3
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
    """Цена с учётом скидки вида '10%' или '50сом'."""
    if not discount:
        return price
    try:
        if discount.endswith("%"):
            return round(price * (1 - float(discount[:-1]) / 100), 2)
        if discount.endswith("сом"):
            return max(round(price - float(discount[:-3]), 2), 0)
    except ValueError:
        pass
    return price


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
            [types.KeyboardButton(text="🚫 Забаненные")],
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


# ============================================================
#  ТОВАРЫ
# ============================================================
@dp.message(F.text == "📦 Управление товарами", IsAdmin())
async def products_menu(message: types.Message):
    kb = types.ReplyKeyboardMarkup(
        keyboard=[
            [types.KeyboardButton(text="➕ Добавить товар"),
             types.KeyboardButton(text="🗑 Снять товар")],
            [types.KeyboardButton(text="📜 Весь список товаров"),
             types.KeyboardButton(text="🔙 Главное меню")],
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
            f"Цена: {p['price']:g} сом / {esc(p['unit'])}{disc_text}"
        )
        if p["photo"]:
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
        types.InlineKeyboardButton(text="Скидка сом", callback_data="dtype_som"),
    ]])
    await callback.message.edit_text("Тип скидки:", reply_markup=kb)
    await callback.answer()


@dp.callback_query(F.data.startswith("dtype_"), IsAdmin())
async def process_discount_type(callback: types.CallbackQuery, state: FSMContext):
    dtype = "%" if callback.data == "dtype_percent" else "сом"
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
    await message.answer("Название рекламы:", reply_markup=types.ReplyKeyboardRemove())
    await state.set_state(AdState.waiting_for_title)


@dp.message(AdState.waiting_for_title, F.text)
async def process_ad_title(message: types.Message, state: FSMContext):
    await state.update_data(title=message.text.strip())
    await message.answer("Текст рекламы:")
    await state.set_state(AdState.waiting_for_content)


@dp.message(AdState.waiting_for_content, F.text)
async def process_ad_content(message: types.Message, state: FSMContext):
    data = await state.get_data()
    execute("INSERT INTO advertisements (title, content, photo) VALUES (?, ?, ?)",
            (data["title"], message.text, None))
    await state.clear()
    await message.answer("Реклама добавлена✅")
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
<title>Магазин</title>
<style>
  :root { --bg:#f5f6f8; --card:#fff; --text:#1c1e21; --muted:#6b7280; --accent:#16a34a; --red:#dc2626; }
  @media (prefers-color-scheme: dark) {
    :root { --bg:#111315; --card:#1b1e21; --text:#eceff1; --muted:#9aa3ab; }
  }
  * { box-sizing:border-box; }
  body { margin:0; font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;
         background:var(--bg); color:var(--text); }
  header { padding:20px 16px 8px; max-width:1100px; margin:0 auto; }
  h1 { margin:0; font-size:26px; }
  main { max-width:1100px; margin:0 auto; padding:8px 16px 40px; }
  .ads { display:grid; gap:10px; margin:12px 0; }
  .ad { background:linear-gradient(135deg,#f59e0b,#ef4444); color:#fff; border-radius:14px; padding:14px 16px; }
  .ad b { display:block; font-size:17px; margin-bottom:4px; }
  .chips { display:flex; gap:8px; overflow-x:auto; padding:8px 0 12px; }
  .chip { border:1px solid var(--muted); background:transparent; color:var(--text);
          border-radius:999px; padding:7px 14px; cursor:pointer; white-space:nowrap; font-size:14px; }
  .chip.active { background:var(--accent); border-color:var(--accent); color:#fff; }
  .grid { display:grid; grid-template-columns:repeat(auto-fill,minmax(200px,1fr)); gap:14px; }
  .card { background:var(--card); border-radius:14px; overflow:hidden; display:flex; flex-direction:column;
          box-shadow:0 1px 4px rgba(0,0,0,.08); position:relative; }
  .card img { width:100%; aspect-ratio:1/1; object-fit:cover; background:#ddd; }
  .badge { position:absolute; top:10px; left:10px; background:var(--red); color:#fff;
           font-size:13px; font-weight:700; padding:3px 9px; border-radius:8px; }
  .info { padding:12px; }
  .name { font-weight:600; margin-bottom:6px; }
  .cat { color:var(--muted); font-size:13px; margin-bottom:6px; }
  .price { font-size:18px; font-weight:700; }
  .old { color:var(--muted); text-decoration:line-through; font-size:14px; margin-left:6px; font-weight:400; }
  .unit { color:var(--muted); font-size:13px; font-weight:400; }
  .empty { color:var(--muted); padding:30px 0; }
</style>
</head>
<body>
<header><h1>🛒 Магазин</h1></header>
<main>
  <div id="ads" class="ads"></div>
  <div id="chips" class="chips"></div>
  <div id="grid" class="grid"></div>
</main>
<script>
  let products = [];
  let current = "all";

  function money(n) {
    return Number(n).toLocaleString("ru-RU", { maximumFractionDigits: 2 }) + " сом";
  }

  function el(tag, cls, text) {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text !== undefined) e.textContent = text;
    return e;
  }

  function renderAds(ads) {
    const box = document.getElementById("ads");
    box.innerHTML = "";
    ads.forEach(a => {
      const d = el("div", "ad");
      d.appendChild(el("b", "", a.title));
      d.appendChild(el("span", "", a.content));
      box.appendChild(d);
    });
  }

  function renderChips() {
    const box = document.getElementById("chips");
    box.innerHTML = "";
    const cats = ["all", ...new Set(products.map(p => p.category).filter(Boolean))];
    cats.forEach(c => {
      const b = el("button", "chip" + (c === current ? " active" : ""), c === "all" ? "Все" : c);
      b.onclick = () => { current = c; renderChips(); renderProducts(); };
      box.appendChild(b);
    });
  }

  function renderProducts() {
    const grid = document.getElementById("grid");
    grid.innerHTML = "";
    const list = products.filter(p => current === "all" || p.category === current);
    if (!list.length) {
      grid.appendChild(el("div", "empty", "Товаров пока нет"));
      return;
    }
    list.forEach(p => {
      const card = el("div", "card");
      if (p.photo) {
        const img = el("img");
        img.src = p.photo;
        img.alt = p.name;
        img.loading = "lazy";
        card.appendChild(img);
      }
      if (p.discount) card.appendChild(el("div", "badge", "-" + p.discount));
      const info = el("div", "info");
      info.appendChild(el("div", "name", p.name));
      if (p.category) info.appendChild(el("div", "cat", p.category));
      const price = el("div", "price", money(p.final_price));
      if (p.final_price !== p.price) price.appendChild(el("span", "old", money(p.price)));
      price.appendChild(el("span", "unit", " / " + p.unit));
      info.appendChild(price);
      card.appendChild(info);
      grid.appendChild(card);
    });
  }

  async function load() {
    try {
      const [p, a] = await Promise.all([
        fetch("/api/products").then(r => r.json()),
        fetch("/api/ads").then(r => r.json()),
      ]);
      if (!Array.isArray(p) || !Array.isArray(a)) throw new Error("bad data");
      products = p;
      renderAds(a);
      renderChips();
      renderProducts();
    } catch (e) {
      document.getElementById("grid").textContent = "Не удалось загрузить данные";
    }
  }
  load();
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
        "photo": f"/photo/{r['photo']}" if r["photo"] else None,
    } for r in rows]
    return web.json_response(data)


async def handle_api_ads(request):
    if not get_flag("site_active"):
        return web.json_response({"error": "site_off"}, status=503)
    rows = query("SELECT id, title, content FROM advertisements ORDER BY id DESC")
    return web.json_response([dict(r) for r in rows])


async def handle_photo(request):
    """Отдаёт фото товара из Telegram (file_id -> картинка)."""
    if not get_flag("site_active"):
        raise web.HTTPServiceUnavailable()
    file_id = request.match_info["file_id"]
    if not query("SELECT 1 FROM products WHERE photo = ?", (file_id,), one=True):
        raise web.HTTPNotFound()
    try:
        tg_file = await bot.get_file(file_id)
        buf = await bot.download_file(tg_file.file_path)
    except Exception as e:
        logging.warning("Фото не загрузилось: %s", e)
        raise web.HTTPNotFound()
    ctype = mimetypes.guess_type(tg_file.file_path)[0] or "image/jpeg"
    return web.Response(body=buf.read(), content_type=ctype,
                        headers={"Cache-Control": "public, max-age=86400"})


async def run_web():
    app = web.Application()
    app.router.add_get("/", handle_index)
    app.router.add_get("/health", handle_health)
    app.router.add_get("/api/products", handle_api_products)
    app.router.add_get("/api/ads", handle_api_ads)
    app.router.add_get("/photo/{file_id}", handle_photo)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.environ.get("PORT", 10000))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()


async def main():
    if is_admin_user(SUPER_ADMIN_ID) and not get_user(SUPER_ADMIN_ID):
        register_user(SUPER_ADMIN_ID, "Владелец", "")
    await run_web()
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
