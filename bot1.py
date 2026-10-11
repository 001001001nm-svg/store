import os
import asyncio
import sqlite3
from aiohttp import web
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage

# Настройки
BOT_TOKEN = "8963845436:AAHUvJIEGZhN-H9M3UxB19Pvka37Sy5LsfY"
SUPER_ADMIN_ID = 8636623905

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

# Глобальные флаги состояния
BOT_ACTIVE = True
SITE_ACTIVE = True

# --- База данных ---
def init_db():
    conn = sqlite3.connect("store.db")
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            name TEXT,
            username TEXT,
            is_admin INTEGER DEFAULT 0
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS categories (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT UNIQUE
        )
    """)
    cur.execute("""
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
    cur.execute("""
        CREATE TABLE IF NOT EXISTS advertisements (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT,
            content TEXT,
            photo TEXT
        )
    """)
    conn.commit()
    conn.close()

init_db()

def get_user(user_id):
    conn = sqlite3.connect("store.db")
    cur = conn.cursor()
    cur.execute("SELECT name, is_admin FROM users WHERE user_id = ?", (user_id,))
    row = cur.fetchone()
    conn.close()
    return row

def register_user(user_id, name, username):
    is_admin_val = 1 if user_id == SUPER_ADMIN_ID else 0
    conn = sqlite3.connect("store.db")
    cur = conn.cursor()
    cur.execute("""
        INSERT OR REPLACE INTO users (user_id, name, username, is_admin) 
        VALUES (?, ?, ?, COALESCE((SELECT is_admin FROM users WHERE user_id = ?), ?))
    """, (user_id, name, username, user_id, is_admin_val))
    if user_id == SUPER_ADMIN_ID:
        cur.execute("UPDATE users SET is_admin = 1 WHERE user_id = ?", (SUPER_ADMIN_ID,))
    conn.commit()
    conn.close()

def set_admin_status(user_id, status):
    conn = sqlite3.connect("store.db")
    cur = conn.cursor()
    cur.execute("UPDATE users SET is_admin = ? WHERE user_id = ?", (status, user_id))
    conn.commit()
    conn.close()

# --- FSM Состояния ---
class RegState(StatesGroup):
    waiting_for_name = State()

class OwnerState(StatesGroup):
    waiting_for_admin_input = State()

class ProductState(StatesGroup):
    waiting_for_name = State.id = State()
    waiting_for_category = State()
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

# --- Главное меню ---
def main_menu_kb(is_owner, is_admin):
    kb = []
    if is_admin or is_owner:
        kb.append([types.KeyboardButton(text="📦 Управление товарами"), types.KeyboardButton(text="📁 Каталоги")])
        kb.append([types.KeyboardButton(text="🏷 Скидки"), types.KeyboardButton(text="📢 Реклама")])
    if is_owner:
        kb.append([types.KeyboardButton(text="👑 Панель владельца")])
    return types.ReplyKeyboardMarkup(keyboard=kb, resize_keyboard=True)

# --- Хэндлеры ---
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

# --- ПАНЕЛЬ ВЛАДЕЛЬЦА ---
@dp.message(F.text == "👑 Панель владельца")
async def owner_panel(message: types.Message):
    if message.from_user.id != SUPER_ADMIN_ID:
        return
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

@dp.message(F.text == "➕ Назначить админа")
async def make_admin_button(message: types.Message, state: FSMContext):
    if message.from_user.id != SUPER_ADMIN_ID:
        return

    conn = sqlite3.connect("store.db")
    cur = conn.cursor()
    cur.execute("SELECT user_id, name, username, is_admin FROM users")
    users = cur.fetchall()
    conn.close()

    if not users:
        await message.answer("Пользователей в базе нет.")
        return

    text = "👥 **Все участники бота:**\n\n"
    for u_id, name, uname, is_admin in users:
        role = "Админ 🟢" if is_admin == 1 else "Пользователь"
        text += f"• **{name}** (@{uname}) — ID: `{u_id}` ({role})\n"

    text += "\n✍️ **Напишите ID или юзернейм (@username) для назначения администратором:**"
    await message.answer(text, parse_mode="Markdown")
    await state.set_state(OwnerState.waiting_for_admin_input)

