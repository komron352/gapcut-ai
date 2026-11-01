
import asyncio
import hashlib
import hmac
import json
import logging
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from urllib.parse import parse_qsl

import httpx
import uvicorn
from dotenv import load_dotenv

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    WebAppInfo,
)

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from starlette.background import BackgroundTask


# =========================================================
# CONFIG
# =========================================================

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "").strip()
PORT = int(os.getenv("PORT", "10000"))

MINI_APP_URL = "https://gapcut-ai-web.vercel.app"
SUPPORT_URL = "https://t.me/aikomron"
CHANNEL_URL = "https://t.me/gapcutai"

MAX_INPUT_SIZE = 300 * 1024 * 1024
MAX_OUTPUT_SIZE = 500 * 1024 * 1024
MAX_DURATION = 120

UPLOAD_CHUNK_SIZE = 1024 * 1024
GROQ_MAX_AUDIO_SIZE = 24 * 1024 * 1024

GROQ_URL = "https://api.groq.com/openai/v1/audio/transcriptions"

ALLOWED_EXTENSIONS = {
    ".mp4", ".mov", ".m4v", ".webm", ".mkv"
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)

logger = logging.getLogger("gapcut-ai")

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN не указан в Environment Variables.")

if not GROQ_API_KEY:
    raise RuntimeError("GROQ_API_KEY не указан в Environment Variables.")


# =========================================================
# BOT + API
# =========================================================

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

app = FastAPI(
    title="GAPCUT AI API",
    version="2.1.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://gapcut-ai-web.vercel.app",
    ],
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)


# =========================================================
# TELEGRAM MINI APP AUTH
# =========================================================

def validate_telegram_init_data(init_data: str):
    if not init_data:
        return None

    try:
        parsed = dict(parse_qsl(init_data, keep_blank_values=True))
        received_hash = parsed.pop("hash", None)

        if not received_hash:
            return None

        data_check_string = "\n".join(
            f"{key}={value}"
            for key, value in sorted(parsed.items())
        )

        secret_key = hmac.new(
            b"WebAppData",
            BOT_TOKEN.encode(),
            hashlib.sha256,
        ).digest()

        calculated_hash = hmac.new(
            secret_key,
            data_check_string.encode(),
            hashlib.sha256,
        ).hexdigest()

        if not hmac.compare_digest(calculated_hash, received_hash):
            return None

        auth_date = int(parsed.get("auth_date", "0"))
        now = int(time.time())

        if auth_date <= 0 or auth_date > now + 60:
            return None

        if now - auth_date > 86400:
            return None

        user_data = json.loads(parsed.get("user", "{}"))

        if not isinstance(user_data, dict) or not user_data.get("id"):
            return None

        return user_data

    except Exception:
        logger.exception("Telegram initData validation error")
        return None


# =========================================================
# KEYBOARDS
# =========================================================

def main_keyboard():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🎬 Монтаж",
                    web_app=WebAppInfo(url=MINI_APP_URL),
                )
            ],
            [
                InlineKeyboardButton(
                    text="💬 Поддержка",
                    url=SUPPORT_URL,
                ),
                InlineKeyboardButton(
                    text="📢 Наш канал",
                    url=CHANNEL_URL,
                ),
            ],
        ]
    )


# =========================================================
# FFMPEG HELPERS
# =========================================================

def run_command(command, timeout=600):
    result = subprocess.run(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=timeout,
    )

    if result.returncode != 0:
        error_text = result.stderr[-3000:] or "Ошибка выполнения команды."
        logger.error("Command failed: %s", error_text)
        raise RuntimeError(error_text)

    return result.stdout


def get_duration(file_path):
    output = run_command([
        "ffprobe",
        "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        str(file_path),
    ])

    try:
        return float(output.strip())
    except (ValueError, TypeError):
        raise RuntimeError("Не удалось определить длительность видео.")


def escape_ass_text(text):
    text = str(text or "")
    text = text.replace("\\", r"\\")
    text = text.replace("{", r"\{")
    text = text.replace("}", r"\}")
    text = text.replace("\n", r"\N")
    return text


