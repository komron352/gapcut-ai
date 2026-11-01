
import asyncio
import hashlib
import hmac
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import time

from pathlib import Path
from urllib.parse import parse_qsl

import httpx
import uvicorn

from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart, Command
from aiogram.types import (
    Message,
    FSInputFile,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    WebAppInfo
)

from dotenv import load_dotenv
from fastapi import FastAPI, UploadFile, File, Form, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from starlette.background import BackgroundTask


# =====================================
# 1. SETTINGS
# =====================================

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")

PORT = int(os.getenv("PORT", "10000"))

MAX_INPUT_SIZE = 19 * 1024 * 1024
MAX_OUTPUT_SIZE = 49 * 1024 * 1024
MAX_DURATION = 180

GROQ_URL = "https://api.groq.com/openai/v1/audio/transcriptions"

MINI_APP_URL = "https://gapcut-ai-web.vercel.app"

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is missing")

if not GROQ_API_KEY:
    raise RuntimeError("GROQ_API_KEY is missing")


# =====================================
# 2. LOGGING
# =====================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

logger = logging.getLogger("GapCutAI")


# =====================================
# 3. TELEGRAM BOT
# =====================================

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

user_busy = set()


# =====================================
# 4. FASTAPI
# =====================================

app = FastAPI(
    title="GAPCUT AI API",
    version="1.0.0"
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://gapcut-ai-web.vercel.app"
    ],
    allow_credentials=False,
    allow_methods=["GET", "POST"],
    allow_headers=["*"]
)


@app.get("/")
async def home():
    return {
        "name": "GAPCUT AI",
        "status": "running",
        "version": "1.0.0"
    }


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "service": "GAPCUT AI"
    }


# =====================================
# 5. TELEGRAM MINI APP AUTH
# =====================================

def validate_telegram_init_data(init_data: str):

    if not init_data:
        return None

    try:
        values = dict(parse_qsl(
            init_data,
            keep_blank_values=True
        ))

        received_hash = values.pop("hash", None)

        if not received_hash:
            return None

        auth_date = int(values.get("auth_date", "0"))

        if auth_date <= 0:
            return None

        if time.time() - auth_date > 86400:
            return None

        data_check_string = "\n".join(
            f"{key}={value}"
            for key, value in sorted(values.items())
        )

        secret_key = hmac.new(
            b"WebAppData",
            BOT_TOKEN.encode(),
            hashlib.sha256
        ).digest()

        calculated_hash = hmac.new(
            secret_key,
            data_check_string.encode(),
            hashlib.sha256
        ).hexdigest()

        if not hmac.compare_digest(
            calculated_hash,
            received_hash
        ):
            return None

        user_data = json.loads(values.get("user", "{}"))

        return user_data

    except Exception:
        logger.warning("Telegram authentication failed")
        return None


# =====================================
# 6. FFMPEG COMMAND
# =====================================

def run_command(command, timeout=300):

    result = subprocess.run(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=timeout
    )

    if result.returncode != 0:
        logger.error(result.stderr[-4000:])
        raise RuntimeError("FFmpeg command failed")

    return result


# =====================================
# 7. VIDEO DURATION
# =====================================

def get_video_duration(video_path):

    result = run_command([
        "ffprobe",
        "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        str(video_path)
    ])

    return float(result.stdout.strip())


# =====================================
# 8. EXTRACT AUDIO
# =====================================

def extract_audio(video_path, audio_path):

    run_command([
        "ffmpeg",
        "-y",
        "-i", str(video_path),
        "-vn",
        "-ac", "1",
        "-ar", "16000",
        "-c:a", "pcm_s16le",
        str(audio_path)
    ], timeout=300)


# =====================================
# 9. GROQ WHISPER
# =====================================

