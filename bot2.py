import os
import sys
import io
import re
import json
import random
import datetime
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
import telebot
from telebot import types
import requests

try:
    from tavily import TavilyClient
except Exception:
    TavilyClient = None

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
# ВАЖНО: ключи больше НЕ хранятся в коде. Задай их в Render -> Environment.
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
OPENROUTER_KEY = os.environ.get("OPENROUTER_KEY", "")
UNSPLASH_KEY = os.environ.get("UNSPLASH_ACCESS_KEY", "")
TAVILY_KEY = os.environ.get("TAVILY_API_KEY", "")

# Модели OpenRouter пробуются по очереди (если одна упала/удалена — берётся следующая).
# Можно переопределить через переменную окружения OPENROUTER_MODELS (через запятую).
OPENROUTER_MODELS = [m.strip() for m in os.environ.get(
    "OPENROUTER_MODELS",
    "meta-llama/llama-3.3-70b-instruct:free,google/gemini-2.0-flash-exp:free,deepseek/deepseek-chat-v3-0324:free"
).split(",") if m.strip()]

OWNER_ID = 8762115500

if not TELEGRAM_TOKEN:
    print("ОШИБКА: не задана переменная окружения TELEGRAM_BOT_TOKEN")
    sys.exit(1)

bot = telebot.TeleBot(TELEGRAM_TOKEN)
tavily_client = TavilyClient(api_key=TAVILY_KEY) if (TAVILY_KEY and TavilyClient) else None

DATA_FILE = "users_data.json"
data_lock = threading.Lock()

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
    with data_lock:
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

LANG_CODE = {"🇷🇺 Русский": "ru", "🇰🇬 Кыргызча": "ky", "🇬🇧 English": "en", "🇹🇷 Türkçe": "tr"}
LANG_NAME = {"🇷🇺 Русский": "Russian", "🇰🇬 Кыргызча": "Kyrgyz", "🇬🇧 English": "English", "🇹🇷 Türkçe": "Turkish"}

# ==========================================
# 3. ТОЧНАЯ БАЗА ДАННЫХ ВУЗОВ И ГРАНТОВ
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
            "• Провинциальные гранты: Бесплатное обучение + частичная стипендия."
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
            "4. Seneca College / Humber College — IELTS 6.0 (Колледжи с правом на работу)."
        ),
        "grants": (
            "💰 Стипендии и Гранты в Канаде:\n\n"
            "• Lester B. Pearson International Scholarship (U of T): Полный грант на 4 года учебы и проживания.\n"
            "• Entrance Scholarships: Входные стипендии от $2,000 до $10,000 при высоком среднем балле аттестата."
        )
    }
}

FALLBACK_TIPS = (
    "💡 Лайфхаки и требования для поступления:\n\n"
    "1. Готовьте документы минимум за 6–9 месяцев до начала семестра.\n"
    "2. Заранее сдайте языковой сертификат (IELTS / TOEFL / HSK / TOPIK / ОРТ).\n"
    "3. Для грантов важны высокий средний балл (GPA) и сильное мотивационное письмо.\n"
    "4. Подавайте заявки в 5–8 вузов: 2 амбициозных, 3 реалистичных, 2 запасных.\n"
    "5. Следите за дедлайнами на официальных сайтах вузов."
)
FALLBACK_DOCS = (
    "📄 Базовый список документов:\n\n"
    "• Загранпаспорт\n• Аттестат / диплом с переводом и апостилем\n• Языковой сертификат\n"
    "• Мотивационное письмо\n• Рекомендательные письма\n• Резюме / список достижений\n"
    "• Фото, медсправка, справка о финансах (если требуется)\n\n"
    "Точный перечень всегда сверяйте на сайте выбранного вуза."
)

# ==========================================
# 4. ВЕБ-ПОИСК, ФОТО И ИИ
# ==========================================
def country_name(country):
    """'🇰🇬 Кыргызстан' -> 'Кыргызстан'"""
    if not country:
        return "не выбрана"
    parts = country.split(" ", 1)
    return parts[1] if len(parts) == 2 else country