@dp.message(OwnerState.waiting_for_admin_input)
async def process_admin_input(message: types.Message, state: FSMContext):
    if message.from_user.id != SUPER_ADMIN_ID:
        return

    user_input = message.text.strip().replace("@", "")
    conn = sqlite3.connect("store.db")
    cur = conn.cursor()

    if user_input.isdigit():
        target_id = int(user_input)
        cur.execute("SELECT user_id, name FROM users WHERE user_id = ?", (target_id,))
    else:
        cur.execute("SELECT user_id, name FROM users WHERE LOWER(username) = LOWER(?)", (user_input,))

    target_user = cur.fetchone()
    if target_user:
        target_id, target_name = target_user
        cur.execute("UPDATE users SET is_admin = 1 WHERE user_id = ?", (target_id,))
        conn.commit()
        await message.answer(f"✅ {target_name} (ID: `{target_id}`) успешно назначен администратором!", parse_mode="Markdown")
        try:
            await bot.send_message(target_id, "🎉 Вам выданы права администратора! Нажмите /start для открытия меню.")
        except:
            pass
    else:
        await message.answer("❌ Пользователь не найден. Проверьте правильность ID или юзернейма.")

    conn.close()
    await state.clear()
    await owner_panel(message)

@dp.message(F.text == "👥 Список админов")
async def manage_admins(message: types.Message):
    if message.from_user.id != SUPER_ADMIN_ID:
        return
    conn = sqlite3.connect("store.db")
    cur = conn.cursor()
    cur.execute("SELECT user_id, name, username, is_admin FROM users WHERE is_admin = 1")
    admins = cur.fetchall()
    conn.close()

    text = "👥 **Список администраторов:**\n\n"
    kb_builder = []
    for u_id, name, uname, is_admin in admins:
        text += f"• {name} (@{uname}) [ID: `{u_id}`]\n"
        if u_id != SUPER_ADMIN_ID:
            kb_builder.append([types.InlineKeyboardButton(text=f"Снять админа: {name}", callback_data=f"unadmin_{u_id}")])
            kb_builder.append([types.InlineKeyboardButton(text=f"Забанить: {name}", callback_data=f"ban_{u_id}")])

    markup = types.InlineKeyboardMarkup(inline_keyboard=kb_builder) if kb_builder else None
    await message.answer(text, parse_mode="Markdown", reply_markup=markup)

@dp.callback_query(F.data.startswith(("unadmin_", "ban_")))
async def admin_actions(callback: types.CallbackQuery):
    if callback.from_user.id != SUPER_ADMIN_ID:
        return
    action, target_id = callback.data.split("_")
    target_id = int(target_id)
    
    conn = sqlite3.connect("store.db")
    cur = conn.cursor()
    if action == "unadmin":
        cur.execute("UPDATE users SET is_admin = 0 WHERE user_id = ?", (target_id,))
        await callback.answer("Админ снят!")
    elif action == "ban":
        cur.execute("DELETE FROM users WHERE user_id = ?", (target_id,))
        await callback.answer("Пользователь забанен!")
    conn.commit()
    conn.close()
    await callback.message.edit_text("Список обновлен.")

# --- УПРАВЛЕНИЕ ТОВАРАМИ ---
@dp.message(F.text == "📦 Управление товарами")
async def products_menu(message: types.Message):
    kb = types.ReplyKeyboardMarkup(
        keyboard=[
            [types.KeyboardButton(text="➕ Добавить товар"), types.KeyboardButton(text="🗑 Снять товар")],
            [types.KeyboardButton(text="📜 Весь список товаров"), types.KeyboardButton(text="🔙 Главное меню")]
        ],
        resize_keyboard=True
    )
    await message.answer("📦 Меню управления товарами:", reply_markup=kb)

