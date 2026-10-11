import os
import sys
import io
import json
import random
import datetime
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
import telebot
from telebot import types
import requests
from tavily import TavilyClient

# ==========================================
# 1. МИНИ ВЕБ-СЕРВЕР ДЛЯ RENDER
# ==========================================
class SimpleHTTPRequestHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")

    def do_HEAD(self):
        self.send_response(200)

def run_web_server():
    port = int(os.environ.get("PORT", 10000))
    try:
        server = HTTPServer(('0.0.0.0', port), SimpleHTTPRequestHandler)
        print(f"Web server running on port {port}")
        server.serve_forever()
    except Exception as e:
        print(f"Web server error: {e}")

threading.Thread(target=run_web_server, daemon=True).start()

# ==========================================
# 2. ИНИЦИАЛИЗАЦИЯ И ХРАНЕНИЕ ДАННЫХ
# ==========================================
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "8891087735:AAEv55LG3oQwkEJMN_LBUnEgM-ZMq7qwPf4")
# Вставьте ваш ключ OpenRouter (sk-or-v1-...) в двойные кавычки, если его нет в Environment Variables Render:
OPENROUTER_KEY = os.environ.get("OPENROUTER_KEY", "")
UNSPLASH_KEY = os.environ.get("UNSPLASH_ACCESS_KEY", "odaMYOHO8tsFHyGe_Q8-0EZtab_NohgvOYRBEvtIkRE")
TAVILY_KEY = os.environ.get("TAVILY_API_KEY", "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")

OWNER_ID = 8762115500

bot = telebot.TeleBot(TELEGRAM_TOKEN)
tavily_client = TavilyClient(api_key=TAVILY_KEY) if TAVILY_KEY else None

DATA_FILE = "users_data.json"

def load_data():
    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                all_users = {int(k): v for k, v in data.get("all_users", {}).items()}
                admins = set(data.get("admins", []))
                banned_users = set(data.get("banned_users", []))
                bot_enabled = data.get("bot_enabled", True)
                return all_users, admins, banned_users, bot_enabled
        except Exception as e:
            print(f"Ошибка чтения базы данных: {e}")
    return {}, set(), set(), True

def save_data():
    try:
        data = {
            "all_users": all_users,
            "admins": list(admins),
            "banned_users": list(banned_users),
            "bot_enabled": bot_enabled
        }
        with open(DATA_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"Ошибка сохранения базы данных: {e}")

all_users, admins, banned_users, bot_enabled = load_data()
user_states = {}

LANGUAGES = ["🇷🇺 Русский", "🇰🇬 Кыргызча", "🇬🇧 English", "🇹🇷 Türkçe"]
COUNTRIES = ["🇰🇬 Кыргызстан", "🇹🇷 Турция", "🇺🇸 США", "🇨🇳 Китай", "🇰🇷 Южная Корея", "🇨🇦 Канада"]

# ==========================================
# 3. ФУНКЦИЯ ВЕБ-ПОИСКА (TAVILY API)
# ==========================================
def search_web(query):
    if not tavily_client:
        return ""
    try:
        response = tavily_client.search(query=query, search_depth="basic", max_results=3)
        results = response.get("results", [])
        if not results:
            return ""
        snippets = []
        for item in results:
            snippets.append(f"Заголовок: {item.get('title')}\nИнформация: {item.get('content')}")
        return "\n\n".join(snippets)
    except Exception as e:
        print(f"Ошибка поиска в веб: {e}")
        return ""

# ==========================================
# 4. ПОЛУЧЕНИЕ ТОЧНЫХ ФОТО И ИИ
# ==========================================
def fetch_photo_bytes(query):
    if UNSPLASH_KEY:
        search_url = f"https://api.unsplash.com/search/photos?page=1&query={query}&per_page=10&client_id={UNSPLASH_KEY}"
        try:
            res = requests.get(search_url, timeout=7)
            if res.status_code == 200:
                results = res.json().get('results', [])
                if results:
                    img_url = random.choice(results)['urls']['small']
                    img_res = requests.get(img_url, timeout=7)
                    if img_res.status_code == 200:
                        photo_bytes = io.BytesIO(img_res.content)
                        photo_bytes.name = "image.jpg"
                        return photo_bytes
        except Exception as e:
            print(f"Unsplash error: {e}")

    fallback_urls = ["https://picsum.photos/800/600", "https://picsum.photos/800/601"]
    try:
        fb_res = requests.get(random.choice(fallback_urls), timeout=7)
        if fb_res.status_code == 200:
            photo_bytes = io.BytesIO(fb_res.content)
            photo_bytes.name = "image.jpg"
            return photo_bytes
    except Exception as e:
        print(f"Fallback photo error: {e}")
    return None

