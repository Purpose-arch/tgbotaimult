import os
import logging
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


if not OPENROUTER_API_KEY:
    raise ValueError("OPENROUTER_API_KEY не найден в .env")
if not BOT_TOKEN:
    raise ValueError("TELEGRAM_BOT_TOKEN не найден в .env")


bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()


client = AsyncOpenAI(
    base_url=BASE_URL,
    api_key=OPENROUTER_API_KEY,
)


MODELS = {
    "qwen/qwen2.5-vl-72b-instruct:free": "Qwen VL 72B (Free)",
    "cognitivecomputations/dolphin3.0-r1-mistral-24b:free": "Dolphin 3.0 Mistral 24B (Free)",
    "google/gemini-exp-1206:free": "Gemini Experimental (Free)"
}


user_sessions = {}

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
        "🤖 Добро пожаловать в нейро-чат! Выберите модель:",
        reply_markup=model_selection_keyboard()
    )

@dp.message(F.text.in_(MODELS.values()), ChatStates.choosing_model)
async def model_selected(message: types.Message, state: FSMContext):
    user_id = message.from_user.id
    model_key = [k for k, v in MODELS.items() if v == message.text][0]
    
    user_sessions[user_id] = {
        'model': model_key,
        'history': []
    }
    
    await state.set_state(ChatStates.waiting_for_message)
    await message.answer(
        f"✅ Выбрана модель: {message.text}\nТеперь вы можете начать общение!",
        reply_markup=types.ReplyKeyboardRemove()
    )

@dp.message(F.text == "/clear")
async def clear_history(message: types.Message, state: FSMContext):
    user_id = message.from_user.id
    if user_id in user_sessions:
        del user_sessions[user_id]
    await state.set_state(ChatStates.choosing_model)
    await message.answer(
        "История очищена. Выберите модель:",
        reply_markup=model_selection_keyboard()
    )

@dp.message(F.text, ChatStates.waiting_for_message)
async def handle_message(message: types.Message):
    user_id = message.from_user.id
    if user_id not in user_sessions:
        await message.answer("Сначала выберите модель!")
        return
    
    session = user_sessions[user_id]
    model = session['model']
    history = session['history']
    
    history.append({"role": "user", "content": message.text})
    
    try:
        response = await client.chat.completions.create(
            model=model,
            messages=history,
            extra_headers={
                "HTTP-Referer": "https://github.com/Purpose-arch/Telegram_Neurobot",
                "X-Title": "Telegram_Neurobot"
            }
        )
        
        answer = response.choices[0].message.content
        history.append({"role": "assistant", "content": answer})
        
        if len(history) > 15:
            session['history'] = history[-10:]
            
        await message.answer(answer)
        
    except Exception as e:
        logger.error(f"Ошибка: {str(e)}")
        await message.answer("⚠️ Произошла ошибка при обработке запроса")

if __name__ == "__main__":
    import asyncio
    asyncio.run(dp.start_polling(bot))