async def transcribe_audio(audio_path):

    headers = {
        "Authorization": f"Bearer {GROQ_API_KEY}"
    }

    data = {
        "model": "whisper-large-v3-turbo",
        "response_format": "verbose_json",
        "timestamp_granularities[]": "word"
    }

    async with httpx.AsyncClient(timeout=180) as client:

        with open(audio_path, "rb") as audio:

            files = {
                "file": (
                    "audio.wav",
                    audio,
                    "audio/wav"
                )
            }

            response = await client.post(
                GROQ_URL,
                headers=headers,
                data=data,
                files=files
            )

    if response.status_code != 200:

        logger.error(
            "Groq error: %s",
            response.text[:2000]
        )

        raise RuntimeError("Groq transcription failed")

    result = response.json()

    if not isinstance(result, dict):
        raise RuntimeError("Groq response format is invalid")

    return result


# =====================================
# 10. SUBTITLE HELPERS
# =====================================

def ass_time(seconds):

    total = max(0, int(round(seconds * 100)))

    hours = total // 360000
    minutes = (total % 360000) // 6000
    seconds_part = (total % 6000) / 100

    return (
        f"{hours}:"
        f"{minutes:02d}:"
        f"{seconds_part:05.2f}"
    )


def clean_ass_text(text):

    text = str(text)
    text = text.replace("\\", "")
    text = text.replace("{", "")
    text = text.replace("}", "")
    text = text.replace("\n", " ")

    return text.strip()


# =====================================
# 11. SUBTITLE STYLES
# =====================================

STYLE_SETTINGS = {

    "Viral": {
        "font": "Arial",
        "size": 64,
        "primary": "&H0000FFFF",
        "outline": 5,
        "bold": -1
    },

    "Classic": {
        "font": "Arial",
        "size": 52,
        "primary": "&H00FFFFFF",
        "outline": 3,
        "bold": 0
    },

    "Cinema": {
        "font": "Arial",
        "size": 48,
        "primary": "&H00FFFFFF",
        "outline": 2,
        "bold": 0
    },

    "Podcast": {
        "font": "Arial",
        "size": 56,
        "primary": "&H00FFFFFF",
        "outline": 4,
        "bold": -1
    },

    "Karaoke": {
        "font": "Arial",
        "size": 62,
        "primary": "&H0000FFFF",
        "outline": 4,
        "bold": -1
    },

    "Creator": {
        "font": "Arial",
        "size": 58,
        "primary": "&H00FFFFFF",
        "outline": 4,
        "bold": -1
    }
}


# =====================================
# 12. CREATE ASS SUBTITLES
# =====================================

