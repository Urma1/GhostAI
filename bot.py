import httpx
import asyncio
import logging
import os
import sqlite3
import signal

from aiogram import Bot, Dispatcher
from aiogram.filters import Command
from aiogram.types import Message
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
#   РАБОТА С БАЗОЙ
# -------------------------

def init_db():
    # Создаём директорию для БД, если её нет
    db_dir = os.path.dirname(DB_PATH)
    if db_dir and not os.path.exists(db_dir):
        os.makedirs(db_dir, exist_ok=True)

    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS chat_summaries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER,
            summary TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
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


# -------------------------
#   ГЛОБАЛЬНАЯ ПАМЯТЬ В RAM
# -------------------------

def add_to_memory(chat_id, role, text):
    """Добавляет сообщение в краткосрочную память чата"""
    if chat_id not in memory_buffer:
        memory_buffer[chat_id] = []

    memory_buffer[chat_id].append({"role": role, "content": text})

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
        "model": "tngtech/deepseek-r1t2-chimera:free",
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
                "model": "tngtech/deepseek-r1t2-chimera:free",
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

async def ask_ai(user_message: str, chat_id: int):
    url = "https://openrouter.ai/api/v1/chat/completions"

    headers = {
        "Authorization": f"Bearer {OPENROUTER_KEY}",
        "Content-Type": "application/json",
        "Referer": "https://github.com/Urma1/GhostAI",
        "X-Title": "GhostAI Bot"
    }

    history = get_memory(chat_id)
    summaries = load_recent_summaries(chat_id)

    summary_messages = [
        {
            "role": "system",
            "content": f"Краткая сводка прошлых разговоров в этом чате: {s}"
        }
        for s in summaries
    ]

    body = {
        "model": "tngtech/deepseek-r1t2-chimera:free",
        "messages": [
            {
                "role": "system",
                "content": (
                    "Ты дружелюбный участник телеграм-чата. "
                    "Отвечай КОРОТКО: 1–2 предложения максимум. "
                    "Пиши просто, как человек: без формальностей, "
                    "без сложных слов, без больших абзацев. "
                    "Если вопрос неполный — уточни. "
                    "Учитывай контекст последних сообщений и сводки прошлых разговоров."
                )
            },
            *summary_messages,
            *history,
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
        "Привет! Я теперь помню контекст, делаю сводки и отвечаю кратко, как человек."
    )


@dp.message()
async def handler(message: Message):

    chat_id = message.chat.id
    username = message.from_user.first_name or message.from_user.username or "Пользователь"

    # --------------------------
    # ЛИЧНЫЕ СООБЩЕНИЯ
    # --------------------------
    if message.chat.type == ChatType.PRIVATE:

        add_to_memory(chat_id, "user", f"{username}: {message.text}")

        reply = await ask_ai(message.text, chat_id)

        add_to_memory(chat_id, "assistant", f"Бот: {reply}")

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
        add_to_memory(chat_id, "user", f"{username}: {message.text}")

        # Проверяем упоминание бота - отвечаем только если упомянули
        if f"@{bot_username}" in message.text.lower():
            # убираем упоминание для чистого запроса к AI
            clean_text = message.text.replace(f"@{bot_username}", "").strip()

            reply = await ask_ai(clean_text, chat_id)

            add_to_memory(chat_id, "assistant", f"Бот: {reply}")

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


async def main():
    logging.basicConfig(level=logging.INFO)

    # Регистрируем обработчики сигналов
    signal.signal(signal.SIGTERM, signal_handler)  # Railway отправляет SIGTERM при остановке
    signal.signal(signal.SIGINT, signal_handler)   # Ctrl+C локально

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
