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
# 1. МИНИ ВЕБ-СЕРВЕР ДЛЯ RENDER (Статус UP)
# ==========================================
class SimpleHTTPRequestHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"<html><body><h1>Barsbek Bot Active & Running!</h1></body></html>")

    def do_HEAD(self):
        self.send_response(200)
        self.send_header("Content-type", "text/html; charset=utf-8")
        self.end_headers()

    def log_message(self, format, *args):
        return

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
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "8891087735:AAFPy02IyHL-1dhu5QTxiMIWAnq3C8g1Vmo")
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
# 3. ТОЧНАЯ БАЗА ДАННЫХ ВУЗОВ И ГРАНТОВ (ГАРАНТИЯ 100%)
# ==========================================
DB_INFO = {
    "🇰🇬 Кыргызстан": {
        "unis": (
            "🏛️ Ведущие ВУЗы и Колледжи Кыргызстана:\n\n"
            "1. АУЦА (AUCA) — Внутренние тесты / SAT 1150+ / TOEFL 80+ / ОРТ 140+\n"
            "2. КНУ им. Ж. Баласагына — Проходной балл ОРТ: 110–145+ (в зависимости от факультета)\n"
            "3. КРСУ им. Б. Ельцина — Проходной балл ОРТ: 120–150+\n"
            "4. КГТУ им. И. Раззакова (Политех) — Проходной балл ОРТ: 110–135+\n"
            "5. КТУ Манас — Внутреннее тестирование КТУ / ОРТ 110+\n"
            "6. МУК (Международный Университет Кыргызстана) — ОРТ 110+\n"
            "7. Колледж АУЦА / IT-Колледжи — По результатам аттестата и собеседования."
        ),
        "grants": (
            "💰 Стипендии и Гранты в Кыргызстане:\n\n"
            "• Государственный бюджет (Грант): Покрывает 100% обучения. Требуются баллы ОРТ от 165–190+.\n"
            "• Золотой сертификат ОРТ: Полный грант в любой ВУЗ КР + Президентская стипендия.\n"
            "• Скидки АУЦА / МУК: Финансовая поддержка от 25% до 75% на основе финансового положения и успеваемости."
        )
    },
    "🇹🇷 Турция": {
        "unis": (
            "🏛️ Ведущие ВУЗы и Колледжи Турции:\n\n"
            "1. METU (Middle East Technical University) — SAT 1350+ / YÖS 85+\n"
            "2. Boğaziçi Üniversitesi — SAT 1400+ / TOEFL 85+\n"
            "3. Istanbul Technical University (İTÜ) — SAT 1300+ / YÖS 80+\n"
            "4. Hacettepe University — SAT 1300+ / YÖS (Медицина 90+)\n"
            "5. Koç University / Sabancı University — SAT 1350+ + Собеседование."
        ),
        "grants": (
            "💰 Стипендии и Гранты в Турции:\n\n"
            "• Türkiye Bursları: 100% бесплатное обучение + бесплатное проживание + авиабилеты + ежемесячная стипендия (~1000–1800 лир).\n"
            "• Гранты частных ВУЗов (Koç/Sabancı): Скидки от 50% до 100% за высокие баллы SAT."
        )
    },
    "🇺🇸 США": {
        "unis": (
            "🏛️ Ведущие ВУЗы и Колледжи США:\n\n"
            "1. MIT / Harvard / Stanford — SAT 1500+ / ACT 34+ / TOEFL 100+ / GPA 3.9+\n"
            "2. NYU / Columbia University — SAT 1450+ / TOEFL 100+\n"
            "3. Arizona State University — SAT 1150+ / TOEFL 80+ / GPA 3.0+\n"
            "4. Community Colleges (Двухгодичные колледжи) — TOEFL 61+ / IELTS 5.5 (Без SAT)."
        ),
        "grants": (
            "💰 Стипендии и Гранты в США:\n\n"
            "• Need-Based Financial Aid: Покрытие до 100% стоимости жизни и учебы на основе доходов семьи (Harvard, Yale, Amherst).\n"
            "• Merit-Based Scholarships: Стипендии за академические и спортивные достижения ($5,000 – $30,000 в год)."
        )
    },
    "🇨🇳 Китай": {
        "unis": (
            "🏛️ Ведущие ВУЗы Китая:\n\n"
            "1. Tsinghua University / Peking University — HSK 5–6 / SAT 1450+ / Внутренний экзамен\n"
            "2. Zhejiang University — HSK 5 (от 210 баллов) / IELTS 6.5\n"
            "3. Fudan University — HSK 5 / IELTS 6.5+\n"
            "4. BIT (Beijing Institute of Technology) — HSK 4–5 / IELTS 6.0."
        ),
        "grants": (
            "💰 Стипендии и Гранты в Китае:\n\n"
            "• CSC (Правительственный грант Китая): 100% обучение, бесплатное общежитие, медстраховка + стипендия 2500–3000 юаней/мес.\n"
            "• Провинциальные гранты: Бесплатное обучение + partial стипендия."
        )
    },
    "🇰🇷 Южная Корея": {
        "unis": (
            "🏛️ Ведущие ВУЗы Южной Кореи:\n\n"
            "1. Seoul National University (SNU) — TOPIK 5–6 / IELTS 7.0+\n"
            "2. KAIST (Технический) — SAT / IELTS 6.5+ (Обучение на английском)\n"
            "3. Korea University / Yonsei University — TOPIK 4–5 / IELTS 6.5+\n"
            "4. Hanyang University — TOPIK 3–4 / IELTS 6.0."
        ),
        "grants": (
            "💰 Стипендии и Гранты в Корее:\n\n"
            "• GKS (Global Korea Scholarship): 100% учеба + перелет + языковые курсы 1 год + 1,000,000 вон/мес.\n"
            "• Университетские скидки: 30%–100% скидка на учебу за высокий уровень TOPIK (4–6 уровень)."
        )
    },
    "🇨🇦 Канада": {
        "unis": (
            "🏛️ Ведущие ВУЗы и Колледжи Канады:\n\n"
            "1. University of Toronto — IELTS 6.5+ (ни ниже 6.0) / GPA 3.7+\n"
            "2. UBC (University of British Columbia) — IELTS 6.5+ / GPA 3.5+\n"
            "3. McGill University — IELTS 6.5+ / SAT 1350+\n"
            "4. Seneca College / Humber College — IELTS 6.0 (Колледжи с правым на работу)."
        ),
        "grants": (
            "💰 Стипендии и Гранты в Канаде:\n\n"
            "• Lester B. Pearson International Scholarship (U of T): Полный грант на 4 года учебы и проживания.\n"
            "• Entrance Scholarships: Входные стипендии от $2,000 до $10,000 при высоком среднем балле аттестата."
        )
    }
}

