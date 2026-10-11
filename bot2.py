import asyncio
import logging
import sys
import os
from aiohttp import web
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardMarkup, KeyboardButton

TOKEN = "8963845436:AAEX8346I6DEZGnDKCdqIcXBW6STyGlBxeo"

# Временная база данных
categories = ["Кофе", "Сладости", "Снеки", "Напитки", "Рамен"]
products = [
    {"id": 1, "name": "Кока-Кола 1.5л", "price": 150, "category": "Напитки"}
]
ads = [
    {"id": 1, "title": "Скидки до 30% на соки!", "photo": None}
]

class AdStates(StatesGroup):
    waiting_for_photo = State()
    waiting_for_title = State()

def get_main_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🛍 Каталог"), KeyboardButton(text="👤 Мой профиль")],
            [KeyboardButton(text="➕ Добавить товар"), KeyboardButton(text="🗑 Снять товар")],
            [KeyboardButton(text="🏷 Сделать скидку"), KeyboardButton(text="📋 Список товаров")],
            [KeyboardButton(text="👑 Панель Владельца")]
        ],
        resize_keyboard=True
    )

def get_owner_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📢 Поставить рекламу"), KeyboardButton(text="🗑 Убрать рекламу")],
            [KeyboardButton(text="📂 Удалить каталог"), KeyboardButton(text="❌ Убрать все товары")],
            [KeyboardButton(text="🔙 Назад в главное меню")]
        ],
        resize_keyboard=True
    )

# Заглушка веб-сервера для Render, чтобы он не отключал бота
async def handle(request):
    return web.Response(text="Bot is running!")

async def web_server():
    app = web.Application()
    app.router.add_get("/", handle)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.environ.get("PORT", 10000))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()

async def main():
    logging.basicConfig(level=logging.INFO, stream=sys.stdout)
    bot = Bot(token=TOKEN)
    dp = Dispatcher()

    @dp.message(Command("start"))
    async def cmd_start(message: types.Message, state: FSMContext):
        await state.clear()
        await message.answer(
            "Привет! Бот успешно запущен в облаке и работает 24/7.",
            reply_markup=get_main_keyboard()
        )

    @dp.message(F.text == "🔙 Назад в главное меню")
    async def back_to_main(message: types.Message, state: FSMContext):
        await state.clear()
        await message.answer("Главное меню:", reply_markup=get_main_keyboard())

    @dp.message(F.text == "👑 Панель Владельца")
    async def owner_panel(message: types.Message):
        await message.answer("Панель управления рекламой и каталогом:", reply_markup=get_owner_keyboard())

    @dp.message(F.text == "🛍 Каталог")
    async def menu_catalog(message: types.Message):
        cats_text = ", ".join(categories)
        await message.answer(f"📂 Текущие категории в каталоге:\n{cats_text}")

    @dp.message(F.text == "👤 Мой профиль")
    async def menu_profile(message: types.Message):
        await message.answer(f"👤 Администратор магазина.\n🆔 ID: {message.from_user.id}")

    @dp.message(F.text == "❌ Убрать все товары")
    async def clear_all_products(message: types.Message):
        products.clear()
        await message.answer("✅ Все товары удалены!", reply_markup=get_owner_keyboard())

    @dp.message(F.text == "📢 Поставить рекламу")
    async def start_add_ad(message: types.Message, state: FSMContext):
        await message.answer("Отправьте **фотографию** для рекламного баннера:")
        await state.set_state(AdStates.waiting_for_photo)

    @dp.message(AdStates.waiting_for_photo, F.photo)
    async def ad_received_photo(message: types.Message, state: FSMContext):
        photo_id = message.photo[-1].file_id
        await state.update_data(photo=photo_id)
        await message.answer("Теперь введите текст рекламы:")
        await state.set_state(AdStates.waiting_for_title)

    @dp.message(AdStates.waiting_for_title, F.text)
    async def ad_received_title(message: types.Message, state: FSMContext):
        data = await state.get_data()
        ads.append({"id": len(ads) + 1, "title": message.text, "photo": data.get("photo")})
        await state.clear()
        await message.answer("🎉 Рекламный баннер добавлен!", reply_markup=get_owner_keyboard())

    @dp.message(F.text == "📋 Список товаров")
    async def list_products(message: types.Message):
        if not products:
            await message.answer("Список пуст.")
            return
        text = "\n".join([f"- {p['name']} ({p['price']} ₽)" for p in products])
        await message.answer(f"Товары:\n{text}")

    # Запускаем и веб-сервер для Render, и самого бота одновременно
    await asyncio.gather(web_server(), dp.start_polling(bot))

if __name__ == "__main__":
    asyncio.run(main())
