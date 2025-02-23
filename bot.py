import os
import logging
import sqlite3
from datetime import datetime
from dotenv import load_dotenv
from aiogram import Bot, Dispatcher, types, F
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State
from aiogram.utils.keyboard import ReplyKeyboardBuilder
from openai import AsyncOpenAI

load_dotenv()


logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
BASE_URL = "https://openrouter.ai/api/v1"
HISTORY_LIMIT = 10


if not OPENROUTER_API_KEY:
    raise ValueError("OPENROUTER_API_KEY не найден в .env")
if not BOT_TOKEN:
    raise ValueError("TELEGRAM_BOT_TOKEN не найден в .env")


class Database:
    def __init__(self):
        self.conn = sqlite3.connect('chat_history.db')
        self._create_tables()

    def _create_tables(self):
        cursor = self.conn.cursor()
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS history (
                user_id INTEGER,
                timestamp DATETIME,
                role TEXT,
                content TEXT
            )
        ''')
        self.conn.commit()

    def add_message(self, user_id: int, role: str, content: str):
        cursor = self.conn.cursor()
        cursor.execute('''
            INSERT INTO history (user_id, timestamp, role, content)
            VALUES (?, ?, ?, ?)
        ''', (user_id, datetime.now(), role, content))
        self.conn.commit()

    def get_history(self, user_id: int, limit: int = HISTORY_LIMIT):
        cursor = self.conn.cursor()
        cursor.execute('''
            SELECT role, content FROM history 
            WHERE user_id = ? 
            ORDER BY timestamp DESC 
            LIMIT ?
        ''', (user_id, limit))
        result = cursor.fetchall()
        return [{"role": role, "content": content} for role, content in reversed(result)]

    def clear_history(self, user_id: int):
        cursor = self.conn.cursor()
        cursor.execute('''
            DELETE FROM history WHERE user_id = ?
        ''', (user_id,))
        self.conn.commit()

db = Database()


bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

client = AsyncOpenAI(
    base_url=BASE_URL,
    api_key=OPENROUTER_API_KEY,
)

MODELS = {
    "qwen/qwen2.5-vl-72b-instruct:free": "Qwen VL 72B",
    "cognitivecomputations/dolphin3.0-r1-mistral-24b:free": "Dolphin 3.0 Mistral 24B",
    "google/gemini-exp-1206:free": "Gemini Experimental"
}

class ChatStates(StatesGroup):
    choosing_model = State()
    waiting_for_message = State()

def model_selection_keyboard():
    builder = ReplyKeyboardBuilder()
    for model in MODELS.values():
        builder.add(types.KeyboardButton(text=model))
    builder.adjust(1)
    return builder.as_markup(resize_keyboard=True)

@dp.message(F.text == "/start")
async def cmd_start(message: types.Message, state: FSMContext):
    await state.set_state(ChatStates.choosing_model)
    await message.answer(
        "🤖 Привет! Выберите модель:",
        reply_markup=model_selection_keyboard()
    )

@dp.message(F.text.in_(MODELS.values()), ChatStates.choosing_model)
async def model_selected(message: types.Message, state: FSMContext):
    user_id = message.from_user.id
    model_key = [k for k, v in MODELS.items() if v == message.text][0]
    
    # Загрузка истории из БД
    history = db.get_history(user_id)
    
    await state.update_data(model=model_key)
    await state.set_state(ChatStates.waiting_for_message)
    await message.answer(
        f"✅ Выбрана модель: {message.text}\nТеперь вы можете начать общение!",
        reply_markup=types.ReplyKeyboardRemove()
    )

@dp.message(F.text == "/clear")
async def clear_history(message: types.Message, state: FSMContext):
    user_id = message.from_user.id
    db.clear_history(user_id)
    await state.set_state(ChatStates.choosing_model)
    await message.answer(
        "История очищена. Выберите модель:",
        reply_markup=model_selection_keyboard()
    )

@dp.message(F.text, ChatStates.waiting_for_message)
async def handle_message(message: types.Message, state: FSMContext):
    user_id = message.from_user.id
    data = await state.get_data()
    model = data.get('model')
    
    if not model:
        await message.answer("Сначала выберите модель!")
        return
    
    
    db.add_message(user_id, "user", message.text)
    
    try:
        
        history = db.get_history(user_id)
        
        response = await client.chat.completions.create(
            model=model,
            messages=history + [{"role": "user", "content": message.text}],
            extra_headers={
                "HTTP-Referer": "https://github.com/Purpose-arch/tgbotaimult",
                "X-Title": "tgbotaimult"
            }
        )
        
        answer = response.choices[0].message.content
        
        
        db.add_message(user_id, "assistant", answer)
        
        
        cursor = db.conn.cursor()
        cursor.execute('''
            DELETE FROM history 
            WHERE rowid NOT IN (
                SELECT rowid FROM history 
                WHERE user_id = ? 
                ORDER BY timestamp DESC 
                LIMIT ?
            )
        ''', (user_id, HISTORY_LIMIT))
        db.conn.commit()
        
        await message.answer(answer)
        
    except Exception as e:
        logger.error(f"Ошибка: {str(e)}")
        await message.answer("⚠️ Произошла ошибка при обработке запроса")

if __name__ == "__main__":
    import asyncio
    asyncio.run(dp.start_polling(bot))