def clean_ai_response(text):
    lines = text.split('\n')
    filtered = [l for l in lines if "user safety:" not in l.lower()]
    res = "\n".join(filtered).strip()
    for char in ["*", "#", "_", "`", "~"]:
        res = res.replace(char, "")
    return res

def ask_ai(prompt, system_instruction):
    if OPENROUTER_KEY and len(OPENROUTER_KEY) > 10:
        url = "https://openrouter.ai/api/v1/chat/completions"
        headers = {
            "Authorization": f"Bearer {OPENROUTER_KEY}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://render.com",
            "X-Title": "Barsbek Bot"
        }
        
        models_to_try = [
            "google/gemini-2.0-flash-lite-preview-02-05:free",
            "meta-llama/llama-3.3-70b-instruct:free",
            "openai/gpt-4o-mini"
        ]
        
        for model in models_to_try:
            data = {
                "model": model,
                "messages": [
                    {"role": "system", "content": system_instruction},
                    {"role": "user", "content": prompt}
                ],
                "max_tokens": 500,
                "temperature": 0.3
            }
            try:
                response = requests.post(url, headers=headers, json=data, timeout=25)
                if response.status_code == 200:
                    res_data = response.json()
                    if 'choices' in res_data and len(res_data['choices']) > 0:
                        raw_content = res_data['choices'][0]['message']['content']
                        if raw_content and raw_content.strip():
                            return clean_ai_response(raw_content)
            except Exception as e:
                print(f"Ошибка ИИ запроса ({model}): {e}")
                continue

    # Подробный ответ-резерв по категориям
    if "ВУЗ" in prompt or "ЖОЖ" in prompt:
        return "🏛️ Топ ВУЗы и Колледжи:\n1. Городской Университет — ведет прием по международным программам.\n2. Технологический Институт — гранты на технические специальности.\n3. Национальная Академия — программы обмена и стипендии."
    elif "Стипендии" in prompt or "Гранты" in prompt:
        return "💰 Стипендии и Гранты:\n• Государственные гранты (Full Tuition) — покрывают 100% стоимости.\n• Стипендии университетов — от $1,000 до $5,000 в год.\n• Гранты на проживание и питание."
    elif "Документ" in prompt:
        return "📄 Основные документы:\n1. Заграничный паспорт (срок действия от 1 года).\n2. Аттестат/диплом с нотариальным переводом.\n3. Языковой сертификат (IELTS / TOEFL / HSK).\n4. Мотивационное письмо."
    else:
        return "💡 Полезные рекомендации:\n• Подавайте документы минимум за 6 месяцев до начала учебы.\n• Проверяйте дедлайны подачи на стипендии заранее."

# ==========================================
# 5. КЛАВИАТУРЫ
# ==========================================
def get_language_keyboard():
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
    markup.row("🇷🇺 Русский", "🇰🇬 Кыргызча")
    markup.row("🇬🇧 English", "🇹🇷 Türkçe")
    return markup

def get_country_keyboard():
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
    markup.row("🇰🇬 Кыргызстан", "🇹🇷 Турция")
    markup.row("🇺🇸 США", "🇨🇳 Китай")
    markup.row("🇰🇷 Южная Корея", "🇨🇦 Канада")
    return markup

def get_main_keyboard(user_id, lang):
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    if lang == "🇰🇬 Кыргызча":
        markup.row("🔥 Топ ЖОЖдор жана Колледждер", "💡 Лайфхактар жана Талаптар")
        markup.row("📄 Документтер тизмеси", "💰 Стипендиялар")
        markup.row("🌍 Өлкөнү алмаштыруу", "🌐 Тилди алмаштыруу")
        markup.row("🔄 Жаңы диалог")
    else:
        markup.row("🔥 Топ ВУЗы и Колледжи", "💡 Лайфхаки и Требования")
        markup.row("📄 Список документов", "💰 Стипендии и Гранты")
        markup.row("🌍 Сменить страну", "🌐 Сменить язык")
        markup.row("🔄 Начать заново")

    is_owner = (user_id == OWNER_ID)
    is_admin = (user_id in admins) or is_owner

    admin_btns = []
    if is_admin:
        admin_btns.append("🛠 Панель Админа")
    if is_owner:
        admin_btns.append("👑 Панель Владельца")
    
    if admin_btns:
        markup.row(*admin_btns)

    return markup

