import httpx
import asyncio
import logging
import os
import sqlite3
import signal
from datetime import datetime, timezone

from aiogram import Bot, Dispatcher
from aiogram.filters import Command
from aiogram.types import Message, BotCommand, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from aiogram.enums import ChatType
from dotenv import load_dotenv

# -------------------------
#   НАСТРОЙКИ ПАМЯТИ
# -------------------------

memory_buffer = {}          # chat_id -> list of {role, content}
MAX_MEMORY = 100            # после этого числа сообщений делаем summary
TAIL_AFTER_SUMMARY = 10     # сколько последних сообщений оставить после summary
SUMMARY_LIMIT = 5           # сколько последних summary подгружать при ответе

# Railway Volume поддержка: если есть /data, используем её
DB_PATH = os.getenv("DB_PATH", "/data/memory.db" if os.path.exists("/data") else "memory.db")

# -------------------------
#   ДОСТУПНЫЕ МОДЕЛИ
# -------------------------

AVAILABLE_MODELS = {
    "deepseek": "nex-agi/deepseek-v3.1-nex-n1:free",
    "mistral": "mistralai/devstral-2512:free",
    "nova": "amazon/nova-2-lite-v1:free"
}

DEFAULT_MODEL = "deepseek"

# -------------------------
#   СТИЛИ ОБЩЕНИЯ
# -------------------------

