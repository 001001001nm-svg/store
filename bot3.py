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
BOT_TOKEN = os.environ.get("BOT_TOKEN", "8873651066:AAHT3ma7r3vv1YRWxRwfBz8zJ74rT_Kzm20")
SUPER_ADMIN_ID = int(os.environ.get("SUPER_ADMIN_ID", "8636623905"))
DB_PATH = os.environ.get("DB_PATH", "store.db")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

BOT_ACTIVE = True
SITE_ACTIVE = True

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
    except Exception as e:
        logging.error(f"DB Error: {e}")

init_db()

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

@dp.message(F.text == "🔙 Главное меню")
async def back_to_main(message: types.Message):
    user_id = message.from_user.id
    user_data = get_user(user_id)
    is_admin = user_data[1] == 1 if user_data else False
    kb = main_menu_kb(user_id == SUPER_ADMIN_ID, is_admin)
    await message.answer("Главное меню:", reply_markup=kb)

# ============================================================
#  ВЕБ-СЕРВЕР И КРАСИВАЯ ВИТРИНА С КОРЗИНОЙ И ДОСТАВКОЙ
# ============================================================
async def handle_website(request):
    global SITE_ACTIVE
    if not SITE_ACTIVE:
        return web.Response(text="🛠 Сайт временно отключен владельцем.", status=503, content_type="text/plain; charset=utf-8")

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

    html_content = f"""<!DOCTYPE html>
<html lang="ru">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Продуктовый Магазин</title>
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
    <style>
        * {{ box-sizing: border-box; }}
        body {{ font-family: 'Inter', sans-serif; background-color: #f4f7f5; margin: 0; padding: 0; color: #2c3e50; }}
        
        header {{ background: #1e7e34; color: white; padding: 15px 30px; display: flex; justify-content: space-between; align-items: center; box-shadow: 0 4px 12px rgba(0,0,0,0.1); sticky: top; top: 0; z-index: 100; }}
        .logo {{ font-size: 22px; font-weight: 700; display: flex; align-items: center; gap: 10px; }}
        .cart-btn-header {{ background: #ffffff; color: #1e7e34; border: none; padding: 10px 18px; border-radius: 25px; font-weight: 600; cursor: pointer; display: flex; align-items: center; gap: 8px; transition: all 0.2s ease; box-shadow: 0 2px 6px rgba(0,0,0,0.1); }}
        .cart-btn-header:hover {{ background: #e8f5e9; transform: translateY(-1px); }}
        .cart-badge {{ background: #e74c3c; color: white; border-radius: 50%; padding: 2px 7px; font-size: 12px; font-weight: 700; }}

        .search-bar {{ background: #fff; padding: 15px 20px; display: flex; justify-content: center; border-bottom: 1px solid #e2e8f0; }}
        .search-input {{ width: 100%; max-width: 600px; padding: 12px 20px; border-radius: 25px; border: 1px solid #cbd5e1; outline: none; font-size: 15px; transition: border 0.2s; }}
        .search-input:focus {{ border-color: #1e7e34; }}

        .container {{ max-width: 1200px; margin: 25px auto; padding: 0 20px; }}

        .banner {{ background: linear-gradient(135deg, #1e7e34, #27ae60); color: white; border-radius: 16px; padding: 25px 30px; margin-bottom: 30px; box-shadow: 0 6px 18px rgba(30,126,52,0.25); }}
        .banner h2 {{ margin: 0 0 10px 0; font-size: 24px; font-weight: 700; }}
        .banner p {{ margin: 0; font-size: 15px; opacity: 0.95; }}

        .categories-container {{ display: flex; gap: 10px; margin-bottom: 30px; overflow-x: auto; padding-bottom: 8px; scrollbar-width: none; }}
        .cat-pill {{ background: #e8f5e9; color: #1e7e34; padding: 8px 18px; border-radius: 20px; font-weight: 600; font-size: 14px; white-space: nowrap; border: 1px solid #c8e6c9; cursor: pointer; transition: all 0.2s; }}
        .cat-pill:hover {{ background: #1e7e34; color: white; }}

        h3 {{ font-size: 22px; color: #1e293b; margin-bottom: 20px; font-weight: 700; }}
        .products-grid {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(240px, 1fr)); gap: 22px; margin-bottom: 40px; }}
        .product-card {{ background: white; border-radius: 16px; padding: 18px; box-shadow: 0 4px 12px rgba(0,0,0,0.03); border: 1px solid #f1f5f9; display: flex; flex-direction: column; justify-content: space-between; transition: transform 0.2s, box-shadow 0.2s; }}
        .product-card:hover {{ transform: translateY(-4px); box-shadow: 0 10px 20px rgba(0,0,0,0.06); }}
        .product-cat {{ font-size: 11px; color: #1e7e34; background: #e8f5e9; padding: 4px 8px; border-radius: 6px; display: inline-block; margin-bottom: 10px; font-weight: 700; text-transform: uppercase; letter-spacing: 0.5px; }}
        .product-title {{ font-size: 16px; font-weight: 600; margin-bottom: 8px; color: #1e293b; line-height: 1.3; }}
        .product-price {{ font-size: 20px; color: #1e7e34; font-weight: 700; margin-bottom: 15px; display: flex; align-items: center; gap: 8px; }}
        .product-discount {{ background: #e67e22; color: white; padding: 2px 6px; border-radius: 6px; font-size: 12px; font-weight: 700; }}
        .add-btn {{ background: #1e7e34; color: white; border: none; padding: 10px; border-radius: 10px; cursor: pointer; font-weight: 600; width: 100%; transition: background 0.2s; }}
        .add-btn:hover {{ background: #155d27; }}

        /* КОРЗИНА И САЙДБАР */
        .cart-overlay {{ position: fixed; top: 0; left: 0; width: 100%; height: 100%; background: rgba(0,0,0,0.4); display: none; z-index: 1000; backdrop-filter: blur(2px); }}
        .cart-sidebar {{ position: fixed; top: 0; right: -420px; width: 100%; max-width: 420px; height: 100%; background: white; z-index: 1001; transition: right 0.3s ease; display: flex; flex-direction: column; box-shadow: -6px 0 20px rgba(0,0,0,0.15); }}
        .cart-sidebar.open {{ right: 0; }}
        .cart-overlay.open {{ display: block; }}

        .cart-header {{ padding: 20px; border-bottom: 1px solid #f1f5f9; display: flex; justify-content: space-between; align-items: center; }}
        .cart-header h3 {{ margin: 0; font-size: 20px; }}
        .close-cart {{ background: none; border: none; font-size: 24px; cursor: pointer; color: #64748b; }}

        /* КРАСОЧНЫЙ БАННЕР БЕСПЛАТНОЙ ДОСТАВКИ В КОРЗИНЕ */
        .delivery-promo {{ background: linear-gradient(135deg, #fff3cd, #ffe8a1); border: 1px solid #ffe082; padding: 14px 18px; margin: 15px 20px 5px 20px; border-radius: 12px; font-size: 13px; color: #856404; font-weight: 600; box-shadow: 0 2px 6px rgba(0,0,0,0.03); }}
        .delivery-progress-bar {{ background: #ffe082; height: 6px; border-radius: 3px; margin-top: 8px; overflow: hidden; }}
        .delivery-progress-fill {{ background: #27ae60; height: 100%; width: 0%; transition: width 0.3s; }}

        .cart-items {{ flex: 1; overflow-y: auto; padding: 15px 20px; }}
        .cart-item {{ display: flex; justify-content: space-between; align-items: center; margin-bottom: 15px; padding-bottom: 15px; border-bottom: 1px solid #f1f5f9; }}
        .cart-item-title {{ font-weight: 600; font-size: 14px; margin-bottom: 4px; color: #1e293b; }}
        .cart-item-price {{ color: #1e7e34; font-weight: 700; font-size: 14px; }}
        .cart-controls {{ display: flex; align-items: center; gap: 8px; }}
        .qty-btn {{ background: #f1f5f9; border: none; width: 26px; height: 26px; border-radius: 6px; font-weight: bold; cursor: pointer; }}

        .cart-footer {{ padding: 20px; border-top: 1px solid #f1f5f9; background: #fafafa; }}
        .cart-summary-line {{ display: flex; justify-content: space-between; margin-bottom: 8px; font-size: 14px; color: #64748b; }}
        .cart-summary-total {{ display: flex; justify-content: space-between; margin-top: 12px; font-size: 18px; font-weight: 700; color: #1e293b; border-top: 1px solid #e2e8f0; padding-top: 10px; }}
        .checkout-btn {{ background: #1e7e34; color: white; border: none; width: 100%; padding: 14px; border-radius: 12px; font-weight: 700; font-size: 16px; margin-top: 15px; cursor: pointer; transition: background 0.2s; }}
        .checkout-btn:hover {{ background: #155d27; }}

        footer {{ background: #1e7e34; color: white; padding: 40px 20px; margin-top: 60px; }}
        .footer-content {{ max-width: 1200px; margin: 0 auto; display: flex; justify-content: space-between; flex-wrap: wrap; gap: 30px; }}
        .footer-col h4 {{ margin-top: 0; margin-bottom: 15px; font-size: 16px; font-weight: 700; }}
        .footer-col p, .footer-col a {{ font-size: 14px; opacity: 0.9; color: white; text-decoration: none; display: block; margin-bottom: 8px; }}
        .footer-bottom {{ text-align: center; margin-top: 30px; font-size: 13px; opacity: 0.8; border-top: 1px solid rgba(255,255,255,0.2); padding-top: 15px; }}
    </style>
</head>
<body>
    <header>
        <div class="logo">🛒 Продуктовый Магазин</div>
        <button class="cart-btn-header" onclick="openCart()">
            🛒 Корзина <span class="cart-badge" id="cartBadge">0</span>
        </button>
    </header>

    <div class="search-bar">
        <input type="text" class="search-input" placeholder="Поиск в Продуктовом Магазине..." onkeyup="filterProducts(this.value)">
    </div>

    <div class="container">
    """

    if ads:
        for title, content in ads:
            html_content += f"""
            <div class="banner">
                <h2>🔥 {html.escape(title)}</h2>
                <p>{html.escape(content)}</p>
            </div>
            """
    else:
        html_content += """
        <div class="banner">
            <h2>🎉 Свежие продукты с доставкой!</h2>
            <p>Заказывайте любимые товары онлайн. Бесплатная доставка при заказе от 5000 руб.!</p>
        </div>
        """

    if categories:
        html_content += '<div class="categories-container"><div class="cat-pill" onclick="filterCat(\'\')">Все товары</div>'
        for cat in categories:
            html_content += f'<div class="cat-pill" onclick="filterCat(\'{html.escape(cat)}\')">{html.escape(cat)}</div>'
        html_content += '</div>'

    html_content += '<h3>Популярные товары</h3><div class="products-grid" id="productsGrid">'
    if products:
        for p_id, name, unit, price, photo, discount, cat in products:
            disc_badge = f'<span class="product-discount">-{html.escape(str(discount))}</span>' if discount else ''
            cat_label = f'<div class="product-cat">{html.escape(cat or "Общее")}</div>'
            html_content += f"""
                <div class="product-card" data-title="{html.escape(name.lower())}" data-cat="{html.escape(cat or '')}">
                    <div>
                        {cat_label}
                        <div class="product-title">{html.escape(name)}</div>
                        <div class="product-price">{price} руб. {disc_badge}</div>
                    </div>
                    <button class="add-btn" onclick="addToCart({p_id}, '{html.escape(name)}', {price}, '{html.escape(unit)}')">В корзину ({html.escape(unit)})</button>
                </div>
            """
    else:
        html_content += '<p style="grid-column: 1/-1; text-align: center; color: #64748b;">В магазине пока нет товаров. Добавьте их через Telegram-бота.</p>'

    html_content += """
        </div>
    </div>

    <!-- КОРЗИНА -->
    <div class="cart-overlay" id="cartOverlay" onclick="closeCart()"></div>
    <div class="cart-sidebar" id="cartSidebar">
        <div class="cart-header">
            <h3>Корзина</h3>
            <button class="close-cart" onclick="closeCart()">&times;</button>
        </div>

        <!-- КРАСОЧНЫЙ БАННЕР БЕСПЛАТНОЙ ДОСТАВКИ -->
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
                <h4>Продуктовый Магазин</h4>
                <p>Все свежие продукты прямо к вашей двери. Быстрая доставка и лучшая цена.</p>
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
            © 2026 Продуктовый Магазин. Все права защищены.
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
                let diff = 5000 - subtotal;
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
                            <div class="cart-item-price">${item.price * item.qty} руб.</div>
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

    return web.Response(text=html_content, content_type="text/html; charset=utf-8")

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
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