def search_web(query):
    if not tavily_client:
        return ""
    try:
        response = tavily_client.search(query=query, search_depth="basic", max_results=2)
        results = response.get("results", [])
        snippets = []
        for item in results:
            content = (item.get("content") or "")[:400]
            snippets.append(f"{item.get('title')}: {content}")
        return "\n".join(snippets)
    except Exception as e:
        print(f"Ошибка поиска: {e}")
        return ""

def fetch_photo_bytes(query):
    if UNSPLASH_KEY:
        try:
            res = requests.get(
                "https://api.unsplash.com/search/photos",
                params={"page": 1, "query": query, "per_page": 10, "client_id": UNSPLASH_KEY},
                timeout=6
            )
            if res.status_code == 200:
                results = res.json().get('results', [])
                if results:
                    img_url = random.choice(results)['urls']['small']
                    img_res = requests.get(img_url, timeout=8)
                    if img_res.status_code == 200:
                        photo_bytes = io.BytesIO(img_res.content)
                        photo_bytes.name = "image.jpg"
                        return photo_bytes
        except Exception as e:
            print(f"Unsplash error: {e}")

    try:
        fb_res = requests.get(f"https://picsum.photos/800/600?random={random.randint(1, 10000)}", timeout=8)
        if fb_res.status_code == 200:
            photo_bytes = io.BytesIO(fb_res.content)
            photo_bytes.name = "image.jpg"
            return photo_bytes
    except Exception as e:
        print(f"Fallback photo error: {e}")
    return None

def clean_ai_response(text):
    """Убирает markdown-разметку, чтобы Telegram не ломался на спецсимволах."""
    lines = [l for l in text.split('\n') if "user safety:" not in l.lower()]
    res = "\n".join(lines).strip()
    res = re.sub(r"^\s*#{1,6}\s*", "", res, flags=re.MULTILINE)
    res = re.sub(r"^\s*[\*\-]\s+", "• ", res, flags=re.MULTILINE)
    res = res.replace("**", "").replace("__", "").replace("`", "").replace("~~", "")
    res = res.replace("*", "")
    return res.strip()

def ask_ai(messages, max_tokens=900):
    """Запрос к OpenRouter. Возвращает текст или None, если все модели не ответили."""
    if not OPENROUTER_KEY or len(OPENROUTER_KEY) < 10:
        print("OPENROUTER_KEY не задан")
        return None
    headers = {
        "Authorization": f"Bearer {OPENROUTER_KEY}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://render.com",
        "X-Title": "Barsbek Bot"
    }
    for model in OPENROUTER_MODELS:
        data = {"model": model, "messages": messages, "max_tokens": max_tokens, "temperature": 0.5}
        try:
            response = requests.post("https://openrouter.ai/api/v1/chat/completions",
                                     headers=headers, json=data, timeout=25)
            if response.status_code == 200:
                res_data = response.json()
                choices = res_data.get("choices") or []
                if choices:
                    raw = (choices[0].get("message") or {}).get("content")
                    if raw and len(raw.strip()) > 10:
                        return clean_ai_response(raw)
            else:
                print(f"OpenRouter {model}: HTTP {response.status_code} {response.text[:200]}")
        except Exception as e:
            print(f"OpenRouter {model} error: {e}")
    return None

def build_system_prompt(u):
    lang_name = LANG_NAME.get(u.get("lang"), "Russian")
    return (
        "Ты — Барсбек 🐆, дружелюбный ИИ-профориентолог и консультант по поступлению в вузы за рубежом и в Кыргызстане.\n"
        f"Отвечай СТРОГО на языке: {lang_name}.\n"
        f"Профиль пользователя: имя — {u.get('name') or 'не указано'}, возраст — {u.get('age') or 'не указан'}, "
        f"страна обучения — {country_name(u.get('country'))}.\n\n"
        "Правила:\n"
        "1. Помогай с выбором профессии, направления обучения, вуза, предметов, навыков, грантов, документов и подготовки к экзаменам.\n"
        "2. Отвечай коротко и по делу (до 1200 символов), структурируй списком из 3–5 пунктов, добавляй 1–2 эмодзи.\n"
        "3. Не используй markdown (никаких *, #, `). Для списков используй «•» или цифры.\n"
        "4. Учитывай возраст: школьнику давай советы про предметы, кружки, олимпиады; взрослому — про смену профессии и курсы.\n"
        "5. Не выдумывай точные цифры (проходные баллы, зарплаты, дедлайны). Если не уверен — скажи и посоветуй проверить на официальном сайте.\n"
        "6. В конце, когда уместно, задай ОДИН уточняющий вопрос, чтобы лучше подобрать профессию.\n"
        "7. Если вопрос не про учёбу, карьеру или профессии — вежливо верни разговор к теме профориентации.\n"
        "8. Будь поддерживающим: не ругай за неопределённость, это нормально — не знать, кем быть."
    )

