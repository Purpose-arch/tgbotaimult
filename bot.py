import os
import logging
import sqlite3
import asyncio
import time
from datetime import datetime
from dotenv import load_dotenv
from aiogram import Bot, Dispatcher, types, F
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State
from aiogram.utils.keyboard import ReplyKeyboardBuilder, InlineKeyboardBuilder
from openai import AsyncOpenAI

load_dotenv()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Конфигурация
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
            CREATE TABLE IF NOT EXISTS chats (
                chat_id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                model TEXT,
                created_at DATETIME,
                title TEXT
            )
        ''')
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS history (
                message_id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER,
                role TEXT,
                content TEXT,
                timestamp DATETIME
            )
        ''')
        self.conn.commit()

    def create_chat(self, user_id: int, model: str, title: str):
        cursor = self.conn.cursor()
        cursor.execute('''
            INSERT INTO chats (user_id, model, created_at, title)
            VALUES (?, ?, ?, ?)
        ''', (user_id, model, datetime.now(), title))
        self.conn.commit()
        return cursor.lastrowid

    def get_chats(self, user_id: int):
        cursor = self.conn.cursor()
        cursor.execute('''
            SELECT chat_id, title, model FROM chats 
            WHERE user_id = ? 
            ORDER BY created_at DESC
        ''', (user_id,))
        return cursor.fetchall()

    def delete_chat(self, chat_id: int):
        cursor = self.conn.cursor()
        cursor.execute('DELETE FROM chats WHERE chat_id = ?', (chat_id,))
        cursor.execute('DELETE FROM history WHERE chat_id = ?', (chat_id,))
        self.conn.commit()

    def rename_chat(self, chat_id: int, new_title: str):
        cursor = self.conn.cursor()
        cursor.execute('''
            UPDATE chats SET title = ? 
            WHERE chat_id = ?
        ''', (new_title, chat_id))
        self.conn.commit()

    def add_message(self, chat_id: int, role: str, content: str):
        cursor = self.conn.cursor()
        cursor.execute('''
            INSERT INTO history (chat_id, role, content, timestamp)
            VALUES (?, ?, ?, ?)
        ''', (chat_id, role, content, datetime.now()))
        self.conn.commit()

    def get_history(self, chat_id: int, limit: int = HISTORY_LIMIT):
        cursor = self.conn.cursor()
        cursor.execute('''
            SELECT role, content FROM history 
            WHERE chat_id = ? 
            ORDER BY timestamp DESC 
            LIMIT ?
        ''', (chat_id, limit))
        result = cursor.fetchall()
        return [{"role": role, "content": content} for role, content in reversed(result)]

    def clear_history(self, chat_id: int):
        cursor = self.conn.cursor()
        cursor.execute('DELETE FROM history WHERE chat_id = ?', (chat_id,))
        self.conn.commit()

db = Database()

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

client = AsyncOpenAI(
    base_url=BASE_URL,
    api_key=OPENROUTER_API_KEY,
)

MODELS = {
    "qwen/qwen2.5-vl-72b-instruct:free": "Qwen 2.5",
    "deepseek/deepseek-r1:free": "Deepseek R1",
    "google/gemini-exp-1206:free": "Gemini Exp. 1206"
}

class ChatStates(StatesGroup):
    choosing_model = State()
    naming_chat = State()
    renaming_chat = State()
    waiting_for_message = State()

def main_menu_keyboard():
    builder = ReplyKeyboardBuilder()
    builder.add(types.KeyboardButton(text="➕ Новый чат"))
    builder.add(types.KeyboardButton(text="📂 Мои чаты"))
    builder.adjust(2)
    return builder.as_markup(resize_keyboard=True)

def model_selection_keyboard():
    builder = ReplyKeyboardBuilder()
    for model in MODELS.values():
        builder.add(types.KeyboardButton(text=model))
    builder.add(types.KeyboardButton(text="↩️ Назад"))
    builder.adjust(1)
    return builder.as_markup(resize_keyboard=True)

def chat_actions_keyboard(chat_id: int):
    builder = InlineKeyboardBuilder()
    builder.add(types.InlineKeyboardButton(
        text="✏️ Переименовать",
        callback_data=f"rename_{chat_id}"
    ))
    builder.add(types.InlineKeyboardButton(
        text="🗑️ Удалить",
        callback_data=f"delete_{chat_id}"
    ))
    return builder.as_markup()

@dp.message(F.text == "/start")
async def cmd_start(message: types.Message, state: FSMContext):
    await message.answer("🤖 Добро пожаловать в нейро-чат!")
    await message.answer(
        "👇 Выберите действие:",
        reply_markup=main_menu_keyboard()
    )

@dp.message(F.text == "/menu")
async def cmd_menu(message: types.Message, state: FSMContext):
    await message.answer(
        "📝 Главное меню:",
        reply_markup=main_menu_keyboard()
    )

@dp.message(F.text == "➕ Новый чат")
async def create_new_chat(message: types.Message, state: FSMContext):
    await state.set_state(ChatStates.choosing_model)
    await message.answer(
        "🤖 Выберите модель для нового чата:",
        reply_markup=model_selection_keyboard()
    )

