import httpx
import asyncio
import logging
import os
import sqlite3

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

DB_PATH = "memory.db"


# -------------------------
#   РАБОТА С БАЗОЙ
# -------------------------

def init_db():
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
        "model": "x-ai/grok-4.1-fast:free",
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
#       AI: ОТВЕТ БОТА
# -------------------------

async def ask_ai(user_message: str, chat_id: int):
    url = "https://openrouter.ai/api/v1/chat/completions"

    headers = {
        "Authorization": f"Bearer {OPENROUTER_KEY}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://github.com/Urma1/GhostAI",
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
        "model": "x-ai/grok-4.1-fast:free",
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

        # если бота не упоминали — игнор
        if f"@{bot_username}" not in message.text.lower():
            return

        # убираем упоминание
        clean_text = message.text.replace(f"@{bot_username}", "").strip()

        # добавляем в память с автором
        add_to_memory(chat_id, "user", f"{username}: {clean_text}")

        reply = await ask_ai(clean_text, chat_id)

        add_to_memory(chat_id, "assistant", f"Бот: {reply}")

        # если память большая — делаем summary
        if len(get_memory(chat_id)) > MAX_MEMORY:
            await summarize_chat(chat_id)

        return await message.reply(reply)


# -------------------------
#       СТАРТ ПОЛЛИНГА
# -------------------------

async def main():
    logging.basicConfig(level=logging.INFO)
    await dp.start_polling(bot)


if __name__ == "__main__":
    init_db()
    asyncio.run(main())