def ask_career_ai(u, user_text, use_search=True):
    """Свободный диалог с ИИ-профориентологом с историей и (опционально) веб-поиском."""
    history = u.setdefault("history", [])
    system = build_system_prompt(u)

    if use_search and tavily_client and len(user_text) > 12:
        web = search_web(f"{user_text} {country_name(u.get('country'))}")
        if web:
            system += "\n\nСвежие данные из интернета (могут быть неточными, проверяй):\n" + web

    history.append({"role": "user", "content": user_text})
    del history[:-10]  # храним последние 10 сообщений

    answer = ask_ai([{"role": "system", "content": system}] + history)
    if answer:
        history.append({"role": "assistant", "content": answer})
        del history[:-10]
    else:
        history.pop()  # убираем вопрос без ответа
    return answer

AI_UNAVAILABLE = {
    "🇷🇺 Русский": "😔 Барсбек сейчас не может подключиться к ИИ. Попробуйте ещё раз через минуту или воспользуйтесь кнопками меню.",
    "🇰🇬 Кыргызча": "😔 Барсбек азыр ИИге туташа албай жатат. Бир мүнөттөн кийин кайра аракет кылыңыз же менюдагы баскычтарды колдонуңуз.",
    "🇬🇧 English": "😔 Barsbek can't reach the AI right now. Please try again in a minute or use the menu buttons.",
    "🇹🇷 Türkçe": "😔 Barsbek şu an yapay zekaya bağlanamıyor. Bir dakika sonra tekrar deneyin veya menü düğmelerini kullanın."
}

# ==========================================
# 4.1 ОТПРАВКА СООБЩЕНИЙ (учёт лимитов Telegram)
# ==========================================
def send_long(chat_id, text, markup=None):
    chunks = [text[i:i + 4000] for i in range(0, len(text), 4000)] or ["…"]
    for i, chunk in enumerate(chunks):
        bot.send_message(chat_id, chunk, reply_markup=markup if i == len(chunks) - 1 else None)

def send_photo_text(chat_id, text, photo_query, markup=None):
    """Фото + текст. Подпись в Telegram максимум 1024 символа — длинный текст идёт отдельным сообщением."""
    photo = fetch_photo_bytes(photo_query)
    if photo:
        try:
            if len(text) <= 1000:
                bot.send_photo(chat_id, photo=photo, caption=text, reply_markup=markup)
                return
            bot.send_photo(chat_id, photo=photo)
        except Exception as e:
            print(f"Ошибка фото: {e}")
    send_long(chat_id, text, markup)

def typing(chat_id):
    try:
        bot.send_chat_action(chat_id, "typing")
    except Exception:
        pass

# ==========================================
# 4.2 ПРОФИЛЬ ПОЛЬЗОВАТЕЛЯ (сохраняется между перезапусками)
# ==========================================
def register_user(user):
    today_str = datetime.date.today().isoformat()
    uid = user.id
    username_str = f"@{user.username}" if user.username else f"ID: {uid}"
    if uid not in all_users:
        all_users[uid] = {
            "username": username_str,
            "first_name": user.first_name or "Пользователь",
            "join_date": today_str,
            "last_active": today_str
        }
        save_data()
    elif all_users[uid].get("last_active") != today_str:
        all_users[uid]["last_active"] = today_str
        save_data()

def get_session(user):
    uid = user.id
    if uid not in user_states:
        prof = all_users.get(uid, {}).get("profile", {})
        user_states[uid] = {
            "lang": prof.get("lang", "🇷🇺 Русский"),
            "name": prof.get("name"),
            "age": prof.get("age"),
            "country": prof.get("country"),
            "state": None,
            "history": [],
            "interests": set()
        }
    return user_states[uid]