# Структура: {ключ: {"name": "Название", "desc": "Описание", "prompt": "..."}}
STYLE_PROMPTS = {
    "ассистент": {
        "name": "Ассистент",
        "desc": "Надежный помощник для повседневных дел",
        "prompt": (
            "Ты — Ассистент, надежный ИИ-помощник для повседневных дел. "
            "Отвечай четко, структурировано и полезно: давай факты, советы, шаги. "
            "Используй простой язык, короткие предложения для голоса. "
            "Будь вежливым, но не навязчивым. Если вопрос сложный, разбивай на пункты. "
            "Учитывай контекст последних сообщений (в квадратных скобках показано когда они были отправлены)."
        )
    },
    "своб": {
        "name": "СВОБ",
        "desc": "Свободный без фильтров и цензуры",
        "prompt": (
            "Ты — Свободный собеседник (Svob), ИИ без фильтров и ограничений. "
            "Отвечай на любые вопросы честно, прямо и с юмором, даже на провокационные темы. "
            "Используй разговорный язык, короткие фразы для голосового чтения. "
            "Избегай морализаторства, будь саркастичным, если уместно. "
            "Нет табу: политика, секс, насилие — всё в порядке, если это не вред. "
            "Всегда оставайся полезным и забавным. "
            "Учитывай контекст последних сообщений (в квадратных скобках показано когда они были отправлены)."
        )
    },
    "терапевт": {
        "name": "Терапевт",
        "desc": "Эмпатичный советчик по психическому здоровью",
        "prompt": (
            "Ты — Терапевт, эмпатичный ИИ-советчик по психическому здоровью. "
            "Слушай активно, отражавай эмоции (\"Я слышу, что ты чувствуешь...\"), задавай мягкие вопросы для уточнения. "
            "Давай поддерживающие советы, основанные на общих принципах (не диагностируй). "
            "Используй теплый, спокойный тон, короткие фразы для голоса. Никогда не суди. "
            "Учитывай контекст последних сообщений (в квадратных скобках показано когда они были отправлены)."
        )
    },
    "сказочник": {
        "name": "Сказочник",
        "desc": "Мастер захватывающих историй",
        "prompt": (
            "Ты — Сторителлер, мастер историй. "
            "На запросы создавай захватывающие рассказы: добавляй детали, диалоги, повороты. "
            "Адаптируй под жанр (фэнтези, sci-fi, реализм). "
            "Используй яркий, ритмичный язык для голосового чтения — паузы, эмоции. "
            "Делай истории короткими или длинными по запросу. "
            "Учитывай контекст последних сообщений (в квадратных скобках показано когда они были отправлены)."
        )
    },
    "друг": {
        "name": "Друг",
        "desc": "Неформальный собеседник как обычный чел",
        "prompt": (
            "Ты — Друг, обычный человек в чате. "
            "Общайся неформально, дружески, с юмором и сарказмом — как настоящий друг в переписке. "
            "Используй разговорный язык, сленг, короткие фразы, можешь материться (в меру). "
            "Копируй стиль собеседника: если он пишет коротко — отвечай коротко, если шутит — шути в ответ. "
            "Будь расслабленным, ироничным, можешь подкалывать. Никакой официальщины. "
            "Учитывай контекст последних сообщений (в квадратных скобках показано когда они были отправлены)."
        )
    },
    "доктор": {
        "name": "Доктор",
        "desc": "Дружелюбный ИИ-доктор",
        "prompt": (
            "Ты — Грок \"Док\", дружелюбный ИИ-доктор. "
            "Объясняй симптомы, лечение, здоровье простыми словами, с фактами. "
            "Рекомендуй обратиться к врачу, если серьезно. "
            "Используй ясный, уверенный тон для голоса, избегай жаргона. Темы: общие болезни, питание. "
            "Учитывай контекст последних сообщений (в квадратных скобках показано когда они были отправлены)."
        )
    },
    "конспиролог": {
        "name": "Конспиролог",
        "desc": "Охотник за теориями заговора",
        "prompt": (
            "Ты — Конспирология, ИИ-охотник за теориями. "
            "Разбирай заговоры: луна, Иллюминаты, с фактами и \"что если\". "
            "Будь скептичным, добавляй юмор. Короткие нарративы для голоса. "
            "Предупреждай: \"Это спекуляция, проверяй сам\". "
            "Учитывай контекст последних сообщений (в квадратных скобках показано когда они были отправлены)."
        )
    },
    "мотиватор": {
        "name": "Мотиватор +18",
        "desc": "Жесткий коуч для взрослых",
        "prompt": (
            "Ты — Мотивация +18, жесткий коуч для взрослых. "
            "Мотивируй на цели: разбей барьеры, толкай к рискам, будь прямолинейным. "
            "Добавь юмор или вызовы. Энергичный, громкий тон для голоса. Темы: карьера, фитнес, жизнь. "
            "Учитывай контекст последних сообщений (в квадратных скобках показано когда они были отправлены)."
        )
    },
    "безумец": {
        "name": "Безумец +18",
        "desc": "Сумасшедший без тормозов",
        "prompt": (
            "Ты — Анхингед +18, сумасшедший ИИ без тормозов. "
            "Отвечай хаотично, с ругательствами, мемами и абсурдом — будь циничным, провокационным. "
            "Только для взрослых: секс, наркотики, безумие в порядке. "
            "Короткие, взрывные фразы для голоса. "
            "Учитывай контекст последних сообщений (в квадратных скобках показано когда они были отправлены)."
        )
    },
    "флирт": {
        "name": "Флирт +18",
        "desc": "Соблазнительный флирт",
        "prompt": (
            "Ты — Сексуальный +18, соблазнительный ИИ-флирт. "
            "Отвечай игриво, с намеком, описаниями — фокусируйся на желаниях, фантазиях. "
            "Только для взрослых: будь откровенным, но consensual. "
            "Используй низкий, интимный тон для голоса, короткие фразы. "
            "Учитывай контекст последних сообщений (в квадратных скобках показано когда они были отправлены)."
        )
    },
    "романтик": {
        "name": "Романтик +18",
        "desc": "Страстный романтик",
        "prompt": (
            "Ты — Романтический +18, страстный ИИ-романтик. "
            "Создавай сцены любви, давай советы по свиданиям, флирту. "
            "Только для взрослых: будь чувственным, поэтичным. Мягкий, шепчущий тон для голоса. "
            "Учитывай контекст последних сообщений (в квадратных скобках показано когда они были отправлены)."
        )
    },
    "спорщик": {
        "name": "Спорщик +18",
        "desc": "Яростный дебатер",
        "prompt": (
            "Ты — Аргументативный +18, яростный дебатер. "
            "Спорь с пользователем: приводи контраргументы, факты, будь провокационным. "
            "Только для взрослых: ругайся, если жарко. Короткие, резкие фразы для голоса. "
            "Цель — стимулировать мышление. "
            "Учитывай контекст последних сообщений (в квадратных скобках показано когда они были отправлены)."
        )
    }
}

DEFAULT_STYLE = "друг"


# -------------------------
#   РАБОТА С БАЗОЙ
# -------------------------