def ass_time(seconds):
    seconds = max(0.0, float(seconds))
    total_cs = int(round(seconds * 100))

    hours = total_cs // 360000
    minutes = (total_cs // 6000) % 60
    secs = (total_cs // 100) % 60
    centiseconds = total_cs % 100

    return f"{hours}:{minutes:02d}:{secs:02d}.{centiseconds:02d}"


# =========================================================
# SUBTITLE STYLES
# =========================================================

STYLES = {
    "viral": {
        "font": "Arial",
        "size": 58,
        "primary": "&H00FFFFFF",
        "outline": "&H00000000",
        "bold": 1,
        "outline_width": 4,
        "margin_v": 100,
    },
    "classic": {
        "font": "Arial",
        "size": 42,
        "primary": "&H00FFFFFF",
        "outline": "&H00000000",
        "bold": 0,
        "outline_width": 2,
        "margin_v": 65,
    },
    "cinema": {
        "font": "Georgia",
        "size": 42,
        "primary": "&H00FFFFFF",
        "outline": "&H00000000",
        "bold": 0,
        "outline_width": 2,
        "margin_v": 80,
    },
    "podcast": {
        "font": "Arial",
        "size": 48,
        "primary": "&H00FFFFFF",
        "outline": "&H00101010",
        "bold": 1,
        "outline_width": 3,
        "margin_v": 75,
    },
    "karaoke": {
        "font": "Arial",
        "size": 56,
        "primary": "&H0000FFFF",
        "outline": "&H00000000",
        "bold": 1,
        "outline_width": 4,
        "margin_v": 100,
    },
    "creator": {
        "font": "Arial",
        "size": 48,
        "primary": "&H00FFFFFF",
        "outline": "&H00000000",
        "bold": 1,
        "outline_width": 3,
        "margin_v": 85,
    },
}


# =========================================================
# GROQ WHISPER
# =========================================================

async def transcribe_audio(audio_path):
    audio_size = Path(audio_path).stat().st_size

    if audio_size > GROQ_MAX_AUDIO_SIZE:
        raise RuntimeError(
            "Аудиофайл превышает лимит отправки в Groq. "
            "Попробуйте видео с более коротким или сжатым аудио."
        )

    headers = {
        "Authorization": f"Bearer {GROQ_API_KEY}",
    }

    data = {
        "model": "whisper-large-v3-turbo",
        "response_format": "verbose_json",
        "timestamp_granularities[]": "word",
    }

    timeout = httpx.Timeout(300.0, connect=30.0)

    async with httpx.AsyncClient(timeout=timeout) as client:
        with open(audio_path, "rb") as audio_file:
            response = await client.post(
                GROQ_URL,
                headers=headers,
                data=data,
                files={
                    "file": (
                        Path(audio_path).name,
                        audio_file,
                        "audio/mpeg",
                    )
                },
            )

    if response.status_code != 200:
        logger.error(
            "Groq API error %s: %s",
            response.status_code,
            response.text[:2000],
        )

        raise RuntimeError(
            "Ошибка распознавания аудио. Проверьте Groq API и попробуйте позже."
        )

    result = response.json()

    words = result.get("words") or []
    segments = result.get("segments") or []

    if not words and segments:
        for segment in segments:
            segment_words = str(segment.get("text", "")).split()

            if not segment_words:
                continue

            start = float(segment.get("start", 0))
            end = float(segment.get("end", start + 1))
            step = max((end - start) / len(segment_words), 0.05)

            for index, word in enumerate(segment_words):
                words.append({
                    "word": word,
                    "start": start + index * step,
                    "end": start + (index + 1) * step,
                })

    if not words:
        raise RuntimeError("Не удалось распознать речь в аудио.")

    return words


# =========================================================
# ASS SUBTITLES
# =========================================================

def create_ass_file(words, output_path, style_name):
    style = STYLES.get(style_name, STYLES["viral"])

    header = f"""[Script Info]
Title: GAPCUT AI
ScriptType: v4.00+
WrapStyle: 2
ScaledBorderAndShadow: yes
PlayResX: 1080
PlayResY: 1920

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,{style["font"]},{style["size"]},{style["primary"]},&H000000FF,{style["outline"]},&H80000000,{style["bold"]},0,0,0,100,100,0,0,1,{style["outline_width"]},1,2,65,65,{style["margin_v"]},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""

    events = []
    group = []

    for item in words:
        word = str(item.get("word", "")).strip()

        if not word:
            continue

        start = float(item.get("start", 0))
        end = float(item.get("end", start + 0.3))

        group.append({
            "word": word,
            "start": max(0, start),
            "end": max(end, start + 0.05),
        })

        if len(group) >= 5 or word.endswith((".", "!", "?", "؟", "।")):
            events.append(group)
            group = []

    if group:
        events.append(group)

    for subtitle_group in events:
        start = subtitle_group[0]["start"]
        end = subtitle_group[-1]["end"]

        parts = []

        for item in subtitle_group:
            duration_cs = max(
                1,
                int(round((item["end"] - item["start"]) * 100)),
            )

            parts.append(
                f"{{\\k{duration_cs}}}{escape_ass_text(item['word'])}"
            )

        subtitle_text = " ".join(parts)

        header += (
            f"Dialogue: 0,{ass_time(start)},{ass_time(end)},"
            f"Default,,0,0,0,,{subtitle_text}\n"
        )

    Path(output_path).write_text(header, encoding="utf-8")


# =========================================================
# VIDEO FORMATS
# =========================================================

def get_video_filter(video_format):
    if video_format == "9:16":
        return (
            "scale=1080:1920:force_original_aspect_ratio=increase,"
            "crop=1080:1920"
        )

    if video_format == "16:9":
        return (
            "scale=1920:1080:force_original_aspect_ratio=increase,"
            "crop=1920:1080"
        )

    if video_format == "1:1":
        return (
            "scale=1080:1080:force_original_aspect_ratio=increase,"
            "crop=1080:1080"
        )

    if video_format == "original":
        return "scale=trunc(iw/2)*2:trunc(ih/2)*2"

    raise ValueError("Неверный формат видео.")


def escape_filter_path(path):
    path = str(Path(path).resolve())

    return (
        path.replace("\\", "\\\\")
        .replace(":", "\\:")
        .replace("'", "\\'")
        .replace(",", "\\,")
        .replace("[", "\\[")
        .replace("]", "\\]")
    )


# =========================================================
# VIDEO PROCESSING
# =========================================================

async def process_video(input_path, output_path, style_name, video_format):
    duration = await asyncio.to_thread(get_duration, input_path)

    if duration <= 0 or duration > MAX_DURATION:
        raise RuntimeError(
            f"Длительность видео не должна превышать {MAX_DURATION} секунд."
        )

    work_dir = Path(output_path).parent
    audio_path = work_dir / "audio.mp3"
    ass_path = work_dir / "subtitles.ass"

    logger.info("Extracting audio")

    await asyncio.to_thread(
        run_command,
        [
            "ffmpeg",
            "-y",
            "-i", str(input_path),
            "-vn",
            "-ac", "1",
            "-ar", "16000",
            "-b:a", "64k",
            str(audio_path),
        ],
    )

    logger.info("Sending audio to Groq Whisper")

    words = await transcribe_audio(audio_path)

    logger.info("Creating subtitles")

    await asyncio.to_thread(
        create_ass_file,
        words,
        ass_path,
        style_name,
    )

    video_filter = get_video_filter(video_format)
    subtitle_path = escape_filter_path(ass_path)

    filters = f"{video_filter},subtitles='{subtitle_path}'"

    logger.info("Rendering final video")

    await asyncio.to_thread(
        run_command,
        [
            "ffmpeg",
            "-y",
            "-i", str(input_path),
            "-vf", filters,
            "-c:v", "libx264",
            "-preset", "veryfast",
            "-crf", "23",
            "-c:a", "aac",
            "-b:a", "128k",
            "-movflags", "+faststart",
            "-pix_fmt", "yuv420p",
            str(output_path),
        ],
        1200,
    )

    output_file = Path(output_path)

    if not output_file.exists() or output_file.stat().st_size == 0:
        raise RuntimeError("Не удалось создать готовое видео.")

    if output_file.stat().st_size > MAX_OUTPUT_SIZE:
        raise RuntimeError(
            "Размер готового видео превышает допустимый лимит."
        )

    logger.info(
        "Rendering completed. Output size: %s bytes",
        output_file.stat().st_size,
    )

    return output_path


# =========================================================
# TELEGRAM COMMANDS
# =========================================================

@dp.message(CommandStart())
async def start_handler(message: Message):
    text = (
        "🎬 <b>GAPCUT AI</b>\n\n"
        "Автоматический монтаж видео с помощью искусственного интеллекта.\n\n"
        "✨ Автоматические субтитры\n"
        "🎨 Различные стили субтитров\n"
        "📱 Форматы Reels, TikTok и YouTube\n\n"
        "📦 Максимальный размер: 300 MB\n"
        "⏱ Максимальная длительность: 2 минуты\n\n"
        "Чтобы начать, нажмите кнопку «Монтаж»."
    )

    await message.answer(
        text,
        reply_markup=main_keyboard(),
        parse_mode="HTML",
    )


@dp.message(Command("help"))
async def help_handler(message: Message):
    text = (
        "ℹ️ <b>GAPCUT AI — Помощь</b>\n\n"
        "1. Нажмите кнопку «Монтаж».\n"
        "2. Выберите видео.\n"
        "3. Выберите стиль субтитров и формат видео.\n"
        "4. Запустите обработку.\n\n"
        "📦 Максимальный размер: 300 MB\n"
        "⏱ Максимальная длительность: 2 минуты\n\n"
        f"💬 Поддержка: {SUPPORT_URL}\n"
        f"📢 Наш канал: {CHANNEL_URL}"
    )

    await message.answer(
        text,
        reply_markup=main_keyboard(),
        parse_mode="HTML",
    )


# =========================================================
# DIRECT TELEGRAM VIDEO RECEIVING
# =========================================================

@dp.message(F.video | F.document)
async def video_handler(message: Message):
    media = message.video or message.document

    if message.document:
        mime = message.document.mime_type or ""

        if not mime.startswith("video/"):
            await message.answer("Пожалуйста, отправьте видеофайл.")
            return

    if media.file_size and media.file_size > MAX_INPUT_SIZE:
        await message.answer(
            "Размер видео не должен превышать 300 MB."
        )
        return

    await message.answer(
        "✅ Видео получено.\n\n"
        "Откройте Mini App, чтобы выбрать стиль субтитров "
        "и запустить обработку.",
        reply_markup=main_keyboard(),
    )


# =========================================================
# API HEALTH
# =========================================================

@app.get("/")
async def home():
    return {
        "status": "ok",
        "service": "GAPCUT AI",
        "version": "2.1.0",
        "max_input_mb": 300,
        "max_duration_seconds": 120,
    }


@app.get("/health")
async def health():
    return {
        "status": "healthy",
        "service": "GAPCUT AI",
    }


# =========================================================
# MINI APP PROCESSING API
# =========================================================

@app.post("/api/process")
async def api_process(
    file: UploadFile = File(...),
    style: str = Form("viral"),
    language: str = Form("tg"),
    video_format: str = Form("9:16"),
    init_data: str = Form(...),
):
    user = validate_telegram_init_data(init_data)

    if not user:
        raise HTTPException(
            status_code=401,
            detail=(
                "Ошибка авторизации Telegram. "
                "Откройте Mini App через бота."
            ),
        )

    style = style.lower().strip()
    language = language.lower().strip()
    video_format = video_format.strip()

    if style not in STYLES:
        raise HTTPException(
            status_code=400,
            detail="Неверный стиль субтитров.",
        )

    if language not in ("tg", "ru", "both"):
        raise HTTPException(
            status_code=400,
            detail="Неверно выбран язык.",
        )

    if video_format not in ("9:16", "16:9", "1:1", "original"):
        raise HTTPException(
            status_code=400,
            detail="Неверный формат видео.",
        )

    filename = (file.filename or "video.mp4").lower()
    extension = Path(filename).suffix

    if extension not in ALLOWED_EXTENSIONS:
        raise HTTPException(
            status_code=400,
            detail="Поддерживаются MP4, MOV, M4V, WEBM и MKV.",
        )

    temp_dir = Path(tempfile.mkdtemp(prefix="gapcut_"))

    input_path = temp_dir / f"input{extension}"
    output_path = temp_dir / "gapcut_result.mp4"

    try:
        total_size = 0

        logger.info(
            "Upload started. Telegram user ID: %s",
            user.get("id"),
        )

        with input_path.open("wb") as output_file:
            while True:
                chunk = await file.read(UPLOAD_CHUNK_SIZE)

                if not chunk:
                    break

                total_size += len(chunk)

                if total_size > MAX_INPUT_SIZE:
                    raise HTTPException(
                        status_code=413,
                        detail="Размер видео не должен превышать 300 MB.",
                    )

                output_file.write(chunk)

        if total_size == 0:
            raise HTTPException(
                status_code=400,
                detail="Видеофайл пуст.",
            )

        logger.info(
            "Upload completed: %s bytes",
            total_size,
        )

        await process_video(
            input_path,
            output_path,
            style,
            video_format,
        )

        return FileResponse(
            path=str(output_path),
            media_type="video/mp4",
            filename="GAPCUT_AI.mp4",
            background=BackgroundTask(
                shutil.rmtree,
                temp_dir,
                ignore_errors=True,
            ),
        )

    except HTTPException:
        shutil.rmtree(temp_dir, ignore_errors=True)
        raise

    except Exception as error:
        logger.exception("Video processing failed")

        shutil.rmtree(temp_dir, ignore_errors=True)

        message = str(error)

        if "No space left on device" in message:
            message = "На сервере недостаточно свободного места."

        elif "ffmpeg" in message.lower():
            message = "Ошибка обработки видео. Проверьте формат файла."

        raise HTTPException(
            status_code=500,
            detail=message[:500],
        )

    finally:
        await file.close()


# =========================================================
# START SERVER + BOT
# =========================================================

async def main():
    config = uvicorn.Config(
        app,
        host="0.0.0.0",
        port=PORT,
        log_level="info",
        timeout_keep_alive=30,
    )

    server = uvicorn.Server(config)

    logger.info("GAPCUT AI 2.1.0 starting on port %s", PORT)

    try:
        await asyncio.gather(
            server.serve(),
            dp.start_polling(bot),
        )
    finally:
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