def make_ass_subtitles(
    transcription,
    output_path,
    style="Viral"
):

    settings = STYLE_SETTINGS.get(
        style,
        STYLE_SETTINGS["Viral"]
    )

    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,{settings["font"]},{settings["size"]},{settings["primary"]},&H0000FFFF,&H00000000,&H80000000,{settings["bold"]},0,1,{settings["outline"]},1,2,70,70,250,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""

    events = []
    words = []

    if not isinstance(transcription, dict):
        raise RuntimeError("Ҷавоби Groq нодуруст аст.")

    raw_words = transcription.get("words") or []

    if not raw_words:

        segments = transcription.get("segments") or []

        if isinstance(segments, list):

            for segment in segments:

                if not isinstance(segment, dict):
                    continue

                segment_words = segment.get("words") or []

                if isinstance(segment_words, list):
                    raw_words.extend(segment_words)

    if isinstance(raw_words, list):

        for word in raw_words:

            if not isinstance(word, dict):
                continue

            text = clean_ass_text(
                word.get("word", "")
            )

            if not text:
                continue

            try:
                start = float(word["start"])
                end = float(word["end"])
            except (KeyError, TypeError, ValueError):
                continue

            if end <= start:
                end = start + 0.1

            words.append({
                "start": start,
                "end": end,
                "text": text
            })

    if words:

        groups = []
        current_group = []

        for word in words:

            current_group.append(word)

            punctuation = bool(
                re.search(r"[.!?؟…]$", word["text"])
            )

            if len(current_group) >= 6 or punctuation:
                groups.append(current_group)
                current_group = []

        if current_group:
            groups.append(current_group)

        for group in groups:

            start = group[0]["start"]
            end = group[-1]["end"]

            subtitle_text = ""

            for word in group:

                duration = max(
                    5,
                    int(round(
                        (word["end"] - word["start"]) * 100
                    ))
                )

                subtitle_text += (
                    f"{{\\k{duration}}}"
                    f"{word['text']} "
                )

            events.append(
                "Dialogue: 0,"
                f"{ass_time(start)},"
                f"{ass_time(end)},"
                "Default,,0,0,0,,"
                f"{subtitle_text.strip()}"
            )

    else:

        segments = transcription.get("segments") or []

        if isinstance(segments, list):

            for segment in segments:

                if not isinstance(segment, dict):
                    continue

                text = clean_ass_text(
                    segment.get("text", "")
                )

                if not text:
                    continue

                try:
                    start = float(segment.get("start", 0))
                    end = float(segment.get("end", start + 2))
                except (TypeError, ValueError):
                    continue

                if end <= start:
                    end = start + 1

                events.append(
                    "Dialogue: 0,"
                    f"{ass_time(start)},"
                    f"{ass_time(end)},"
                    "Default,,0,0,0,,"
                    f"{text}"
                )

        if not events:

            text = clean_ass_text(
                transcription.get("text", "")
            )

            if text:

                try:
                    duration = float(
                        transcription.get("duration") or 5
                    )
                except (TypeError, ValueError):
                    duration = 5

                if duration <= 0:
                    duration = 5

                events.append(
                    "Dialogue: 0,"
                    "0:00:00.00,"
                    f"{ass_time(duration)},"
                    "Default,,0,0,0,,"
                    f"{text}"
                )

    if not events:
        raise RuntimeError(
            "Groq матни субтитрҳоро барнагардонд."
        )

    with open(output_path, "w", encoding="utf-8") as file:
        file.write(
            header + "\n".join(events)
        )


# =====================================
# 13. VIDEO FORMAT
# =====================================

FORMAT_FILTERS = {

    "9:16": (
        "scale=1080:1920:"
        "force_original_aspect_ratio=increase,"
        "crop=1080:1920"
    ),

    "16:9": (
        "scale=1920:1080:"
        "force_original_aspect_ratio=increase,"
        "crop=1920:1080"
    ),

    "1:1": (
        "scale=1080:1080:"
        "force_original_aspect_ratio=increase,"
        "crop=1080:1080"
    ),

    "original": "null"
}


# =====================================
# 14. RENDER VIDEO
# =====================================

def render_video(
    video_path,
    subtitle_path,
    output_path,
    video_format="9:16"
):

    format_filter = FORMAT_FILTERS.get(
        video_format,
        FORMAT_FILTERS["9:16"]
    )

    subtitle_filter = (
        "ass="
        + str(subtitle_path).replace("\\", "/")
    )

    if video_format == "original":
        video_filter = subtitle_filter
    else:
        video_filter = format_filter + "," + subtitle_filter

    run_command([
        "ffmpeg",
        "-y",
        "-i", str(video_path),
        "-vf", video_filter,
        "-c:v", "libx264",
        "-preset", "ultrafast",
        "-crf", "28",
        "-c:a", "aac",
        "-b:a", "128k",
        "-movflags", "+faststart",
        str(output_path)
    ], timeout=600)


# =====================================
# 15. COMPLETE PROCESSING PIPELINE
# =====================================

