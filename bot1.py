import os
import asyncio
import sqlite3
from aiohttp import web
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage

# Новый токен бота
BOT_TOKEN = "8963845436:AAHUvJIEGZhN-H9M3UxB19Pvka37Sy5LsfY"

# Укажи свой Telegram ID, чтобы ты мог назначать админов (0 — если хочешь сделать команду доступной всем до настройки)
SUPER_ADMIN_ID = 0  

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

# --- База данных SQLite ---
def init_db():
    conn = sqlite3.connect("users.db")
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            name TEXT,
            is_admin INTEGER DEFAULT 0
        )
    """)
    conn.commit()
    conn.close()

init_db()

def get_user(user_id):
    conn = sqlite3.connect("users.db")
    cur = conn.cursor()
    cur.execute("SELECT name, is_admin FROM users WHERE user_id = ?", (user_id,))
    row = cur.fetchone()
    conn.close()
    return row

def register_user(user_id, name):
    conn = sqlite3.connect("users.db")
    cur = conn.cursor()
    cur.execute("""
        INSERT OR REPLACE INTO users (user_id, name, is_admin) 
        VALUES (?, ?, COALESCE((SELECT is_admin FROM users WHERE user_id = ?), 0))
    """, (user_id, name, user_id))
    conn.commit()
    conn.close()

def set_admin_status(user_id, is_admin=1):
    conn = sqlite3.connect("users.db")
    cur = conn.cursor()
    cur.execute("UPDATE users SET is_admin = ? WHERE user_id = ?", (is_admin, user_id))
    conn.commit()
    conn.close()

# --- FSM Состояния ---
class Registration(StatesGroup):
    waiting_for_name = State()

# --- Хэндлеры ---

@dp.message(CommandStart())
async def cmd_start(message: types.Message, state: FSMContext):
    user_data = get_user(message.from_user.id)
    
    if not user_data:
        # Новый пользователь: спрашиваем имя
        await message.answer("Привет! Как тебя зовут? Введи свое имя:")
        await state.set_state(Registration.waiting_for_name)
    else:
        name, is_admin = user_data
        if is_admin == 1:
            # Главное меню для подтвержденного администратора
            kb = types.ReplyKeyboardMarkup(
                keyboard=[
                    [types.KeyboardButton(text="📦 Управление товарами"), types.KeyboardButton(text="📊 Статистика")]
                ],
                resize_keyboard=True
            )
            await message.answer(f"Привет, {name}! Добро пожаловать в админ-панель.", reply_markup=kb)
        else:
            await message.answer(f"Привет, {name}!\n\nОжидайте назначения на роль администратора. Пока вам не назначены права, команды недоступны.")

@dp.message(Registration.waiting_for_name)
async def process_name(message: types.Message, state: FSMContext):
    user_name = message.text.strip()
    register_user(message.from_user.id, user_name)
    await state.clear()
    
    await message.answer(
        f"Приятно познакомиться, {user_name}!\n\n"
        f"Ваше имя сохранено. Пожалуйста, ожидайте назначения на роль администратора. "
        f"До этого момента функционал панели недоступен."
    )

# Команда для назначения админов: /makeadmin ID
@dp.message(Command("makeadmin"))
async def cmd_make_admin(message: types.Message):
    if SUPER_ADMIN_ID != 0 and message.from_user.id != SUPER_ADMIN_ID:
        return
    
    args = message.text.split()
    if len(args) < 2:
        await message.answer("Использование: `/makeadmin USER_ID`", parse_mode="Markdown")
        return
    
    try:
        target_id = int(args[1])
        set_admin_status(target_id, 1)
        user_info = get_user(target_id)
        target_name = user_info[0] if user_info else "Пользователь"
        
        await message.answer(f"✅ {target_name} (ID: {target_id}) успешно назначен администратором!")
        
        # Оповещаем пользователя
        await bot.send_message(target_id, f"🎉 Поздравляем, {target_name}! Вам выданы права администратора. Введите /start для входа в панель.")
    except Exception as e:
        await message.answer(f"Ошибка: {e}")

# --- Веб-сервер для Render (Health Check) ---
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
