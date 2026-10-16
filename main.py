import asyncio
import logging
import os
import time
from aiohttp import web
from aiogram import Bot, Dispatcher, F, Router, types as aiogram_types
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from dotenv import load_dotenv
import edge_tts
from deep_translator import GoogleTranslator, DeeplTranslator
from gtts import gTTS
from google import genai
from google.genai import types as genai_types

# Загружаем переменные окружения из .env файла
load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
PORT = int(os.getenv("PORT", 10000))
DEEPL_API_KEY = os.getenv("DEEPL_API_KEY", "")

# Собираем ключи Gemini для автоматической ротации (GEMINI_API_KEY, GEMINI_API_KEY2, GEMINI_API_KEY3)
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
        return None
    key = GEMINI_KEYS[current_key_index]
    try:
        return genai.Client(api_key=key)
    except Exception as e:
        logging.warning(f"Не удалось инициализировать GenAI клиента с ключом #{current_key_index + 1}: {e}")
        return None

def rotate_ai_client():
    global current_key_index
    if len(GEMINI_KEYS) > 1:
        current_key_index = (current_key_index + 1) % len(GEMINI_KEYS)
        logging.info(f"🔄 Переключение на запасной ключ Gemini #{current_key_index + 1}")
    return get_ai_client()

if not TELEGRAM_BOT_TOKEN:
    raise ValueError("❌ Ошибка: Не задан TELEGRAM_BOT_TOKEN в файле .env!")

bot = Bot(token=TELEGRAM_BOT_TOKEN)
dp = Dispatcher()

# Создаем роутер и фильтруем сообщения от других ботов
router = Router()
router.message.filter(F.from_user.is_bot == False)

logging.basicConfig(level=logging.INFO)

class CreateMedia(StatesGroup):
    waiting_for_content = State()

@router.message(Command("start"))
async def cmd_start(message: aiogram_types.Message, state: FSMContext):
    welcome_text = (
        "Привет! 👋 Я ИИ-бот для создания видео и озвучки.\n\n"
        "🎬 **Что я умею:**\n"
        "• Озвучивать текст голосом с помощью **Edge TTS**.\n"
        "• Генерировать кинематографичные видео по описанию с помощью модели **Google Veo**.\n\n"
        "📌 **Как пользоваться:**\n"
        "Отправьте мне текстовое описание (промпт) или прикрепите фото/видео с подписью."
    )
    await message.answer(welcome_text, parse_mode="Markdown")
    await state.set_state(CreateMedia.waiting_for_content)

@router.message(CreateMedia.waiting_for_content)
async def handle_media_creation(message: aiogram_types.Message, state: FSMContext):
    text_prompt = message.caption or message.text

    if not text_prompt or text_prompt.startswith("/"):
        await message.answer("Пожалуйста, отправьте текстовое описание для генерации видео и озвучки.")
        return

    status_msg = await message.answer("🎙 **Шаг 1/2:** Генерирую бесплатную озвучку текста...")

    # 1. Генерация озвучки через Edge TTS
    audio_path = f"output_voice_{message.from_user.id}.mp3"
    voice = "ru-RU-SvetlanaNeural"

    try:
        communicate = edge_tts.Communicate(text_prompt, voice)
        await communicate.save(audio_path)
        await message.answer_audio(
            audio=aiogram_types.FSInputFile(audio_path),
            caption="🎙 Голосовая озвучка готова!"
        )
    except Exception as e:
        logging.error(f"Ошибка TTS: {e}")
        await message.answer("⚠️ Не удалось создать озвучку.")
    finally:
        if os.path.exists(audio_path):
            os.remove(audio_path)

    # 2. Генерация видео через Veo API (Google GenAI) с ротацией ключей
    await status_msg.edit_text("🎬 **Шаг 2/2:** Запускаю генерацию видео через Veo (это может занять пару минут)...")
    video_filename = f"generated_video_{message.from_user.id}.mp4"

    def generate_veo_video():
        client = get_ai_client()
        if not client:
            raise ValueError("API-ключи Gemini/GenAI не настроены.")

        for _ in range(len(GEMINI_KEYS) or 1):
            try:
                operation = client.models.generate_videos(
                    model="veo-3.1-generate-preview",
                    prompt=text_prompt,
                )
                while not operation.done:
                    time.sleep(10)
                    operation = client.operations.get(operation)

                generated_video = operation.response.generated_videos[0]
                client.files.download(file=generated_video.video, destination=video_filename)
                return True
            except Exception as e:
                err_str = str(e)
                if "429" in err_str or "RESOURCE_EXHAUSTED" in err_str:
                    logging.warning(f"Лимит ключа исчерпан, ротация ключа Gemini... Ошибка: {e}")
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
                caption=f"✨ Видео успешно создано по промпту:\n«{text_prompt}»"
            )
            os.remove(video_filename)
        else:
            await message.answer("❌ Не удалось сгенерировать видео из-за исчерпания лимитов API.")
    except Exception as e:
        logging.error(f"Ошибка Veo: {e}")
        await message.answer(f"⚠️ Ошибка при создании видео: {e}")

    await status_msg.delete()

# HTTP-сервер для поддержки активности бота на Render (Prevent Sleep)
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
    logging.info(f"🌐 HTTP-сервер запущен на порту {PORT}")

async def main():
    await bot.delete_webhook(drop_pending_updates=True)

    commands = [
        aiogram_types.BotCommand(command="start", description="Запустить бота и сгенерировать видео")
    ]
    await bot.set_my_commands(commands)

    dp.include_router(router)
    await web_server_runner()
    logging.info("🚀 Запуск бота через Long Polling...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())