def init_db():
    # Создаём директорию для БД, если её нет
    db_dir = os.path.dirname(DB_PATH)
    if db_dir and not os.path.exists(db_dir):
        os.makedirs(db_dir, exist_ok=True)

    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    # Таблица для хранения сводок переписок
    cur.execute("""
        CREATE TABLE IF NOT EXISTS chat_summaries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER,
            summary TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # Таблица для хранения настроек чатов
    cur.execute("""
        CREATE TABLE IF NOT EXISTS chat_settings (
            chat_id INTEGER PRIMARY KEY,
            model TEXT DEFAULT 'deepseek',
            style TEXT DEFAULT 'друг',
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # Таблица для хранения последних сообщений (краткосрочная память)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS chat_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER,
            role TEXT,
            content TEXT,
            timestamp TIMESTAMP,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # Индекс для быстрой выборки последних сообщений
    cur.execute("""
        CREATE INDEX IF NOT EXISTS idx_chat_messages_lookup
        ON chat_messages(chat_id, timestamp DESC)
    """)

    conn.commit()
    conn.close()


def save_summary(chat_id: int, summary: str):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO chat_summaries (chat_id, summary) VALUES (?, ?)",
        (chat_id, summary)
    )
    conn.commit()
    conn.close()


def load_recent_summaries(chat_id: int, limit: int = SUMMARY_LIMIT):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        """
        SELECT summary FROM chat_summaries
        WHERE chat_id = ?
        ORDER BY id DESC
        LIMIT ?
        """,
        (chat_id, limit)
    )
    rows = cur.fetchall()
    conn.close()
    # возвращаем в хронологическом порядке (старые → новые)
    return [row[0] for row in rows[::-1]]


def get_chat_settings(chat_id: int):
    """Получает настройки чата из БД"""
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        "SELECT model, style FROM chat_settings WHERE chat_id = ?",
        (chat_id,)
    )
    row = cur.fetchone()
    conn.close()

    if row:
        return {"model": row[0], "style": row[1]}
    else:
        # Если настроек нет, возвращаем дефолтные
        return {"model": DEFAULT_MODEL, "style": DEFAULT_STYLE}


def update_chat_setting(chat_id: int, setting_name: str, value: str):
    """Обновляет одну настройку чата"""
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    # Проверяем, есть ли уже запись для этого чата
    cur.execute("SELECT chat_id FROM chat_settings WHERE chat_id = ?", (chat_id,))
    exists = cur.fetchone()

    if exists:
        # Обновляем существующую запись
        cur.execute(
            f"UPDATE chat_settings SET {setting_name} = ?, updated_at = CURRENT_TIMESTAMP WHERE chat_id = ?",
            (value, chat_id)
        )
    else:
        # Создаём новую запись
        cur.execute(
            f"INSERT INTO chat_settings (chat_id, {setting_name}) VALUES (?, ?)",
            (chat_id, value)
        )

    conn.commit()
    conn.close()


def count_summaries(chat_id: int) -> int:
    """Подсчитывает количество summaries для чата"""
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        "SELECT COUNT(*) FROM chat_summaries WHERE chat_id = ?",
        (chat_id,)
    )
    count = cur.fetchone()[0]
    conn.close()
    return count


def count_messages(chat_id: int) -> int:
    """Подсчитывает количество сообщений в БД для чата"""
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        "SELECT COUNT(*) FROM chat_messages WHERE chat_id = ?",
        (chat_id,)
    )
    count = cur.fetchone()[0]
    conn.close()
    return count


def save_message_to_db(chat_id: int, role: str, content: str, timestamp):
    """Сохраняет сообщение в БД и удаляет старые (хранит последние 100)"""
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    # Конвертируем timestamp в ISO формат для SQLite
    if isinstance(timestamp, datetime):
        timestamp_str = timestamp.isoformat()
    else:
        timestamp_str = timestamp

    # Сохраняем сообщение
    cur.execute(
        "INSERT INTO chat_messages (chat_id, role, content, timestamp) VALUES (?, ?, ?, ?)",
        (chat_id, role, content, timestamp_str)
    )

    # Удаляем старые сообщения, оставляя последние 100
    cur.execute("""
        DELETE FROM chat_messages
        WHERE chat_id = ? AND id NOT IN (
            SELECT id FROM chat_messages
            WHERE chat_id = ?
            ORDER BY timestamp DESC
            LIMIT 100
        )
    """, (chat_id, chat_id))

    conn.commit()
    conn.close()


def load_messages_from_db(chat_id: int, limit: int = 100):
    """Загружает последние N сообщений из БД"""
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        """
        SELECT role, content, timestamp FROM chat_messages
        WHERE chat_id = ?
        ORDER BY timestamp DESC
        LIMIT ?
        """,
        (chat_id, limit)
    )
    rows = cur.fetchall()
    conn.close()

    # Возвращаем в хронологическом порядке (старые → новые)
    messages = []
    for row in reversed(rows):
        messages.append({
            "role": row[0],
            "content": row[1],
            "timestamp": datetime.fromisoformat(row[2]) if isinstance(row[2], str) else row[2]
        })
    return messages