def get_admin_panel_keyboard():
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    markup.row("👥 Все участники", "📊 Участников за сегодня")
    markup.row("🔙 Главное меню")
    return markup

def get_owner_panel_keyboard():
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    markup.row("👥 Все участники", "📊 Участников за сегодня")
    markup.row("📋 Список администраторов", "➕ Назначить администратором")
    
    status_btn = "🔴 Выключить бот" if bot_enabled else "🟢 Включить бот"
    markup.row(status_btn)
    markup.row("🔙 Главное меню")
    return markup

def get_reels_inline_buttons():
    markup = types.InlineKeyboardMarkup()
    markup.add(
        types.InlineKeyboardButton("🔥 Ещё факт / ВУЗ", callback_data="act_more_fact"),
        types.InlineKeyboardButton("📸 Посмотреть фото", callback_data="act_more_photo")
    )
    return markup

# ==========================================
# 6. ХЭНДЛЕРЫ I: ИНЛАЙН УПРАВЛЕНИЕ
# ==========================================
@bot.callback_query_handler(func=lambda call: True)
def handle_callbacks(call):
    global bot_enabled
    user_id = call.from_user.id
    data = call.data

    if data.startswith("ban_"):
        target_id = int(data.split("_")[1])
        banned_users.add(target_id)
        save_data()
        bot.answer_callback_query(call.id, "❌ Пользователь заблокирован")
        bot.edit_message_text(f"⛔ Пользователь {target_id} заблокирован!", call.message.chat.id, call.message.message_id)
        return

    if data.startswith("unban_"):
        target_id = int(data.split("_")[1])
        banned_users.discard(target_id)
        save_data()
        bot.answer_callback_query(call.id, "✅ Пользователь разблокирован")
        bot.edit_message_text(f"✅ Пользователь {target_id} разблокирован!", call.message.chat.id, call.message.message_id)
        return

    if data.startswith("removeadmin_"):
        if user_id != OWNER_ID:
            bot.answer_callback_query(call.id, "⛔ Доступно только Владельцу!")
            return
        target_id = int(data.split("_")[1])
        admins.discard(target_id)
        save_data()
        bot.answer_callback_query(call.id, "✅ Админ снят с должности")
        bot.edit_message_text(f"❌ Пользователь {target_id} больше не администратор.", call.message.chat.id, call.message.message_id)
        return

    if data.startswith("act_"):
        user_data = user_states.get(user_id, {})
        c_country = user_data.get("country", "Китай")
        c_lang = user_data.get("lang", "🇷🇺 Русский")

        try:
            bot.answer_callback_query(call.id, "⏳ Обработка запроса...")
        except Exception:
            pass

        if data == "act_more_photo":
            photo_file = fetch_photo_bytes(f"{c_country} university campus student")
            short_caption = f"🏛️ Кампус и университетская атмосфера в {c_country}."
            if photo_file:
                try:
                    bot.send_photo(call.message.chat.id, photo=photo_file, caption=short_caption, reply_markup=get_reels_inline_buttons())
                    return
                except Exception as e:
                    print(f"Ошибка фото: {e}")
            bot.send_message(call.message.chat.id, "📸 Не удалось загрузить фото. Попробуйте еще раз.", reply_markup=get_reels_inline_buttons())
            return

        prompt = f"Напишите 1 интересный факт или ведущий университет в {c_country}."
        sys_prompt = f"You are a professional educational advisor. Language: {c_lang}. Professional tone, concise (max 2 sentences). Use clean emojis."
        ai_text = ask_ai(prompt, sys_prompt)
        photo_file = fetch_photo_bytes(f"{c_country} university building")

        if photo_file:
            try:
                bot.send_photo(call.message.chat.id, photo=photo_file, caption=ai_text, reply_markup=get_reels_inline_buttons())
                return
            except Exception as e:
                print(f"Ошибка фото: {e}")

        bot.send_message(call.message.chat.id, ai_text, reply_markup=get_reels_inline_buttons())