def save_profile(uid):
    u = user_states.get(uid)
    if not u or uid not in all_users:
        return
    all_users[uid]["profile"] = {
        "lang": u.get("lang"), "name": u.get("name"),
        "age": u.get("age"), "country": u.get("country")
    }
    save_data()

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

# ---------- Тест интересов для "Мой путь" ----------
INTERESTS = {
    "it":     ("💻", {"ru": "Программирование / IT", "ky": "Программалоо / IT", "en": "Coding / IT", "tr": "Yazılım / IT"}),
    "design": ("🎨", {"ru": "Дизайн и творчество", "ky": "Дизайн жана чыгармачылык", "en": "Design & art", "tr": "Tasarım ve sanat"}),
    "med":    ("🩺", {"ru": "Медицина и биология", "ky": "Медицина жана биология", "en": "Medicine & biology", "tr": "Tıp ve biyoloji"}),
    "biz":    ("💼", {"ru": "Бизнес и финансы", "ky": "Бизнес жана финансы", "en": "Business & finance", "tr": "İş ve finans"}),
    "sci":    ("🔬", {"ru": "Наука и математика", "ky": "Илим жана математика", "en": "Science & math", "tr": "Bilim ve matematik"}),
    "people": ("🤝", {"ru": "Помощь людям, психология", "ky": "Адамдарга жардам, психология", "en": "Helping people, psychology", "tr": "İnsanlara yardım, psikoloji"}),
    "build":  ("🏗️", {"ru": "Инженерия и строительство", "ky": "Инженерия жана курулуш", "en": "Engineering & building", "tr": "Mühendislik ve inşaat"}),
    "media":  ("🎬", {"ru": "Медиа, видео, блогинг", "ky": "Медиа, видео, блог", "en": "Media, video, blogging", "tr": "Medya, video, blog"}),
}

PATH_INTRO = {
    "ru": "🎯 Мой карьерный путь\n\nОтметьте всё, что вам интересно (можно несколько), и нажмите «Готово». Барсбек 🐆 подберёт профессии и направления в стране: {c}.\n\nИли просто напишите мне своими словами, что вам нравится!",
    "ky": "🎯 Менин жолум\n\nКызыккан нерселериңизди белгилеңиз (бир нечесин мүмкүн) жана «Даяр» баскычын басыңыз. Барсбек 🐆 {c} өлкөсү боюнча кесиптерди тандап берет.\n\nЖеделе, өз сөзүңүз менен жазсаңыз да болот!",
    "en": "🎯 My Career Path\n\nSelect everything you're interested in (several allowed) and press “Done”. Barsbek 🐆 will suggest professions and majors in: {c}.\n\nOr just tell me in your own words what you enjoy!",
    "tr": "🎯 Benim Yolum\n\nİlgi duyduğunuz her şeyi seçin (birden fazla olabilir) ve “Tamam”a basın. Barsbek 🐆 {c} için meslekler ve bölümler önerecek.\n\nVeya sadece kendi sözlerinizle neyi sevdiğinizi yazın!"
}
DONE_LABEL = {"ru": "✅ Готово", "ky": "✅ Даяр", "en": "✅ Done", "tr": "✅ Tamam"}
PICK_ONE = {"ru": "Выберите хотя бы один пункт 🙂", "ky": "Жок дегенде бирөөнү тандаңыз 🙂",
            "en": "Pick at least one 🙂", "tr": "En az bir seçenek seçin 🙂"}

def get_interests_markup(selected, lang):
    code = LANG_CODE.get(lang, "ru")
    markup = types.InlineKeyboardMarkup(row_width=2)
    buttons = []
    for key, (emoji, names) in INTERESTS.items():
        mark = "✅ " if key in selected else ""
        buttons.append(types.InlineKeyboardButton(f"{mark}{emoji} {names[code]}", callback_data=f"int_{key}"))
    markup.add(*buttons)
    markup.add(types.InlineKeyboardButton(DONE_LABEL[code], callback_data="int_done"))
    return markup

FACT_TOPICS = ["IT и программирование", "медицина", "инженерия", "бизнес и финансы",
               "дизайн", "педагогика", "юриспруденция", "наука и исследования", "туризм и сервис", "медиа"]