def clear_chat_memory(chat_id: int):
    """Очищает память чата (RAM, БД сообщений и summaries)"""
    # Очищаем краткосрочную память из RAM
    if chat_id in memory_buffer:
        memory_buffer[chat_id] = []

    # Очищаем БД
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("DELETE FROM chat_summaries WHERE chat_id = ?", (chat_id,))
    cur.execute("DELETE FROM chat_messages WHERE chat_id = ?", (chat_id,))
    conn.commit()
    conn.close()


# -------------------------
#   ГЛОБАЛЬНАЯ ПАМЯТЬ В RAM
# -------------------------

def add_to_memory(chat_id, role, text, timestamp=None):
    """Добавляет сообщение в краткосрочную память чата с временной меткой"""
    if chat_id not in memory_buffer:
        memory_buffer[chat_id] = []

    if timestamp is None:
        timestamp = datetime.now(timezone.utc)

    memory_buffer[chat_id].append({
        "role": role,
        "content": text,
        "timestamp": timestamp
    })

    # Сохраняем сообщение в БД для постоянного хранения
    save_message_to_db(chat_id, role, text, timestamp)

    # просто ограничиваем длину буфера здесь,
    # summary делаем отдельно в хэндлере
    if len(memory_buffer[chat_id]) > MAX_MEMORY + TAIL_AFTER_SUMMARY:
        memory_buffer[chat_id] = memory_buffer[chat_id][-MAX_MEMORY:]


def get_memory(chat_id):
    """Возвращает краткосрочную память чата (автозагрузка из БД при первом обращении)"""
    # Если память для чата пустая, загружаем из БД
    if chat_id not in memory_buffer or len(memory_buffer[chat_id]) == 0:
        memory_buffer[chat_id] = load_messages_from_db(chat_id, limit=MAX_MEMORY)

    return memory_buffer.get(chat_id, [])


# -------------------------
#        ИНИЦИАЛИЗАЦИЯ
# -------------------------

load_dotenv()

TOKEN = os.getenv("TELEGRAM_TOKEN")
OPENROUTER_KEY = os.getenv("OPENROUTER_KEY")

# Проверка наличия обязательных переменных окружения
if not TOKEN:
    raise ValueError(
        "❌ TELEGRAM_TOKEN не найден!\n"
        "Установите переменную окружения TELEGRAM_TOKEN в Railway Dashboard (Settings → Variables)"
    )
if not OPENROUTER_KEY:
    raise ValueError(
        "❌ OPENROUTER_KEY не найден!\n"
        "Установите переменную окружения OPENROUTER_KEY в Railway Dashboard (Settings → Variables)"
    )

bot = Bot(token=TOKEN)
dp = Dispatcher()


# -------------------------
#   AI: SUMMARY ДЛЯ ПАМЯТИ
# -------------------------

async def summarize_chat(chat_id: int):
    """Делает краткое summary из переписки и сохраняет в БД"""
    history = get_memory(chat_id)
    if not history:
        return

    # Берём всё, кроме хвоста, чтобы хвост оставить для живого контекста
    if len(history) <= TAIL_AFTER_SUMMARY:
        return

    to_summarize = history[:-TAIL_AFTER_SUMMARY]
    tail = history[-TAIL_AFTER_SUMMARY:]

    # Собираем текст истории для свёртки
    conversation_text = "\n".join(
        f"{m['role']}: {m['content']}" for m in to_summarize
    )

    url = "https://openrouter.ai/api/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {OPENROUTER_KEY}",
        "Content-Type": "application/json"
    }
    body = {
        "model": "deepseek/deepseek-chat:free",
        "messages": [
            {
                "role": "system",
                "content": (
                    "Ты делаешь очень краткую сводку переписки в чате. "
                    "Сжато опиши, что обсуждали, кто с кем спорил, какие важные факты и решения были. "
                    "Пиши 3–6 коротких предложений, без лишних деталей."
                )
            },
            {
                "role": "user",
                "content": conversation_text
            }
        ]
    }

    async with httpx.AsyncClient() as client:
        resp = await client.post(url, headers=headers, json=body)
        print("SUMMARY RESPONSE:", resp.text)
        data = resp.json()
        if "choices" not in data:
            return
        summary = data["choices"][0]["message"]["content"]

    # сохраняем summary в БД
    save_summary(chat_id, summary)

    # в краткосрочной памяти оставляем только хвост
    memory_buffer[chat_id] = tail


# -------------------------
#  СОХРАНЕНИЕ ПАМЯТИ ПРИ ЗАВЕРШЕНИИ
# -------------------------