# ==========================================
# 7. ХЭНДЛЕРЫ II: СООБЩЕНИЯ И ЛОГИКА
# ==========================================
@bot.message_handler(commands=['start'])
def start(message):
    global bot_enabled
    user_id = message.from_user.id

    if user_id in banned_users:
        return

    if not bot_enabled and user_id != OWNER_ID:
        bot.send_message(message.chat.id, "🔴 Бот временно выключен на техническое обслуживание.")
        return

    today_str = datetime.date.today().isoformat()
    username_str = f"@{message.from_user.username}" if message.from_user.username else f"ID: {user_id}"

    if user_id not in all_users:
        all_users[user_id] = {
            "username": username_str,
            "first_name": message.from_user.first_name or "Пользователь",
            "join_date": today_str,
            "last_active": today_str
        }
        save_data()
    else:
        all_users[user_id]["last_active"] = today_str
        save_data()

    if user_id not in user_states:
        user_states[user_id] = {
            "lang": "🇷🇺 Русский", "name": None, "age": None, "country": None, "state": None
        }

    u_data = user_states[user_id]

    if u_data.get("name") and u_data.get("age"):
        u_data["state"] = "WAITING_FOR_COUNTRY"
        name = u_data["name"]
        msg = f"Здравствуйте, {name}! 👋 Рад приветствовать вас снова.\nПожалуйста, выберите страну для консультации по обучению:"
        bot.send_message(message.chat.id, msg, reply_markup=get_country_keyboard())
    else:
        u_data["state"] = "WAITING_FOR_LANG"
        msg = "Здравствуйте! Я Барсбек 🐆 — ваш консультант по образованию за рубежом.\n\nВыберите язык / Тилди тандаңыз:"
        bot.send_message(message.chat.id, msg, reply_markup=get_language_keyboard())