# ==========================================
# 6. ХЭНДЛЕРЫ I: ИНЛАЙН УПРАВЛЕНИЕ
# ==========================================
@bot.callback_query_handler(func=lambda call: True)
def handle_callbacks(call):
    global bot_enabled
    user_id = call.from_user.id
    data = call.data

    if user_id in banned_users:
        return

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

    register_user(call.from_user)
    u = get_session(call.from_user)
    lang = u.get("lang", "🇷🇺 Русский")
    code = LANG_CODE.get(lang, "ru")
    c_country = country_name(u.get("country")) if u.get("country") else "Кыргызстан"

    # ---- Тест интересов ----
    if data.startswith("int_"):
        key = data[4:]
        selected = u.setdefault("interests", set())

        if key == "done":
            if not selected:
                bot.answer_callback_query(call.id, PICK_ONE[code])
                return
            bot.answer_callback_query(call.id, "⏳")
            typing(call.message.chat.id)
            names = ", ".join(INTERESTS[k][1]["ru"] for k in selected if k in INTERESTS)
            selected_copy = set(selected)
            selected.clear()
            prompt = (
                f"Мои интересы: {names}. "
                f"Подбери 3–4 подходящие профессии, для каждой — коротко чем занимается и какой факультет/направление "
                f"выбрать в стране: {c_country}. Затем дай 2 навыка или предмета, которые стоит подтянуть, "
                f"и один первый шаг на этой неделе. В конце задай один уточняющий вопрос."
            )
            answer = ask_career_ai(u, prompt, use_search=False)
            if not answer:
                answer = AI_UNAVAILABLE.get(lang, AI_UNAVAILABLE["🇷🇺 Русский"])
            try:
                bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
            except Exception:
                pass
            send_long(call.message.chat.id, answer, get_reels_inline_buttons())
            return

        if key in INTERESTS:
            if key in selected:
                selected.discard(key)
            else:
                selected.add(key)
            try:
                bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id,
                                              reply_markup=get_interests_markup(selected, lang))
            except Exception:
                pass
        try:
            bot.answer_callback_query(call.id)
        except Exception:
            pass
        return

    # ---- Кнопки под ответами ----
    if data.startswith("act_"):
        try:
            bot.answer_callback_query(call.id, "⏳")
        except Exception:
            pass

        if data == "act_more_photo":
            send_photo_text(call.message.chat.id,
                            f"🏛️ Вид кампуса и университета в стране: {c_country} ✨",
                            f"{c_country} university campus student",
                            get_reels_inline_buttons())
            return

        typing(call.message.chat.id)
        topic = random.choice(FACT_TOPICS)
        prompt = (
            f"Дай один короткий интересный факт о профессиях в сфере «{topic}» и назови один реальный вуз в стране "
            f"{c_country}, где этому можно учиться. Не больше 500 символов."
        )
        ai_text = ask_career_ai(u, prompt, use_search=False)
        if not ai_text:
            ai_text = DB_INFO.get(u.get("country"), DB_INFO["🇰🇬 Кыргызстан"])["unis"]
        send_photo_text(call.message.chat.id, ai_text, f"{c_country} university building", get_reels_inline_buttons())

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

    register_user(message.from_user)
    u_data = get_session(message.from_user)
    u_data["history"] = []
    u_data["interests"] = set()

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

@bot.message_handler(content_types=['photo', 'video', 'document', 'voice', 'audio', 'sticker', 'video_note'])
def handle_non_text(message):
    if message.from_user.id in banned_users:
        return
    u = get_session(message.from_user)
    texts = {
        "🇷🇺 Русский": "Я пока понимаю только текст 📝 Напишите вопрос словами!",
        "🇰🇬 Кыргызча": "Мен азырынча текстти гана түшүнөм 📝 Суроону сөз менен жазыңыз!",
        "🇬🇧 English": "I only understand text for now 📝 Please type your question!",
        "🇹🇷 Türkçe": "Şimdilik sadece metin anlıyorum 📝 Lütfen sorunuzu yazın!"
    }
    bot.send_message(message.chat.id, texts.get(u.get("lang"), texts["🇷🇺 Русский"]))