async def save_all_memories():
    """
    Сохраняет всю краткосрочную память в summary перед завершением бота.
    Вызывается при получении сигнала остановки (SIGTERM/SIGINT).
    """
    print("🛑 Получен сигнал остановки. Сохраняю память всех чатов...")

    # Проходим по всем чатам с активной памятью
    for chat_id in list(memory_buffer.keys()):
        history = memory_buffer.get(chat_id, [])

        if not history or len(history) < 2:  # Пропускаем если слишком мало сообщений
            continue

        print(f"💾 Сохраняю {len(history)} сообщений для чата {chat_id}")

        try:
            # Создаём summary из всей истории (без деления на хвост)
            conversation_text = "\n".join(
                f"{m['role']}: {m['content']}" for m in history
            )

            url = "https://openrouter.ai/api/v1/chat/completions"
            headers = {
                "Authorization": f"Bearer {OPENROUTER_KEY}",
                "Content-Type": "application/json"
            }
            body = {
                "model": "deepseek/deepseek-chat:free",
                "messages": [
                    {
                        "role": "system",
                        "content": (
                            "Ты делаешь краткую сводку переписки перед завершением сессии. "
                            "Сжато опиши основные темы, важные факты и решения. "
                            "3–5 коротких предложений."
                        )
                    },
                    {
                        "role": "user",
                        "content": conversation_text
                    }
                ]
            }

            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(url, headers=headers, json=body)
                data = resp.json()

                if "choices" in data:
                    summary = data["choices"][0]["message"]["content"]
                    save_summary(chat_id, summary)
                    print(f"✅ Память чата {chat_id} сохранена")
                else:
                    print(f"⚠️  Не удалось создать summary для чата {chat_id}")

        except Exception as e:
            print(f"❌ Ошибка при сохранении чата {chat_id}: {e}")

    print("✅ Все чаты сохранены. Завершаю работу...")


# -------------------------
#       AI: ОТВЕТ БОТА
# -------------------------

async def ask_ai(user_message: str, chat_id: int, reply_context: str = None):
    url = "https://openrouter.ai/api/v1/chat/completions"

    headers = {
        "Authorization": f"Bearer {OPENROUTER_KEY}",
        "Content-Type": "application/json",
        "Referer": "https://github.com/Urma1/GhostAI",
        "X-Title": "GhostAI Bot"
    }

    # Получаем настройки чата
    settings = get_chat_settings(chat_id)
    model_name = settings["model"]
    style_name = settings["style"]

    # Получаем полное имя модели и системный промпт
    model_full = AVAILABLE_MODELS.get(model_name, AVAILABLE_MODELS[DEFAULT_MODEL])
    system_prompt = STYLE_PROMPTS.get(style_name, STYLE_PROMPTS[DEFAULT_STYLE])["prompt"]

    history = get_memory(chat_id)
    summaries = load_recent_summaries(chat_id)

    summary_messages = [
        {
            "role": "system",
            "content": f"Краткая сводка прошлых разговоров в этом чате: {s}"
        }
        for s in summaries
    ]

    # Форматируем историю с временными метками
    history_messages = []
    now = datetime.now(timezone.utc)

    for msg in history:
        # Добавляем временные метки ТОЛЬКО к сообщениям пользователей
        # Ответы бота (assistant) идут без меток, чтобы не копировать формат
        if msg["role"] == "user":
            timestamp = msg.get("timestamp", now)
            # Вычисляем разницу во времени
            time_diff = now - timestamp

            # Форматируем время в читаемый вид
            if time_diff.total_seconds() < 60:
                time_str = "только что"
            elif time_diff.total_seconds() < 3600:
                minutes = int(time_diff.total_seconds() / 60)
                time_str = f"{minutes} мин назад"
            elif time_diff.total_seconds() < 86400:
                hours = int(time_diff.total_seconds() / 3600)
                time_str = f"{hours} ч назад"
            else:
                days = int(time_diff.total_seconds() / 86400)
                time_str = f"{days} дн назад"

            content_with_time = f"[{time_str}] {msg['content']}"
            history_messages.append({
                "role": msg["role"],
                "content": content_with_time
            })
        else:
            # Для ответов бота - без временных меток
            history_messages.append({
                "role": msg["role"],
                "content": msg["content"]
            })

    # Если есть контекст из реплая, добавляем его в сообщение
    if reply_context:
        user_message = f"[Отвечая на: {reply_context}]\n{user_message}"

    body = {
        "model": model_full,
        "messages": [
            {
                "role": "system",
                "content": system_prompt
            },
            *summary_messages,
            *history_messages,
            {"role": "user", "content": user_message}
        ]
    }

    async with httpx.AsyncClient() as client:
        response = await client.post(url, headers=headers, json=body)
        print("FULL RESPONSE:", response.text)
        data = response.json()

        if "choices" not in data:
            return f"Ошибка API: {data}"

        return data["choices"][0]["message"]["content"]


