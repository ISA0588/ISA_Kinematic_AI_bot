import asyncio
import logging
import os
import time
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

# Настройки платного/бесплатного доступа
FREE_ACCESS_IDS = {8186927099, 1153214776, 7785086792, 1359780349}
STAR_PRICE = 10

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)

# Собираем ключи Gemini для ротации
GEMINI_KEYS = []
for key_name in ["GEMINI_API_KEY", "GEMINI_API_KEY2", "GEMINI_API_KEY3", "GEMINI_API_KEYS"]:
    val = os.getenv(key_name, "")
    if val:
        for k in val.split(","):
            clean_k = k.strip()
            if clean_k and clean_k not in GEMINI_KEYS:
                GEMINI_KEYS.append(clean_k)

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

    await execute_generation(message, state, media_type, text_prompt)

@router.pre_checkout_query()
async def pre_checkout_query_handler(pre_checkout_query: PreCheckoutQuery):
    await bot.answer_pre_checkout_query(pre_checkout_query.id, ok=True)

@router.message(F.successful_payment)
async def successful_payment_handler(message: aiogram_types.Message, state: FSMContext):
    data = await state.get_data()
    text_prompt = data.get("text_prompt")
    media_type = data.get("media_type", "video")

    if not text_prompt:
        await message.answer("✅ Оплата прошла успешно! Но данные сессии устарели. Пожалуйста, повторите запрос через /start.")
        return

    await message.answer("✅ Оплата успешно получена! ⭐️ Запускаю генерацию...")
    await execute_generation(message, state, media_type, text_prompt)

async def execute_generation(message: aiogram_types.Message, state: FSMContext, media_type: str, text_prompt: str):
    user_id = message.from_user.id
    logging.info(f"📥 [{user_id}] Запуск процесса для {media_type}: «{text_prompt}»")
    
    if media_type == "video":
        status_msg = await message.answer("🎬 **Шаг 1/2:** Создаю озвучку и запускаю генерацию видео через Veo...")
        
        audio_path = f"output_voice_{user_id}.mp3"
        try:
            communicate = edge_tts.Communicate(text_prompt, "ru-RU-SvetlanaNeural")
            await communicate.save(audio_path)
        except Exception as e:
            logging.warning(f"⚠️ Не удалось создать аудио для видео: {e}")

        video_filename = f"generated_video_{user_id}.mp4"

        def generate_veo_video():
            client = get_ai_client()
            if not client:
                raise ValueError("API-ключи Gemini недоступны.")

            for attempt in range(len(GEMINI_KEYS) or 1):
                try:
                    logging.info(f"🎬 [{user_id}] Запрос к Veo (попытка {attempt + 1})...")
                    # Актуальный вызов через source согласно рекомендациям SDK
                    operation = client.models.generate_videos(
                        model="veo-3.1-generate-preview",
                        source={"prompt": text_prompt},
                    )
                    while not operation.done:
                        time.sleep(10)
                        operation = client.operations.get(operation)
                        logging.info(f"⏳ [{user_id}] Видео генерируется...")

                    generated_video = operation.response.generated_videos[0]
                    client.files.download(file=generated_video.video, destination=video_filename)
                    return True
                except Exception as e:
                    err_str = str(e)
                    if "429" in err_str or "RESOURCE_
