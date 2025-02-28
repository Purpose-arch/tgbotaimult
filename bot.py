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
from openai import AsyncOpenAI, APIConnectionError, RateLimitError, APIError

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

# Инициализация базы данных
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

# Инициализация словаря моделей как пустого (будет заполнен динамически)
MODELS = {}

# Получение списка доступных моделей
async def get_available_models():
    try:
        response = await client.models.list()
        # Фильтруем только бесплатные модели
        return [model.id for model in response.data if model.id.endswith(":free")]
    except Exception as e:
        logger.error(f"Ошибка при получении списка моделей: {e}")
        return []

# Обновление словаря MODELS
async def update_models():
    global MODELS
    available_models = await get_available_models()
    MODELS = {model: model.split('/')[-1].replace(':free', '') for model in available_models}

# Запуск обновления моделей при старте
asyncio.run(update_models())

class ChatStates(StatesGroup):
    choosing_model = State()
    naming_chat = State()
    renaming_chat = State()
    waiting_for_message = State()

def main_menu_keyboard():
    builder = ReplyKeyboardBuilder()
    builder.add(types.KeyboardButton(text="➕ Новый чат"))
    builder.add(types.KeyboardButton(text="📂 Мои чаты"))
    builder.add(types.KeyboardButton(text="📊 Текущий чат"))
    builder.add(types.KeyboardButton(text="🧹 Очистить историю"))
    builder.add(types.KeyboardButton(text="📤 Экспорт истории"))
    builder.adjust(2, 2)
    return builder.as_markup(resize_keyboard=True)

def model_selection_keyboard():
    builder = ReplyKeyboardBuilder()
    for model in MODELS.values():
        builder.add(types.KeyboardButton(text=model))
    builder.add(types.KeyboardButton(text="↩️ Назад"))
    builder.adjust(1)
    return builder.as_markup(resize_keyboard=True)

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
    if not MODELS:
        await message.answer("⚠️ Нет доступных моделей. Попробуйте позже.")
        return
    await state.set_state(ChatStates.choosing_model)
    await message.answer(
        "🤖 Выберите модель для нового чата:",
        reply_markup=model_selection_keyboard()
    )

@dp.message(F.text.in_(MODELS.values()), ChatStates.choosing_model)
async def model_selected(message: types.Message, state: FSMContext):
    selected_model_name = message.text
    model_key = next((k for k, v in MODELS.items() if v == selected_model_name), None)
    if model_key:
        await state.update_data(selected_model=model_key)
        await state.set_state(ChatStates.naming_chat)
        await message.answer(
            "📝 Введите название для нового чата:",
            reply_markup=types.ReplyKeyboardRemove()
        )
    else:
        await message.answer("❌ Выбранная модель недоступна. Пожалуйста, выберите другую.")

@dp.message(ChatStates.naming_chat)
async def chat_named(message: types.Message, state: FSMContext):
    data = await state.get_data()
    model_key = data['selected_model']
    title = message.text[:30]
    
    chat_id = db.create_chat(message.from_user.id, model_key, title)
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
        model_display = MODELS.get(chat[2], chat[2])  # Если модель неизвестна, показываем ключ
        builder.row(types.InlineKeyboardButton(
            text=f"{chat[1]} ({model_display})",
            callback_data=f"chat_{chat[0]}"
        ))
        builder.row(types.InlineKeyboardButton(
            text="✏️ Переименовать",
            callback_data=f"rename_{chat[0]}"
        ), types.InlineKeyboardButton(
            text="🗑️ Удалить",
            callback_data=f"delete_{chat[0]}"
        ))
    builder.row(types.InlineKeyboardButton(
        text="♻️ Обновить",
        callback_data="refresh_chats"
    ))
    
    await message.answer(
        "📂 Ваши чаты:",
        reply_markup=builder.as_markup()
    )

@dp.callback_query(F.data == "refresh_chats")
async def refresh_chats(callback: types.CallbackQuery):
    await show_chats(callback.message)
    await callback.answer("♻️ Список чатов обновлен")

@dp.callback_query(F.data.startswith("chat_"))
async def select_chat(callback: types.CallbackQuery, state: FSMContext):
    chat_id = int(callback.data.split("_")[1])
    await state.update_data(current_chat=chat_id)
    await callback.message.answer(
        "✅ Переключено на выбранный чат",
        reply_markup=main_menu_keyboard()
    )
    await callback.answer()

@dp.message(F.text == "📊 Текущий чат")
async def show_current_chat(message: types.Message, state: FSMContext):
    data = await state.get_data()
    chat_id = data.get('current_chat')
    if chat_id:
        chats = db.get_chats(message.from_user.id)
        chat_info = next((c for c in chats if c[0] == chat_id), None)
        if chat_info:
            model_display = MODELS.get(chat_info[2], chat_info[2])
            await message.answer(f"🔮 Активный чат: {chat_info[1]}\nМодель: {model_display}")
            return
    await message.answer("❌ Нет активного чата")

@dp.message(F.text == "📤 Экспорт истории")
async def export_history(message: types.Message, state: FSMContext):
    data = await state.get_data()
    chat_id = data.get('current_chat')
    
    if not chat_id:
        await message.answer("❌ Нет активного чата")
        return
    
    history = db.get_history(chat_id, limit=100)
    formatted = "\n\n".join([f"{msg['role']}: {msg['content']}" for msg in history])
    
    await message.answer_document(
        types.BufferedInputFile(
            formatted.encode('utf-8'), 
            filename=f"chat_history_{chat_id}.txt"
        ),
        caption="📝 История вашего чата"
    )

async def check_model_availability(model_key: str) -> bool:
    try:
        await client.models.retrieve(model_key)
        return True
    except Exception:
        return False

@dp.message(F.text, ChatStates.waiting_for_message)
async def handle_message(message: types.Message, state: FSMContext):
    data = await state.get_data()
    chat_id = data.get('current_chat')
    
    if not chat_id:
        await message.answer("❌ Сначала выберите или создайте чат!")
        return
    
    db.add_message(chat_id, "user", message.text)
    
    try:
        chats = db.get_chats(message.from_user.id)
        chat_info = next((c for c in chats if c[0] == chat_id), None)
        if not chat_info:
            await message.answer("❌ Чат не найден")
            return
            
        model_key = chat_info[2]

        history = db.get_history(chat_id)
        
        sent_message = await message.answer("●")
        full_answer = ""
        last_edit_time = time.monotonic()
        edit_interval = 1.5
        
        stream = await client.chat.completions.create(
            model=model_key,
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
        
    except APIConnectionError as e:
        logger.error(f"Ошибка подключения: {str(e)}")
        await message.answer("🔌 Проблемы с подключением к API")
    except RateLimitError as e:
        logger.error(f"Лимит запросов: {str(e)}")
        await message.answer("⏳ Превышен лимит запросов, попробуйте позже")
    except APIError as e:
        logger.error(f"API ошибка: {str(e)}")
        await message.answer("⚠️ Ошибка API, попробуйте еще раз")
    except Exception as e:
        logger.error(f"Ошибка: {str(e)}")
        await message.answer("⚠️ Произошла ошибка при обработке запроса")

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

@dp.message(F.text == "🧹 Очистить историю")
async def clear_history(message: types.Message, state: FSMContext):
    data = await state.get_data()
    chat_id = data.get('current_chat')
    if chat_id:
        db.clear_history(chat_id)
        await message.answer("✅ История текущего чата очищена")
    else:
        await message.answer("❌ Нет активного чата")

if __name__ == "__main__":
    asyncio.run(dp.start_polling(bot))