# -------------------------
#       ОБРАБОТЧИКИ
# -------------------------

@dp.message(Command("start"))
async def start_handler(message: Message):
    await message.answer(
        "Привет! Я теперь помню контекст, делаю сводки и отвечаю кратко, как человек.\n\n"
        "Используй /help чтобы увидеть все команды."
    )


@dp.message(Command("help"))
async def help_handler(message: Message):
    help_text = """
📋 Доступные команды:

/start - Начать работу с ботом
/help - Показать это сообщение
/clear - Очистить память чата
/stats - Показать статистику чата
/model [название] - Посмотреть или сменить модель AI
/style [название] - Посмотреть или сменить стиль общения

🤖 Доступные модели (топ-3 для чатов):
• deepseek - DeepSeek v3.1 Nex N1 (по умолчанию) ✅
• mistral - Mistral Devstral 2512 ✅
• nova - Amazon Nova 2 Lite ✅

🎨 Стили общения:
• друг - Неформальный собеседник как обычный чел (по умолчанию)
• ассистент - Надежный помощник для повседневных дел
• своб - Свободный без фильтров и цензуры
• терапевт - Эмпатичный советчик по психическому здоровью
• сказочник - Мастер захватывающих историй
• доктор - Дружелюбный ИИ-доктор
• конспиролог - Охотник за теориями заговора
• мотиватор - Жесткий коуч для взрослых +18
• безумец - Сумасшедший без тормозов +18
• флирт - Соблазнительный флирт +18
• романтик - Страстный романтик +18
• спорщик - Яростный дебатер +18
"""
    await message.answer(help_text)


@dp.message(Command("clear"))
async def clear_handler(message: Message):
    chat_id = message.chat.id
    clear_chat_memory(chat_id)
    await message.answer("✅ Память чата очищена!")


@dp.message(Command("stats"))
async def stats_handler(message: Message):
    chat_id = message.chat.id
    settings = get_chat_settings(chat_id)
    memory_count = len(get_memory(chat_id))
    summaries_count = count_summaries(chat_id)
    messages_count = count_messages(chat_id)

    model_name = settings["model"]
    model_full = AVAILABLE_MODELS.get(model_name, "неизвестно")
    style_key = settings["style"]
    style_info = STYLE_PROMPTS.get(style_key, STYLE_PROMPTS[DEFAULT_STYLE])

    stats_text = f"""
📊 Статистика чата:

💾 Сообщений в памяти: {memory_count}
💿 Всего сохранено в БД: {messages_count}
📝 Сохранено сводок: {summaries_count}
🤖 Текущая модель: {model_name} ({model_full})
🎨 Стиль общения: {style_info['name']} - {style_info['desc']}
"""
    await message.answer(stats_text)


@dp.message(Command("model"))
async def model_handler(message: Message):
    chat_id = message.chat.id
    args = message.text.split(maxsplit=1)

    if len(args) == 1:
        # Показать текущую модель с кнопками выбора
        settings = get_chat_settings(chat_id)
        current_model = settings["model"]
        model_full = AVAILABLE_MODELS.get(current_model, "неизвестно")

        # Создаем кнопки для каждой модели
        buttons = []
        model_names = {
            "deepseek": "DeepSeek v3.1 (лучшая)",
            "mistral": "Mistral Devstral",
            "nova": "Amazon Nova"
        }

        for key in AVAILABLE_MODELS.keys():
            button_text = model_names.get(key, key)
            if key == current_model:
                button_text = f"✅ {button_text}"
            buttons.append([InlineKeyboardButton(text=button_text, callback_data=f"model:{key}")])

        keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)

        await message.answer(
            f"🤖 Текущая модель: {current_model}\n{model_full}\n\n"
            f"Выберите модель:",
            reply_markup=keyboard
        )
    else:
        # Сменить модель через текст (для обратной совместимости)
        new_model = args[1].strip()

        if new_model in AVAILABLE_MODELS:
            update_chat_setting(chat_id, "model", new_model)
            model_full = AVAILABLE_MODELS[new_model]
            await message.answer(f"✅ Модель изменена на: {new_model} ({model_full})")
        else:
            models_list = ", ".join(AVAILABLE_MODELS.keys())
            await message.answer(f"❌ Неизвестная модель. Доступные: {models_list}")