@dp.message(F.text == "📜 Весь список товаров")
async def list_all_products(message: types.Message):
    conn = sqlite3.connect("store.db")
    cur = conn.cursor()
    cur.execute("SELECT id, name, unit, price, photo, discount, category FROM products")
    products = cur.fetchall()
    conn.close()

    if not products:
        await message.answer("В базе пока нет товаров.")
        return

    await message.answer(f"📜 Всего товаров: {len(products)}")
    for p in products:
        p_id, name, unit, price, photo, discount, cat = p
        disc_text = f"\n🏷 Скидка: {discount}" if discount else ""
        cat_text = f"\n📁 Каталог: {cat}" if cat else ""
        caption = f"📦 *{name}*{cat_text}\nЦена: {price} руб. / {unit}{disc_text}"
        if photo:
            await message.answer_photo(photo=photo, caption=caption, parse_mode="Markdown")
        else:
            await message.answer(caption, parse_mode="Markdown")

@dp.message(F.text == "➕ Добавить товар")
async def add_product_start(message: types.Message, state: FSMContext):
    conn = sqlite3.connect("store.db")
    cur = conn.cursor()
    cur.execute("SELECT name FROM categories")
    cats = cur.fetchall()
    conn.close()

    if not cats:
        await message.answer("❌ Сначала создайте каталог!")
        return

    kb_builder = [[types.KeyboardButton(text=c[0])] for c in cats]
    kb_builder.append([types.KeyboardButton(text="🔙 Главное меню")])
    markup = types.ReplyKeyboardMarkup(keyboard=kb_builder, resize_keyboard=True)

    await message.answer("📁 Выберите каталог, в который хотите добавить товар:", reply_markup=markup)
    await state.set_state(ProductState.waiting_for_category)

@dp.message(ProductState.waiting_for_category)
async def process_prod_category(message: types.Message, state: FSMContext):
    if message.text == "🔙 Главное меню":
        await state.clear()
        await back_to_main(message)
        return

    await state.update_data(category=message.text.strip())
    await message.answer("Введите название товара:", reply_markup=types.ReplyKeyboardRemove())
    await state.set_state(ProductState.waiting_for_name)

@dp.message(ProductState.waiting_for_name)
async def process_prod_name(message: types.Message, state: FSMContext):
    await state.update_data(name=message.text.strip())
    kb = types.ReplyKeyboardMarkup(
        keyboard=[
            [types.KeyboardButton(text="КГ"), types.KeyboardButton(text="100 г"), types.KeyboardButton(text="шт")]
        ],
        resize_keyboard=True
    )
    await message.answer("Выберите единицу измерения:", reply_markup=kb)
    await state.set_state(ProductState.waiting_for_unit)

@dp.message(ProductState.waiting_for_unit)
async def process_prod_unit(message: types.Message, state: FSMContext):
    await state.update_data(unit=message.text.strip())
    await message.answer("Введите цену товара (цифрой):", reply_markup=types.ReplyKeyboardRemove())
    await state.set_state(ProductState.waiting_for_price)

@dp.message(ProductState.waiting_for_price)
async def process_prod_price(message: types.Message, state: FSMContext):
    try:
        price = float(message.text.strip())
    except ValueError:
        await message.answer("Введите корректную цену (число):")
        return
    await state.update_data(price=price)
    await message.answer("Пришлите фото товара:")
    await state.set_state(ProductState.waiting_for_photo)

# ИСПРАВЛЕНИЕ: Прием фото или файла (документа с картинкой)
@dp.message(ProductState.waiting_for_photo, F.photo | F.document)
async def process_prod_photo(message: types.Message, state: FSMContext):
    if message.photo:
        photo_id = message.photo[-1].file_id
    elif message.document:
        photo_id = message.document.file_id
    else:
        await message.answer("Пожалуйста, отправьте именно фото товара.")
        return

    data = await state.get_data()
    
    conn = sqlite3.connect("store.db")
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO products (name, unit, price, photo, category) VALUES (?, ?, ?, ?, ?)",
        (data["name"], data["unit"], data["price"], photo_id, data["category"])
    )
    conn.commit()
    conn.close()
    
    await state.clear()
    await message.answer("Товар успешно добавлен✅")
    await products_menu(message)

