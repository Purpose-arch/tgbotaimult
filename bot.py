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
        
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS favorites (
                user_id INTEGER,
                model_id TEXT,
                PRIMARY KEY (user_id, model_id)
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

    
    def delete_all_chats(self, user_id: int):
        cursor = self.conn.cursor()
        cursor.execute('''
            DELETE FROM history WHERE chat_id IN (
                SELECT chat_id FROM chats WHERE user_id = ?
            )
        ''', (user_id,))
        cursor.execute('DELETE FROM chats WHERE user_id = ?', (user_id,))
        self.conn.commit()

   
    def get_favorites(self, user_id: int):
        cursor = self.conn.cursor()
        cursor.execute('SELECT model_id FROM favorites WHERE user_id = ?', (user_id,))
        rows = cursor.fetchall()
        return [row[0] for row in rows]

    def add_favorite(self, user_id: int, model_id: str):
        cursor = self.conn.cursor()
        cursor.execute('INSERT OR IGNORE INTO favorites (user_id, model_id) VALUES (?, ?)', (user_id, model_id))
        self.conn.commit()

    def remove_favorite(self, user_id: int, model_id: str):
        cursor = self.conn.cursor()
        cursor.execute('DELETE FROM favorites WHERE user_id = ? AND model_id = ?', (user_id, model_id))
        self.conn.commit()

db = Database()

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

client = AsyncOpenAI(
    base_url=BASE_URL,
    api_key=OPENROUTER_API_KEY,
)


MODELS = {}

async def get_available_models():
    try:
        response = await client.models.list()
        return [model.id for model in response.data if model.id.endswith(":free")]
    except Exception as e:
        logger.error(f"Ошибка при получении списка моделей: {e}")
        return []

async def update_models():
    global MODELS
    available_models = await get_available_models()
    
    MULTIMODAL_INDICATORS = ["gpt-4", "multimodal", "vision"]
    MODELS = {}
    for model in available_models:
        short_name = model.split('/')[-1].replace(':free', '')
        is_multimodal = any(ind in short_name.lower() for ind in MULTIMODAL_INDICATORS)
        MODELS[model] = {"name": short_name, "multimodal": is_multimodal}

async def model_updater():
    while True:
        await update_models()
        await asyncio.sleep(60 * 5)  # Update every 5 minutes

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
    builder.add(types.KeyboardButton(text="📤 Экспорт истории"))
    builder.add(types.KeyboardButton(text="⚙️ Настройки"))
    builder.adjust(2, 2, 1)
    return builder.as_markup(resize_keyboard=True)

def model_selection_keyboard(user_id: int):
    builder = ReplyKeyboardBuilder()
    favorites = db.get_favorites(user_id)
    favorite_models = []
    other_models = {}
    
    for model_key, model_data in MODELS.items():
        if model_key in favorites:
            favorite_models.append((model_key, model_data))
        else:
            folder = model_data["name"].split('-')[0] 
            if folder not in other_models:
                other_models[folder] = []
            other_models[folder].append((model_key, model_data))
    
    favorite_models.sort(key=lambda x: x[1]["name"])
    for folder in other_models:
        other_models[folder].sort(key=lambda x: x[1]["name"])
    
    if favorite_models:
        builder.add(types.KeyboardButton(text="⭐ Избранное"))
        for model_key, model_data in favorite_models:
            display = model_data["name"]
            if model_data["multimodal"]:
                display += " 🖼️"
            builder.add(types.KeyboardButton(text=display))
    
    for folder, models in other_models.items():
        builder.add(types.KeyboardButton(text=f"📁 {folder}"))
        for model_key, model_data in models:
            display = model_data["name"]
            if model_data["multimodal"]:
                display += " 🖼️"
            builder.add(types.KeyboardButton(text=display))
    
    builder.add(types.KeyboardButton(text="↩️ Назад"))
    builder.adjust(1)
    return builder.as_markup(resize_keyboard=True)


def settings_menu_keyboard():
    builder = InlineKeyboardBuilder()
    builder.row(types.InlineKeyboardButton(text="Избранные модели", callback_data="settings_favorites"))
    builder.row(types.InlineKeyboardButton(text="↩️ Назад", callback_data="settings_back"))
    return builder.as_markup()

def favorite_models_keyboard(user_id: int):
    builder = InlineKeyboardBuilder()
    favorites = db.get_favorites(user_id)
    for model_key, model_data in MODELS.items():
        is_fav = model_key in favorites
        text = f"{model_data['name']} {'✅' if is_fav else '❌'}"
        builder.row(types.InlineKeyboardButton(text=text, callback_data=f"toggle_fav_{model_key}"))
    builder.row(types.InlineKeyboardButton(text="↩️ Назад", callback_data="settings_back"))
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
    if not MODELS:
        await message.answer("⚠️ Нет доступных моделей. Попробуйте позже.")
        return
    await state.set_state(ChatStates.choosing_model)
    await message.answer(
        "🤖 Выберите модель для нового чата:",
        reply_markup=model_selection_keyboard(message.from_user.id)
    )

@dp.message(ChatStates.choosing_model)
async def model_selected(message: types.Message, state: FSMContext):
    if message.text == "↩️ Назад":
        await state.clear()
        await message.answer("↩️ Отмена создания нового чата", reply_markup=main_menu_keyboard())
        return

    selected_text = message.text.replace(" 🖼️", "")
    selected_model_key = None
    for model_key, model_data in MODELS.items():
        if model_data["name"] == selected_text:
            selected_model_key = model_key
            break
    if selected_model_key:
        await state.update_data(selected_model=selected_model_key)
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
        model_info = MODELS.get(chat[2])
        if model_info:
            model_display = model_info["name"]
            if model_info["multimodal"]:
                model_display += " 🖼️"
        else:
            model_display = chat[2]
        builder.row(types.InlineKeyboardButton(
            text=f"{chat[1]} ({model_display})",
            callback_data=f"chat_{chat[0]}"
            )
        )
        builder.row(
            types.InlineKeyboardButton(text="✏️ Переименовать", callback_data=f"rename_{chat[0]}"),
            types.InlineKeyboardButton(text="🗑️ Удалить", callback_data=f"delete_{chat[0]}")
        )
    builder.row(types.InlineKeyboardButton(
        text="🗑️ Удалить все чаты",
        callback_data="delete_all_chats"
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
    await state.set_state(ChatStates.waiting_for_message)  # Устанавливаем нужное состояние
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
            model_info = MODELS.get(chat_info[2])
            model_display = model_info["name"] if model_info else chat_info[2]
            if model_info and model_info["multimodal"]:
                model_display += " 🖼️"
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

@dp.message(F.text == "⚙️ Настройки")
async def settings_menu(message: types.Message):
    await message.answer(
        "⚙️ Настройки:",
        reply_markup=settings_menu_keyboard()
    )

@dp.message(F.text)
async def handle_message(message: types.Message, state: FSMContext):
    current_state = await state.get_state()
    if current_state != ChatStates.waiting_for_message:
        return
    data = await state.get_data()
    chat_id = data.get('current_chat')
    
    if not chat_id:
        await message.answer("❌ Сначала выберите или создайте чат!")
        return
    
    content = message.text
    if message.photo:
        content += "\n[Прикреплено фото]"
    if message.document:
        content += "\n[Прикреплен документ]"
        
    db.add_message(chat_id, "user", content)
    
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
            messages=history + [{"role": "user", "content": content}],
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
    if callback.data == "delete_all_chats":
        return
    try:
        chat_id = int(callback.data.split("_")[1])
    except ValueError:
        await callback.answer("Некорректный идентификатор чата")
        return
    db.delete_chat(chat_id)
    await callback.message.edit_text("✅ Чат успешно удален")
    await callback.answer()

@dp.callback_query(F.data == "delete_all_chats")
async def delete_all_chats(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    db.delete_all_chats(user_id)
    await callback.message.edit_text("✅ Все чаты успешно удалены")
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

@dp.callback_query(F.data == "settings_back")
async def settings_back(callback: types.CallbackQuery):
    await callback.message.edit_text("📝 Главное меню:", reply_markup=main_menu_keyboard())
    await callback.answer()

@dp.callback_query(F.data == "settings_favorites")
async def settings_favorites(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    keyboard = favorite_models_keyboard(user_id)
    await callback.message.edit_text("⭐ Выберите избранные модели (нажмите для переключения):", reply_markup=keyboard)
    await callback.answer()

@dp.callback_query(F.data.startswith("toggle_fav_"))
async def toggle_favorite(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    model_key = callback.data[len("toggle_fav_"):]
    favorites = db.get_favorites(user_id)
    if model_key in favorites:
        db.remove_favorite(user_id, model_key)
    else:
        db.add_favorite(user_id, model_key)
    keyboard = favorite_models_keyboard(user_id)
    await callback.message.edit_text("⭐ Выберите избранные модели (нажмите для переключения):", reply_markup=keyboard)
    await callback.answer("Избранное переключено")

@dp.startup()
async def on_startup():
    asyncio.create_task(model_updater())

if __name__ == "__main__":
    asyncio.run(dp.start_polling(bot))
