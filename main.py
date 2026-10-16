import asyncio
import logging
import os
import time
from aiohttp import web, ClientSession
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
UPTIMEROBOT_API_KEY = os.getenv("UPTIMEROBOT_API_KEY", "")

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
            ],
            [
                InlineKeyboardButton(text="📊 Статус сервера", callback_data="check_status")
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

# Обработчик проверки статуса через UptimeRobot API
@router.callback_query(F.data == "check_status")
async def process_check_status(callback: aiogram_types.CallbackQuery):
    await callback.answer("Запрашиваю статус серверов...")
    if not UPTIMEROBOT_API_KEY:
        await callback.message.answer("⚠️ API-ключ UptimeRobot не настроен в переменных окружения.")
        return

    url = "https://api.uptimerobot.com/v2/getMonitors"
    payload = {
        "api_key": UPTIMEROBOT_API_KEY,
        "format": "json"
    }
    
    try:
        async with ClientSession() as session:
            async with session.post(url, data=payload) as response:
                if response.status == 200:
                    data = await response.json()
                    monitors = data.get("monitors", [])
                    if not monitors:
                        await callback.message.answer("📭 Активных мониторов в UptimeRobot не найдено.")
                        return
                    
                    status_report = "📊 **Статус систем (UptimeRobot):**\n\n"
                    for m in monitors:
                        name = m.get("friendly_name")
                        status_code = m.get("status")
                        status_str = "🟢 Работает (Up)" if status_code == 2.0 or status_code == 2 else "🔴 Спит / Ошибка (Down)"
                        status_report += f"• **{name}**: {status_str}\n"
                    
                    await callback.message.answer(status_report, parse_mode="Markdown")
                else:
                    await callback.message.answer("❌ Не удалось получить данные от UptimeRobot API.")
    except Exception as e:
        logging.error(f"Ошибка запроса к UptimeRobot API: {e}")
        await callback.message.answer("⚠️ Произошла ошибка при обращении к сервису мониторинга.")

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
        
        # Генерируем аудио во внутренних целях (без отправки отдельным файлом в чат)
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
                    operation = client.models.generate_videos(
                        model="veo-3.1-generate-preview",
                        prompt=text_prompt,
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
                    if "429" in err_str or "RESOURCE_EXHAUSTED" in err_str:
                        logging.warning(f"⚠️ [{user_id}] Лимит ключа (429), ротация...")
                        client = rotate_ai_client()
                        if not client:
                            break
                    else:
                        raise e
            return False

        try:
            success = await asyncio.to_thread(generate_veo_video)
            if success and os.path.exists(video_filename):
                await message.answer_video(
                    video=aiogram_types.FSInputFile(video_filename),
                    caption=f"✨ Видео успешно создано по промпту:\n«{text_prompt}»",
                    reply_markup=get_main_menu_keyboard()
                )
                os.remove(video_filename)
            else:
                await message.answer("❌ Не удалось сгенерировать видео из-за исчерпания лимитов API.", reply_markup=get_main_menu_keyboard())
        except Exception as e:
            logging.error(f"❌ [{user_id}] Ошибка Veo: {e}")
            await message.answer(f"⚠️ **Ошибка при генерации видео:**\n`{e}`", parse_mode="Markdown", reply_markup=get_main_menu_keyboard())
        finally:
            if os.path.exists(audio_path):
                os.remove(audio_path)

    else:
        # Режим генерации фото (аудио не создается и не отправляется)
        status_msg = await message.answer("🖼 Генерирую изображение по вашему промпту...")
        photo_filename = f"generated_photo_{user_id}.jpg"

        def generate_gemini_image():
            client = get_ai_client()
            if not client:
                raise ValueError("API-ключи Gemini недоступны.")

            for attempt in range(len(GEMINI_KEYS) or 1):
                try:
                    result = client.models.generate_images(
                        model="imagen-3.0-generate-002",
                        prompt=text_prompt,
                        config=dict(
                            number_of_images=1,
                            output_mime_type="image/jpeg",
                            aspect_ratio="1:1",
                        ),
                    )
                    for generated_image in result.generated_images:
                        image_bytes = generated_image.image.image_bytes
                        with open(photo_filename, "wb") as f:
                            f.write(image_bytes)
                        return True
                except Exception as e:
                    err_str = str(e)
                    if "429" in err_str or "RESOURCE_EXHAUSTED" in err_str:
                        client = rotate_ai_client()
                        if not client:
                            break
                    else:
                        raise e
            return False

        try:
            success = await asyncio.to_thread(generate_gemini_image)
            if success and os.path.exists(photo_filename):
                await message.answer_photo(
                    photo=aiogram_types.FSInputFile(photo_filename),
                    caption=f"✨ Фото успешно создано по промпту:\n«{text_prompt}»",
                    reply_markup=get_main_menu_keyboard()
                )
                os.remove(photo_filename)
            else:
                await message.answer("❌ Не удалось сгенерировать фото.", reply_markup=get_main_menu_keyboard())
        except Exception as e:
            logging.error(f"❌ [{user_id}] Ошибка генерации фото: {e}")
            await message.answer(f"⚠️ **Ошибка:**\n`{e}`", parse_mode="Markdown", reply_markup=get_main_menu_keyboard())

    try:
        await status_msg.delete()
    except Exception:
        pass
    
    await state.clear()

async def handle_ping(request):
    return web.Response(text="Bot is running and awake!")

async def web_server_runner():
    app = web.Application()
    app.router.add_get("/", handle_ping)
    app.router.add_get("/ping", handle_ping)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    logging.info(f"🌐 HTTP-сервер для удержания от сна запущен на порту {PORT}")

async def main():
    await bot.delete_webhook(drop_pending_updates=True)
    commands = [aiogram_types.BotCommand(command="start", description="Главное меню / Запуск")]
    await bot.set_my_commands(commands)

    dp.include_router(router)
    await web_server_runner()
    logging.info("🚀 Бот успешно запущен и ожидает сообщения (Long Polling)...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