@dp.message(F.text == "🗑 Снять товар")
async def remove_product_list(message: types.Message):
    conn = sqlite3.connect("store.db")
    cur = conn.cursor()
    cur.execute("SELECT id, name FROM products")
    products = cur.fetchall()
    conn.close()

    if not products:
        await message.answer("Нет доступных товаров для удаления.")
        return

    kb_builder = [[types.InlineKeyboardButton(text=p[1], callback_data=f"delprod_{p[0]}")] for p in products]
    markup = types.InlineKeyboardMarkup(inline_keyboard=kb_builder)
    await message.answer("Выберите товар, чтобы убрать его с сайта:", reply_markup=markup)

@dp.callback_query(F.data.startswith("delprod_"))
async def delete_product(callback: types.CallbackQuery):
    prod_id = int(callback.data.split("_")[1])
    conn = sqlite3.connect("store.db")
    cur = conn.cursor()
    cur.execute("DELETE FROM products WHERE id = ?", (prod_id,))
    conn.commit()
    conn.close()
    await callback.answer("Товар успешно убран")
    await callback.message.edit_text("Товар успешно убран✅")

# --- КАТАЛОГИ ---
@dp.message(F.text == "📁 Каталоги")
async def categories_menu(message: types.Message):
    kb = types.ReplyKeyboardMarkup(
        keyboard=[
            [types.KeyboardButton(text="➕ Добавить каталог"), types.KeyboardButton(text="🗑 Убрать каталог")],
            [types.KeyboardButton(text="📂 Весь список каталогов"), types.KeyboardButton(text="🔙 Главное меню")]
        ],
        resize_keyboard=True
    )
    await message.answer("📁 Управление каталогами:", reply_markup=kb)

@dp.message(F.text == "📂 Весь список каталогов")
async def list_all_categories(message: types.Message):
    conn = sqlite3.connect("store.db")
    cur = conn.cursor()
    cur.execute("SELECT name FROM categories")
    cats = cur.fetchall()
    conn.close()

    if not cats:
        await message.answer("Каталогов пока нет.")
        return

    text = "📂 *Список всех каталогов:*\n\n"
    for idx, c in enumerate(cats, 1):
        text += f"{idx}. {c[0]}\n"
    await message.answer(text, parse_mode="Markdown")

@dp.message(F.text == "➕ Добавить каталог")
async def add_cat_start(message: types.Message, state: FSMContext):
    await message.answer("Введите название каталога:")
    await state.set_state(CategoryState.waiting_for_name)

@dp.message(CategoryState.waiting_for_name)
async def process_cat_name(message: types.Message, state: FSMContext):
    cat_name = message.text.strip()
    conn = sqlite3.connect("store.db")
    cur = conn.cursor()
    try:
        cur.execute("INSERT INTO categories (name) VALUES (?)", (cat_name,))
        conn.commit()
        await message.answer("Каталог успешно добавлен✅")
    except sqlite3.IntegrityError:
        await message.answer("Такой каталог уже существует.")
    conn.close()
    await state.clear()
    await categories_menu(message)

@dp.message(F.text == "🗑 Убрать каталог")
async def remove_cat_list(message: types.Message):
    conn = sqlite3.connect("store.db")
    cur = conn.cursor()
    cur.execute("SELECT id, name FROM categories")
    cats = cur.fetchall()
    conn.close()

    if not cats:
        await message.answer("Нет каталогов для удаления.")
        return

    kb_builder = [[types.InlineKeyboardButton(text=c[1], callback_data=f"delcat_{c[0]}")] for c in cats]
    markup = types.InlineKeyboardMarkup(inline_keyboard=kb_builder)
    await message.answer("Выберите каталог для удаления:", reply_markup=markup)