# ==========================================
# 4. ВЕБ-ПОИСК И ФОТО
# ==========================================
def search_web(query):
    if not tavily_client:
        return ""
    try:
        response = tavily_client.search(query=query, search_depth="basic", max_results=2)
        results = response.get("results", [])
        if not results:
            return ""
        snippets = []
        for item in results:
            snippets.append(f"{item.get('title')}: {item.get('content')}")
        return "\n".join(snippets)
    except Exception as e:
        print(f"Ошибка поиска: {e}")
        return ""

def fetch_photo_bytes(query):
    if UNSPLASH_KEY:
        search_url = f"https://api.unsplash.com/search/photos?page=1&query={query}&per_page=10&client_id={UNSPLASH_KEY}"
        try:
            res = requests.get(search_url, timeout=5)
            if res.status_code == 200:
                results = res.json().get('results', [])
                if results:
                    img_url = random.choice(results)['urls']['small']
                    img_res = requests.get(img_url, timeout=5)
                    if img_res.status_code == 200:
                        photo_bytes = io.BytesIO(img_res.content)
                        photo_bytes.name = "image.jpg"
                        return photo_bytes
        except Exception as e:
            print(f"Unsplash error: {e}")

    fallback_urls = ["https://picsum.photos/800/600", "https://picsum.photos/800/601"]
    try:
        fb_res = requests.get(random.choice(fallback_urls), timeout=5)
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