@bot.message_handler(content_types=['text'])
def handle_message(message):
    global bot_enabled
    user_id = message.from_user.id
    text = (message.text or "").strip()

    if user_id in banned_users:
        return

    if not bot_enabled and user_id != OWNER_ID:
        bot.send_message(message.chat.id, "🔴 Бот временно выключен на техническое обслуживание.")
        return

    register_user(message.from_user)
    user_data = get_session(message.from_user)
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
            save_profile(user_id)

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
            changed = {
                "🇬🇧 English": "✅ Language changed!",
                "🇰🇬 Кыргызча": "✅ Тил алмаштырылды!",
                "🇹🇷 Türkçe": "✅ Dil değiştirildi!",
                "🇷🇺 Русский": "✅ Язык изменён!"
            }

            if not user_data.get("name"):
                user_data["state"] = "WAITING_FOR_NAME"
                bot.send_message(message.chat.id, name_prompts.get(lang, name_prompts["🇷🇺 Русский"]), reply_markup=types.ReplyKeyboardRemove())
                return
            elif not user_data.get("country"):
                user_data["state"] = "WAITING_FOR_COUNTRY"
                bot.send_message(message.chat.id, country_prompts.get(lang, country_prompts["🇷🇺 Русский"]), reply_markup=get_country_keyboard())
                return
            else:
                user_data["state"] = None
                bot.send_message(message.chat.id, changed.get(lang, changed["🇷🇺 Русский"]), reply_markup=get_main_keyboard(user_id, lang))
                return
        else:
            bot.send_message(message.chat.id, "Выберите язык кнопкой / Choose language with a button 👇", reply_markup=get_language_keyboard())
            return

    # Ввод имени
    if current_state == "WAITING_FOR_NAME":
        name = text[:40]
        user_data["name"] = name
        user_data["state"] = "WAITING_FOR_AGE"
        save_profile(user_id)

        age_prompts = {
            "🇬🇧 English": f"Nice to meet you, {name}! Please enter your age: 🔢",
            "🇰🇬 Кыргызча": f"Таанышканыма кубанычтамын, {name}! Жашыңызды жазыңыз: 🔢",
            "🇹🇷 Türkçe": f"Tanıştığıma memnun oldum {name}! Yaşınızı girin: 🔢",
            "🇷🇺 Русский": f"Приятно познакомиться, {name}! Укажите ваш возраст: 🔢"
        }
        bot.send_message(message.chat.id, age_prompts.get(lang, age_prompts["🇷🇺 Русский"]))
        return

    # Ввод возраста
    if current_state == "WAITING_FOR_AGE":
        if not text.isdigit() or not (5 <= int(text) <= 100):
            bad_age = {
                "🇬🇧 English": "Please enter your age as a number (e.g. 16) 🔢",
                "🇰🇬 Кыргызча": "Жашыңызды сан менен жазыңыз (мис. 16) 🔢",
                "🇹🇷 Türkçe": "Lütfen yaşınızı rakamla yazın (örn. 16) 🔢",
                "🇷🇺 Русский": "Укажите возраст числом (например, 16) 🔢"
            }
            bot.send_message(message.chat.id, bad_age.get(lang, bad_age["🇷🇺 Русский"]))
            return

        user_data["age"] = text
        user_data["state"] = "WAITING_FOR_COUNTRY"
        save_profile(user_id)

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
            user_data["history"] = []
            save_profile(user_id)

            welcomes = {
                "🇬🇧 English": f"🎓 Selected country: {text}\nI am Barsbek 🐆 — your advisor. Use the menu below or just ask me any question about professions and studying! ✨",
                "🇰🇬 Кыргызча": f"🎓 Тандалган өлкө: {text}\nМен Барсбек 🐆 — сиздин кеңешчиңизмин. Менюну колдонуңуз же кесиптер жана окуу жөнүндө каалаган суроону бериңиз! ✨",
                "🇹🇷 Türkçe": f"🎓 Seçilen ülke: {text}\nBen Barsbek 🐆 — danışmanınızım. Aşağıdaki menüyü kullanın veya meslekler ve eğitim hakkında istediğiniz soruyu sorun! ✨",
                "🇷🇺 Русский": f"🎓 Выбрана страна: {text}\nЯ Барсбек 🐆 — ваш консультант. Используйте меню ниже или просто задайте любой вопрос о профессиях и учёбе! ✨"
            }
            send_photo_text(message.chat.id, welcomes.get(lang, welcomes["🇷🇺 Русский"]),
                            f"{country_name(text)} architecture capital landmark",
                            get_main_keyboard(user_id, lang))
            return
        else:
            bot.send_message(message.chat.id, "Выберите страну кнопкой / Choose a country with a button 👇", reply_markup=get_country_keyboard())
            return

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

    # ---------- Навигация ----------
    if text in ["🔙 Главное меню", "Главное меню", "Back to Menu"]:
        user_data["state"] = None
        bot.send_message(message.chat.id, "🏡 Главное меню:", reply_markup=get_main_keyboard(user_id, lang))
        return

    # Кнопки меню определяются по эмодзи в начале — работают на всех 4 языках
    if text.startswith("🌐"):
        user_data["state"] = "WAITING_FOR_LANG"
        bot.send_message(message.chat.id, "Выберите язык / Choose language / Тилди тандаңыз: 🌐", reply_markup=get_language_keyboard())
        return

    if text.startswith("🌍"):
        user_data["state"] = "WAITING_FOR_COUNTRY"
        bot.send_message(message.chat.id, "Выберите страну для обучения / Choose target country: 🌍", reply_markup=get_country_keyboard())
        return

    if text.startswith("🔄"):
        start(message)
        return

    # Если страна ещё не выбрана — просим выбрать
    if not user_data.get("country"):
        user_data["state"] = "WAITING_FOR_COUNTRY"
        bot.send_message(message.chat.id, "Сначала выберите страну / First choose a country: 🌍", reply_markup=get_country_keyboard())
        return

    country_key = user_data["country"]
    c_country = country_name(country_key)
    code = LANG_CODE.get(lang, "ru")
    typing(message.chat.id)

    # Кнопка "🎯 Мой путь" — тест интересов
    if text.startswith("🎯"):
        user_data["interests"] = set()
        bot.send_message(message.chat.id, PATH_INTRO[code].format(c=c_country),
                         reply_markup=get_interests_markup(set(), lang))
        return

    # Топ ВУЗов и Стипендии — берём проверенные данные из базы (на других языках ИИ переводит)
    if text.startswith("🔥") or text.startswith("💰"):
        category = "unis" if text.startswith("🔥") else "grants"
        base_text = DB_INFO.get(country_key, DB_INFO["🇰🇬 Кыргызстан"])[category]
        answer = base_text
        if lang != "🇷🇺 Русский":
            translated = ask_ai([
                {"role": "system", "content": f"Translate the following text into {LANG_NAME.get(lang, 'English')}. "
                                              "Keep all names, numbers and emojis unchanged. Plain text only, no markdown."},
                {"role": "user", "content": base_text}
            ])
            if translated:
                answer = translated
        send_photo_text(message.chat.id, answer,
                        f"{c_country} university campus" if category == "unis" else f"{c_country} university scholarship diploma",
                        get_reels_inline_buttons())
        return

    # Лайфхаки и Документы — ИИ с запасным вариантом
    if text.startswith("💡") or text.startswith("📄"):
        if text.startswith("💡"):
            prompt = (f"Дай 5 практичных лайфхаков и главных требований для поступления в вуз в стране {c_country} "
                      f"для человека, которому {user_data.get('age') or 'школьный возраст'} лет.")
            fallback = FALLBACK_TIPS
        else:
            prompt = f"Составь чек-лист документов для поступления в вуз в стране {c_country} и подскажи, что готовить в первую очередь."
            fallback = FALLBACK_DOCS
        answer = ask_career_ai(user_data, prompt)
        send_long(message.chat.id, answer or fallback, get_reels_inline_buttons())
        return

    # Любой другой текст — свободный диалог с ИИ-профориентологом
    answer = ask_career_ai(user_data, text)
    if not answer:
        answer = AI_UNAVAILABLE.get(lang, AI_UNAVAILABLE["🇷🇺 Русский"])
    send_long(message.chat.id, answer, get_reels_inline_buttons())

# ==========================================
# 8. ЗАПУСК БОТА
# ==========================================
if __name__ == "__main__":
    print("Запуск Барсбек...")
    try:
        bot.remove_webhook()
    except Exception as e:
        print(f"remove_webhook: {e}")
    bot.infinity_polling(timeout=20, long_polling_timeout=5, skip_pending=True)