@dp.message(F.text.in_(MODELS.values()), ChatStates.choosing_model)
async def model_selected(message: types.Message, state: FSMContext):
    model_key = next(k for k, v in MODELS.items() if v == message.text)
    await state.update_data(selected_model=model_key)
    await state.set_state(ChatStates.naming_chat)
    await message.answer(
        "📝 Введите название для нового чата:",
        reply_markup=types.ReplyKeyboardRemove()
    )

@dp.message(ChatStates.naming_chat)
async def chat_named(message: types.Message, state: FSMContext):
    data = await state.get_data()
    model = data['selected_model']
    title = message.text[:30]  # Ограничение длины названия
    
    chat_id = db.create_chat(message.from_user.id, model, title)
    await state.update_data(current_chat=chat_id)
    await state.set_state(ChatStates.waiting_for_message)
    await message.answer(
        f"✅ Чат '{title}' создан!\nТеперь вы можете начать общение!",
        reply_markup=main_menu_keyboard()
    )

@dp.message(F.text == "📂 Мои чаты")
async def show_chats(message: types.Message):
    user_id = message.from_user.id
    chats = db.get_chats(user_id)
    
    if not chats:
        await message.answer("📭 У вас пока нет сохраненных чатов")
        return
    
    builder = InlineKeyboardBuilder()
    for chat in chats:
        builder.row(types.InlineKeyboardButton(
            text=f"{chat[1]} ({chat[2]})",
            callback_data=f"chat_{chat[0]}"
        ))
        builder.row(types.InlineKeyboardButton(
            text="✏️ Переименовать",
            callback_data=f"rename_{chat[0]}"
        ), types.InlineKeyboardButton(
            text="🗑️ Удалить",
            callback_data=f"delete_{chat[0]}"
        ))
    
    await message.answer(
        "📂 Ваши чаты:",
        reply_markup=builder.as_markup()
    )

@dp.callback_query(F.data.startswith("chat_"))
async def select_chat(callback: types.CallbackQuery, state: FSMContext):
    chat_id = int(callback.data.split("_")[1])
    await state.update_data(current_chat=chat_id)
    await callback.message.answer(
        "✅ Переключено на выбранный чат",
        reply_markup=main_menu_keyboard()
    )
    await callback.answer()

@dp.callback_query(F.data.startswith("delete_"))
async def delete_chat(callback: types.CallbackQuery):
    chat_id = int(callback.data.split("_")[1])
    db.delete_chat(chat_id)
    await callback.message.edit_text(f"✅ Чат успешно удален")
    await callback.answer()

@dp.callback_query(F.data.startswith("rename_"))
async def rename_chat_start(callback: types.CallbackQuery, state: FSMContext):
    chat_id = int(callback.data.split("_")[1])
    await state.set_state(ChatStates.renaming_chat)
    await state.update_data(renaming_chat=chat_id)
    await callback.message.answer(
        "📝 Введите новое название для чата:",
        reply_markup=types.ReplyKeyboardRemove()
    )
    await callback.answer()

@dp.message(ChatStates.renaming_chat)
async def rename_chat_finish(message: types.Message, state: FSMContext):
    data = await state.get_data()
    chat_id = data['renaming_chat']
    new_title = message.text[:30]
    
    db.rename_chat(chat_id, new_title)
    await state.clear()
    await message.answer(
        f"✅ Название чата изменено на '{new_title}'",
        reply_markup=main_menu_keyboard()
    )

@dp.message(F.text == "/clear")
async def clear_history(message: types.Message, state: FSMContext):
    data = await state.get_data()
    chat_id = data.get('current_chat')
    if chat_id:
        db.clear_history(chat_id)
        await message.answer("✅ История текущего чата очищена")
    else:
        await message.answer("❌ Нет активного чата")

@dp.message(F.text, ChatStates.waiting_for_message)
async def handle_message(message: types.Message, state: FSMContext):
    data = await state.get_data()
    chat_id = data.get('current_chat')
    
    if not chat_id:
        await message.answer("❌ Сначала выберите или создайте чат!")
        return
    
    db.add_message(chat_id, "user", message.text)
    
    try:
        history = db.get_history(chat_id)
        model = next(c[2] for c in db.get_chats(message.from_user.id) if c[0] == chat_id)
        
        sent_message = await message.answer("●")
        full_answer = ""
        last_edit_time = time.monotonic()
        edit_interval = 1.5
        
        stream = await client.chat.completions.create(
            model=model,
            messages=history + [{"role": "user", "content": message.text}],
            stream=True,
            extra_headers={
                "HTTP-Referer": "https://github.com/Purpose-arch/tgbotaimult",
                "X-Title": "tgbotaimult"
            }
        )
        
        async for chunk in stream:
            if chunk.choices[0].delta.content:
                delta_content = chunk.choices[0].delta.content
                full_answer += delta_content
                now = time.monotonic()
                if now - last_edit_time >= edit_interval or len(delta_content) < 3:
                    try:
                        await sent_message.edit_text(full_answer + "●")
                        last_edit_time = now
                    except Exception as e:
                        logger.error(f"Ошибка при обновлении сообщения: {e}")
        
        await sent_message.edit_text(full_answer)
        db.add_message(chat_id, "assistant", full_answer)
        
    except Exception as e:
        logger.error(f"Ошибка: {str(e)}")
        await message.answer("⚠️ Произошла ошибка при обработке запроса")

if __name__ == "__main__":
    asyncio.run(dp.start_polling(bot))