import asyncio
import logging
import os
import time
from io import BytesIO
from PIL import Image
from aiohttp import web
from aiogram import Bot, Dispatcher, F, Router, types as aiogram_types
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    InlineKeyboardButton, 
    InlineKeyboardMarkup, 
    PreCheckoutQuery, 
    LabeledPrice
)
from dotenv import load_dotenv
import edge_tts
from google import genai

# Загружаем переменные окружения
load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
PORT = int(os.getenv("PORT", 10000))

# Загружаем дополнительные API-ключи из панели окружения
DEEPL_API_KEY = os.getenv("DEEPL_API_KEY", "")
UPTIMEROBOT_API_KEY = os.getenv("UPTIMEROBOT_API_KEY", "")

# Настройки платного/бесплатного доступа
FREE_ACCESS_IDS = {8186927099, 1153214776, 7785086792, 1359780349}
STAR_PRICE = 10

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)

# Автоматически собираем все ключи Gemini для ротации
GEMINI_KEYS = []
for key_name, val in os.environ.items():
    if key_name.startswith("GEMINI_API_KEY") and val:
        for k in val.split(","):
            clean_k = k.strip()
            if clean_k and clean_k not in GEMINI_KEYS:
                GEMINI_KEYS.append(clean_k)

logging.info(f"🔑 Загружено ключей Gemini для ротации: {len(GEMINI_KEYS)}")

current_key_index = 0

def get_ai_client():
    global current_key_index
    if not GEMINI_KEYS:
        logging.error("❌ Не найдено ни одного ключа Gemini!")
        return None
    key = GEMINI_KEYS[current_key_index]
    try:
        return genai.Client(api_key=key)
    except Exception as e:
        logging.warning(f"⚠️ Не удалось инициализировать GenAI клиента с ключом #{current_key_index + 1}: {e}")
        return None

def rotate_ai_client():
    global current_key_index
    if len(GEMINI_KEYS) > 1:
        current_key_index = (current_key_index + 1) % len(GEMINI_KEYS)
        logging.info(f"🔄 [РОТАЦИЯ] Переключение на ключ Gemini #{current_key_index + 1}")
    return get_ai_client()

if not TELEGRAM_BOT_TOKEN:
    raise ValueError("❌ Не задан TELEGRAM_BOT_TOKEN!")

bot = Bot(token=TELEGRAM_BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

router = Router()
router.message.filter(F.from_user.is_bot == False)

class CreationStates(StatesGroup):
    waiting_for_media_type = State()
    waiting_for_prompt = State()

def get_main_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="🖼 Создать Фото (ИИ)", callback_data="create_photo"),
                InlineKeyboardButton(text="🎬 Создать Видео (ИИ)", callback_data="create_video"),
            ]
        ]
    )

def get_photo_result_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="🔄 Еще вариант", callback_data="regen_photo"),
            ],
            [
                InlineKeyboardButton(text="🏠 Главное меню", callback_data="back_to_menu"),
            ]
        ]
    )

@router.message(Command("start"))
async def cmd_start(message: aiogram_types.Message, state: FSMContext):
    user_id = message.from_user.id
    logging.info(f"👤 Пользователь {user_id} запустил бота (/start)")
    
    is_free = user_id in FREE_ACCESS_IDS
    status_text = "⭐ У вас **БЕСПЛАТНЫЙ ДОСТУП**!" if is_free else f"💳 Стоимость генерации: **{STAR_PRICE} Telegram Stars**."

    welcome_text = (
        f"Привет! 👋 Я твой личный ИИ-помощник для генерации контента и озвучки.\n\n"
        f"{status_text}\n\n"
        "👇 **Выбери в меню ниже, что именно ты хочешь создать:**"
    )
    await state.clear()
    await message.answer(welcome_text, parse_mode="Markdown", reply_markup=get_main_menu_keyboard())
    await state.set_state(CreationStates.waiting_for_media_type)

@router.callback_query(F.data == "back_to_menu")
async def process_back_to_menu(callback: aiogram_types.CallbackQuery, state: FSMContext):
    await callback.answer()
    await state.clear()
    await callback.message.edit_text(
        "👇 **Выбери в меню ниже, что именно ты хочешь создать:**",
        reply_markup=get_main_menu_keyboard()
    )
    await state.set_state(CreationStates.waiting_for_media_type)

@router.callback_query(F.data.in_(["create_photo", "create_video"]))
async def process_media_choice(callback: aiogram_types.CallbackQuery, state: FSMContext):
    await callback.answer()
    user_id = callback.from_user.id
    choice = callback.data
    
    media_type = "photo" if choice == "create_photo" else "video"
    await state.update_data(media_type=media_type)
    
    title = "фото" if media_type == "photo" else "видео"
    logging.info(f"🔘 Пользователь {user_id} выбрал режим создания: {media_type}")

    await callback.message.edit_text(
        f"Отлично! Вы выбрали создание **{title}** 🚀\n\n"
        "📝 Теперь отправьте мне **текстовое описание (промпт)**, что именно нужно сгенерировать!"
    )
    await state.set_state(CreationStates.waiting_for_prompt)

@router.message(CreationStates.waiting_for_prompt)
async def process_user_prompt(message: aiogram_types.Message, state: FSMContext):
    user_id = message.from_user.id
    text_prompt = message.caption or message.text

    if not text_prompt or text_prompt.startswith("/"):
        await message.answer("⚠️ Пожалуйста, отправьте текстовое описание (промпт) для генерации.")
        return

    await state.update_data(text_prompt=text_prompt)
    data = await state.get_data()
    media_type = data.get("media_type", "video")

    if user_id not in FREE_ACCESS_IDS:
        try:
            prices = [LabeledPrice(label="Генерация контента (AI Bot)", amount=STAR_PRICE)]
            await bot.send_invoice(
                chat_id=message.chat.id,
                title="Оплата генерации контента",
                description=f"Создание {media_type} по вашему промпту",
                payload=f"gen_{media_type}_{user_id}_{int(time.time())}",
                currency="XTR",
                prices=prices
            )
            await message.answer(f"⭐️ Стоимость генерации: {STAR_PRICE} звезд.\nПожалуйста, подтвердите оплату выше 👆")
            return
        except Exception as e:
            logging.error(f"Ошибка создания счета: {e}")
            await message.answer(f"❌ Ошибка создания счета на оплату: {e}")
            return

    await execute_generation(message, state