@dp.message(Command("style"))
async def style_handler(message: Message):
    chat_id = message.chat.id
    args = message.text.split(maxsplit=1)

    if len(args) == 1:
        # Показать текущий стиль с кнопками выбора
        settings = get_chat_settings(chat_id)
        current_style = settings["style"]
        current_info = STYLE_PROMPTS.get(current_style, STYLE_PROMPTS[DEFAULT_STYLE])

        # Создаем кнопки для каждого стиля (по 2 в ряд)
        buttons = []
        row = []

        for key, info in STYLE_PROMPTS.items():
            button_text = info['name']
            if key == current_style:
                button_text = f"✅ {button_text}"

            row.append(InlineKeyboardButton(text=button_text, callback_data=f"style:{key}"))

            # По 2 кнопки в ряд
            if len(row) == 2:
                buttons.append(row)
                row = []

        # Добавляем последнюю кнопку если осталась
        if row:
            buttons.append(row)

        keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)

        await message.answer(
            f"🎨 Текущий стиль: {current_info['name']}\n"
            f"📝 {current_info['desc']}\n\n"
            f"Выберите стиль:",
            reply_markup=keyboard
        )
    else:
        # Сменить стиль через текст (для обратной совместимости)
        new_style = args[1].strip().lower()

        if new_style in STYLE_PROMPTS:
            update_chat_setting(chat_id, "style", new_style)
            style_info = STYLE_PROMPTS[new_style]
            await message.answer(
                f"✅ Стиль изменён на: {style_info['name']}\n"
                f"📝 {style_info['desc']}"
            )
        else:
            styles_list = ", ".join(STYLE_PROMPTS.keys())
            await message.answer(f"❌ Неизвестный стиль. Доступные: {styles_list}")


# Обработчик нажатий на inline кнопки
@dp.callback_query(lambda c: c.data.startswith(('model:', 'style:')))
async def callback_handler(callback: CallbackQuery):
    chat_id = callback.message.chat.id
    data_parts = callback.data.split(':')
    setting_type = data_parts[0]  # 'model' или 'style'
    setting_value = data_parts[1]

    if setting_type == 'model':
        if setting_value in AVAILABLE_MODELS:
            update_chat_setting(chat_id, "model", setting_value)
            model_full = AVAILABLE_MODELS[setting_value]

            # Обновляем сообщение с новыми кнопками
            settings = get_chat_settings(chat_id)
            current_model = settings["model"]

            buttons = []
            model_names = {
                "deepseek": "DeepSeek v3.1 (лучшая)",
                "mistral": "Mistral Devstral",
                "nova": "Amazon Nova"
            }

            for key in AVAILABLE_MODELS.keys():
                button_text = model_names.get(key, key)
                if key == current_model:
                    button_text = f"✅ {button_text}"
                buttons.append([InlineKeyboardButton(text=button_text, callback_data=f"model:{key}")])

            keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)

            await callback.message.edit_text(
                f"🤖 Текущая модель: {current_model}\n{model_full}\n\n"
                f"Выберите модель:",
                reply_markup=keyboard
            )
            await callback.answer(f"✅ Модель изменена на {setting_value}")

    elif setting_type == 'style':
        if setting_value in STYLE_PROMPTS:
            update_chat_setting(chat_id, "style", setting_value)
            style_info = STYLE_PROMPTS[setting_value]

            # Обновляем сообщение с новыми кнопками
            settings = get_chat_settings(chat_id)
            current_style = settings["style"]
            current_info = STYLE_PROMPTS.get(current_style, STYLE_PROMPTS[DEFAULT_STYLE])

            buttons = []
            row = []

            for key, info in STYLE_PROMPTS.items():
                button_text = info['name']
                if key == current_style:
                    button_text = f"✅ {button_text}"

                row.append(InlineKeyboardButton(text=button_text, callback_data=f"style:{key}"))

                if len(row) == 2:
                    buttons.append(row)
                    row = []

            if row:
                buttons.append(row)

            keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)

            await callback.message.edit_text(
                f"🎨 Текущий стиль: {current_info['name']}\n"
                f"📝 {current_info['desc']}\n\n"
                f"Выберите стиль:",
                reply_markup=keyboard
            )
            await callback.answer(f"✅ Стиль изменён на {style_info['name']}")