@dp.callback_query(F.data.startswith("delcat_"))
async def delete_catalog(callback: types.CallbackQuery):
    cat_id = int(callback.data.split("_")[1])
    conn = sqlite3.connect("store.db")
    cur = conn.cursor()
    cur.execute("DELETE FROM categories WHERE id = ?", (cat_id,))
    conn.commit()
    conn.close()
    await callback.answer("Каталог успешно убран")
    await callback.message.edit_text("Каталог успешно убран✅")

# --- СКИДКИ ---
@dp.message(F.text == "🏷 Скидки")
async def discounts_menu(message: types.Message):
    kb = types.ReplyKeyboardMarkup(
        keyboard=[
            [types.KeyboardButton(text="➕ Сделать скидку"), types.KeyboardButton(text="🗑 Убрать скидку")],
            [types.KeyboardButton(text="🔙 Главное меню")]
        ],
        resize_keyboard=True
    )
    await message.answer("🏷 Меню скидок:", reply_markup=kb)

@dp.message(F.text == "➕ Сделать скидку")
async def discount_prod_list(message: types.Message):
    conn = sqlite3.connect("store.db")
    cur = conn.cursor()
    cur.execute("SELECT id, name FROM products")
    products = cur.fetchall()
    conn.close()
    if not products:
        await message.answer("Нет товаров.")
        return
    kb_builder = [[types.InlineKeyboardButton(text=p[1], callback_data=f"disc_{p[0]}")] for p in products]
    markup = types.InlineKeyboardMarkup(inline_keyboard=kb_builder)
    await message.answer("Выберите товар для скидки:", reply_markup=markup)

@dp.callback_query(F.data.startswith("disc_"))
async def select_discount_type(callback: types.CallbackQuery, state: FSMContext):
    prod_id = callback.data.split("_")[1]
    await state.update_data(prod_id=prod_id)
    kb = types.InlineKeyboardMarkup(inline_keyboard=[
        [types.InlineKeyboardButton(text="Скидка %", callback_data="dtype_percent"),
         types.InlineKeyboardButton(text="Скидка Рублями", callback_data="dtype_rubles")]
    ])
    await callback.message.edit_text("Выберите тип скидки:", reply_markup=kb)

@dp.callback_query(F.data.startswith("dtype_"))
async def process_discount_type(callback: types.CallbackQuery, state: FSMContext):
    dtype = "%" if callback.data == "dtype_percent" else "руб"
    await state.update_data(dtype=dtype)
    await callback.message.answer(f"Введите значение скидки ({dtype}):")
    await state.set_state(DiscountState.waiting_for_value)
    await callback.answer()

@dp.message(DiscountState.waiting_for_value)
async def process_discount_value(message: types.Message, state: FSMContext):
    val = message.text.strip()
    data = await state.get_data()
    prod_id = data["prod_id"]
    dtype = data["dtype"]
    discount_str = f"{val}{dtype}"

    conn = sqlite3.connect("store.db")
    cur = conn.cursor()
    cur.execute("UPDATE products SET discount = ? WHERE id = ?", (discount_str, prod_id))
    conn.commit()
    conn.close()

    await state.clear()
    await message.answer("Скидка успешно добавлена✅")

@dp.message(F.text == "🗑 Убрать скидку")
async def remove_discount_list(message: types.Message):
    conn = sqlite3.connect("store.db")
    cur = conn.cursor()
    cur.execute("SELECT id, name FROM products WHERE discount IS NOT NULL")
    products = cur.fetchall()
    conn.close()
    if not products:
        await message.answer("Нет товаров со скидками.")
        return
    kb_builder = [[types.InlineKeyboardButton(text=p[1], callback_data=f"rmdisc_{p[0]}")] for p in products]
    markup = types.InlineKeyboardMarkup(inline_keyboard=kb_builder)
    await message.answer("Выберите товар, чтобы убрать скидку:", reply_markup=markup)

