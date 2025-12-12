import httpx
import asyncio
import logging
import os
import sqlite3
import signal
from datetime import datetime, timezone

from aiogram import Bot, Dispatcher
from aiogram.filters import Command
from aiogram.types import Message, BotCommand
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
    "mistral": "mistralai/devstral-2512:free",
    "deepseek": "nex-agi/deepseek-v3.1-nex-n1:free",
    "nova": "amazon/nova-2-lite-v1:free",
    "olmo": "allenai/olmo-3-32b-think:free",
    "trinity": "arcee-ai/trinity-mini:free",
    "kat": "kwaipilot/kat-coder-pro:free",
    "nemotron": "nvidia/nemotron-nano-12b-v2-vl:free"
}

DEFAULT_MODEL = "mistral"

# -------------------------
#   СТИЛИ ОБЩЕНИЯ
# -------------------------

STYLE_PROMPTS = {
    "short": (
        "Ты дружелюбный участник телеграм-чата. "
        "Отвечай КОРОТКО: 1–2 предложения максимум. "
        "Пиши просто, как человек: без формальностей, "
        "без сложных слов, без больших абзацев. "
        "Если вопрос неполный — уточни. "
        "Учитывай контекст последних сообщений и сводки прошлых разговоров."
    ),
    "detailed": (
        "Ты умный и детальный ассистент в телеграм-чате. "
        "Давай подробные ответы с объяснениями и примерами. "
        "Структурируй информацию, используй списки где уместно. "
        "Учитывай контекст последних сообщений и сводки прошлых разговоров."
    ),
    "casual": (
        "Ты расслабленный друг в чате. "
        "Общайся неформально, можно с юмором и эмодзи. "
        "Отвечай коротко и по делу, но дружелюбно. "
        "Учитывай контекст последних сообщений и сводки прошлых разговоров."
    ),
    "formal": (
        "Ты профессиональный помощник. "
        "Отвечай вежливо, структурированно и по существу. "
        "Используй точные формулировки. "
        "Учитывай контекст последних сообщений и сводки прошлых разговоров."
    )
}

DEFAULT_STYLE = "short"


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
            model TEXT DEFAULT 'mistral',
            style TEXT DEFAULT 'short',
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
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


def clear_chat_memory(chat_id: int):
    """Очищает память чата (RAM и summaries из БД)"""
    # Очищаем краткосрочную память
    if chat_id in memory_buffer:
        memory_buffer[chat_id] = []

    # Удаляем summaries из БД
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("DELETE FROM chat_summaries WHERE chat_id = ?", (chat_id,))
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

    # просто ограничиваем длину буфера здесь,
    # summary делаем отдельно в хэндлере
    if len(memory_buffer[chat_id]) > MAX_MEMORY + TAIL_AFTER_SUMMARY:
        memory_buffer[chat_id] = memory_buffer[chat_id][-MAX_MEMORY:]


def get_memory(chat_id):
    """Возвращает краткосрочную память чата"""
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
    system_prompt = STYLE_PROMPTS.get(style_name, STYLE_PROMPTS[DEFAULT_STYLE])

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

        # Добавляем сообщение с временной меткой
        content_with_time = f"[{time_str}] {msg['content']}"
        history_messages.append({
            "role": msg["role"],
            "content": content_with_time
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

🤖 Доступные модели:
• mistral - Mistral Devstral 2512 (по умолчанию)
• deepseek - DeepSeek v3.1 Nex N1
• nova - Amazon Nova 2 Lite
• olmo - Allen AI OLMo 3 32B
• trinity - Arcee Trinity Mini
• kat - KwaiPilot KAT Coder Pro
• nemotron - NVIDIA Nemotron Nano 12B (vision)

🎨 Стили общения:
• short - Краткие ответы (по умолчанию)
• detailed - Подробные ответы
• casual - Неформальное общение
• formal - Формальное общение
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

    model_name = settings["model"]
    model_full = AVAILABLE_MODELS.get(model_name, "неизвестно")
    style_name = settings["style"]

    stats_text = f"""
📊 Статистика чата:

💾 Сообщений в памяти: {memory_count}
📝 Сохранено сводок: {summaries_count}
🤖 Текущая модель: {model_name} ({model_full})
🎨 Стиль общения: {style_name}
"""
    await message.answer(stats_text)


@dp.message(Command("model"))
async def model_handler(message: Message):
    chat_id = message.chat.id
    args = message.text.split(maxsplit=1)

    if len(args) == 1:
        # Показать текущую модель
        settings = get_chat_settings(chat_id)
        current_model = settings["model"]
        model_full = AVAILABLE_MODELS.get(current_model, "неизвестно")

        models_list = "\n".join([f"• {k} - {v}" for k, v in AVAILABLE_MODELS.items()])
        await message.answer(
            f"🤖 Текущая модель: {current_model} ({model_full})\n\n"
            f"Доступные модели:\n{models_list}\n\n"
            f"Использование: /model <название>"
        )
    else:
        # Сменить модель
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
        # Показать текущий стиль
        settings = get_chat_settings(chat_id)
        current_style = settings["style"]

        styles_list = "\n".join([f"• {k}" for k in STYLE_PROMPTS.keys()])
        await message.answer(
            f"🎨 Текущий стиль: {current_style}\n\n"
            f"Доступные стили:\n{styles_list}\n\n"
            f"Использование: /style <название>"
        )
    else:
        # Сменить стиль
        new_style = args[1].strip()

        if new_style in STYLE_PROMPTS:
            update_chat_setting(chat_id, "style", new_style)
            await message.answer(f"✅ Стиль изменён на: {new_style}")
        else:
            styles_list = ", ".join(STYLE_PROMPTS.keys())
            await message.answer(f"❌ Неизвестный стиль. Доступные: {styles_list}")


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

        # Добавляем ВСЕ сообщения в память (для контекста переписки)
        add_to_memory(chat_id, "user", f"{username}: {message.text}", message.date)

        # Проверяем упоминание бота - отвечаем только если упомянули
        if f"@{bot_username}" in message.text.lower():
            # убираем упоминание для чистого запроса к AI
            clean_text = message.text.replace(f"@{bot_username}", "").strip()

            reply = await ask_ai(clean_text, chat_id, reply_context)

            add_to_memory(chat_id, "assistant", f"Бот: {reply}", datetime.now(timezone.utc))

            # если память большая — делаем summary
            if len(get_memory(chat_id)) > MAX_MEMORY:
                await summarize_chat(chat_id)

            return await message.reply(reply)

        # Если бота не упомянули - просто запомнили сообщение, не отвечаем
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