@bot.message_handler(func=lambda message: True)
def handle_message(message):
    global bot_enabled
    user_id = message.from_user.id
    text = message.text

    if user_id in banned_users:
        return

    if not bot_enabled and user_id != OWNER_ID:
        bot.send_message(message.chat.id, "🔴 Бот временно выключен на техническое обслуживание.")
        return

    today_str = datetime.date.today().isoformat()
    username_str = f"@{message.from_user.username}" if message.from_user.username else f"ID: {user_id}"

    if user_id not in all_users:
        all_users[user_id] = {
            "username": username_str,
            "first_name": message.from_user.first_name or "Пользователь",
            "join_date": today_str,
            "last_active": today_str
        }
        save_data()
    else:
        all_users[user_id]["last_active"] = today_str

    if user_id not in user_states:
        user_states[user_id] = {"lang": "🇷🇺 Русский", "name": None, "age": None, "country": "Китай", "state": None}

    user_data = user_states[user_id]
    current_state = user_data.get("state")

    if current_state == "WAITING_FOR_ADMIN_INPUT" and user_id == OWNER_ID:
        user_data["state"] = None
        target = text.strip().replace("@", "")
        found_id = int(target) if target.isdigit() else None
        
        if not found_id:
            for uid, info in all_users.items():
                if info["username"].replace("@", "").lower() == target.lower():
                    found_id = uid
                    break

        if found_id:
            admins.add(found_id)
            save_data()
            bot.send_message(message.chat.id, f"✅ Пользователь {target} назначен администратором.", reply_markup=get_owner_panel_keyboard())
        else:
            bot.send_message(message.chat.id, "❌ Участник не найден в базе.", reply_markup=get_owner_panel_keyboard())
        return

    if current_state == "WAITING_FOR_LANG" or text in LANGUAGES:
        if text in LANGUAGES:
            user_data["lang"] = text
            if not user_data.get("name"):
                user_data["state"] = "WAITING_FOR_NAME"
                bot.send_message(message.chat.id, "Как к вам обращаться? (Укажите ваше имя)", reply_markup=types.ReplyKeyboardRemove())
                return
            else:
                user_data["state"] = "WAITING_FOR_COUNTRY"
                bot.send_message(message.chat.id, "Выберите страну для обучения:", reply_markup=get_country_keyboard())
                return

    if current_state == "WAITING_FOR_NAME":
        user_data["name"] = text
        user_data["state"] = "WAITING_FOR_AGE"
        bot.send_message(message.chat.id, f"Приятно познакомиться, {text}! Укажите ваш возраст:")
        return

    if current_state == "WAITING_FOR_AGE":
        user_data["age"] = text
        user_data["state"] = "WAITING_FOR_COUNTRY"
        bot.send_message(message.chat.id, "Спасибо! Теперь выберите страну обучения:", reply_markup=get_country_keyboard())
        return

    if current_state == "WAITING_FOR_COUNTRY" or text in COUNTRIES:
        if text in COUNTRIES:
            user_data["country"] = text
            user_data["state"] = None
            lang = user_data.get("lang", "🇷🇺 Русский")
            
            welcome_txt = f"🎓 Выбрана страна: {text}\nНажмите на интересующие вас разделы меню ниже."
            photo = fetch_photo_bytes(f"{text} architecture capital landmark")
            
            if photo:
                try:
                    bot.send_photo(message.chat.id, photo=photo, caption=welcome_txt, reply_markup=get_main_keyboard(user_id, lang))
                    return
                except Exception:
                    pass
            bot.send_message(message.chat.id, welcome_txt, reply_markup=get_main_keyboard(user_id, lang))
            return

    lang = user_data.get("lang", "🇷🇺 Русский")
    c_country = user_data.get("country", "Китай")

    if text == "🛠 Панель Админа" and (user_id == OWNER_ID or user_id in admins):
        bot.send_message(message.chat.id, "🛠 Панель Администратора", reply_markup=get_admin_panel_keyboard())
        return

    if text == "👑 Панель Владельца" and user_id == OWNER_ID:
        bot.send_message(message.chat.id, "👑 Панель Владельца", reply_markup=get_owner_panel_keyboard())
        return

    if text in ["🔴 Выключить бот", "🟢 Включить бот"] and user_id == OWNER_ID:
        bot_enabled = (text == "🟢 Включить бот")
        save_data()
        status_msg = "🟢 Бот включен." if bot_enabled else "🔴 Бот выключен для всех пользователей."
        bot.send_message(message.chat.id, status_msg, reply_markup=get_owner_panel_keyboard())
        return

    if text == "➕ Назначить администратором" and user_id == OWNER_ID:
        user_data["state"] = "WAITING_FOR_ADMIN_INPUT"
        bot.send_message(message.chat.id, "Введите @username или Telegram ID:")
        return

    if text == "👥 Все участники" and (user_id == OWNER_ID or user_id in admins):
        if not all_users:
            bot.send_message(message.chat.id, "👥 Список пуст.")
            return

        bot.send_message(message.chat.id, f"👥 **Всего зарегистрировано пользователей: {len(all_users)}**", parse_mode="Markdown")
        for uid, info in all_users.items():
            uname = info.get("username", f"ID: {uid}")
            name = info.get("first_name", "Пользователь")
            is_ban = uid in banned_users
            
            markup = types.InlineKeyboardMarkup()
            if is_ban:
                markup.add(types.InlineKeyboardButton("✅ Разблокировать", callback_data=f"unban_{uid}"))
            else:
                markup.add(types.InlineKeyboardButton("⛔ Заблокировать", callback_data=f"ban_{uid}"))

            status_str = " (⛔ Заблокирован)" if is_ban else ""
            bot.send_message(message.chat.id, f"👤 **{name}** ({uname}){status_str}\n🆔 `{uid}`", parse_mode="Markdown", reply_markup=markup)
        return

    if text == "📋 Список администраторов" and user_id == OWNER_ID:
        if not admins:
            bot.send_message(message.chat.id, "📋 Список администраторов пуст.")
            return

        bot.send_message(message.chat.id, f"📋 **Назначенные администраторы ({len(admins)}):**", parse_mode="Markdown")
        for admin_id in list(admins):
            info = all_users.get(admin_id, {})
            uname = info.get("username", f"ID: {admin_id}")
            name = info.get("first_name", "Администратор")
            is_ban = admin_id in banned_users

            markup = types.InlineKeyboardMarkup()
            markup.row(
                types.InlineKeyboardButton("❌ Снять с админа", callback_data=f"removeadmin_{admin_id}"),
                types.InlineKeyboardButton("✅ Разблокировать" if is_ban else "⛔ Заблокировать", callback_data=f"unban_{admin_id}" if is_ban else f"ban_{admin_id}")
            )

            bot.send_message(message.chat.id, f"👑 **{name}** ({uname})\n🆔 `{admin_id}`", parse_mode="Markdown", reply_markup=markup)
        return

    if text == "📊 Участников за сегодня" and (user_id == OWNER_ID or user_id in admins):
        today_str = datetime.date.today().isoformat()
        count = sum(1 for u in all_users.values() if u.get("last_active") == today_str)
        bot.send_message(message.chat.id, f"📊 Активных пользователей за сегодня: {count}")
        return

    if text == "🔙 Главное меню":
        bot.send_message(message.chat.id, "🏡 Главное меню:", reply_markup=get_main_keyboard(user_id, lang))
        return

    if text in ["🌐 Сменить язык", "🌐 Тилди алмаштыруу"]:
        user_data["state"] = "WAITING_FOR_LANG"
        bot.send_message(message.chat.id, "Выберите язык / Тилди тандаңыз:", reply_markup=get_language_keyboard())
        return

    if text in ["🌍 Сменить страну", "🌍 Өлкөнү алмаштыруу"]:
        user_data["state"] = "WAITING_FOR_COUNTRY"
        bot.send_message(message.chat.id, "Выберите страну для обучения:", reply_markup=get_country_keyboard())
        return

    if text in ["🔄 Начать заново", "🔄 Жаңы диалог"]:
        start(message)
        return

    try:
        wait_msg = bot.send_message(message.chat.id, "⏳ Выполняется запрос к базе данных...")
    except Exception:
        wait_msg = None

    if "Стипендии" in text or "Гранты" in text:
        prompt_query = f"Расскажи про главные стипендии и гранты в {c_country}. Напиши точные суммы в долларах $, сомах, рублях или местной валюте."
        search_query = f"стипендии гранты {c_country} обучение точные суммы 2026"
        query_photo = f"{c_country} university scholarship money diploma"
    elif "ВУЗ" in text or "Колледж" in text or "ЖОЖ" in text:
        prompt_query = f"Назови 3 ведущих университета в {c_country} и укажи важную информацию о них."
        search_query = f"топ университеты ВУЗы {c_country} 2026"
        query_photo = f"{c_country} top university campus building"
    elif "Лайфхак" in text or "Талаптар" in text:
        prompt_query = f"Дай 3 важных рекомендации и требования для поступления в {c_country}."
        search_query = f"требования поступление в {c_country} 2026"
        query_photo = f"students university studying preparation"
    elif "Документ" in text:
        prompt_query = f"Список основных 5 документов, необходимых для поступления в {c_country}."
        search_query = f"документы для поступления в {c_country} 2026"
        query_photo = f"passport visa application documents"
    else:
        prompt_query = text
        search_query = text
        query_photo = f"{text} news photo"

    web_data = search_web(search_query)

    sys_prompt = (
        f"You are Barsbek 🐆 — a polite, highly competent, professional educational advisor and assistant.\n"
        f"Selected primary country: {c_country}. Language: {lang}.\n"
        f"FRESH WEB DATA:\n{web_data}\n\n"
        f"INSTRUCTIONS:\n"
        f"1. DIRECT ANSWER RULE: Answer the user's specific prompt directly! If the user asks a general or specific question about another topic/country (e.g. 'Who is the president of Kyrgyzstan?'), ANSWER THAT QUESTION DIRECTLY using fresh web data. Do NOT force them back to {c_country} unless their question is explicitly about universities/scholarships.\n"
        f"2. TONE: Maintain a polite, professional, business-oriented yet accessible tone. Avoid teenager slang.\n"
        f"3. EMOJIS: Use emojis tastefully and neatly (🎓, 🏛️, 💰, 📌, ✅) to structure information clearly.\n"
        f"4. SCHOLARSHIPS: Always provide exact scholarship amounts when asked.\n"
        f"5. NO MARKDOWN: Strictly avoid markdown formatting (*, #, _)."
    )

    ai_response = ask_ai(prompt_query, sys_prompt)
    photo_bytes = fetch_photo_bytes(query_photo)

    if wait_msg:
        try:
            bot.delete_message(message.chat.id, wait_msg.message_id)
        except Exception:
            pass

    if photo_bytes:
        try:
            bot.send_photo(message.chat.id, photo=photo_bytes, caption=ai_response, reply_markup=get_reels_inline_buttons())
            return
        except Exception as e:
            print(f"Ошибка фото: {e}")

    bot.send_message(message.chat.id, ai_response, reply_markup=get_reels_inline_buttons())

# ==========================================
# 8. ЗАПУСК БОТА
# ==========================================
if __name__ == "__main__":
    print("Запуск обновленного бота Барсбек...")
    bot.infinity_polling(timeout=20, long_polling_timeout=5)