def ask_ai_or_db(prompt, system_instruction, country, category):
    # Если OpenRouter отвечает быстро — используем его
    if OPENROUTER_KEY and len(OPENROUTER_KEY) > 10:
        url = "https://openrouter.ai/api/v1/chat/completions"
        headers = {
            "Authorization": f"Bearer {OPENROUTER_KEY}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://render.com",
            "X-Title": "Barsbek Bot"
        }
        data = {
            "model": "google/gemini-2.0-flash-lite-preview-02-05:free",
            "messages": [
                {"role": "system", "content": system_instruction},
                {"role": "user", "content": prompt}
            ],
            "max_tokens": 1000,
            "temperature": 0.2
        }
        try:
            response = requests.post(url, headers=headers, json=data, timeout=8)
            if response.status_code == 200:
                res_data = response.json()
                if 'choices' in res_data and len(res_data['choices']) > 0:
                    raw_content = res_data['choices'][0]['message']['content']
                    if raw_content and len(raw_content.strip()) > 30 and "сервер" not in raw_content.lower():
                        return clean_ai_response(raw_content)
        except Exception as e:
            print(f"OpenRouter timeout/error: {e}")

    # МГНОВЕННЫЙ НАДЕЖНЫЙ ОТВЕТ ИЗ БАЗЫ
    c_data = DB_INFO.get(country, DB_INFO["🇰🇬 Кыргызстан"])
    if category == "grants":
        return c_data["grants"]
    elif category == "unis":
        return c_data["unis"]
    else:
        return (
            f"📌 Требования и советы по поступлению в {country}:\n\n"
            f"1. Готовьте документы минимум за 6–9 месяцев до начала семестра.\n"
            f"2. Уделяйте внимание языковым сертификатам (IELTS / TOEFL / HSK / TOPIK / ОРТ).\n"
            f"3. Для грантов важен высокий средний балл аттестата (GPA) и мотивационное письмо."
        )

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
        markup.row("🎯 Мой путь", "🌍 Өлкөнү алмаштыруу")
        markup.row("🌐 Тилди алмаштыруу", "🔄 Жаңы диалог")
    elif lang == "🇬🇧 English":
        markup.row("🔥 Top Universities & Colleges", "💡 Advice & Requirements")
        markup.row("📄 Document List", "💰 Scholarships & Grants")
        markup.row("🎯 My Path", "🌍 Change Country")
        markup.row("🌐 Change Language", "🔄 Restart")
    elif lang == "🇹🇷 Türkçe":
        markup.row("🔥 En İyi Üniversiteler", "💡 Tavsiyeler ve Şartlar")
        markup.row("📄 Belge Listesi", "💰 Burslar ve Hibelere")
        markup.row("🎯 Benim Yolum", "🌍 Ülkeyi Değiştir")
        markup.row("🌐 Dili Değiştir", "🔄 Yeniden Başlat")
    else:
        markup.row("🔥 Топ ВУЗы и Колледжи", "💡 Лайфхаки и Требования")
        markup.row("📄 Список документов", "💰 Стипендии и Гранты")
        markup.row("🎯 Мой путь", "🌍 Сменить страну")
        markup.row("🌐 Сменить язык", "🔄 Начать заново")

    is_owner = (user_id == OWNER_ID)
    is_admin = (user_id in admins)

    if is_owner:
        markup.row("👑 Панель Владельца")
    elif is_admin:
        markup.row("🛠 Панель Админа")

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
        types.InlineKeyboardButton("🔥 Ещё факт / ВУЗ 🏛️", callback_data="act_more_fact"),
        types.InlineKeyboardButton("📸 Посмотреть фото 🎓", callback_data="act_more_photo")
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
            bot.answer_callback_query(call.id, "⛔ Только для Владельца!")
            return
        target_id = int(data.split("_")[1])
        admins.discard(target_id)
        save_data()
        bot.answer_callback_query(call.id, "✅ Админ снят")
        bot.edit_message_text(f"❌ Пользователь {target_id} больше не админ.", call.message.chat.id, call.message.message_id)
        return

    if data.startswith("act_"):
        user_data = user_states.get(user_id, {})
        c_country = user_data.get("country", "Кыргызстан")
        c_lang = user_data.get("lang", "🇷🇺 Русский")

        try:
            bot.answer_callback_query(call.id, "⏳ Обработка...")
        except Exception:
            pass

        if data == "act_more_photo":
            photo_file = fetch_photo_bytes(f"{c_country} university campus student")
            short_caption = f"🏛️ Вид кампуса и университета в стране: {c_country} ✨"
            if photo_file:
                try:
                    bot.send_photo(call.message.chat.id, photo=photo_file, caption=short_caption, reply_markup=get_reels_inline_buttons())
                    return
                except Exception as e:
                    print(f"Ошибка фото: {e}")
            bot.send_message(call.message.chat.id, "📸 Фото временно недоступно.", reply_markup=get_reels_inline_buttons())
            return

        prompt = f"Напишите 1 конкретный реальный университет в {c_country} и его проходной балл."
        sys_prompt = f"Отвечай строго на языке: {c_lang}. Только реальные названия ВУЗов."
        ai_text = ask_ai_or_db(prompt, sys_prompt, c_country, "unis")
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
        bot.send_message(message.chat.id, "🔴 Бот временно выключен на техническое обслуживание. 🛠️")
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
        lang = u_data.get("lang", "🇷🇺 Русский")
        
        msgs = {
            "🇬🇧 English": f"Welcome back, {name}! 👋 Please choose a target country for your career & university guidance:",
            "🇰🇬 Кыргызча": f"Саламатсызбы, {name}! 👋 Окуу жана кесип тандоо үчүн өлкөнү тандаңыз:",
            "🇹🇷 Türkçe": f"Hoş geldiniz, {name}! 👋 Lütfen eğitim almak istediğiniz ülkeyi seçin:",
            "🇷🇺 Русский": f"Здравствуйте, {name}! 👋 Я Барсбек 🐆 — ваш консультант. Выберите страну для подбора ВУЗа и профессии:"
        }
        bot.send_message(message.chat.id, msgs.get(lang, msgs["🇷🇺 Русский"]), reply_markup=get_country_keyboard())
    else:
        u_data["state"] = "WAITING_FOR_LANG"
        msg = "Здравствуйте! 👋 Я Барсбек 🐆 — ваш консультант по образованию и выбору профессии.\n\nВыберите язык / Choose language / Тилди тандаңыз:"
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
        user_states[user_id] = {"lang": "🇷🇺 Русский", "name": None, "age": None, "country": "Кыргызстан", "state": None}

    user_data = user_states[user_id]
    current_state = user_data.get("state")
    lang = user_data.get("lang", "🇷🇺 Русский")

    # Назначение Админа
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
            bot.send_message(message.chat.id, f"✅ Пользователь {target} назначен администратором! 👑", reply_markup=get_owner_panel_keyboard())
        else:
            bot.send_message(message.chat.id, "❌ Участник не найден в базе.", reply_markup=get_owner_panel_keyboard())
        return

    # Выбор языка
    if current_state == "WAITING_FOR_LANG" or text in LANGUAGES:
        if text in LANGUAGES:
            user_data["lang"] = text
            lang = text
            
            name_prompts = {
                "🇬🇧 English": "What is your name? ✍️",
                "🇰🇬 Кыргызча": "Сиздин атыңыз ким? ✍️",
                "🇹🇷 Türkçe": "Adınız nedir? ✍️",
                "🇷🇺 Русский": "Как к вам обращаться? (Укажите ваше имя) ✍️"
            }
            country_prompts = {
                "🇬🇧 English": "Please select a country for study & career: 🌍",
                "🇰🇬 Кыргызча": "Окуу жана кесип тандоо үчүн өлкөнү тандаңыз: 🌍",
                "🇹🇷 Türkçe": "Lütfen eğitim ülkesini seçin: 🌍",
                "🇷🇺 Русский": "Выберите страну для обучения и подбора ВУЗа: 🌍"
            }

            if not user_data.get("name"):
                user_data["state"] = "WAITING_FOR_NAME"
                bot.send_message(message.chat.id, name_prompts.get(lang, name_prompts["🇷🇺 Русский"]), reply_markup=types.ReplyKeyboardRemove())
                return
            else:
                user_data["state"] = "WAITING_FOR_COUNTRY"
                bot.send_message(message.chat.id, country_prompts.get(lang, country_prompts["🇷🇺 Русский"]), reply_markup=get_country_keyboard())
                return

    # Ввод имени
    if current_state == "WAITING_FOR_NAME":
        user_data["name"] = text
        user_data["state"] = "WAITING_FOR_AGE"
        
        age_prompts = {
            "🇬🇧 English": f"Nice to meet you, {text}! Please enter your age: 🔢",
            "🇰🇬 Кыргызча": f"Таанышканыма кубанычтамын, {text}! Жашыңызды жазыңыз: 🔢",
            "🇹🇷 Türkçe": f"Tanıştığıma memnun oldum {text}! Yaşınızı girin: 🔢",
            "🇷🇺 Русский": f"Приятно познакомиться, {text}! Укажите ваш возраст: 🔢"
        }
        bot.send_message(message.chat.id, age_prompts.get(lang, age_prompts["🇷🇺 Русский"]))
        return

    # Ввод возраста
    if current_state == "WAITING_FOR_AGE":
        user_data["age"] = text
        user_data["state"] = "WAITING_FOR_COUNTRY"
        
        c_prompts = {
            "🇬🇧 English": "Awesome! Now choose your target country: 🌍",
            "🇰🇬 Кыргызча": "Сонун! Эми өлкөнү тандаңыз: 🌍",
            "🇹🇷 Türkçe": "Harika! Şimdi hedef ülkeyi seçin: 🌍",
            "🇷🇺 Русский": "Спасибо! Теперь выберите страну обучения: 🌍"
        }
        bot.send_message(message.chat.id, c_prompts.get(lang, c_prompts["🇷🇺 Русский"]), reply_markup=get_country_keyboard())
        return

    # Выбор страны
    if current_state == "WAITING_FOR_COUNTRY" or text in COUNTRIES:
        if text in COUNTRIES:
            user_data["country"] = text
            user_data["state"] = None
            
            welcomes = {
                "🇬🇧 English": f"🎓 Selected country: {text}\nI am Barsbek 🐆 — your advisor. Use the menu below to explore universities, scholarships, and career paths! ✨",
                "🇰🇬 Кыргызча": f"🎓 Тандалган өлкө: {text}\nМен Барсбек 🐆 — сиздин кеңешчиңизмин. ЖОЖдорду, стипендияларды жана кесиптерди көрүү үчүн менюну колдонуңуз! ✨",
                "🇹🇷 Türkçe": f"🎓 Seçilen ülke: {text}\nBen Barsbek 🐆 — danışmanınızım. Üniversiteleri, bursları ve meslekleri incelemek için aşağıdaki menüyü kullanın! ✨",
                "🇷🇺 Русский": f"🎓 Выбрана страна: {text}\nЯ Барсбек 🐆 — ваш консультант. Используйте меню ниже для подбора ВУЗов, стипендий и построения карьеры! ✨"
            }
            photo = fetch_photo_bytes(f"{text} architecture capital landmark")
            
            if photo:
                try:
                    bot.send_photo(message.chat.id, photo=photo, caption=welcomes.get(lang, welcomes["🇷🇺 Русский"]), reply_markup=get_main_keyboard(user_id, lang))
                    return
                except Exception:
                    pass
            bot.send_message(message.chat.id, welcomes.get(lang, welcomes["🇷🇺 Русский"]), reply_markup=get_main_keyboard(user_id, lang))
            return

    c_country = user_data.get("country", "Кыргызстан")

    # Панель Владельца
    if text == "👑 Панель Владельца" and user_id == OWNER_ID:
        bot.send_message(message.chat.id, "👑 Панель Владельца Барсбек ⚙️", reply_markup=get_owner_panel_keyboard())
        return

    # Панель Админа
    if text == "🛠 Панель Админа" and (user_id == OWNER_ID or user_id in admins):
        bot.send_message(message.chat.id, "🛠 Панель Администратора ⚙️", reply_markup=get_admin_panel_keyboard())
        return

    # Включить/Выключить бота
    if text in ["🔴 Выключить бот", "🟢 Включить бот"] and user_id == OWNER_ID:
        bot_enabled = (text == "🟢 Включить бот")
        save_data()
        status_msg = "🟢 Бот включен для всех пользователей! 🚀" if bot_enabled else "🔴 Бот выключен для пользователей."
        bot.send_message(message.chat.id, status_msg, reply_markup=get_owner_panel_keyboard())
        return

    # Назначить Админа
    if text == "➕ Назначить администратором" and user_id == OWNER_ID:
        user_data["state"] = "WAITING_FOR_ADMIN_INPUT"
        bot.send_message(message.chat.id, "Введите @username или Telegram ID нового админа: 👤")
        return

    # Список админов
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
                types.InlineKeyboardButton("❌ Снять", callback_data=f"removeadmin_{admin_id}"),
                types.InlineKeyboardButton("✅ Разбанить" if is_ban else "⛔ Забанить", callback_data=f"unban_{admin_id}" if is_ban else f"ban_{admin_id}")
            )
            bot.send_message(message.chat.id, f"👑 **{name}** ({uname})\n🆔 `{admin_id}`", parse_mode="Markdown", reply_markup=markup)
        return

    # Все участники
    if text == "👥 Все участники" and (user_id == OWNER_ID or user_id in admins):
        if not all_users:
            bot.send_message(message.chat.id, "👥 Список пользователей пуст.")
            return

        bot.send_message(message.chat.id, f"👥 **Всего пользователей в базе: {len(all_users)}**", parse_mode="Markdown")
        for uid, info in all_users.items():
            uname = info.get("username", f"ID: {uid}")
            name = info.get("first_name", "User")
            is_ban = uid in banned_users
            
            markup = types.InlineKeyboardMarkup()
            if is_ban:
                markup.add(types.InlineKeyboardButton("✅ Разбанить", callback_data=f"unban_{uid}"))
            else:
                markup.add(types.InlineKeyboardButton("⛔ Забанить", callback_data=f"ban_{uid}"))

            status_str = " (⛔ Забанен)" if is_ban else ""
            bot.send_message(message.chat.id, f"👤 **{name}** ({uname}){status_str}\n🆔 `{uid}`", parse_mode="Markdown", reply_markup=markup)
        return

    # Статистика за день
    if text == "📊 Участников за сегодня" and (user_id == OWNER_ID or user_id in admins):
        today_str = datetime.date.today().isoformat()
        count = sum(1 for u in all_users.values() if u.get("last_active") == today_str)
        bot.send_message(message.chat.id, f"📊 Активных пользователей за сегодня: **{count}** 🔥", parse_mode="Markdown")
        return

    # Навигация
    if text in ["🔙 Главное меню", "Главное меню", "Back to Menu"]:
        bot.send_message(message.chat.id, "🏡 Главное меню:", reply_markup=get_main_keyboard(user_id, lang))
        return

    if text in ["🌐 Сменить язык", "🌐 Тилди алмаштыруу", "🌐 Change Language", "🌐 Dili Değiştir"]:
        user_data["state"] = "WAITING_FOR_LANG"
        bot.send_message(message.chat.id, "Выберите язык / Choose language / Тилди тандаңыз: 🌐", reply_markup=get_language_keyboard())
        return

    if text in ["🌍 Сменить страну", "🌍 Өлкөнү алмаштыруу", "🌍 Change Country", "🌍 Ülkeyi Değiştir"]:
        user_data["state"] = "WAITING_FOR_COUNTRY"
        bot.send_message(message.chat.id, "Выберите страну для обучения / Choose target country: 🌍", reply_markup=get_country_keyboard())
        return

    if text in ["🔄 Начать заново", "🔄 Жаңы диалог", "🔄 Restart", "🔄 Yeniden Başlat"]:
        start(message)
        return

    # Кнопка "🎯 Мой путь"
    if text in ["🎯 Мой путь", "🎯 My Path", "🎯 Benim Yolum"]:
        path_msgs = {
            "🇬🇧 English": "🎯 **My Career Path** ✨\n\nTell me: What subjects or activities do you enjoy most? (e.g., programming, drawing, logic, medicine). I am Barsbek 🐆, I will analyze your strengths and suggest the best professions & university majors in " + c_country + "!",
            "🇰🇬 Кыргызча": "🎯 **Менин жолум** ✨\n\nМага жазыңыз: Кайсы сабактарды же иштерди жакшы көрөсүз? (мисалы: программалоо, сүрөт тартуу, логика, медицина). Мен Барсбек 🐆, сизге " + c_country + " өлкөсүндөгү эң ылайыктуу кесиптерди жана факультеттерди тандап берем!",
            "🇹🇷 Türkçe": "🎯 **Benim Yolum** ✨\n\nBana yazın: Hangi dersleri veya aktiviteleri seviyorsunuz? Ben Barsbek 🐆, sizin için " + c_country + " ülkesindeki en uygun meslekleri ve üniversite bölümlerini analiz edeyim!",
            "🇷🇺 Русский": "🎯 **Мой карьерный путь** ✨\n\nНапишите мне: Какие предметы или занятия вам нравятся больше всего? (например: программирование, рисование, логика, бизнес, медицина). Я Барсбек 🐆, проанализирую ваши сильные стороны и подберу подходящие профессии и факультеты в стране: " + c_country + "!"
        }
        bot.send_message(message.chat.id, path_msgs.get(lang, path_msgs["🇷🇺 Русский"]), parse_mode="Markdown")
        return

    # Запросы с гарантированным быстрым ответом
    category = "general"
    if "Стипендии" in text or "Гранты" in text or "Burslar" in text or "Scholarships" in text:
        category = "grants"
        prompt_query = f"Перечисли стипендии и гранты в {c_country}."
        query_photo = f"{c_country} university scholarship diploma"
    elif "ВУЗ" in text or "Колледж" in text or "ЖОЖ" in text or "Üniversiteler" in text or "Universities" in text:
        category = "unis"
        prompt_query = f"Перечисли реальные ВУЗы и проходные баллы в {c_country}."
        query_photo = f"{c_country} top university campus building"
    else:
        prompt_query = f"Ответь на вопрос: {text} про учебу в {c_country}."
        query_photo = f"{c_country} university student career"

    sys_prompt = f"Пиши ответ СТРОГО на языке: {lang}. Используй реальные названия ВУЗов в {c_country}."

    ai_response = ask_ai_or_db(prompt_query, sys_prompt, c_country, category)
    photo_bytes = fetch_photo_bytes(query_photo)

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
    print("Запуск Барсбек...")
    bot.infinity_polling(timeout=20, long_polling_timeout=5)