async def process_video(
    video_path,
    output_path,
    style="Viral",
    video_format="9:16"
):

    folder = Path(video_path).parent

    audio_path = folder / "audio.wav"
    subtitle_path = folder / "subtitles.ass"

    duration = await asyncio.to_thread(
        get_video_duration,
        video_path
    )

    if duration <= 0:
        raise ValueError("Давомнокии видео нодуруст аст.")

    if duration > MAX_DURATION:
        raise ValueError(
            "Видео бояд то 3 дақиқа бошад."
        )

    await asyncio.to_thread(
        extract_audio,
        video_path,
        audio_path
    )

    transcription = await transcribe_audio(audio_path)

    await asyncio.to_thread(
        make_ass_subtitles,
        transcription,
        subtitle_path,
        style
    )

    await asyncio.to_thread(
        render_video,
        video_path,
        subtitle_path,
        output_path,
        video_format
    )

    if not output_path.exists():
        raise RuntimeError("Видеои ниҳоӣ сохта нашуд.")

    if output_path.stat().st_size > MAX_OUTPUT_SIZE:
        raise ValueError(
            "Ҳаҷми видеои тайёр аз 49 MB зиёд шуд."
        )

    return output_path


# =====================================
# 16. TELEGRAM START
# =====================================

@dp.message(CommandStart())
async def start_handler(message: Message):

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🎬 Монтаж",
                    web_app=WebAppInfo(
                        url=MINI_APP_URL
                    )
                )
            ]
        ]
    )

    await message.answer(
        "🎬 Хуш омадед ба GapCut AI!\n\n"
        "Ман видеои шуморо автоматӣ таҳрир мекунам.\n\n"
        "✅ Шинохтани овоз\n"
        "✅ Сохтани субтитрҳо\n"
        "✅ Субтитрҳои калима ба калима\n"
        "✅ Сохтани видеои тайёр\n\n"
        "📤 Видеоро фиристед.\n\n"
        "⏱ Максимум: 3 дақиқа\n"
        "📦 Максимум: 19 MB",
        reply_markup=keyboard
    )


# =====================================
# 17. TELEGRAM HELP
# =====================================

@dp.message(Command("help"))
async def help_handler(message: Message):

    await message.answer(
        "📖 Дастури GapCut AI\n\n"
        "1. Тугмаи 🎬 Монтаж-ро пахш кунед.\n"
        "2. Видеоро интихоб кунед.\n"
        "3. Услуб ва формати субтитрро интихоб кунед.\n"
        "4. Монтажро оғоз кунед.\n\n"
        "Формат: видео\n"
        "Давомнокӣ: то 3 дақиқа\n"
        "Ҳаҷм: то 19 MB"
    )


# =====================================
# 18. TELEGRAM VIDEO HANDLER
# =====================================

@dp.message(F.video | F.document)
async def video_handler(message: Message):

    user_id = message.from_user.id

    if user_id in user_busy:

        await message.answer(
            "⏳ Видеои қаблии шумо ҳоло коркард шуда истодааст."
        )

        return

    user_busy.add(user_id)

    try:

        await message.answer(
            "🎬 Видео қабул шуд!\n"
            "Лутфан интизор шавед..."
        )

        with tempfile.TemporaryDirectory() as temp_dir:

            folder = Path(temp_dir)

            video_path = folder / "input.mp4"
            output_path = folder / "gapcut_output.mp4"

            media = message.video

            if not media and message.document:

                if not (
                    message.document.mime_type
                    and message.document.mime_type.startswith("video/")
                ):
                    raise ValueError(
                        "Лутфан файли видео фиристед."
                    )

                media = message.document

            if not media:
                raise ValueError("Видео ёфт нашуд.")

            if media.file_size and media.file_size > MAX_INPUT_SIZE:
                raise ValueError(
                    "Ҳаҷми видео бояд аз 19 MB зиёд набошад."
                )

            file_info = await bot.get_file(media.file_id)

            await bot.download_file(
                file_info.file_path,
                destination=video_path
            )

            await message.answer(
                "🧠 AI овозро мешиносад ва субтитр месозад..."
            )

            await process_video(
                video_path,
                output_path,
                style="Viral",
                video_format="9:16"
            )

            await message.answer_video(
                FSInputFile(output_path),
                caption=(
                    "✅ GapCut AI — видео омода шуд!\n\n"
                    "🎬 Субтитрҳо илова шуданд.\n"
                    "✨ Ташаккур барои истифода!"
                ),
                supports_streaming=True
            )

    except ValueError as error:

        await message.answer(f"⚠️ {error}")

    except Exception:

        logger.exception("Telegram video processing error")

        await message.answer(
            "❌ Ҳангоми коркарди видео хатогӣ шуд.\n\n"
            "Лутфан баъдтар дубора кӯшиш кунед."
        )

    finally:
        user_busy.discard(user_id)