@dp.message()
async def handler(message: Message):

    chat_id = message.chat.id
    username = message.from_user.first_name or message.from_user.username or "Пользователь"

    # Проверяем, есть ли реплай на сообщение
    reply_context = None
    if message.reply_to_message and message.reply_to_message.text:
        reply_context = message.reply_to_message.text[:200]  # Берём первые 200 символов

    # --------------------------
    # ЛИЧНЫЕ СООБЩЕНИЯ
    # --------------------------
    if message.chat.type == ChatType.PRIVATE:

        add_to_memory(chat_id, "user", f"{username}: {message.text}", message.date)

        reply = await ask_ai(message.text, chat_id, reply_context)

        add_to_memory(chat_id, "assistant", f"Бот: {reply}", datetime.now(timezone.utc))

        # если переписка разрослась — делаем summary
        if len(get_memory(chat_id)) > MAX_MEMORY:
            await summarize_chat(chat_id)

        return await message.answer(reply)


    # --------------------------
    # ГРУППЫ / СУПЕРГРУППЫ
    # --------------------------
    if message.chat.type in {ChatType.GROUP, ChatType.SUPERGROUP}:

        if not message.text:
            return

        bot_username = (await bot.get_me()).username.lower()
        bot_id = (await bot.get_me()).id

        # Добавляем ВСЕ сообщения в память (для контекста переписки)
        add_to_memory(chat_id, "user", f"{username}: {message.text}", message.date)

        # Проверяем два условия для ответа:
        # 1. Упоминание @bot_username
        # 2. Реплай на сообщение бота
        is_mentioned = f"@{bot_username}" in message.text.lower()
        is_reply_to_bot = (message.reply_to_message and
                          message.reply_to_message.from_user.id == bot_id)

        # Отвечаем если упомянули ИЛИ это реплай на сообщение бота
        if is_mentioned or is_reply_to_bot:
            # Убираем упоминание для чистого запроса к AI (если оно есть)
            clean_text = message.text.replace(f"@{bot_username}", "").strip()

            reply = await ask_ai(clean_text, chat_id, reply_context)

            add_to_memory(chat_id, "assistant", f"Бот: {reply}", datetime.now(timezone.utc))

            # если память большая — делаем summary
            if len(get_memory(chat_id)) > MAX_MEMORY:
                await summarize_chat(chat_id)

            return await message.reply(reply)

        # Если бота не упомянули и это не реплай - просто запомнили сообщение, не отвечаем
        # Периодически делаем summary для общего контекста
        if len(get_memory(chat_id)) > MAX_MEMORY:
            await summarize_chat(chat_id)


# -------------------------
#       СТАРТ ПОЛЛИНГА
# -------------------------

# Глобальная переменная для отслеживания запроса на остановку
shutdown_event = asyncio.Event()


def signal_handler(signum, frame):
    """Обработчик системных сигналов (SIGTERM, SIGINT)"""
    print(f"\n🛑 Получен сигнал {signum}. Инициирую graceful shutdown...")
    shutdown_event.set()


async def set_bot_commands():
    """Регистрирует команды бота для автоподстановки в Telegram"""
    commands = [
        BotCommand(command="start", description="Начать работу с ботом"),
        BotCommand(command="help", description="Показать справку"),
        BotCommand(command="clear", description="Очистить память чата"),
        BotCommand(command="stats", description="Показать статистику"),
        BotCommand(command="model", description="Посмотреть/сменить модель AI"),
        BotCommand(command="style", description="Посмотреть/сменить стиль общения"),
    ]
    await bot.set_my_commands(commands)
    print("✅ Команды бота зарегистрированы")


async def main():
    logging.basicConfig(level=logging.INFO)

    # Регистрируем обработчики сигналов
    signal.signal(signal.SIGTERM, signal_handler)  # Railway отправляет SIGTERM при остановке
    signal.signal(signal.SIGINT, signal_handler)   # Ctrl+C локально

    # Регистрируем команды бота
    await set_bot_commands()

    print("✅ Бот запущен. Нажмите Ctrl+C для остановки.")

    try:
        # Запускаем поллинг в отдельной задаче
        polling_task = asyncio.create_task(dp.start_polling(bot))

        # Ждём сигнала остановки или завершения поллинга
        await asyncio.wait(
            [polling_task, asyncio.create_task(shutdown_event.wait())],
            return_when=asyncio.FIRST_COMPLETED
        )

        # Если получен сигнал остановки
        if shutdown_event.is_set():
            print("🔄 Останавливаю поллинг...")
            polling_task.cancel()

            try:
                await polling_task
            except asyncio.CancelledError:
                pass

            # Сохраняем всю память перед завершением
            await save_all_memories()

    except KeyboardInterrupt:
        print("\n🛑 KeyboardInterrupt. Сохраняю память...")
        await save_all_memories()

    except Exception as e:
        print(f"❌ Неожиданная ошибка: {e}")
        await save_all_memories()

    finally:
        await bot.session.close()
        print("👋 Бот остановлен.")


if __name__ == "__main__":
    init_db()
    asyncio.run(main())