@dp.callback_query(F.data.startswith("rmdisc_"))
async def remove_discount_action(callback: types.CallbackQuery):
    prod_id = int(callback.data.split("_")[1])
    conn = sqlite3.connect("store.db")
    cur = conn.cursor()
    cur.execute("UPDATE products SET discount = NULL WHERE id = ?", (prod_id,))
    conn.commit()
    conn.close()
    await callback.answer("Скидка успешно убрана")
    await callback.message.edit_text("Скидка успешно убрана✅")

# --- РЕКЛАМА ---
@dp.message(F.text == "📢 Реклама")
async def adv_menu(message: types.Message):
    kb = types.ReplyKeyboardMarkup(
        keyboard=[
            [types.KeyboardButton(text="➕ Поставить рекламу"), types.KeyboardButton(text="🗑 Снять рекламу")],
            [types.KeyboardButton(text="🔙 Главное меню")]
        ],
        resize_keyboard=True
    )
    await message.answer("📢 Меню рекламы:", reply_markup=kb)

@dp.message(F.text == "➕ Поставить рекламу")
async def add_ad_start(message: types.Message, state: FSMContext):
    await message.answer("Напишите название рекламы:")
    await state.set_state(AdState.waiting_for_title)

@dp.message(AdState.waiting_for_title)
async def process_ad_title(message: types.Message, state: FSMContext):
    await state.update_data(title=message.text.strip())
    await message.answer("Пришлите фото или текст для рекламы:")
    await state.set_state(AdState.waiting_for_content)

@dp.message(AdState.waiting_for_content)
async def process_ad_content(message: types.Message, state: FSMContext):
    data = await state.get_data()
    content = message.caption or message.text or "Без текста"
    photo_id = message.photo[-1].file_id if message.photo else None

    conn = sqlite3.connect("store.db")
    cur = conn.cursor()
    cur.execute("INSERT INTO advertisements (title, content, photo) VALUES (?, ?, ?)", (data["title"], content, photo_id))
    conn.commit()
    conn.close()

    await state.clear()
    await message.answer("Реклама успешно добавлена✅")

@dp.message(F.text == "🗑 Снять рекламу")
async def remove_ad_list(message: types.Message):
    conn = sqlite3.connect("store.db")
    cur = conn.cursor()
    cur.execute("SELECT id, title FROM advertisements")
    ads = cur.fetchall()
    conn.close()
    if not ads:
        await message.answer("Нет активной рекламы.")
        return
    kb_builder = [[types.InlineKeyboardButton(text=a[1], callback_data=f"delad_{a[0]}")] for a in ads]
    markup = types.InlineKeyboardMarkup(inline_keyboard=kb_builder)
    await message.answer("Выберите рекламу, чтобы убрать:", reply_markup=markup)

@dp.callback_query(F.data.startswith("delad_"))
async def delete_ad(callback: types.CallbackQuery):
    ad_id = int(callback.data.split("_")[1])
    conn = sqlite3.connect("store.db")
    cur = conn.cursor()
    cur.execute("DELETE FROM advertisements WHERE id = ?", (ad_id,))
    conn.commit()
    conn.close()
    await callback.answer("Реклама успешно убрана")
    await callback.message.edit_text("Реклама успешно убрана✅")

@dp.message(F.text == "🔙 Главное меню")
async def back_to_main(message: types.Message):
    user_id = message.from_user.id
    user_data = get_user(user_id)
    is_admin = user_data[1] == 1 if user_data else False
    is_owner = (user_id == SUPER_ADMIN_ID)
    kb = main_menu_kb(is_owner, is_admin)
    await message.answer("Главное меню:", reply_markup=kb)

# --- WEB SERVER (Render) ---
async def handle_ping(request):
    return web.Response(text="OK", status=200)

async def start_web_server():
    app = web.Application()
    app.router.add_get('/', handle_ping)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.environ.get("PORT", 10000))
    site = web.TCPSite(runner, '0.0.0.0', port)
    await site.start()

async def main():
    await start_web_server()
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