# =====================================
# 19. MINI APP VIDEO API
# =====================================

@app.post("/api/process")
async def api_process(
    file: UploadFile = File(...),
    style: str = Form("Viral"),
    language: str = Form("tg"),
    video_format: str = Form("9:16"),
    init_data: str = Form(...)
):

    telegram_user = validate_telegram_init_data(init_data)

    if not telegram_user:
        raise HTTPException(
            status_code=401,
            detail="Telegram authentication failed. Open the Mini App inside Telegram."
        )

    if style not in STYLE_SETTINGS:
        raise HTTPException(
            status_code=400,
            detail="Invalid video style."
        )

    if language not in ("tg", "ru", "both"):
        raise HTTPException(
            status_code=400,
            detail="Invalid subtitle language."
        )

    if video_format not in FORMAT_FILTERS:
        raise HTTPException(
            status_code=400,
            detail="Invalid video format."
        )

    if not file.content_type or not file.content_type.startswith("video/"):
        raise HTTPException(
            status_code=400,
            detail="Please upload a video file."
        )

    temp_dir = tempfile.mkdtemp(prefix="gapcut_")

    folder = Path(temp_dir)

    video_path = folder / "input_video"
    output_path = folder / "gapcut_output.mp4"

    try:

        total_size = 0

        with open(video_path, "wb") as destination:

            while True:

                chunk = await file.read(1024 * 1024)

                if not chunk:
                    break

                total_size += len(chunk)

                if total_size > MAX_INPUT_SIZE:
                    raise HTTPException(
                        status_code=413,
                        detail="Video must be smaller than 19 MB."
                    )

                destination.write(chunk)

        if total_size == 0:
            raise HTTPException(
                status_code=400,
                detail="The uploaded video is empty."
            )

        logger.info(
            "Mini App video received from Telegram user %s",
            telegram_user.get("id")
        )

        await process_video(
            video_path,
            output_path,
            style=style,
            video_format=video_format
        )

        return FileResponse(
            path=str(output_path),
            media_type="video/mp4",
            filename="gapcut_ai_video.mp4",
            background=BackgroundTask(
                shutil.rmtree,
                temp_dir,
                ignore_errors=True
            )
        )

    except HTTPException:

        shutil.rmtree(temp_dir, ignore_errors=True)
        raise

    except ValueError as error:

        shutil.rmtree(temp_dir, ignore_errors=True)

        raise HTTPException(
            status_code=400,
            detail=str(error)
        )

    except Exception:

        logger.exception("Mini App processing error")

        shutil.rmtree(temp_dir, ignore_errors=True)

        raise HTTPException(
            status_code=500,
            detail="Video processing failed. Please try again."
        )

    finally:
        await file.close()


# =====================================
# 20. MAIN SERVER
# =====================================

async def main():

    config = uvicorn.Config(
        app,
        host="0.0.0.0",
        port=PORT,
        log_level="info"
    )

    server = uvicorn.Server(config)

    logger.info("GAPCUT AI API started on port %s", PORT)
    logger.info("GAPCUT AI Telegram bot started")

    await asyncio.gather(
        server.serve(),
        dp.start_polling(bot)
    )


# =====================================
# 21. RUN
# =====================================

if __name__ == "__main__":

    try:
        asyncio.run(main())

    except KeyboardInterrupt:
        logger.info("GAPCUT AI stopped")
