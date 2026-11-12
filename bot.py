"""
GAPCUT AI 3.0.0 - backend + Telegram bot.

Stage 1 (backend only). web/index.html is NOT changed by this file.

Compatible endpoints
--------------------
GET  /                      service info
GET  /health                health check (also HEAD)
POST /api/process           one-shot mode used by the existing Mini App
                            (form fields: file, style, language,
                             video_format, init_data)  -> MP4

New endpoints (async jobs with progress + editable transcript)
--------------------------------------------------------------
POST   /api/jobs                 form: file, language, init_data -> {job_id}
GET    /api/jobs/{id}            status, progress, editable lines
POST   /api/jobs/{id}/render     form: style, video_format, lines (JSON, optional),
                                 accent (RRGGBB), size (s|m|l),
                                 position (auto|top|center), subtitles (1|0),
                                 init_data
GET    /api/jobs/{id}/download   final MP4
DELETE /api/jobs/{id}            remove the job and its files

init_data may be sent as a form field or as the X-Init-Data header
(GET/DELETE use the header, download also accepts ?init_data=).
"""

import asyncio
import difflib
import base64
import wave
import hashlib
import io
import hmac
import random
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import time
import uuid
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from urllib.parse import parse_qsl, quote

import httpx
import uvicorn
from dotenv import load_dotenv
from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    CallbackQuery,
    WebAppInfo,
    FSInputFile,
)
from fastapi import FastAPI, File, Form, Header, HTTPException, Query, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response
from starlette.background import BackgroundTask

try:
    import numpy as np
except ImportError:  # music and covers are switched off without numpy
    np = None
try:
    from PIL import Image, ImageDraw, ImageFont
except ImportError:
    Image = ImageDraw = ImageFont = None


# =========================================================
# CONFIG
# =========================================================

load_dotenv()

VERSION = "3.0.0"

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "").strip()
PORT = int(os.getenv("PORT", "10000"))

MINI_APP_URL = os.getenv("WEB_APP_URL", "https://gapcut-ai-web.vercel.app").strip()
SUPPORT_URL = os.getenv("SUPPORT_URL", "https://t.me/aikomron").strip()
CHANNEL_URL = os.getenv("CHANNEL_URL", "https://t.me/gapcutai").strip()

ALLOWED_ORIGINS = [
    origin.strip()
    for origin in os.getenv("ALLOWED_ORIGINS", MINI_APP_URL).split(",")
    if origin.strip()
]

GROQ_MODEL = "whisper-large-v3-turbo"
GROQ_MODEL_BEST = os.getenv("GROQ_MODEL_BEST", "whisper-large-v3").strip()
GROQ_URL = "https://api.groq.com/openai/v1/audio/transcriptions"

# Render Free: conservative limits.
MAX_INPUT_SIZE = 100 * 1024 * 1024
MAX_OUTPUT_SIZE = 150 * 1024 * 1024
MAX_DURATION = 60
UPLOAD_CHUNK_SIZE = 256 * 1024
GROQ_MAX_AUDIO_SIZE = 24 * 1024 * 1024

JOB_TTL_SECONDS = int(os.getenv("JOB_TTL_SECONDS", "86400"))
MAX_STORED_JOBS = int(os.getenv("MAX_STORED_JOBS", "10"))
PIXABAY_API_KEY = os.getenv("PIXABAY_API_KEY", "").strip()
CUSTOM_FONTS_DIR = "/usr/local/share/fonts/gapcut"
FONT_CANDIDATES = [
    # Sans-serif / modern
    ("Montserrat", 1), ("Inter", 1), ("Roboto", 1), ("Open Sans", 1),
    ("Noto Sans", 1), ("Golos Text", 1), ("Manrope", 1), ("Fira Sans", 1),
    ("PT Sans", 1), ("Source Sans 3", 1), ("Ubuntu", 1), ("Nunito", 1),
    ("Raleway", 1), ("Arial", 1), ("Tahoma", 1), ("Verdana", 1),
    ("Segoe UI", 1), ("Jost", 1), ("Work Sans", 1), ("IBM Plex Sans", 1),
    ("Gilroy", 1), ("TT Norms", 1), ("TT Commons", 1), ("Rubik", 1),
    ("Exo 2", 1), ("Roboto Condensed", 1), ("Fira Sans Condensed", 1),
    # Display / bold
    ("Oswald", 1), ("Bebas Neue", 1), ("Impact", 1), ("Roboto Slab", 1),
    # Serif
    ("Noto Serif", 1), ("Lora", 1), ("PT Serif", 1), ("Merriweather", 1),
    ("Playfair Display", 1), ("Times New Roman", 1), ("Georgia", 1),
    ("Bitter", 1), ("Crimson Text", 1), ("Literata", 1),
    ("IBM Plex Serif", 1), ("Source Serif Pro", 1),
    # Rounded / handwritten / mono
    ("Caveat", 0), ("Comfortaa", 1), ("Pacifico", 0), ("Neucha", 0),
    ("Marck Script", 0), ("Roboto Mono", 1), ("Fira Code", 1),
    ("DejaVu Sans", 1),
]
TAJIK_CHARSET = "493 4e3 49b 4ef 4b3 4b7 492 4e2 49a 4ee 4b2 4b6"
AVAILABLE_FONTS = [{"family": "DejaVu Sans", "bold": 1}]
KEYWORDS_LLM = os.getenv("KEYWORDS_LLM", "1").strip() != "0"
LLM_BASE_URL = os.getenv("LLM_BASE_URL", "").strip().rstrip("/")
LLM_API_KEY = os.getenv("LLM_API_KEY", "").strip()
LLM_MODEL = os.getenv("LLM_MODEL", os.getenv("KEYWORD_MODEL", "auto")).strip() or "auto"
LLM_CHAT_URL = f"{LLM_BASE_URL}/chat/completions" if LLM_BASE_URL else ""
FFMPEG_PRESET = os.getenv("FFMPEG_PRESET", "ultrafast").strip() or "ultrafast"

BASE_DIR = Path(tempfile.gettempdir()) / "gapcut_ai"
FONTS_DIR = Path(os.getenv("FONTS_DIR", str(Path(__file__).resolve().parent / "fonts")))

ALLOWED_EXTENSIONS = {".mp4", ".mov", ".m4v", ".webm", ".mkv", ".avi", ".flv", ".wmv", ".3gp", ".3g2", ".mpeg", ".mpg", ".ts", ".mts", ".m2ts", ".vob", ".ogv", ".mxf", ".asf", ".f4v"}
ALLOWED_STYLES = {"viral", "brandly", "classic", "cinema", "podcast", "karaoke", "creator", "keyword", "minimal", "dynamic"}
ALLOWED_FORMATS = {"9:16", "16:9", "1:1", "original"}
ALLOWED_LANGUAGES = {"tg", "ru", "tg_ru"}
LANGUAGE_ALIASES = {"both": "tg_ru", "tg+ru": "tg_ru", "tg-ru": "tg_ru"}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("gapcut-ai")

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN не указан в Environment Variables.")
if not GROQ_API_KEY:
    logger.warning("GROQ_API_KEY не указан: распознавание речи работать не будет.")
if not (LLM_API_KEY and LLM_CHAT_URL):
    logger.warning("FreeLLMAPI не настроен: AI-анализ будет использовать резервные правила.")
else:
    logger.info("FreeLLMAPI configured; model=%s", LLM_MODEL)

bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()

# Only one heavy job (transcription or render) at a time on a small server.
PROCESS_LOCK = asyncio.Lock()
IMAGE_GENERATION_LOCK = asyncio.Lock()
IMAGE_GENERATION_LAST_REQUEST = 0.0


class UserError(Exception):
    """Error whose message is safe to show to the user."""

    def __init__(self, message, status_code=400):
        super().__init__(message)
        self.message = message
        self.status_code = status_code


# =========================================================
# TELEGRAM BOT
# =========================================================

def main_keyboard():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🎬 Начать монтаж",
                    web_app=WebAppInfo(url=MINI_APP_URL),
                )
            ],
            [
                InlineKeyboardButton(
                    text="📁 Мои ролики",
                    callback_data="my_videos",
                )
            ],
            [
                InlineKeyboardButton(text="💬 Поддержка", url=SUPPORT_URL),
                InlineKeyboardButton(text="📢 Наш канал", url=CHANNEL_URL),
            ],
        ]
    )


@dp.message(CommandStart())
async def start_handler(message: Message):
    text = (
        "🎬 <b>GAPCUT AI</b>\n\n"
        "Автоматические субтитры для ваших видео.\n\n"
        "✨ Распознавание речи: таджикский, русский и смешанная речь\n"
        "🎨 5 стилей субтитров\n"
        "📱 Форматы Reels, TikTok и YouTube\n\n"
        "📦 Максимальный размер: 100 MB\n"
        "⏱ Максимальная длительность: 60 секунд\n\n"
        "Нажмите «Начать монтаж», чтобы начать."
    )
    await message.answer(text, reply_markup=main_keyboard())


@dp.message(Command("help"))
async def help_handler(message: Message):
    text = (
        "ℹ️ <b>GAPCUT AI — Помощь</b>\n\n"
        "1. Нажмите «Монтаж».\n"
        "2. Выберите видео.\n"
        "3. Выберите язык, стиль и формат.\n"
        "4. Запустите обработку.\n\n"
        "📦 Максимальный размер: 100 MB\n"
        "⏱ Максимальная длительность: 60 секунд\n\n"
        f"💬 Поддержка: {SUPPORT_URL}\n"
        f"📢 Канал: {CHANNEL_URL}"
    )
    await message.answer(text, reply_markup=main_keyboard())


@dp.message(F.video | F.document)
async def video_handler(message: Message):
    media = message.video or message.document

    if message.document:
        mime = message.document.mime_type or ""
        if not mime.startswith("video/"):
            await message.answer("Пожалуйста, отправьте видеофайл.")
            return

    if media.file_size and media.file_size > MAX_INPUT_SIZE:
        await message.answer("Размер видео не должен превышать 100 MB.")
        return

    await message.answer(
        "✅ Видео получено.\n\n"
        "Откройте Mini App, чтобы выбрать язык, стиль субтитров "
        "и запустить обработку.",
        reply_markup=main_keyboard(),
    )


@dp.callback_query(F.data == "my_videos")
async def my_videos_handler(callback: CallbackQuery):
    user_id = str(callback.from_user.id)
    user_jobs = [
        job for job in JOBS.values()
        if str(job.get("user_id")) == user_id
    ]
    user_jobs.sort(key=lambda job: job.get("updated", 0), reverse=True)

    await callback.answer()

    if not user_jobs:
        await callback.message.answer(
            "📁 У вас пока нет роликов.\n\n"
            "Нажмите «🎬 Начать монтаж», чтобы создать первое видео.",
            reply_markup=main_keyboard(),
        )
        return

    status_labels = {
        "queued": "⏳ В очереди",
        "transcribing": "🎧 Распознавание речи",
        "transcribed": "✍️ Текст готов",
        "rendering": "🎬 Монтаж",
        "done": "✅ Готово",
        "error": "❌ Ошибка",
    }
    result_lines = ["📁 <b>Мои ролики</b>", ""]
    for index, job in enumerate(user_jobs[:10], start=1):
        status = status_labels.get(
            job.get("status"),
            str(job.get("status", "Неизвестно")),
        )
        progress = int(job.get("progress") or 0)
        result_lines.append(
            f"{index}. {status} — {progress}%\n"
            f"   ID: <code>{str(job.get('id', ''))[:12]}</code>"
        )
        if job.get("status") == "error" and job.get("error"):
            result_lines.append(f"   {job['error']}")
        result_lines.append("")

    await callback.message.answer(
        "\n".join(result_lines),
        reply_markup=main_keyboard(),
    )


async def run_polling_forever():
    """Telegram polling that never takes the HTTP API down with it."""
    while True:
        try:
            await bot.delete_webhook(drop_pending_updates=False)
            await dp.start_polling(bot, handle_signals=False)
            return
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Telegram polling stopped, restarting in 5 s")
            await asyncio.sleep(5)


# =========================================================
# TELEGRAM MINI APP AUTH
# =========================================================

def validate_telegram_init_data(init_data):
    if not init_data:
        return None

    try:
        parsed = dict(parse_qsl(init_data, keep_blank_values=True))
        received_hash = parsed.pop("hash", None)
        if not received_hash:
            return None

        data_check_string = "\n".join(
            f"{key}={value}" for key, value in sorted(parsed.items())
        )

        secret_key = hmac.new(
            b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256
        ).digest()
        calculated_hash = hmac.new(
            secret_key, data_check_string.encode(), hashlib.sha256
        ).hexdigest()

        if not hmac.compare_digest(calculated_hash, received_hash):
            return None

        auth_date = int(parsed.get("auth_date", "0"))
        now = int(time.time())
        if auth_date <= 0 or auth_date > now + 60 or now - auth_date > 86400:
            return None

        user = json.loads(parsed.get("user", "{}"))
        if not isinstance(user, dict) or not user.get("id"):
            return None

        return user
    except Exception:
        logger.exception("Telegram initData validation error")
        return None


def require_user(*candidates):
    for value in candidates:
        user = validate_telegram_init_data(value)
        if user:
            return user
    raise HTTPException(
        status_code=401,
        detail="Откройте Mini App через Telegram-бота.",
    )


# =========================================================
# PROCESS HELPERS (FFmpeg / FFprobe)
# =========================================================

async def run_command(command, timeout=300, cwd=None, capture=False):
    """Run a command with conservative FFmpeg threading for small Render instances."""
    command = [str(part) for part in command]
    if command and Path(command[0]).name == "ffmpeg":
        # FFmpeg otherwise creates decoder/filter worker threads based on host CPU
        # count; on a 512 MB instance their frame buffers can trigger an OOM restart.
        command[1:1] = [
            "-threads", "1",
            "-filter_threads", "1",
            "-filter_complex_threads", "1",
        ]
    with tempfile.TemporaryFile(mode="w+b") as error_file:
        process = await asyncio.create_subprocess_exec(
            *command,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=error_file,
            cwd=str(cwd) if cwd else None,
        )
        try:
            await asyncio.wait_for(process.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()
            raise UserError(
                "Время обработки видео истекло. Попробуйте более короткое видео.",
                504,
            )

        error_file.seek(0, os.SEEK_END)
        size = error_file.tell()
        error_file.seek(max(0, size - (200000 if capture else 3000)))
        error_text = error_file.read().decode("utf-8", errors="replace")

    if process.returncode != 0:
        logger.error("Command failed (%s): %s", command[0], error_text[-3000:])
        raise UserError(
            "Ошибка FFmpeg. Проверьте видео и попробуйте другой файл.", 500
        )

    return error_text


async def get_video_info(video_path):
    process = await asyncio.create_subprocess_exec(
        "ffprobe", "-v", "error",
        "-show_format", "-show_streams",
        "-of", "json",
        str(video_path),
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        stdout, _ = await asyncio.wait_for(process.communicate(), timeout=30)
    except asyncio.TimeoutError:
        process.kill()
        await process.wait()
        raise UserError("Не удалось прочитать видеофайл.")

    if process.returncode != 0:
        raise UserError("Не удалось прочитать видеофайл. Проверьте формат.")

    try:
        info = json.loads(stdout.decode("utf-8"))
        duration = float(info.get("format", {}).get("duration", 0))
    except (ValueError, TypeError):
        raise UserError("Не удалось определить длительность видео.")

    if duration <= 0:
        raise UserError("Не удалось определить длительность видео.")

    return info, duration


def probe_summary(info):
    """Return (has_video, has_audio, display_width, display_height)."""
    has_audio = False
    video = None

    for stream in info.get("streams", []):
        kind = stream.get("codec_type")
        if kind == "audio":
            has_audio = True
        elif kind == "video" and video is None:
            if stream.get("disposition", {}).get("attached_pic") == 1:
                continue
            video = stream

    if video is None:
        return False, has_audio, 0, 0

    width = int(video.get("width") or 0)
    height = int(video.get("height") or 0)

    rotation = 0
    tags = video.get("tags") or {}
    if "rotate" in tags:
        with suppress(ValueError, TypeError):
            rotation = int(float(tags["rotate"]))
    for side in video.get("side_data_list") or []:
        if "rotation" in side:
            with suppress(ValueError, TypeError):
                rotation = int(float(side["rotation"]))

    if abs(rotation) % 180 == 90:
        width, height = height, width

    return True, has_audio, width, height


def even(value):
    value = int(round(value))
    return max(2, value - (value % 2))


def get_output_size(video_format, display_width, display_height):
    if video_format == "9:16":
        return 720, 1280
    if video_format == "16:9":
        return 1280, 720
    if video_format == "1:1":
        return 720, 720

    # original: keep the aspect ratio, never upscale, fit in 1280x1280.
    width = display_width or 720
    height = display_height or 1280
    factor = min(1.0, 1280 / width, 1280 / height)
    return even(width * factor), even(height * factor)


def get_video_filter(video_format, width, height):
    if video_format == "original":
        return f"scale={width}:{height},setsar=1"
    return (
        f"scale={width}:{height}:force_original_aspect_ratio=increase,"
        f"crop={width}:{height},setsar=1"
    )


# =========================================================
# SPEECH RECOGNITION (Groq Whisper)
# =========================================================

TG_PROMPT = "Ин гуфтори тоҷикӣ аст. Матнро танҳо бо алифбои кириллӣ нависед. Ҳарфҳои ғ, ӣ, қ, ӯ, ҳ, ҷ-ро дуруст истифода баред. Калимаҳои тоҷикиро ба шакли дурусти имлоӣ нависед, на бо имлои русӣ ё фонетикӣ. Ба ибораҳои гуфтугӯӣ ва калимаҳои қарзӣ диққат диҳед. Намунаҳои имло: имрӯз, тоҷикӣ, мегӯям, мешавад, ки, курси, старти курси ман. Алифбои форсӣ ё арабиро истифода набаред. Салом! Имрӯз мо бо забони тоҷикӣ сӯҳбат мекунем."
RU_PROMPT = "Привет! Сегодня мы говорим по-русски."
TG_LETTERS = set("ғӣқӯҳҷҒӢҚӮҲҶ")


async def extract_audio(video_path, audio_path, work_dir):
    await run_command(
        [
            "ffmpeg", "-y", "-nostdin", "-hide_banner", "-loglevel", "error",
            "-threads", "1",
            "-i", str(video_path),
            "-map", "0:a:0",
            "-vn",
            "-ac", "1",
            "-ar", "16000",
            "-b:a", "48k",
            "-t", str(MAX_DURATION),
            "-c:a", "libmp3lame",
            str(audio_path),
        ],
        timeout=180,
        cwd=work_dir,
    )

    if not Path(audio_path).exists() or Path(audio_path).stat().st_size == 0:
        raise UserError("Не удалось извлечь звук из видео.", 500)

    if Path(audio_path).stat().st_size > GROQ_MAX_AUDIO_SIZE:
        raise UserError("Аудиофайл превышает установленный лимит Groq.", 413)


async def groq_transcribe(client, audio_path, language, prompt, model=None):
    data = {
        "model": model or GROQ_MODEL,
        "response_format": "verbose_json",
        "temperature": "0",
        "timestamp_granularities[]": ["word", "segment"],
    }
    if language and language != "tg":
        data["language"] = language
    if prompt:
        data["prompt"] = prompt

    headers = {"Authorization": f"Bearer {GROQ_API_KEY}"}
    last_status = None

    for attempt in range(1, 4):
        with open(audio_path, "rb") as audio_file:
            response = await client.post(
                GROQ_URL,
                headers=headers,
                data=data,
                files={"file": (Path(audio_path).name, audio_file, "audio/mpeg")},
            )

        last_status = response.status_code

        if response.status_code == 200:
            return response.json()

        logger.error(
            "Groq API error %s (attempt %s): %s",
            response.status_code, attempt, response.text[:800],
        )

        if (
            response.status_code in (400, 404)
            and data["model"] != GROQ_MODEL
            and "model" in response.text.lower()
        ):
            data["model"] = GROQ_MODEL  # the better model is unavailable
            continue

        if (
            response.status_code == 400
            and "language" in data
            and "language" in response.text.lower()
        ):
            # The language code was rejected: retry with auto-detection.
            data.pop("language", None)
            continue

        if response.status_code in (429, 500, 502, 503, 504) and attempt < 3:
            retry_after = response.headers.get("retry-after", "")
            try:
                delay = min(20.0, max(1.0, float(retry_after)))
            except ValueError:
                delay = 2.0 * attempt
            await asyncio.sleep(delay)
            continue

        break

    if last_status in (401, 403):
        raise UserError("Ключ Groq API неверный или не имеет доступа.", 502)
    if last_status == 429:
        raise UserError("Лимит Groq API исчерпан. Попробуйте через минуту.", 503)
    raise UserError("Ошибка распознавания речи. Попробуйте позже.", 502)


def normalize_words(words):
    """Sort, drop empty tokens and make timings monotonic."""
    cleaned = []
    for item in words:
        word = str(item.get("word", "")).strip()
        if not word:
            continue
        try:
            start = max(0.0, float(item.get("start", 0)))
            end = float(item.get("end", start + 0.2))
        except (TypeError, ValueError):
            continue
        entry = {"word": word, "start": start, "end": max(end, start + 0.04)}
        if "key" in item:
            entry["key"] = bool(item["key"])
        for extra in ("lang", "low", "fixed", "orig"):
            if extra in item:
                entry[extra] = item[extra]
        cleaned.append(entry)

    cleaned.sort(key=lambda w: w["start"])

    for index in range(len(cleaned) - 1):
        current, following = cleaned[index], cleaned[index + 1]
        if current["end"] > following["start"]:
            current["end"] = max(current["start"] + 0.03, following["start"])

    return cleaned


def parse_groq_result(result, offset=0.0, prompt=""):
    segments = result.get("segments") or []
    silent_ranges = []
    weighted_logprob = 0.0
    weighted_time = 0.0

    for segment in segments:
        try:
            seg_start = float(segment.get("start", 0))
            seg_end = float(segment.get("end", seg_start + 0.5))
        except (TypeError, ValueError):
            continue
        span = max(seg_end - seg_start, 0.01)
        logprob = segment.get("avg_logprob")
        no_speech = float(segment.get("no_speech_prob") or 0)

        if no_speech > 0.6 and (logprob is None or logprob < -1.0):
            silent_ranges.append((seg_start, seg_end))
            continue
        if logprob is not None:
            weighted_logprob += float(logprob) * span
            weighted_time += span

    avg_logprob = weighted_logprob / weighted_time if weighted_time else -5.0
    seg_scores = []
    for segment in segments:
        try:
            seg_scores.append((
                float(segment.get("start", 0)), float(segment.get("end", 0)),
                float(segment.get("avg_logprob")),
            ))
        except (TypeError, ValueError):
            continue

    def in_silence(moment):
        return any(a <= moment <= b for a, b in silent_ranges)

    words = []
    raw_words = result.get("words") or []

    if raw_words:
        for item in raw_words:
            try:
                start = float(item.get("start", 0))
                end = float(item.get("end", start + 0.2))
            except (TypeError, ValueError):
                continue
            if in_silence((start + end) / 2):
                continue
            words.append({"word": item.get("word", ""), "start": start, "end": end})
    else:
        for segment in segments:
            try:
                seg_start = float(segment.get("start", 0))
                seg_end = float(segment.get("end", seg_start + 1))
            except (TypeError, ValueError):
                continue
            if in_silence((seg_start + seg_end) / 2):
                continue
            parts = str(segment.get("text", "")).split()
            if not parts:
                continue
            step = max((seg_end - seg_start) / len(parts), 0.05)
            for index, part in enumerate(parts):
                words.append({
                    "word": part,
                    "start": seg_start + index * step,
                    "end": seg_start + (index + 1) * step,
                })

    words = normalize_words(words)
    text = " ".join(w["word"] for w in words)

    # Whisper sometimes echoes the prompt when there is no speech.
    if prompt and text.strip().lower() == prompt.strip().lower():
        words, text = [], ""

    for item in words:
        middle = (item["start"] + item["end"]) / 2
        item["low"] = any(a <= middle <= b and lp < -0.9 for a, b, lp in seg_scores)
        item["start"] += offset
        item["end"] += offset

    return {"words": words, "text": text, "avg_logprob": avg_logprob}


def script_score(parsed):
    text = parsed["text"]
    letters = [c for c in text if c.isalpha()]
    if not letters:
        return -9.0

    cyrillic = sum("\u0400" <= c <= "\u04ff" for c in letters) / len(letters)
    score = parsed["avg_logprob"]
    if cyrillic < 0.8:
        score -= 2.0
    if any(c in TG_LETTERS for c in text):
        score += 0.25
    return score


async def detect_silences(audio_path, work_dir):
    stderr = await run_command(
        [
            "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "info",
            "-i", str(audio_path),
            "-af", "silencedetect=noise=-35dB:d=0.3",
            "-f", "null", "-",
        ],
        timeout=60,
        cwd=work_dir,
        capture=True,
    )

    starts = [float(x) for x in re.findall(r"silence_start:\s*(-?[\d.]+)", stderr)]
    ends = [float(x) for x in re.findall(r"silence_end:\s*(-?[\d.]+)", stderr)]
    return [
        (max(0.0, s), e) for s, e in zip(starts, ends) if e > s
    ]


def plan_chunks(duration, silences):
    """Cut points are placed in silences, chunks are about 8-18 s long."""
    if duration <= 18:
        return [(0.0, duration)]

    mids = [(s + e) / 2 for s, e in silences]
    chunks = []
    position = 0.0

    while duration - position > 18:
        candidates = [m for m in mids if position + 8 < m < position + 18]
        if candidates:
            cut = min(candidates, key=lambda m: abs(m - (position + 13)))
        else:
            cut = position + 14
        chunks.append((position, cut))
        position = cut

    chunks.append((position, duration))
    return chunks


# =========================================================
# TAJIK / RUSSIAN TRANSCRIPT CORRECTOR (stage 1)
# =========================================================
#
# Rules that keep subtitles safe:
# * only single words are replaced (1:1), so word timings never change;
# * a fix must look like the original word (similarity check);
# * every changed word keeps its original in word["orig"] so the user can undo.

CORRECT_LLM = os.getenv("CORRECT_LLM", "1").strip() != "0"
TG_MAP = str.maketrans({
    "ғ": "г", "Ғ": "Г", "қ": "к", "Қ": "К", "ҳ": "х", "Ҳ": "Х",
    "ҷ": "ч", "Ҷ": "Ч", "ӣ": "и", "Ӣ": "И", "ӯ": "у", "Ӯ": "У",
})

# Common Tajik words that contain the letters ғ ӣ қ ӯ ҳ ҷ. Speech recognition often
# writes the plain Russian letter (г, к, х, ч, и, у) instead. Words that collide with
# frequent Russian words (куча, чудо, пуст, гор, ...) are left out on purpose.
TAJIK_WORDS = """
ҳам ҳамин ҳамон ҳама ҳамаи ҳамеша ҳамроҳ ҳамчун ҳамчунин ҳамкор ҳамкорӣ ҳамсоя ҳар ҳатто
ҳанӯз ҳоло ҳозир ҳаст ҳастанд ҳастам ҳастем ҳастед ҳастӣ ҳеҷ ҳаргиз ҳатмӣ ҳаёт ҳақ ҳақиқат
ҳақиқӣ ҳадаф ҳикоя ҳисоб ҳуқуқ ҳукумат ҳунар ҳунарманд ҳодиса ҳафта ҳафт ҳашт ҳазор ҳаво
ҳайвон ҳайрон ҳавлӣ ҳудуд ҳиссиёт ҳифз ҳосил ӯро ӯст ӯзбек
ғоя ғояҳо ғайр ғайриоддӣ ғолиб ғалаба ғамгин ғам ғизо ғазаб ғарб ғурур ғалат ғазал ғайрат
ғамхорӣ ғанӣ
қадам қарор қувва қувват қисм қисса қимат қиммат қалам қалб қатор қобилият қонун қариб
қадимӣ қавӣ қатъӣ қайд қаҳрамон қаҳва қурбон қасам қоида қисмат қарз қулай қавл қаноат
қудрат қаҳрамонӣ
ҷаҳон ҷаҳонӣ ҷой ҷои ҷавон ҷавонон ҷавонӣ ҷавоб ҷамъ ҷуфт ҷисм ҷомеа ҷашн ҷанг ҷаҳд ҷаноб
ҷанобон ҷӯра ҷуръат ҷазо ҷамъият ҷамъиятӣ ҷумҳурӣ ҷумҳурии ҷустуҷӯ ҷон ҷони ҷонам ҷолиб
ҷиддӣ ҷиддият ҷойгир ҷойгоҳ ҷорӣ ҷаҳиш ҷалб ҷудоӣ
зиндагӣ оддӣ миллӣ умумӣ расмӣ воқеӣ табиӣ тоҷикӣ тоҷик тоҷикон англисӣ дӯстӣ хушбахтӣ
хубӣ зебоӣ осонӣ душворӣ кӯтоҳӣ озодӣ бародарӣ касбӣ иқтисодӣ фарҳангӣ сиёсӣ иҷтимоӣ
молиявӣ донишҷӯ донишҷӯён донишгоҳ донишманд омӯзгор омӯзиш омӯхтан омӯзонидан
рӯз рӯзи рӯзҳо рӯзона рӯй рӯи рӯйхат рӯзнома рӯзгор рӯшан рӯшноӣ пӯшидан пӯшок пӯшида
гӯш гӯянда гӯям гӯед гӯӣ гӯем мегӯям мегӯед мегӯянд мегӯӣ мегӯем хӯрок хӯрдан хӯроки
мӯй мӯҳлат мӯҳр кӯдак кӯдакон кӯшиш кӯмак кӯҳна кӯтоҳ кӯҳ кӯҳҳо сӯҳбат нӯҳ бӯса дӯст
дӯстон дӯстам дӯкон дӯхтан тӯҳфа тӯй шӯхӣ шӯро шӯр шӯрбо шӯриш ҳӯшёр зӯр нӯшидан
нӯшокӣ сӯзан
мекунӣ мебинӣ медонӣ мехоҳӣ мешавӣ мекашӣ мешунавӣ мехонӣ менависӣ мерасӣ мефаҳмӣ мегирӣ
медиҳӣ мебарӣ меояӣ мешинӣ мехӯрӣ мехоҳам мехоҳем мехоҳед мехоҳанд фаҳмидан фаҳмед фаҳмидам
роҳ роҳи роҳнамо раҳбар раҳбарӣ раҳо оҳиста оҳанг баҳо баҳор баҳс шаҳр шаҳри шаҳрвандӣ
шаҳодат саҳифа саҳна саҳар аҳамият аҳолӣ маҳсулот маҳсул маҳз маҳорат маҳдуд муҳим
муҳимтар муҳаббат муҳандис таҳлил таҳия таҷриба таҳсил таҳқиқ тарҳ тарҷума тиҷорат
тиҷоратӣ маҷмӯа маҷмӯъ мақсад мақола мақом миқдор маъно маъқул маълумот маълум нақша нақл
нақд нуқта нуқтаи таъсир таъриф тағйир тақрибан таълим таълиф фақат фарҳанг фурӯш
фурӯхтан фурӯшанда фурӯшгоҳ ҳуҷҷат ҳуҷҷати иқтидор иқтисод иҷозат иҷро иҷрои
Тоҷикистон Хуҷанд Ҳисор Кӯлоб Қӯрғонтеппа
""".split()


def core_token(text):
    return re.sub(r"[\W_]+", "", str(text), flags=re.UNICODE)


def tg_key(word):
    return core_token(word).lower().translate(TG_MAP)


def build_tajik_index():
    index = {}
    for entry in TAJIK_WORDS:
        key = tg_key(entry)
        if len(key) < 3:
            continue
        if key in index and index[key] != entry.lower():
            index[key] = None  # ambiguous, never used
        else:
            index[key] = entry.lower()
    return index


TAJIK_INDEX = build_tajik_index()


def clean_glossary(text):
    terms = []
    for raw in re.split(r"[,;\n]+", str(text or "")):
        term = raw.strip()[:40]
        if len(term) >= 2 and term.lower() not in [t.lower() for t in terms]:
            terms.append(term)
        if len(terms) >= 40:
            break
    return terms


def with_glossary(prompt, glossary):
    if not glossary:
        return prompt
    return (prompt + " " + ", ".join(glossary))[:400]


def accept_fix(old, new, min_similarity=0.6):
    old_core, new_core = core_token(old).lower(), core_token(new).lower()
    if not new_core or len(new.strip()) > 40 or len(new.split()) != 1:
        return False
    if old_core == new_core:
        return False
    a, b = old_core.translate(TG_MAP), new_core.translate(TG_MAP)
    if difflib.SequenceMatcher(None, a, b).ratio() < min_similarity:
        return False
    return 0.5 <= len(new_core) / max(1, len(old_core)) <= 2.0


def apply_fix(item, new_core):
    """Replace the word text, keep punctuation and capital letter, remember the original."""
    old = item["word"]
    match = re.match(r"^(\W*)(.*?)(\W*)$", old, flags=re.UNICODE)
    lead, core, trail = match.group(1), match.group(2), match.group(3)
    if core[:1].isupper() and new_core[:1].islower():
        new_core = new_core[:1].upper() + new_core[1:]
    item.setdefault("orig", old)
    item["word"] = lead + new_core + trail
    item["fixed"] = True


def dictionary_fixes(words):
    count = 0

    # High-confidence corrections observed in Tajik speech recognition.
    # Keep a strict one-token-to-one-token mapping so subtitle timestamps
    # remain aligned with the original audio.
    common_tajik_fixes = {
        "ке": "ки",
        "имруз": "имрӯз",
        "експерти": "экспертӣ",
        "експерті": "экспертӣ",
        "стартіг": "старти",
        "стартиг": "старти",
        "курсі": "курси",
    }

    for item in words:
        if item.get("lang") != "tg":
            continue

        core = core_token(item["word"])
        direct = common_tajik_fixes.get(core.lower())
        if direct and direct != core:
            apply_fix(item, direct)
            count += 1
            continue

        key = tg_key(item["word"])
        target = TAJIK_INDEX.get(key)
        if not target or len(key) < 3 or core.lower() == target:
            continue
        if accept_fix(item["word"], target):
            apply_fix(item, target)
            count += 1
    return count


def glossary_fixes(words, glossary):
    terms = [t for t in glossary if len(t.split()) == 1 and len(t) >= 3]
    count = 0
    for item in words:
        core = core_token(item["word"])
        if len(core) < 3 or item.get("fixed"):
            continue
        for term in terms:
            same = tg_key(core) == tg_key(term)
            close = (
                len(core) >= 4
                and abs(len(core) - len(term)) <= 2
                and difflib.SequenceMatcher(
                    None, tg_key(core), tg_key(term)
                ).ratio() >= 0.8
            )
            if (same or close) and core != term and accept_fix(item["word"], term):
                apply_fix(item, term)
                count += 1
                break
    return count


async def llm_fixes(words, language, glossary):
    """Ask the LLM for single-word spelling fixes. Returns {index: new_word}."""
    if not (CORRECT_LLM and LLM_API_KEY and LLM_CHAT_URL) or len(words) < 3:
        return {}
    numbered = " ".join(f"{i}:{w['word']}" for i, w in enumerate(words[:260]))
    hint = ""
    if glossary:
        hint = " Preferred spellings of names and terms: " + ", ".join(glossary) + "."
    payload = {
        "model": LLM_MODEL,
        "temperature": 0,
        "response_format": {"type": "json_object"},
        "messages": [
            {
                "role": "system",
                "content": (
                    "You are an expert proofreader of spoken Tajik (standard Tajik Cyrillic) and Russian ASR transcripts. "
                    "The input is a sequence of numbered word tokens in original order. Use surrounding words as sentence context, not isolated spelling guesses. "
                    "Correct misspelled Tajik words, phonetic Russian-letter spellings of Tajik (especially ғ, ӣ, қ, ӯ, ҳ, ҷ), and obvious speech-recognition substitutions. "
                    "Prefer natural, grammatically correct Tajik while preserving the speaker's actual meaning. Keep valid Russian words in mixed-language speech. "
                    "Do not translate, paraphrase, formalize colloquial speech, invent content, or change correct words just for style. "
                    "Replace a token with its corrected one-word form only; never merge, split, add or remove tokens because each token has a fixed timestamp. "
                    "Preserve punctuation and capitalization where possible. Examples: имрӯз, тоҷикӣ, мешавад, ки, мегӯям, курси, старти курси ман (only when those words were spoken). "
                    "If uncertain, leave the token unchanged." + hint + " Answer only with JSON: "
                    '{"fixes":[{"i":word index,"w":"corrected word"}]}. '
                    'Return {"fixes":[]} if nothing is clearly wrong.'                ),
            },
            {"role": "user", "content": numbered},
        ],
    }
    try:
        async with httpx.AsyncClient(timeout=25.0) as client:
            response = await client.post(
                LLM_CHAT_URL,
                headers={"Authorization": f"Bearer {LLM_API_KEY}"},
                json=payload,
            )
        if response.status_code != 200:
            return {}
        raw = json.loads(response.json()["choices"][0]["message"]["content"])
        out = {}
        for fix in raw.get("fixes", []):
            index = fix.get("i")
            new = str(fix.get("w", "")).strip()
            if isinstance(index, int) and 0 <= index < len(words) and new:
                out[index] = new
        return out
    except Exception:
        logger.warning("Spelling LLM failed", exc_info=True)
        return {}


async def correct_transcript(words, language, glossary):
    """Dictionary, glossary and LLM spelling fixes. Never changes timings or word count."""
    changed = dictionary_fixes(words)
    changed += glossary_fixes(words, glossary or [])
    fixes = await llm_fixes(words, language, glossary or [])
    limit = max(3, int(len(words) * 0.25))
    for index, new in sorted(fixes.items())[:limit]:
        item = words[index]
        if item.get("fixed"):
            continue
        core = core_token(new)
        if accept_fix(item["word"], core, min_similarity=0.45):
            apply_fix(item, core)
            changed += 1
    logger.info("Spelling fixes applied: %s", changed)
    return changed




async def transcribe_video(audio_path, work_dir, language, audio_duration, report,
                           model=None, glossary=None):
    """Return (words, detected_ranges)."""
    if not GROQ_API_KEY:
        raise UserError("GROQ_API_KEY не настроен на сервере.", 503)

    timeout = httpx.Timeout(180.0, connect=20.0, write=60.0, read=180.0)

    async with httpx.AsyncClient(timeout=timeout) as client:
        if language in ("tg", "ru"):
            prompt = with_glossary(TG_PROMPT if language == "tg" else RU_PROMPT, glossary)
            report(30, "Распознаём речь")
            result = await groq_transcribe(client, audio_path, language, prompt, model)
            parsed = parse_groq_result(result, 0.0, prompt)
            words = parsed["words"]
            for item in words:
                item["lang"] = language
            detected = [{"start": 0.0, "end": audio_duration, "lang": language}]
        else:
            report(25, "Анализируем паузы")
            silences = await detect_silences(audio_path, work_dir)
            chunks = plan_chunks(audio_duration, silences)

            words = []
            detected = []

            for index, (start, end) in enumerate(chunks):
                report(
                    30 + int(55 * index / max(1, len(chunks))),
                    f"Распознаём речь ({index + 1}/{len(chunks)})",
                )

                if len(chunks) == 1:
                    chunk_path = Path(audio_path)
                else:
                    chunk_path = Path(work_dir) / f"chunk_{index}.mp3"
                    await run_command(
                        [
                            "ffmpeg", "-y", "-nostdin", "-hide_banner",
                            "-loglevel", "error", "-threads", "1",
                            "-ss", f"{start:.3f}", "-t", f"{end - start:.3f}",
                            "-i", str(audio_path),
                            "-c:a", "libmp3lame", "-b:a", "48k",
                            str(chunk_path),
                        ],
                        timeout=60,
                        cwd=work_dir,
                    )

                tg_prompt = with_glossary(TG_PROMPT, glossary)
                ru_prompt = with_glossary(RU_PROMPT, glossary)
                tg_raw, ru_raw = await asyncio.gather(
                    groq_transcribe(client, chunk_path, "tg", tg_prompt, model),
                    groq_transcribe(client, chunk_path, "ru", ru_prompt, model),
                )
                tg_parsed = parse_groq_result(tg_raw, start, tg_prompt)
                ru_parsed = parse_groq_result(ru_raw, start, ru_prompt)

                if not tg_parsed["words"] and not ru_parsed["words"]:
                    continue
                if not ru_parsed["words"]:
                    chosen, lang = tg_parsed, "tg"
                elif not tg_parsed["words"]:
                    chosen, lang = ru_parsed, "ru"
                elif script_score(tg_parsed) >= script_score(ru_parsed):
                    chosen, lang = tg_parsed, "tg"
                else:
                    chosen, lang = ru_parsed, "ru"

                for item in chosen["words"]:
                    item["lang"] = lang
                words.extend(chosen["words"])
                detected.append({"start": start, "end": end, "lang": lang})

    words = normalize_words(words)
    if not words:
        raise UserError("Не удалось распознать речь в видео.", 422)

    report(85, "Проверяем орфографию")
    await correct_transcript(words, language, glossary)
    report(90, "Выбираем ключевые слова")
    await mark_keywords(words)
    return words, detected


# =========================================================
# FONTS (only fonts that contain all Tajik letters are offered)
# =========================================================

def fc_families(pattern, want_file=False):
    fmt = "%{family}|%{file}\n" if want_file else "%{family}\n"
    result = subprocess.run(
        ["fc-list", pattern, "-f", fmt],
        capture_output=True, text=True, timeout=20,
    )
    return [line for line in result.stdout.splitlines() if line.strip()]


def detect_fonts():
    found = []
    seen = set()
    for family, bold in FONT_CANDIDATES:
        try:
            lines = fc_families(f"{family}:charset={TAJIK_CHARSET}")
        except Exception:
            continue
        names = {n.strip().lower() for line in lines for n in line.split(",")}
        if family.lower() in names and family not in seen:
            found.append({"family": family, "bold": bold})
            seen.add(family)
    try:
        for line in fc_families(f":charset={TAJIK_CHARSET}", want_file=True):
            names, _, path = line.partition("|")
            if path.startswith(CUSTOM_FONTS_DIR):
                family = names.split(",")[0].strip()
                if family and family not in seen:
                    found.append({"family": family, "bold": 0})
                    seen.add(family)
    except Exception:
        pass
    return found or [{"family": "DejaVu Sans", "bold": 1}]


def resolve_font(name):
    name = (name or "").strip()
    for item in AVAILABLE_FONTS:
        if item["family"].lower() == name.lower():
            return item
    return None


# =========================================================
# KEYWORDS (emphasis words for the "keyword" style)
# =========================================================

def clean_token(text):
    return re.sub(r"[\W_]+", "", str(text), flags=re.UNICODE)


def heuristic_keys(words):
    keys = set()
    for start in range(0, len(words), 4):
        window = range(start, min(start + 4, len(words)))
        best = max(window, key=lambda j: len(clean_token(words[j]["word"])))
        if len(clean_token(words[best]["word"])) >= 4:
            keys.add(best)
    return keys


async def llm_keys(words):
    if not (KEYWORDS_LLM and LLM_API_KEY and LLM_CHAT_URL) or len(words) < 3:
        return None
    numbered = " ".join(f"{i}:{w['word']}" for i, w in enumerate(words))
    payload = {
        "model": LLM_MODEL,
        "temperature": 0,
        "response_format": {"type": "json_object"},
        "messages": [
            {
                "role": "system",
                "content": (
                    "You choose emphasis words for short-video captions. "
                    "The text is numbered word by word (Tajik or Russian). "
                    "Pick the most meaningful words (key nouns, verbs, numbers, "
                    "strong adjectives): about one per 3-5 words, never "
                    "prepositions or particles. Answer only with JSON: "
                    '{"keys":[word indices]}.'
                ),
            },
            {"role": "user", "content": numbered},
        ],
    }
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            response = await client.post(
                LLM_CHAT_URL,
                headers={"Authorization": f"Bearer {LLM_API_KEY}"},
                json=payload,
            )
        if response.status_code != 200:
            logger.warning("Keyword LLM status %s", response.status_code)
            return None
        content = response.json()["choices"][0]["message"]["content"]
        raw = json.loads(content).get("keys", [])
        keys = {int(i) for i in raw if isinstance(i, int) and 0 <= i < len(words)}
        return keys or None
    except Exception:
        logger.warning("Keyword LLM failed, using heuristic", exc_info=True)
        return None


async def mark_keywords(words):
    keys = await llm_keys(words)
    if keys is None:
        keys = heuristic_keys(words)
    for index, item in enumerate(words):
        item["key"] = index in keys


# =========================================================
# MEDIA CARDS (Pixabay pictures shown for concrete nouns)
# =========================================================

async def suggest_cards(words, work_dir):
    """Up to 3 cards: {id, at, query, thumb, credit, file}. Never raises."""
    if not (PIXABAY_API_KEY and LLM_API_KEY and LLM_CHAT_URL and KEYWORDS_LLM) or len(words) < 6:
        return []
    numbered = " ".join(f"{i}:{w['word']}" for i, w in enumerate(words))
    payload = {
        "model": LLM_MODEL,
        "temperature": 0,
        "response_format": {"type": "json_object"},
        "messages": [
            {
                "role": "system",
                "content": (
                    "You pick moments of a short video where a stock photo helps. "
                    "The text is numbered word by word (Tajik or Russian). Choose at "
                    "most 3 concrete, visual nouns or objects (not abstract words), "
                    "at least 12 words apart. Answer only with JSON: "
                    '{"cards":[{"index":word index,"query":"2-3 word English stock-photo search"}]}.'
                ),
            },
            {"role": "user", "content": numbered},
        ],
    }
    cards = []
    try:
        async with httpx.AsyncClient(timeout=25.0) as client:
            response = await client.post(
                LLM_CHAT_URL,
                headers={"Authorization": f"Bearer {LLM_API_KEY}"},
                json=payload,
            )
            if response.status_code != 200:
                return []
            raw = json.loads(response.json()["choices"][0]["message"]["content"])
            last_at = -10.0
            for item in raw.get("cards", [])[:3]:
                index = int(item["index"])
                query = str(item["query"]).strip()[:60]
                if not (0 <= index < len(words)) or not query:
                    continue
                at = max(1.0, words[index]["start"])
                if at - last_at < 4.0:
                    continue
                found = await client.get(
                    "https://pixabay.com/api/",
                    params={"key": PIXABAY_API_KEY, "q": query, "per_page": 1,
                            "image_type": "photo", "orientation": "all", "safesearch": "true"},
                )
                photos = found.json().get("hits") if found.status_code == 200 else None
                if not photos:
                    continue
                image = await client.get(photos[0].get("largeImageURL") or photos[0].get("webformatURL", ""))
                if image.status_code != 200 or len(image.content) < 2000:
                    continue
                path = Path(work_dir) / f"card_{len(cards)}.jpg"
                path.write_bytes(image.content)
                cards.append({
                    "id": len(cards),
                    "at": round(at, 2),
                    "query": query,
                    "thumb": photos[0].get("previewURL", ""),
                    "credit": str(photos[0].get("user", ""))[:60],
                    "file": str(path),
                })
                last_at = at
    except Exception:
        logger.warning("Card suggestion failed", exc_info=True)
    return cards


# =========================================================
# LINES (editable transcript) <-> WORDS
# =========================================================

def make_lines(words, max_words=7, max_chars=42, max_gap=0.8):
    lines = []
    current = []

    def flush():
        if not current:
            return
        lines.append({
            "id": len(lines),
            "start": round(current[0]["start"], 2),
            "end": round(current[-1]["end"], 2),
            "text": " ".join(w["word"] for w in current),
            "low": any(w.get("low") for w in current),
            "fixed": any(w.get("fixed") for w in current),
            "words": [
                {
                    "word": w["word"],
                    "start": round(w["start"], 2),
                    "end": round(w["end"], 2),
                    "key": bool(w.get("key", False)),
                    "orig": w.get("orig"),
                }
                for w in current
            ],
        })
        current.clear()

    for index, item in enumerate(words):
        current.append(item)
        text_length = sum(len(w["word"]) + 1 for w in current)
        next_item = words[index + 1] if index + 1 < len(words) else None
        gap = (next_item["start"] - item["end"]) if next_item else 0

        if (
            len(current) >= max_words
            or text_length >= max_chars
            or gap > max_gap
            or item["word"].endswith((".", "!", "?", "…"))
        ):
            flush()

    flush()
    return lines


def lines_to_words(lines, duration):
    """Turn edited lines back into timed words."""
    if not isinstance(lines, list) or not lines:
        raise UserError("Текст субтитров пуст.")
    if len(lines) > 500:
        raise UserError("Слишком много строк субтитров.")

    words = []

    for line in lines:
        if not isinstance(line, dict):
            continue
        text = str(line.get("text", "")).strip()[:300]
        tokens = text.split()
        if not tokens:
            continue

        try:
            start = max(0.0, min(float(line.get("start", 0)), duration))
            end = max(start + 0.1, min(float(line.get("end", start + 1)), duration + 0.5))
        except (TypeError, ValueError):
            continue

        original = line.get("words")
        if isinstance(original, list) and len(original) == len(tokens):
            try:
                for token, source in zip(tokens, original):
                    entry = {
                        "word": token,
                        "start": float(source.get("start", start)),
                        "end": float(source.get("end", end)),
                    }
                    if "key" in source:
                        entry["key"] = bool(source["key"])
                    words.append(entry)
                continue
            except (TypeError, ValueError, AttributeError):
                pass

        weights = [len(token) + 1 for token in tokens]
        total = float(sum(weights))
        cursor = start
        for token, weight in zip(tokens, weights):
            span = (end - start) * weight / total
            words.append({"word": token, "start": cursor, "end": cursor + span})
            cursor += span

    words = normalize_words(words)
    if not words:
        raise UserError("Текст субтитров пуст.")
    return words


# =========================================================
# SUBTITLE STYLES (ASS)
# =========================================================
#
# Colors are RGB hex here and converted to ASS (BGR) when written.

STYLES = {
    "brandly": {
        "font": "Noto Sans", "size": 92, "bold": 1, "spacing": 0,
        "primary": "FFFFFF", "secondary": "FFFFFF", "active": "4AA8FF",
        "outline": "000000", "outline_w": 7, "shadow": 3,
        "border_style": 1, "box_alpha": 0, "mode": "pop",
        "max_words": 4, "max_chars": 34, "upper": True, "margin": 0.22,
    },
    "viral": {
        "font": "Noto Sans", "size": 92, "bold": 1, "spacing": 0,
        "primary": "FFFFFF", "secondary": "FFFFFF", "active": "FFE500",
        "outline": "000000", "outline_w": 7, "shadow": 3,
        "border_style": 1, "box_alpha": 0, "mode": "pop",
        "max_words": 2, "max_chars": 18, "upper": True, "margin": 0.22,
    },
    "creator": {
        "font": "Noto Sans", "size": 80, "bold": 1, "spacing": 0,
        "primary": "FFFFFF", "secondary": "FFFFFF", "active": "5CFFA8",
        "outline": "000000", "outline_w": 6, "shadow": 2,
        "border_style": 1, "box_alpha": 0, "mode": "pop",
        "max_words": 3, "max_chars": 24, "upper": False, "margin": 0.20,
    },
    "classic": {
        "font": "DejaVu Sans", "size": 58, "bold": 0, "spacing": 0,
        "primary": "FFFFFF", "secondary": "FFFFFF", "active": "FFFFFF",
        "outline": "000000", "outline_w": 4, "shadow": 1,
        "border_style": 1, "box_alpha": 0, "mode": "plain",
        "max_words": 6, "max_chars": 38, "upper": False, "margin": 0.14,
    },
    "cinema": {
        "font": "Noto Serif", "size": 56, "bold": 0, "spacing": 3,
        "primary": "F2F2F2", "secondary": "F2F2F2", "active": "F2F2F2",
        "outline": "101010", "outline_w": 2, "shadow": 2,
        "border_style": 1, "box_alpha": 0, "mode": "fade",
        "max_words": 7, "max_chars": 42, "upper": False, "margin": 0.10,
    },
    "podcast": {
        "font": "Noto Sans", "size": 60, "bold": 1, "spacing": 0,
        "primary": "FFFFFF", "secondary": "FFFFFF", "active": "F5B937",
        "outline": "000000", "outline_w": 14, "shadow": 0,
        "border_style": 3, "box_alpha": 0x50, "mode": "highlight",
        "max_words": 4, "max_chars": 28, "upper": False, "margin": 0.14,
    },
    "keyword": {
        # Two-tier captions: small white lead words + big accent keyword.
        "font": "Noto Sans", "size": 96, "bold": 1, "spacing": 0,
        "primary": "FFFFFF", "secondary": "FFFFFF", "active": "00FFA3",
        "outline": "000000", "outline_w": 5, "shadow": 2,
        "border_style": 1, "box_alpha": 0, "mode": "key",
        "max_words": 3, "max_chars": 26, "upper": False, "margin": 0.20,
    },
    "minimal": {
        "font": "Noto Sans", "size": 46, "bold": 0, "spacing": 0,
        "primary": "FFFFFF", "secondary": "FFFFFF", "active": "F5B937",
        "outline": "000000", "outline_w": 2, "shadow": 0,
        "border_style": 1, "box_alpha": 0, "mode": "plain",
        "max_words": 7, "max_chars": 44, "upper": False, "margin": 0.12,
    },
    "dynamic": {
        # Rotates between pop, two-tier keyword and highlight phrase by phrase.
        "font": "Noto Sans", "size": 86, "bold": 1, "spacing": 0,
        "primary": "FFFFFF", "secondary": "FFFFFF", "active": "FFE500",
        "outline": "000000", "outline_w": 6, "shadow": 2,
        "border_style": 1, "box_alpha": 0, "mode": "dynamic",
        "max_words": 3, "max_chars": 24, "upper": False, "margin": 0.20,
    },
    "karaoke": {
        # In karaoke mode Primary = already sung, Secondary = not yet.
        "font": "Noto Sans", "size": 78, "bold": 1, "spacing": 0,
        "primary": "F5B937", "secondary": "FFFFFF", "active": "F5B937",
        "outline": "000000", "outline_w": 6, "shadow": 2,
        "border_style": 1, "box_alpha": 0, "mode": "karaoke",
        "max_words": 4, "max_chars": 26, "upper": False, "margin": 0.18,
    },
}


def ass_color(rgb, alpha=0):
    rgb = rgb.strip("#").upper()
    red, green, blue = rgb[0:2], rgb[2:4], rgb[4:6]
    return f"&H{alpha:02X}{blue}{green}{red}"


def ass_tag_color(rgb):
    rgb = rgb.strip("#").upper()
    return f"&H{rgb[4:6]}{rgb[2:4]}{rgb[0:2]}&"


def ass_time(seconds):
    total_cs = int(round(max(0.0, float(seconds)) * 100))
    hours = total_cs // 360000
    minutes = (total_cs // 6000) % 60
    secs = (total_cs // 100) % 60
    centiseconds = total_cs % 100
    return f"{hours}:{minutes:02d}:{secs:02d}.{centiseconds:02d}"


def escape_ass_text(text):
    text = str(text or "")
    text = text.replace("\\", r"\\")
    text = text.replace("{", r"\{")
    text = text.replace("}", r"\}")
    text = text.replace("\r", " ").replace("\n", " ")
    return text


def split_phrases(words, max_words, max_chars, max_gap=0.7):
    phrases = []
    current = []

    for index, item in enumerate(words):
        current.append(item)
        text_length = sum(len(w["word"]) + 1 for w in current)
        next_item = words[index + 1] if index + 1 < len(words) else None
        gap = (next_item["start"] - item["end"]) if next_item else 0

        if (
            len(current) >= max_words
            or text_length >= max_chars
            or gap > max_gap
            or item["word"].endswith((".", "!", "?", "…"))
        ):
            phrases.append(current)
            current = []

    if current:
        phrases.append(current)
    return phrases


# =========================================================
# TEXT ANIMATIONS (ASS override tags, rendered by libass)
# =========================================================

ANIM_IN = [
    "fade_in", "slide_up", "slide_left", "zoom_in", "pop_up", "bounce",
    "typewriter", "word_by_word", "letter_by_letter", "glitch", "scramble",
    "split", "shake", "rotate_3d", "neon_glow", "elastic", "mask_reveal",
    "light_sweep", "tracking", "blur_reveal", "pop_3d", "liquid", "wave",
    "flip_in", "roll_in", "spin_in", "stretch", "flash", "flicker",
    "rotate_in", "swing", "wipe_reveal", "curtain_reveal", "glowing",
    "floating", "magnetic", "particle", "fire", "smoke", "rgb_split",
    "pulse", "handwriting",
]
ANIM_OUT = ["fade_out", "slide_down", "slide_right", "zoom_out", "blur_out", "flip_out"]
ANIM_MIX = [
    "fade_in", "slide_up", "zoom_in", "pop_up", "bounce", "elastic",
    "blur_reveal", "tracking", "mask_reveal", "wipe_reveal", "spin_in",
    "flip_in", "rotate_3d", "pop_3d", "stretch", "magnetic", "smoke",
    "glitch", "roll_in", "curtain_reveal", "liquid",
]
ANIM_MIX_OUT = ["fade_out", "slide_down", "zoom_out", "blur_out"]
SCRAMBLE_ALPHABET = "АБВГДЕЖЗИКЛМНОПРСТУФХЦЧШЭЮЯҒҚҲҶ"


def _t(a, b, tags):
    return f"\\t({int(a)},{int(b)},{tags})"


def build_entrance(name, c):
    """Return dict(tags, fi, char, layers, skip_main) for an entrance."""
    x, y, u, d, dur = c["x"], c["y"], c["u"], c["d"], c["dur"]
    xl, xr, yt, yb = c["xl"], c["xr"], c["yt"], c["yb"]
    col = c["col"]
    r = {"tags": "", "fi": 0, "char": None, "layers": [], "skip_main": False}
    T = _t
    mv = lambda x1, y1, x2, y2, a, b: f"\\move({int(x1)},{int(y1)},{int(x2)},{int(y2)},{int(a)},{int(b)})"

    if name == "fade_in":
        r["fi"] = d
    elif name == "slide_up":
        r["tags"] = mv(x, y + 90 * u, x, y, 0, d); r["fi"] = d * 0.7
    elif name == "slide_left":
        r["tags"] = mv(x + 300 * u, y, x, y, 0, d); r["fi"] = d * 0.6
    elif name == "zoom_in":
        r["tags"] = "\\fscx40\\fscy40" + T(0, d, "\\fscx100\\fscy100"); r["fi"] = d * 0.6
    elif name == "pop_up":
        r["tags"] = ("\\fscx0\\fscy0" + T(0, d * 0.6, "\\fscx118\\fscy118")
                     + T(d * 0.6, d, "\\fscx100\\fscy100"))
    elif name == "bounce":
        r["tags"] = (mv(x, y - 160 * u, x, y, 0, d * 0.55) + T(d * 0.55, d * 0.7, "\\fscy78")
                     + T(d * 0.7, d, "\\fscy100")); r["fi"] = 60
    elif name == "elastic":
        r["tags"] = ("\\fscx150\\fscy55" + T(0, d * 0.3, "\\fscx88\\fscy112")
                     + T(d * 0.3, d * 0.6, "\\fscx106\\fscy96") + T(d * 0.6, d, "\\fscx100\\fscy100"))
        r["fi"] = 60
    elif name == "typewriter":
        r["char"] = "seq"
    elif name == "word_by_word":
        r["char"] = "word"
    elif name == "letter_by_letter":
        r["char"] = "letter"
    elif name == "particle":
        r["char"] = "rand"
    elif name == "wave":
        r["char"] = "wave"; r["fi"] = 120
    elif name == "scramble":
        r["char"] = "scramble"
    elif name == "glitch":
        r["tags"] = ("\\fax0.35\\fscx108\\3c&H0000FF&" + T(0, 60, "\\fax-0.3\\fscx94\\3c&HFFFF00&")
                     + T(60, 120, "\\fax0.2\\fscx104\\3c&H0000FF&") + T(120, 180, "\\fax0\\fscx100\\3c&H000000&"))
        r["fi"] = 40
    elif name == "shake":
        r["tags"] = ("\\frz3" + T(0, 70, "\\frz-3") + T(70, 140, "\\frz2.5") + T(140, 210, "\\frz-2")
                     + T(210, 280, "\\frz1") + T(280, 350, "\\frz0")); r["fi"] = 80
    elif name == "rotate_3d":
        r["tags"] = "\\frx80" + T(0, d, "\\frx0"); r["fi"] = d * 0.6
    elif name == "pop_3d":
        r["tags"] = ("\\fry70\\frx25\\fscx60\\fscy60" + T(0, d * 0.7, "\\fry-8\\frx-4\\fscx112\\fscy112")
                     + T(d * 0.7, d, "\\fry0\\frx0\\fscx100\\fscy100")); r["fi"] = d * 0.5
    elif name == "flip_in":
        r["tags"] = "\\fry90" + T(0, d, "\\fry0"); r["fi"] = d * 0.5
    elif name == "roll_in":
        r["tags"] = mv(x + 260 * u, y, x, y, 0, d) + "\\frz-25" + T(0, d, "\\frz0"); r["fi"] = d * 0.5
    elif name == "spin_in":
        r["tags"] = "\\frz-360\\fscx30\\fscy30" + T(0, d * 1.2, "\\frz0\\fscx100\\fscy100"); r["fi"] = d * 0.5
    elif name == "rotate_in":
        r["tags"] = "\\frz-20" + T(0, d, "\\frz0"); r["fi"] = d
    elif name == "stretch":
        r["tags"] = "\\fscx260\\fscy55" + T(0, d, "\\fscx100\\fscy100"); r["fi"] = d * 0.6
    elif name == "flash":
        r["tags"] = "\\1c&HFFFFFF&\\3c&HFFFFFF&\\blur9" + T(0, 260, "\\blur0\\3c&H000000&"); r["fi"] = 30
    elif name == "flicker":
        steps = ""
        for k in range(6):
            steps += T(k * 90, k * 90 + 45, "\\alpha&H" + ("B0" if k % 2 == 0 else "00") + "&")
        r["tags"] = "\\alpha&HFF&" + steps + T(540, 560, "\\alpha&H00&")
    elif name == "swing":
        r["tags"] = (f"\\org({int(x)},{int(y - 220 * u)})\\frz9" + T(0, 200, "\\frz-7") + T(200, 400, "\\frz5")
                     + T(400, 600, "\\frz-3") + T(600, 800, "\\frz0")); r["fi"] = 120
    elif name == "pulse":
        cyc = ""
        for k in range(max(1, min(10, int(dur // 600)))):
            cyc += T(k * 600, k * 600 + 300, "\\fscx107\\fscy107") + T(k * 600 + 300, k * 600 + 600, "\\fscx100\\fscy100")
        r["tags"] = cyc; r["fi"] = d * 0.5
    elif name == "liquid":
        r["tags"] = ("\\fscx125\\fscy80\\blur4" + T(0, d * 0.25, "\\fscx86\\fscy116")
                     + T(d * 0.25, d * 0.5, "\\fscx108\\fscy94") + T(d * 0.5, d * 0.75, "\\fscx96\\fscy104")
                     + T(d * 0.75, d, "\\fscx100\\fscy100\\blur0")); r["fi"] = 80
    elif name == "blur_reveal":
        r["tags"] = "\\blur20" + T(0, d, "\\blur0"); r["fi"] = d
    elif name == "smoke":
        r["tags"] = mv(x, y + 45 * u, x, y, 0, d * 1.3) + "\\blur16" + T(0, d * 1.3, "\\blur0"); r["fi"] = d * 1.2
    elif name == "tracking":
        r["tags"] = "\\fsp36" + T(0, d * 1.5, "\\fsp0"); r["fi"] = d
    elif name == "magnetic":
        r["tags"] = "\\fsp70\\blur5" + T(0, d, "\\fsp0\\blur0"); r["fi"] = d * 0.7
    elif name == "mask_reveal":
        r["tags"] = f"\\clip({xl},{yb},{xr},{yb})" + T(0, d * 1.1, f"\\clip({xl},{yt},{xr},{yb})")
    elif name == "wipe_reveal":
        r["tags"] = f"\\clip({xl},{yt},{xl},{yb})" + T(0, d * 1.2, f"\\clip({xl},{yt},{xr},{yb})")
    elif name == "handwriting":
        r["tags"] = (f"\\clip({xl},{yt},{xl},{yb})\\blur0.6"
                     + T(0, min(1500, dur * 0.7), f"\\clip({xl},{yt},{xr},{yb})"))
    elif name == "curtain_reveal":
        r["tags"] = f"\\clip({int(x)},{yt},{int(x)},{yb})" + T(0, d * 1.1, f"\\clip({xl},{yt},{xr},{yb})")
    elif name == "split":
        r["skip_main"] = True
        r["layers"] = [
            (5, f"\\clip(0,0,{int(x)},{c['h']})" + mv(x - 220 * u, y, x, y, 0, d) + f"\\fad({int(d * 0.5)},0)"),
            (5, f"\\clip({int(x)},0,{c['w']},{c['h']})" + mv(x + 220 * u, y, x, y, 0, d) + f"\\fad({int(d * 0.5)},0)"),
        ]
    elif name == "neon_glow":
        r["fi"] = d * 0.6
        r["layers"] = [(4, f"\\1a&HFF&\\bord{max(6, int(9 * u * 1.5))}\\blur{max(6, int(10 * u * 1.5))}\\3c{col}\\shad0")]
    elif name == "glowing":
        b = max(6, int(8 * u * 1.5))
        pulses = ""
        for k in range(max(1, min(8, int(dur // 800)))):
            pulses += T(k * 800, k * 800 + 400, f"\\blur{b * 2}") + T(k * 800 + 400, k * 800 + 800, f"\\blur{b}")
        r["fi"] = d * 0.6
        r["layers"] = [(4, f"\\1a&HFF&\\bord{b}\\blur{b}\\3c{col}\\shad0" + pulses)]
    elif name == "fire":
        fl = ""
        for k in range(max(1, min(14, int(dur // 160)))):
            fl += T(k * 160, k * 160 + 80, "\\1c&H0030FF&") + T(k * 160 + 80, k * 160 + 160, "\\1c&H00C8FF&")
        r["tags"] = "\\1c&H0090FF&\\3c&H000040&" + fl; r["fi"] = d * 0.5
        r["layers"] = [(4, "\\1a&HFF&\\bord10\\blur12\\3c&H0040FF&\\shad0")]
    elif name == "rgb_split":
        r["fi"] = 60
        off = max(8, int(16 * u))
        r["layers"] = [
            (4, "\\1c&H0000FF&\\3c&H0000FF&\\alpha&H40&" + mv(x - off, y, x - 2, y, 0, d * 1.2)),
            (3, "\\1c&HFF0000&\\3c&HFF0000&\\alpha&H40&" + mv(x + off, y, x + 2, y, 0, d * 1.2)),
        ]
    elif name == "light_sweep":
        r["fi"] = d * 0.6
        bw = int((xr - xl) * 0.22)
        r["layers"] = [(6, f"\\1c&HFFFFFF&\\3c&HFFFFFF&\\bord1\\blur2\\clip({xl - bw},{yt},{xl},{yb})"
                        + _t(250, 1050, f"\\clip({xr},{yt},{xr + bw},{yb})") + _t(900, 1100, "\\alpha&HFF&"))]
    return r


def build_exit(name, c, ms):
    x, y, u = c["x"], c["y"], c["u"]
    fade = f"\\fad(0,{ms})"
    mv = lambda x1, y1, x2, y2: f"\\move({int(x1)},{int(y1)},{int(x2)},{int(y2)},0,{ms})"
    if name == "slide_down":
        return mv(x, y, x, y + 90 * u) + fade
    if name == "slide_right":
        return mv(x, y, x + 300 * u, y) + fade
    if name == "zoom_out":
        return _t(0, ms, "\\fscx40\\fscy40") + fade
    if name == "blur_out":
        return _t(0, ms, "\\blur20") + fade
    if name == "flip_out":
        return _t(0, ms, "\\fry90") + fade
    return fade


def _visible_chars(text):
    """Yield (is_visible, token) over an ASS text (tags and \\N stay intact)."""
    i = 0
    while i < len(text):
        if text[i] == "{":
            j = text.find("}", i)
            j = len(text) - 1 if j < 0 else j
            yield False, text[i:j + 1]
            i = j + 1
        elif text.startswith("\\N", i):
            yield False, "\\N"
            i += 2
        elif text[i] == "\\" and i + 1 < len(text):
            yield True, text[i:i + 2]
            i += 2
        else:
            yield True, text[i]
            i += 1


def wrap_chars(text, kind, total_ms, rng):
    letters = [tok for vis, tok in _visible_chars(text) if vis and tok != " "]
    n = max(1, len(letters))
    out, index, word = [], 0, 0
    words = max(1, len(text.split(" ")))
    for vis, tok in _visible_chars(text):
        if not vis:
            out.append(tok)
            continue
        if tok == " ":
            word += 1
            out.append(tok)
            continue
        if kind == "seq":
            a = index * min(total_ms * 0.8, n * 45) / n
            out.append("{\\alpha&HFF&" + _t(a, a + 1, "\\alpha&H00&") + "}" + tok)
        elif kind == "letter":
            a = index * min(total_ms * 0.85, n * 70) / n
            out.append("{\\alpha&HFF&" + _t(a, a + 140, "\\alpha&H00&") + "}" + tok)
        elif kind == "word":
            a = word * min(total_ms * 0.8 / words, 300)
            out.append("{\\alpha&HFF&" + _t(a, a + 120, "\\alpha&H00&") + "}" + tok)
        elif kind == "rand":
            a = rng.random() * min(total_ms * 0.7, 700)
            out.append("{\\alpha&HFF&\\blur6" + _t(a, a + 260, "\\alpha&H00&\\blur0") + "}" + tok)
        elif kind == "wave":
            a = index * 35
            out.append("{" + _t(a, a + 150, "\\fscy130") + _t(a + 150, a + 320, "\\fscy100") + "}" + tok)
        else:
            out.append(tok)
        index += 1
    return "".join(out)


def scramble_text(text, p, rng):
    out = []
    for vis, tok in _visible_chars(text):
        if vis and tok.isalpha() and rng.random() < p:
            out.append(rng.choice(SCRAMBLE_ALPHABET))
        else:
            out.append(tok)
    return "".join(out)


def animated_phrase_events(text, start, end, sname, in_name, out_name, c, rng):
    """Dialogue lines for one phrase with an entrance and optional exit."""
    lines = []
    exit_ms = 260 if out_name and (end - start) * 1000 > 700 else 0
    main_end = end - exit_ms / 1000.0
    main_start = start
    total_ms = max(120, (main_end - start) * 1000)
    c = dict(c, dur=total_ms, d=min(450, total_ms * 0.6))
    an = f"\\an{c['an']}"

    def dialog(layer, s, e, tags, body, style=sname):
        return (f"Dialogue: {layer},{ass_time(s)},{ass_time(e)},{style},,0,0,0,,"
                f"{{{an}{tags}}}{body}")

    r = build_entrance(in_name, c) if in_name else {"tags": "", "fi": 0, "char": None, "layers": [], "skip_main": False}
    fad = f"\\fad({int(r['fi'])},0)" if r["fi"] else ""

    if r["char"] == "scramble" and total_ms > 700:
        for k, p in enumerate((0.95, 0.75, 0.5, 0.3, 0.15)):
            s = start + k * 0.07
            lines.append(dialog(5, s, s + 0.07, "", scramble_text(text, p, rng)))
        main_start = start + 0.35
        r["char"] = None
    elif r["char"] == "scramble":
        r["char"] = None

    body = text
    if r["char"]:
        body = wrap_chars(text, r["char"], total_ms, rng)

    for layer, extra in r["layers"]:
        lines.append(dialog(layer, main_start, main_end, extra if r["skip_main"] else fad + r["tags"] + extra, text))
    if not r["skip_main"]:
        lines.append(dialog(5, main_start, main_end, fad + r["tags"], body))

    if exit_ms:
        etags = build_exit(out_name, c, exit_ms)
        for layer, extra in r["layers"]:
            if not r["skip_main"] and layer < 5:
                lines.append(dialog(layer, main_end, end, etags + extra.split("\\move")[0].split("\\clip")[0], text))
        lines.append(dialog(5, main_end, end, etags, text))
    return lines




def choose_animation(text, index, previous, count):
    """Choose a visible, varied entrance for each phrase in Auto Pro."""
    t = text.strip()
    if index == 0:
        pool = ["pop_3d", "zoom_in", "pop_up", "light_sweep", "slide_up"]
    elif t.endswith("?"):
        pool = ["word_by_word", "wipe_reveal", "typewriter", "flip_in"]
    elif t.endswith("!") or any(ch.isdigit() for ch in t):
        pool = ["pop_3d", "bounce", "zoom_in", "stretch", "glitch", "shake"]
    else:
        # Keep motion noticeable even on longer captions; avoid a run of plain fades.
        pool = [
            "pop_3d", "zoom_in", "light_sweep", "bounce", "glitch",
            "neon_glow", "word_by_word", "elastic", "wipe_reveal",
            "magnetic", "blur_reveal", "flip_in", "liquid",
        ]
    pool = [name for name in pool if name != previous] or ANIM_MIX
    return pool[index % len(pool)]


TRANSITION_KINDS = ["zoom", "whip", "swipe", "motion", "flash", "blur", "glitch"]


def plan_transitions(words, duration, mode, cut_points, scenes=None):
    """Build a reliable transition plan from cuts, scene changes and caption beats."""
    if mode in (None, "", "off"):
        return []

    points = [(t, "cut") for t in cut_points if 0.3 < t < duration - 0.3]

    # Scene analysis can return only one scene. Always add caption/sentence beats
    # as a fallback so Auto mode does not silently produce a video with no transitions.
    if scenes:
        for scene in scenes[1:]:
            moment = float(scene.get("start", 0))
            if 0.8 < moment < duration - 0.5:
                origin = "key" if scene.get("importance", 0.5) >= 0.8 else "scene"
                points.append((moment, origin))

    lines = make_lines(words)
    for previous, line in zip(lines, lines[1:]):
        moment = float(line.get("start", 0))
        if not 0.8 < moment < duration - 0.5:
            continue
        ends_thought = previous["text"].rstrip().endswith((".", "?", "!", "…"))
        points.append((moment, "thought" if ends_thought else "phrase"))

    if not points:
        return []

    priority = {"cut": 0, "key": 1, "scene": 2, "thought": 3, "phrase": 4}
    points.sort(key=lambda item: (item[0], priority.get(item[1], 9)))
    chosen = []
    for moment, origin in points:
        if chosen and moment - chosen[-1][0] < 1.05:
            # Keep the strongest event when two beats are nearly simultaneous.
            if priority.get(origin, 9) < priority.get(chosen[-1][1], 9):
                chosen[-1] = (moment, origin)
            continue
        chosen.append((moment, origin))
    chosen = chosen[:14]

    out = []
    auto_kinds = ("zoom", "whip", "swipe", "motion", "flash", "blur", "glitch")
    for number, (moment, origin) in enumerate(chosen):
        if mode in TRANSITION_KINDS:
            kind = mode
        elif origin == "cut":
            kind = ("whip", "flash", "zoom", "motion")[number % 4]
        elif origin == "key":
            kind = ("glitch", "zoom", "flash")[number % 3]
        else:
            kind = auto_kinds[number % len(auto_kinds)]
        out.append((moment, kind))
    return out


def transition_filters(points, width):
    """Return (zoom_terms, shift_terms, filter_string) for the transition points."""
    zoom_terms, shift_terms, filters = [], [], []
    for number, (moment, kind) in enumerate(points):
        tri = f"max(0,1-abs(t-{moment:.2f})/0.14)"
        win = f"between(t,{moment - 0.1:.2f},{moment + 0.14:.2f})"
        if kind in ("zoom", "whip", "swipe", "motion"):
            zoom_terms.append(f"+0.14*{tri}")
        if kind in ("whip", "swipe"):
            sign = "+" if number % 2 == 0 else "-"
            shift_terms.append(f"{sign}{tri}")
        if kind == "whip":
            filters.append(f"avgblur=sizeX={max(20, int(width * 0.09))}:sizeY=1:enable='{win}'")
        elif kind == "swipe":
            filters.append(f"avgblur=sizeX={max(10, int(width * 0.04))}:sizeY=1:enable='{win}'")
        elif kind == "motion":
            filters.append(f"avgblur=sizeX={max(14, int(width * 0.05))}:sizeY=1:enable='{win}'")
        elif kind == "blur":
            filters.append(f"gblur=sigma=16:enable='{win}'")
        elif kind == "flash":
            filters.append(
                "drawbox=x=0:y=0:w=iw:h=ih:color=white@0.8:t=fill:"
                f"enable='between(t,{moment - 0.03:.2f},{moment + 0.07:.2f})'"
            )
        elif kind == "glitch":
            filters.append(
                f"rgbashift=rh=-20:bh=20:gh=6:enable='{win}',"
                f"negate=enable='between(t,{moment:.2f},{moment + 0.05:.2f})'"
            )
    return zoom_terms, shift_terms, ",".join(filters)




def build_ass(words, style_name, width, height, output_path,
              accent=None, size="m", position="auto",
              font=None, font2=None, mix=False, keys=True, anim=None, anim_out=None, anim_log=None):
    style = dict(STYLES.get(style_name, STYLES["viral"]))
    if accent:
        style["active"] = accent
        if style["mode"] == "karaoke":
            style["primary"] = accent

    scale = min(width, height) / 720.0
    if width > height:
        scale *= 0.8
    scale *= {"s": 0.8, "m": 1.0, "l": 1.25}.get(size, 1.0)

    font_size = int(style["size"] * scale)
    outline_w = max(1, int(round(style["outline_w"] * scale)))
    shadow = int(round(style["shadow"] * scale))
    spacing = style["spacing"] * scale

    margin_ratio = style["margin"]
    if width > height:
        margin_ratio = min(margin_ratio, 0.09)
    margin_v = int(height * margin_ratio)
    alignment = 2
    if position == "top":
        alignment, margin_v = 8, int(height * 0.10)
    elif position == "center":
        alignment, margin_v = 5, 0
    margin_h = int(width * 0.07)

    outline_alpha = style["box_alpha"] if style["border_style"] == 3 else 0
    back_color = ass_color("000000", 0x80)

    main_font = resolve_font(font)
    base_family = main_font["family"] if main_font else style["font"]
    base_bold = (
        style["bold"] and main_font["bold"] if main_font else style["bold"]
    )
    cycle = [(base_family, base_bold)]
    if mix:
        for item in AVAILABLE_FONTS:
            if item["family"] != base_family and len(cycle) < 3:
                cycle.append((item["family"], item["bold"]))
    accent_font = resolve_font(font2)
    accent_family = accent_font["family"] if accent_font else None
    font_tag = f"\\fn{accent_family}" if accent_family else ""

    style_lines = "\n".join(
        f"Style: F{i},{fam},{font_size},"
        f"{ass_color(style['primary'])},{ass_color(style['secondary'])},"
        f"{ass_color(style['outline'], outline_alpha)},{back_color},"
        f"{bold},0,0,0,100,100,{spacing:.1f},0,"
        f"{style['border_style']},{outline_w},{shadow},{alignment},"
        f"{margin_h},{margin_h},{margin_v},1"
        for i, (fam, bold) in enumerate(cycle)
    )

    header = (
        "[Script Info]\n"
        "Title: GAPCUT AI\n"
        "ScriptType: v4.00+\n"
        "WrapStyle: 0\n"
        "ScaledBorderAndShadow: yes\n"
        f"PlayResX: {width}\n"
        f"PlayResY: {height}\n\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
        "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, "
        "ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
        "Alignment, MarginL, MarginR, MarginV, Encoding\n"
        f"{style_lines}\n\n"
        "[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, "
        "Effect, Text\n"
    )

    def display(token):
        text = escape_ass_text(token)
        return text.upper() if style["upper"] else text

    def colored(phrase):
        flagged = [i for i, t in enumerate(phrase) if t.get("key")]
        # Brandly emphasizes one main keyword per caption card.
        if style_name == "brandly" and flagged:
            flagged = flagged[:1]
        if not keys or not flagged:
            return " ".join(display(t["word"]) for t in phrase)
        key_col = ass_tag_color(accent or "F5B937")
        base_col = ass_tag_color(style["primary"])
        return " ".join(
            ("{\\1c%s}%s{\\1c%s}" % (key_col, display(t["word"]), base_col))
            if i in flagged else display(t["word"])
            for i, t in enumerate(phrase)
        )

    phrases = split_phrases(words, style["max_words"], style["max_chars"])
    events = []
    anim_on = bool(anim or anim_out)
    last_anim = None
    anim_master = random.Random(4242)
    if anim_on:
        anchor_x = width // 2
        if alignment == 8:
            anchor_y = margin_v
        elif alignment == 5:
            anchor_y = height // 2
        else:
            anchor_y = height - margin_v
        reach = int(font_size * 2.8)
        if alignment == 8:
            band = (max(0, anchor_y - int(font_size * 0.3)), min(height, anchor_y + reach))
        elif alignment == 5:
            band = (max(0, anchor_y - reach // 2), min(height, anchor_y + reach // 2))
        else:
            band = (max(0, anchor_y - reach), min(height, anchor_y + int(font_size * 0.3)))
        base_ctx = dict(
            x=anchor_x, y=anchor_y, an=alignment, u=height / 1280.0, w=width, h=height,
            xl=margin_h, xr=width - margin_h, yt=band[0], yb=band[1],
            col=ass_tag_color(style["active"]), fs=font_size,
        )

    for p_index, phrase in enumerate(phrases):
        phrase_start = phrase[0]["start"]
        sname = f"F{p_index % len(cycle)}"
        phrase_end = phrase[-1]["end"] + 0.15
        if p_index + 1 < len(phrases):
            phrase_end = min(phrase_end, phrases[p_index + 1][0]["start"] - 0.02)
        phrase_end = max(phrase_end, phrase_start + 0.15)

        mode = style["mode"]
        if mode == "dynamic":
            mode = ("pop", "key", "highlight", "key")[p_index % 4]

        if anim_on:
            act = ass_tag_color(style["active"])
            prim = ass_tag_color(style["primary"])
            if mode == "key":
                flagged = [i for i, t in enumerate(phrase) if t.get("key")]
                pool = flagged or list(range(len(phrase)))
                kidx = max(pool, key=lambda i: len(core_token(phrase[i]["word"])))
                ktext = display(phrase[kidx]["word"])
                ksize = int(font_size * min(1.0, 9.0 / max(len(ktext), 1)))
                lead = " ".join(display(t["word"]) for j, t in enumerate(phrase) if j != kidx)
                pieces = []
                if lead:
                    pieces.append("{\\fs%d\\1c&HFFFFFF&}%s" % (int(font_size * 0.52), lead))
                pieces.append("{\\fs%d\\1c%s%s}%s" % (ksize, act, font_tag, ktext))
                block = "\\N".join(pieces)
            elif mode in ("pop", "highlight"):
                flagged = [i for i, t in enumerate(phrase) if t.get("key")]
                if style_name == "brandly" and flagged:
                    flagged = flagged[:1]
                if not flagged:
                    flagged = [max(range(len(phrase)), key=lambda i: len(core_token(phrase[i]["word"])))]
                block = " ".join(
                    ("{\\1c%s}%s{\\1c%s}" % (act, display(t["word"]), prim)) if i in flagged
                    else display(t["word"])
                    for i, t in enumerate(phrase)
                )
            elif mode == "karaoke":
                bits = []
                for w_index, token in enumerate(phrase):
                    if w_index + 1 < len(phrase):
                        span = phrase[w_index + 1]["start"] - token["start"]
                    else:
                        span = phrase_end - token["start"]
                    bits.append("{\\kf%d}%s" % (max(1, int(round(span * 100))), display(token["word"])))
                block = " ".join(bits)
            else:
                block = colored(phrase)

            plain = " ".join(t["word"] for t in phrase)
            in_name = anim if anim in ANIM_IN else None
            if anim == "auto":
                in_name = choose_animation(plain, p_index, last_anim, len(phrase))
            elif anim == "mix":
                in_name = anim_master.choice([n for n in ANIM_MIX if n != last_anim])
            last_anim = in_name or last_anim
            if anim_log is not None and in_name:
                anim_log.append((phrase_start, in_name))
            out_name = anim_out if anim_out in ANIM_OUT else None
            if anim_out == "auto":
                # Give every sufficiently long phrase a visible, varied exit.
                out_name = ANIM_MIX_OUT[p_index % len(ANIM_MIX_OUT)]
            events.extend(animated_phrase_events(
                block, phrase_start, phrase_end, sname, in_name, out_name,
                base_ctx, random.Random(p_index + 11),
            ))
            continue

        if mode in ("pop", "highlight"):
            active = ass_tag_color(style["active"])
            for w_index, token in enumerate(phrase):
                start = phrase_start if w_index == 0 else token["start"]
                if w_index + 1 < len(phrase):
                    end = phrase[w_index + 1]["start"]
                else:
                    end = phrase_end
                if end <= start:
                    end = start + 0.05

                parts = []
                for t_index, other in enumerate(phrase):
                    text = display(other["word"])
                    if t_index == w_index:
                        if mode == "pop":
                            tag = (
                                "{\\1c" + active + font_tag +
                                "\\fscx112\\fscy112"
                                "\\t(0,110,\\fscx100\\fscy100)}"
                            )
                        else:
                            tag = "{\\1c" + active + font_tag + "}"
                        parts.append(tag + text + "{\\r}")
                    else:
                        parts.append(text)

                events.append(
                    f"Dialogue: 0,{ass_time(start)},{ass_time(end)},{sname},,"
                    f"0,0,0,,{' '.join(parts)}"
                )

        elif mode == "key":
            flagged = [i for i, t in enumerate(phrase) if t.get("key")]
            pool = flagged or list(range(len(phrase)))
            key_idx = max(pool, key=lambda i: len(clean_token(phrase[i]["word"])))
            key_text = display(phrase[key_idx]["word"])
            key_size = int(font_size * min(1.0, 9.0 / max(len(key_text), 1)))
            lead_size = int(font_size * 0.52)
            key_color = ass_tag_color(style["active"])

            for w_index, token in enumerate(phrase):
                start = phrase_start if w_index == 0 else token["start"]
                if w_index + 1 < len(phrase):
                    end = phrase[w_index + 1]["start"]
                else:
                    end = phrase_end
                if end <= start:
                    end = start + 0.05

                lead = " ".join(
                    display(t["word"])
                    for j, t in enumerate(phrase[:w_index + 1])
                    if j != key_idx
                )
                pieces = []
                if lead:
                    pieces.append(f"{{\\fs{lead_size}\\1c&HFFFFFF&}}{lead}")
                if key_idx <= w_index:
                    pop = ""
                    if w_index == key_idx:
                        pop = "\\fscx60\\fscy60\\t(0,140,\\fscx100\\fscy100)"
                    pieces.append(
                        f"{{\\fs{key_size}\\1c{key_color}{font_tag}{pop}}}{key_text}"
                    )
                events.append(
                    f"Dialogue: 0,{ass_time(start)},{ass_time(end)},{sname},,"
                    "0,0,0,," + "\\N".join(pieces)
                )

        elif mode == "karaoke":
            parts = []
            for w_index, token in enumerate(phrase):
                if w_index + 1 < len(phrase):
                    span = phrase[w_index + 1]["start"] - token["start"]
                else:
                    span = phrase_end - token["start"]
                centis = max(1, int(round(span * 100)))
                parts.append(f"{{\\kf{centis}}}{display(token['word'])}")
            events.append(
                f"Dialogue: 0,{ass_time(phrase_start)},{ass_time(phrase_end)},"
                f"{sname},,0,0,0,,{' '.join(parts)}"
            )

        elif mode == "fade":
            text = colored(phrase)
            events.append(
                f"Dialogue: 0,{ass_time(phrase_start)},{ass_time(phrase_end)},"
                f"{sname},,0,0,0,,{{\\fad(250,200)}}{text}"
            )

        else:
            text = colored(phrase)
            events.append(
                f"Dialogue: 0,{ass_time(phrase_start)},{ass_time(phrase_end)},"
                f"{sname},,0,0,0,,{text}"
            )

    Path(output_path).write_text(header + "\n".join(events) + "\n", encoding="utf-8")


# =========================================================
# RENDER
# =========================================================

def zoom_expression(starts, amount=0.12):
    terms = []
    for index, moment in enumerate(starts[:40]):
        sign = "+" if index % 2 == 0 else "-"
        terms.append(f"{sign}clip((t-{moment:.2f})/0.18,0,1)")
    if not terms:
        return None
    body = "".join(terms)
    if body.startswith("+"):
        body = body[1:]
    return f"(1+{amount}*({body}))"


def between_expression(moments, length):
    return "+".join(f"between(t,{m:.2f},{m + length:.2f})" for m in moments)


# =========================================================
# SMART AUTO CUT (stage 2): pauses, filler sounds and stutters
# =========================================================
#
# Cuts are made around words: every kept word keeps a small safety pad, so a word is
# never clipped. Subtitles, zoom and cards are moved onto the new timeline.

CUT_LEVELS = {"light": (0.95, 0.34), "normal": (0.80, 0.30), "tight": (0.65, 0.26)}
FILLER_RE = re.compile(r"^(?:э+м*|е+м+|м{2,}|гм+|хм+|эх+|ам{2,})$")


def is_filler(text):
    core = core_token(text).lower()
    return 0 < len(core) <= 6 and bool(FILLER_RE.match(core))


def plan_cuts(words, duration, level):
    """Return a cut plan or None when cutting would not help or is unsafe."""
    if level not in CUT_LEVELS or len(words) < 3 or duration <= 1.0:
        return None
    min_gap, keep = CUT_LEVELS[level]
    lead, tail = keep * 0.4, keep * 0.6

    speech = []
    fillers = 0
    # Never delete a recognized word automatically: Whisper can mislabel
    # Tajik/Russian words as fillers or repeat adjacent words in its output.
    # Keep every timestamped token and only consider long pauses between them.
    for item in words:
        speech.append((item, False))
    if len(speech) < 3:
        return None

    groups = []
    first = speech[0][0]
    current = [first["start"] - lead, first["end"] + tail]
    last_end = first["end"]
    for item, cut in speech[1:]:
        gap = item["start"] - last_end
        if cut or gap > min_gap:
            groups.append(current)
            current = [item["start"] - lead, item["end"] + tail]
        else:
            current[1] = item["end"] + tail
        last_end = item["end"]
    groups.append(current)

    groups[0][0] = speech[0][0]["start"] - 0.25
    groups[-1][1] = speech[-1][0]["end"] + 0.4
    segments = []
    for a, b in groups:
        a, b = max(0.0, a), min(duration, b)
        if b - a < 0.15:
            continue
        if segments and a <= segments[-1][1] + 0.02:
            segments[-1][1] = max(segments[-1][1], b)
        else:
            segments.append([a, b])

    new_duration = sum(b - a for a, b in segments)
    # Conservative safety gate: retain at least 75% of the original timeline
    # and at least 90% of the timestamped speech span. If recognition timing is
    # poor or the proposed edit is too aggressive, keep the source unchanged.
    spoken = max(0.0, speech[-1][0]["end"] - speech[0][0]["start"])
    if new_duration < 1.0:
        return None
    if new_duration < 0.75 * duration or new_duration < 0.90 * spoken:
        return None
    if duration - new_duration < 0.50:
        return None

    offsets = []
    cursor = 0.0
    for a, b in segments:
        offsets.append(cursor)
        cursor += b - a

    def remap(moment):
        """Time on the new timeline; moments inside removed parts snap forward."""
        for (a, b), offset in zip(segments, offsets):
            if moment < a:
                return offset
            if moment <= b:
                return offset + (moment - a)
        return new_duration

    new_words = []
    for item, _ in speech:
        moved = dict(item)
        moved["start"] = remap(item["start"])
        moved["end"] = max(moved["start"] + 0.04, remap(item["end"]))
        new_words.append(moved)

    return {
        "segments": [(a, b) for a, b in segments],
        "words": new_words,
        "duration": new_duration,
        "remap": remap,
        "pauses": len(segments) - 1,
        "fillers": fillers,
    }


def cut_preview(words, duration):
    out = {}
    for level in CUT_LEVELS:
        plan = plan_cuts(words, duration, level)
        if plan:
            out[level] = {
                "saved": round(duration - plan["duration"], 1),
                "duration": round(plan["duration"], 1),
                "pauses": plan["pauses"],
                "fillers": plan["fillers"],
            }
    return out


def cut_graph_parts(segments):
    """Filtergraph pieces that keep only the given segments of input 0."""
    count = len(segments)
    parts = []
    if count > 1:
        parts.append(f"[0:v]split={count}" + "".join(f"[sv{i}]" for i in range(count)))
        parts.append(f"[0:a]asplit={count}" + "".join(f"[sa{i}]" for i in range(count)))
        video_src = [f"[sv{i}]" for i in range(count)]
        audio_src = [f"[sa{i}]" for i in range(count)]
    else:
        video_src, audio_src = ["[0:v]"], ["[0:a]"]
    joined = ""
    for i, (a, b) in enumerate(segments):
        length = b - a
        parts.append(
            f"{video_src[i]}trim=start={a:.3f}:end={b:.3f},setpts=PTS-STARTPTS[cv{i}]"
        )
        parts.append(
            f"{audio_src[i]}atrim=start={a:.3f}:end={b:.3f},asetpts=PTS-STARTPTS,"
            f"afade=t=in:st=0:d=0.02,afade=t=out:st={max(0.0, length - 0.03):.3f}:d=0.03[ca{i}]"
        )
        joined += f"[cv{i}][ca{i}]"
    parts.append(f"{joined}concat=n={count}:v=1:a=1[cvout][caout]")
    return parts



# =========================================================
# SMART B-ROLL AI (stage 4): relevant stock photos and videos
# =========================================================
#
# 1. the LLM picks concrete, visual moments (with a confidence and a reason);
# 2. Pixabay is searched for videos and photos;
# 3. a second LLM check (or a keyword check) rejects pictures that do not match;
# 4. only the chosen file is downloaded, at render time.

def slug_words(url):
    match = re.search(r"/video/([^/]+?)-\d+/?$", str(url))
    return (match.group(1) if match else "").replace("-", " ")


def keyword_relevant(query, description):
    text = str(description or "").lower()
    stems = [w[:5] for w in re.findall(r"[a-z]{3,}", str(query).lower())]
    return any(stem in text for stem in stems)


def pick_video_file(files):
    mp4 = [
        f for f in files
        if str(f.get("file_type", "")).endswith("mp4") and f.get("link") and f.get("width")
    ]
    small = [f for f in mp4 if int(f["width"]) <= 1100] or mp4
    if not small:
        return None
    return min(small, key=lambda f: abs(int(f["width"]) - 720))


async def llm_json(client, system, user):
    payload = {
        "model": LLM_MODEL,
        "temperature": 0,
        "response_format": {"type": "json_object"},
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
    }
    try:
        response = await client.post(
            LLM_CHAT_URL,
            headers={"Authorization": f"Bearer {LLM_API_KEY}"},
            json=payload,
        )
        if response.status_code != 200:
            return None
        return json.loads(response.json()["choices"][0]["message"]["content"])
    except Exception:
        logger.warning("LLM json call failed", exc_info=True)
        return None


async def find_candidates(client, query):
    """Search Pixabay for relevant portrait video clips and photos."""
    candidates = []
    try:
        found = await client.get(
            "https://pixabay.com/api/videos/",
            params={
                "key": PIXABAY_API_KEY, "q": query, "per_page": 4,
                "video_type": "all", "safesearch": "true",
            },
        )
        videos = found.json().get("hits") if found.status_code == 200 else None
        for video in videos or []:
            variants = video.get("videos") or {}
            chosen = variants.get("medium") or variants.get("large") or variants.get("small")
            length = float(video.get("duration") or 0)
            if chosen and chosen.get("url") and length >= 1.5:
                candidates.append({
                    "kind": "video", "url": chosen["url"],
                    "thumb": chosen.get("thumbnail", ""),
                    "alt": str(video.get("tags", ""))[:160],
                    "credit": str(video.get("user", ""))[:60],
                    "dur": length,
                })
    except Exception:
        logger.warning("Pixabay video search failed", exc_info=True)
    try:
        found = await client.get(
            "https://pixabay.com/api/",
            params={
                "key": PIXABAY_API_KEY, "q": query, "per_page": 4,
                "image_type": "photo", "orientation": "vertical",
                "safesearch": "true",
            },
        )
        photos = found.json().get("hits") if found.status_code == 200 else None
        for photo in photos or []:
            image_url = photo.get("largeImageURL") or photo.get("webformatURL")
            if image_url:
                candidates.append({
                    "kind": "photo", "url": image_url,
                    "thumb": photo.get("previewURL", ""),
                    "alt": str(photo.get("tags", ""))[:160],
                    "credit": str(photo.get("user", ""))[:60],
                    "dur": 0.0,
                })
    except Exception:
        logger.warning("Pixabay photo search failed", exc_info=True)
    return candidates[:6]

async def verify_candidates(client, phrase, query, candidates):
    """Indexes of candidates that really match what is being said."""
    fallback = [i for i, c in enumerate(candidates) if keyword_relevant(query, c["alt"])]
    listing = "; ".join(
        f"{i}: {c['alt'] or 'no description'}" for i, c in enumerate(candidates)
    )
    raw = await llm_json(
        client,
        "You check stock footage. Given a spoken phrase, the search intent and numbered "
        "candidate descriptions, return the candidates that clearly show what the phrase "
        "talks about. Reject unrelated, misleading, abstract or unknown ones. Answer only "
        'with JSON: {"ok":[indexes]}.',
        f"Spoken phrase: {phrase}\nSearch intent: {query}\nCandidates: {listing}",
    )
    if raw and isinstance(raw.get("ok"), list):
        return [i for i in raw["ok"] if isinstance(i, int) and 0 <= i < len(candidates)]
    return fallback


# Deterministic Tajik/Russian -> English visual vocabulary.
# This path works even when no paid LLM endpoint is configured.
BROLL_TERMS = {
    "мошин":"car", "автомобил":"car", "нақлиёт":"car", "машина":"car", "авто":"car",
    "хона":"house", "бино":"building", "биноҳо":"buildings", "квартира":"apartment",
    "мактаб":"school", "донишгоҳ":"university", "курс":"classroom", "курси":"classroom",
    "телефон":"smartphone", "компютер":"computer", "ноутбук":"laptop", "интернет":"technology",
    "пул":"money", "бизнес":"business", "корхона":"business office", "тиҷорат":"business",
    "пизишк":"doctor", "духтур":"doctor", "беморхона":"hospital", "саломатӣ":"healthcare",
    "варзиш":"fitness", "футбол":"football", "бозӣ":"sports", "давидан":"running",
    "ғизо":"food", "таом":"food", "нон":"bread", "мева":"fruit", "сабзавот":"vegetables",
    "об":"water", "қаҳва":"coffee", "чой":"tea", "ош":"pilaf food",
    "сафар":"travel", "ҳавопаймо":"airplane", "самолёт":"airplane", "фурудгоҳ":"airport",
    "меҳмонхона":"hotel", "баҳр":"sea", "кӯҳ":"mountain", "дарё":"river",
    "тоҷикистон":"tajikistan landscape", "душанбе":"dushanbe city",
    "либос":"clothing", "пойафзол":"shoes", "соат":"watch", "сумка":"bag",
    "камера":"camera", "видео":"video camera", "акс":"photography",
    "сохтмон":"construction", "коргар":"construction worker", "семент":"cement",
    "энергия":"solar panels", "барқ":"electricity", "офтоб":"sun", "кишоварзӣ":"farming",
    "гул":"flowers", "дарахт":"tree", "ҳайвон":"animals", "саг":"dog", "гурба":"cat",
    "машина":"car", "дом":"house", "здание":"building", "школа":"school",
    "университет":"university", "курс":"classroom", "обучение":"education",
    "телефон":"smartphone", "компьютер":"computer", "ноутбук":"laptop",
    "деньги":"money", "бизнес":"business", "работа":"office work", "компания":"office",
    "врач":"doctor", "больница":"hospital", "здоровье":"healthcare",
    "спорт":"fitness", "футбол":"football", "еда":"food", "пища":"food",
    "хлеб":"bread", "фрукты":"fruit", "овощи":"vegetables", "вода":"water",
    "кофе":"coffee", "чай":"tea", "плов":"pilaf food", "путешествие":"travel",
    "самолет":"airplane", "аэропорт":"airport", "отель":"hotel", "море":"sea",
    "горы":"mountains", "река":"river", "одежда":"clothing", "обувь":"shoes",
    "часы":"watch", "сумка":"bag", "камера":"camera", "строительство":"construction",
    "рабочий":"construction worker", "цемент":"cement", "электричество":"electricity",
    "солнце":"sun", "сельское":"farming", "цветы":"flowers", "дерево":"tree",
    "животные":"animals", "собака":"dog", "кошка":"cat",
}

def fallback_broll_moments(words, duration, limit):
    picked = []
    last_at = -99.0
    for index, item in enumerate(words):
        token = re.sub(r"[^а-яёғӣқӯҳҷa-z0-9-]", "", str(item.get("word", "")).lower())
        query = BROLL_TERMS.get(token)
        if not query:
            # Handle common Tajik/Russian suffixes (plural, case endings) safely.
            for stem in sorted(BROLL_TERMS, key=len, reverse=True):
                if len(stem) >= 4 and token.startswith(stem):
                    query = BROLL_TERMS[stem]
                    break
        if not query:
            continue
        at = float(item.get("start", 0) or 0)
        if at < 1.2 or at > duration - 2.0 or at - last_at < 7.0:
            continue
        picked.append({"index": index, "query": query, "confidence": 0.78,
                       "reason": "Matched a concrete spoken topic"})
        last_at = at
        if len(picked) >= limit:
            break
    return picked


async def suggest_broll(words, duration, client=None, topic=""):
    """Select relevant B-roll; has a no-LLM fallback and reports actual assets."""
    info = {"suggested": 0, "kept": 0, "source": "none"}
    if not PIXABAY_API_KEY or len(words) < 3 or duration < 6:
        logger.warning("B-roll unavailable: Pixabay key missing or clip too short")
        return [], info
    own_client = client is None
    if own_client:
        client = httpx.AsyncClient(timeout=35.0, follow_redirects=True)
    moments = []
    try:
        # Brandly needs several short visual beats in its final B-roll outro.
        limit = max(1, min(4, int(duration // 6)))
        raw = None
        if LLM_API_KEY and LLM_CHAT_URL and KEYWORDS_LLM:
            numbered = " ".join(f"{i}:{w['word']}" for i, w in enumerate(words))
            if topic:
                numbered = f"Video topic: {topic}\\n" + numbered
            raw = await llm_json(
                client,
                "You are a professional video editor choosing B-roll. The speech is "
                "Tajik or Russian. Choose concrete visual nouns and activities only; "
                "never choose abstract concepts. Select well-spaced moments. Return JSON "
                '{"moments":[{"index":0,"query":"2-3 English stock search words",'
                '"confidence":0.0,"reason":"short reason"}]}.',
                numbered,
            )
        proposed = (raw or {}).get("moments", []) if raw else []
        if not proposed:
            proposed = fallback_broll_moments(words, duration, limit)
            info["source"] = "dictionary_fallback"
        else:
            info["source"] = "llm"
        last_at = -99.0
        for item in proposed[:max(limit * 2, 5)]:
            try:
                index = int(item["index"])
                query = str(item["query"]).strip()[:60]
                conf = float(item.get("confidence", 0.7))
                reason = str(item.get("reason", ""))[:70]
            except (KeyError, TypeError, ValueError):
                continue
            if not (0 <= index < len(words)) or not query:
                continue
            at = float(words[index].get("start", 0) or 0)
            if at < 1.0 or at > duration - 2.0 or at - last_at < 6.0 or conf < 0.55:
                continue
            info["suggested"] += 1
            candidates = await find_candidates(client, query)
            if not candidates:
                # Retry a shorter search phrase; Pixabay can be strict with long queries.
                short_query = query.split()[0]
                if short_query != query:
                    candidates = await find_candidates(client, short_query)
                    if candidates:
                        query = short_query
            if not candidates:
                logger.info("No Pixabay B-roll candidates for query=%s", query)
                continue
            phrase = " ".join(w.get("word", "") for w in words[max(0, index - 5):index + 6])
            good = await verify_candidates(client, phrase, query, candidates)
            alts = [candidates[i] for i in good if 0 <= i < len(candidates)][:3]
            # If verification service is unavailable, retain the first Pixabay result
            # rather than silently dropping every image. Query itself is the relevance gate.
            if not alts:
                alts = candidates[:1]
            moments.append({
                "id": len(moments), "at": round(at, 2), "query": query, "reason": reason,
                "conf": round(conf, 2), "thumb": alts[0].get("thumb", ""),
                "credit": alts[0].get("credit", ""), "alts": alts,
            })
            last_at = at
            if len(moments) >= limit:
                break
        info["kept"] = len(moments)
        logger.info("B-roll selection: %s", info)
    except Exception:
        logger.warning("B-roll suggestion failed", exc_info=True)
    finally:
        if own_client:
            await client.aclose()
    return moments, info


def public_card(card):
    out = {k: card.get(k) for k in ("id", "at", "query", "reason", "conf", "thumb", "credit")}
    out["alts"] = [
        {"kind": a.get("kind"), "thumb": a.get("thumb"), "alt": a.get("alt")}
        for a in card.get("alts", [])
    ]
    return out


async def fetch_broll_assets(cards, work_dir):
    """Download the chosen candidate of every selected moment; failures are skipped."""
    out = []
    if not cards:
        return out
    timeout = httpx.Timeout(60.0, connect=15.0)
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
        for card in cards:
            alts = card.get("alts") or []
            if not alts:
                continue
            cand = alts[max(0, min(int(card.get("alt_index", 0)), len(alts) - 1))]
            is_video = cand.get("kind") == "video"
            limit = 30 * 1024 * 1024 if is_video else 8 * 1024 * 1024
            path = Path(work_dir) / f"broll_{card['id']}{'.mp4' if is_video else '.jpg'}"
            try:
                async with client.stream("GET", cand["url"]) as response:
                    if response.status_code != 200:
                        continue
                    size = 0
                    with path.open("wb") as handle:
                        async for chunk in response.aiter_bytes(65536):
                            size += len(chunk)
                            if size > limit:
                                raise ValueError("file too large")
                            handle.write(chunk)
                if path.stat().st_size < 2000:
                    continue
                out.append(dict(card, file=str(path), kind=cand.get("kind", "photo"),
                                dur=float(cand.get("dur") or 0.0)))
            except Exception:
                logger.warning("B-roll download failed", exc_info=True)
                with suppress(OSError):
                    path.unlink()
    return out


# =========================================================
# PRO SOUND DESIGN (stage 4): synthesised effects mixed to the voice level
# =========================================================

SFX_SPECS = {
    "whoosh": ("anoisesrc=d=0.5:c=pink:a=0.9",
               "highpass=f=350,lowpass=f=6500,afade=t=in:st=0:d=0.16,afade=t=out:st=0.2:d=0.3"),
    "impact": ("aevalsrc='sin(2*PI*(55+80*exp(-t*20))*t)*exp(-t*6)+0.5*(random(0)-0.5)*exp(-t*45)':d=0.8:s=44100",
               "lowpass=f=1200"),
    "hit": ("aevalsrc='sin(2*PI*150*t)*exp(-t*16)+0.4*(random(0)-0.5)*exp(-t*38)':d=0.35:s=44100", "anull"),
    "pop": ("aevalsrc='sin(2*PI*(500+900*exp(-t*60))*t)*exp(-t*38)':d=0.18:s=44100", "anull"),
    "click": ("aevalsrc='(random(0)-0.5)*exp(-t*160)*1.6':d=0.06:s=44100", "highpass=f=1500"),
    "riser": ("aevalsrc='(0.15+0.85*t)*(0.5*sin(2*PI*(180+1400*t*t)*t)+0.5*(random(0)-0.5))':d=0.95:s=44100",
              "lowpass=f=5500,afade=t=out:st=0.9:d=0.05"),
}
SFX_BASE = {"whoosh": 0.55, "impact": 0.9, "hit": 0.7, "pop": 0.55, "click": 0.5, "riser": 0.45}
ANIM_SOUND = {
    "pop_up": "pop", "bounce": "pop", "elastic": "pop", "pop_3d": "pop", "zoom_in": "pop",
    "stretch": "pop", "liquid": "pop", "glitch": "click", "scramble": "click",
    "rgb_split": "click", "flicker": "click", "typewriter": "click",
    "letter_by_letter": "click", "shake": "hit", "slide_up": "whoosh", "slide_left": "whoosh",
    "roll_in": "whoosh", "spin_in": "whoosh", "flip_in": "whoosh", "split": "whoosh",
    "swing": "whoosh", "rotate_3d": "whoosh",
}
TRANS_SOUND = {"whip": "whoosh", "swipe": "whoosh", "motion": "whoosh", "zoom": "whoosh",
               "flash": "hit", "blur": "riser", "glitch": "click"}


def plan_sound(duration, words, starts, trans_points, cards, anim_log, zoom_on, brandly=False):
    """Sound cues (time, kind, strength) tied to what happens on screen."""
    groups = []
    if words:
        groups.append([(max(0.0, words[0]["start"] - 0.02), "impact", 0.8)])
    group = []
    for card in cards:
        group.append((card["at"], "whoosh" if brandly else ("hit" if card.get("mode") == "full" else "pop"), 1.0))
        if card["at"] >= 1.0:
            group.append((card["at"] - 0.9, "riser", 1.0))
    groups.append(group)
    group = []
    for moment, kind in trans_points:
        sound = TRANS_SOUND.get(kind, "whoosh")
        if sound == "riser" and moment < 1.0:
            group.append((moment, "whoosh", 0.7))
        elif sound == "riser":
            group.append((moment - 0.9, "riser", 1.0))
        else:
            group.append((moment, sound, 1.0))
    groups.append(group)
    groups.append([(t, ANIM_SOUND[n], 0.8) for t, n in anim_log if n in ANIM_SOUND])
    if zoom_on:
        groups.append([(s, "whoosh", 0.45) for i, s in enumerate(starts) if i % 2 == 0])
    cues = []
    for group in groups:
        for moment, kind, strength in group:
            if not 0 <= moment < duration - 0.2:
                continue
            if all(abs(moment - other) >= 0.45 for other, _, _ in cues):
                cues.append((moment, kind, strength))
    cues.sort()
    return cues[:24]


def sfx_gain(level, mean_db):
    offset = {"light": 3, "normal": 8, "strong": 12}.get(level, 8)
    target = min(-3.0, (mean_db if mean_db is not None else -20.0) + offset)
    return max(0.03, min(0.9, 10 ** (target / 20.0)))


async def measure_voice_level(path, work_dir):
    try:
        text = await run_command(
            ["ffmpeg", "-nostdin", "-hide_banner", "-i", str(path), "-vn",
             "-af", "volumedetect", "-f", "null", "-"],
            timeout=60, cwd=work_dir, capture=True,
        )
        match = re.search(r"mean_volume:\s*(-?[\d.]+)\s*dB", text)
        return float(match.group(1)) if match else None
    except Exception:
        return None


async def make_sfx_files(kinds, work_dir):
    files = {}
    for kind in kinds:
        source, chain = SFX_SPECS[kind]
        path = Path(work_dir).resolve() / f"sfx_{kind}.wav"
        await run_command(
            ["ffmpeg", "-y", "-nostdin", "-hide_banner", "-loglevel", "error",
             "-f", "lavfi", "-i", source,
             "-af", chain + ",aformat=channel_layouts=stereo", str(path)],
            timeout=30, cwd=work_dir,
        )
        files[kind] = str(path)
    return files


# =========================================================
# AI SCENE DETECTION, AUTO CAMERA MOTION, SMART COLOR GRADING (stage 5)
# =========================================================

SCENE_ROLES = ("hook", "point", "example", "question", "cta", "outro", "emphasis")
ROLE_IMPORTANCE = {"hook": 1.0, "emphasis": 0.85, "question": 0.7, "cta": 0.8,
                   "example": 0.55, "point": 0.5, "outro": 0.6}


def _scene_times(words, ranges, duration):
    """Contiguous time ranges: boundaries sit in the middle of the pauses."""
    bounds = [0.0]
    for (_, prev_end), (next_start, _) in zip(ranges, ranges[1:]):
        bounds.append((words[prev_end]["end"] + words[next_start]["start"]) / 2.0)
    bounds.append(duration)
    return list(zip(bounds, bounds[1:]))


def heuristic_scenes(words, duration):
    if not words:
        return []
    ranges, first = [], 0
    for index, item in enumerate(words):
        following = words[index + 1] if index + 1 < len(words) else None
        pause = following["start"] - item["end"] if following else 99.0
        sentence_end = item["word"].rstrip().endswith((".", "?", "!", "…"))
        if following is None or pause >= 0.7 or (sentence_end and index - first >= 3):
            ranges.append((first, index))
            first = index + 1
    merged = []
    for a, b in ranges:
        length = words[b]["end"] - words[a]["start"]
        if merged and length < 2.0:
            merged[-1] = (merged[-1][0], b)
        else:
            merged.append((a, b))
    if len(merged) > 1 and words[merged[0][1]]["end"] - words[merged[0][0]]["start"] < 2.0:
        merged[1] = (merged[0][0], merged[1][1])
        merged.pop(0)
    scenes = []
    for number, ((a, b), (t0, t1)) in enumerate(zip(merged, _scene_times(words, merged, duration))):
        text = " ".join(w["word"] for w in words[a:b + 1]).rstrip()
        if number == 0:
            role = "hook"
        elif number == len(merged) - 1:
            role = "outro"
        elif text.endswith("?"):
            role = "question"
        elif text.endswith("!"):
            role = "emphasis"
        else:
            role = "point"
        scenes.append({"start": round(t0, 2), "end": round(t1, 2), "role": role,
                       "importance": ROLE_IMPORTANCE[role]})
    return scenes


async def analyze_scenes(words, duration, client=None):
    """Topic and scenes (role, importance). Uses the LLM, falls back to rules."""
    result = {"topic": "", "scenes": heuristic_scenes(words, duration)}
    if not (LLM_API_KEY and LLM_CHAT_URL and KEYWORDS_LLM) or len(words) < 8:
        return result
    own = client is None
    if own:
        client = httpx.AsyncClient(timeout=25.0)
    try:
        numbered = " ".join(f"{i}:{w['word']}" for i, w in enumerate(words))
        raw = await llm_json(
            client,
            "You analyse a short video transcript (Tajik or Russian), numbered word by "
            "word. Split it into 2-8 contiguous scenes by meaning (new point, topic change, "
            "question, example, call to action). Answer only with JSON: "
            '{"topic":"short English topic","scenes":[{"start":first word index,'
            '"end":last word index,"role":"hook|point|example|question|cta|outro",'
            '"importance":0..1}]}. The first scene starts at 0, every next scene starts '
            "right after the previous one ends, the last one ends at the last word.",
            numbered,
        )
        items = (raw or {}).get("scenes")
        if isinstance(items, list) and 1 <= len(items) <= 10:
            ranges, ok, expected = [], True, 0
            for item in items:
                a, b = int(item["start"]), int(item["end"])
                if a != expected or b < a or b >= len(words):
                    ok = False
                    break
                ranges.append((a, b))
                expected = b + 1
            if ok and expected == len(words):
                scenes = []
                for item, (t0, t1) in zip(items, _scene_times(words, ranges, duration)):
                    role = item.get("role") if item.get("role") in SCENE_ROLES else "point"
                    try:
                        importance = max(0.0, min(1.0, float(item.get("importance"))))
                    except (TypeError, ValueError):
                        importance = ROLE_IMPORTANCE[role]
                    scenes.append({"start": round(t0, 2), "end": round(t1, 2),
                                   "role": role, "importance": round(importance, 2)})
                result = {"topic": str(raw.get("topic", ""))[:80], "scenes": scenes}
    except Exception:
        logger.warning("Scene analysis failed, using rules", exc_info=True)
    finally:
        if own:
            await client.aclose()
    return result


async def detect_visual_cuts(video_path, work_dir, duration):
    """Times of real picture changes in the source video (edited footage)."""
    video_path, work_dir = Path(video_path).resolve(), Path(work_dir).resolve()
    try:
        text = await run_command(
            ["ffmpeg", "-nostdin", "-hide_banner", "-i", str(video_path),
             "-vf", "scale=160:-2,select='gt(scene,0.35)',showinfo", "-an", "-f", "null", "-"],
            timeout=90, cwd=work_dir, capture=True,
        )
    except Exception:
        return []
    times = [float(x) for x in re.findall(r"pts_time:([\d.]+)", text)]
    return [t for t in times if 0.5 < t < duration - 0.5][:40]


def merge_visual_cuts(scenes, cuts):
    """Split a scene where the picture really changes (>= 1.2 s from the edges)."""
    out = [dict(s) for s in scenes]
    for moment in cuts:
        for index, scene in enumerate(out):
            if scene["start"] + 1.2 <= moment <= scene["end"] - 1.2:
                tail = dict(scene, start=round(moment, 2), role="point", importance=0.5)
                scene["end"] = round(moment, 2)
                out.insert(index + 1, tail)
                break
    return out


def _clamp(value, low, high):
    return max(low, min(high, value))


def camera_terms(kind, scenes, duration, width, height):
    """Expression terms (zoom, x shift, y shift) for the camera motion kind."""
    if kind in (None, "", "off", "dynamic"):
        return [], [], []
    scenes = scenes or [{"start": 0.0, "end": duration, "role": "point", "importance": 0.5}]
    zoom, shift_x, shift_y = [], [], []
    impact_times = []
    for number, scene in enumerate(scenes):
        start, end = float(scene["start"]), float(scene["end"])
        length = max(end - start, 0.5)
        gate = f"gte(t,{start:.2f})*lt(t,{end:.2f})"
        role, importance = scene.get("role", "point"), scene.get("importance", 0.5)
        if kind == "zoom_in":
            direction, amount = "in", 0.10
        elif kind == "zoom_out":
            direction, amount = "out", 0.10
        elif kind == "push":
            direction, amount = ("in" if number % 2 == 0 else "out"), 0.07
        elif kind == "shake":
            direction, amount = "in", 0.05
        else:  # auto: chosen by what the scene is about
            if role == "hook":
                direction, amount = "in", 0.12
            elif role in ("emphasis", "cta") or importance >= 0.8:
                direction, amount = "in", 0.14
                impact_times.append(start)
            elif role == "question":
                direction, amount = "in", 0.08
            elif role in ("outro", "example"):
                direction, amount = "out", 0.07
            else:
                direction, amount = ("in" if number % 2 == 0 else "out"), 0.07
        if direction == "in":
            zoom.append(f"+{amount}*{gate}*(t-{start:.2f})/{length:.2f}")
        else:
            zoom.append(f"+{amount}*{gate}*(1-(t-{start:.2f})/{length:.2f})")
    if kind == "shake":
        zoom.append("+0.04")
        shift_x.append(f"+{0.012 * width:.1f}*sin(2*PI*1.9*t)+{0.006 * width:.1f}*sin(2*PI*4.3*t+1.3)")
        shift_y.append(f"+{0.010 * height:.1f}*sin(2*PI*2.3*t+0.7)+{0.005 * height:.1f}*sin(2*PI*5.1*t)")
        impact_times = [s["start"] for s in scenes[1:] if s.get("importance", 0.5) >= 0.8]
    for moment in impact_times[:6]:
        local = f"(t-{moment:.2f})"
        zoom.append(f"+0.05*gte(t,{moment:.2f})*lt(t,{moment + 0.5:.2f})")
        shift_x.append(f"+{0.03 * width:.1f}*exp(-8*{local})*sin(2*PI*14*{local})*gte(t,{moment:.2f})")
        shift_y.append(f"+{0.02 * height:.1f}*exp(-8*{local})*cos(2*PI*11*{local})*gte(t,{moment:.2f})")
    return zoom, shift_x, shift_y


async def analyze_color(video_path, work_dir, duration):
    """Average exposure, range, saturation and colour cast of the footage."""
    video_path, work_dir = Path(video_path).resolve(), Path(work_dir).resolve()
    stats_file = work_dir / "color_stats.txt"
    try:
        await run_command(
            ["ffmpeg", "-y", "-nostdin", "-hide_banner", "-loglevel", "error",
             "-i", str(video_path),
             "-vf", f"fps={10.0 / max(duration, 1.0):.4f},scale=320:-2,signalstats,"
                    f"metadata=mode=print:file={stats_file.name}",
             "-an", "-f", "null", "-"],
            timeout=90, cwd=work_dir,
        )
        text = stats_file.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return None
    out = {}
    for key in ("YAVG", "YLOW", "YHIGH", "SATAVG", "UAVG", "VAVG"):
        values = [float(x) for x in re.findall(rf"lavfi\.signalstats\.{key}=([\d.]+)", text)]
        if not values:
            return None
        out[key.lower()] = sum(values) / len(values)
    return out


LOOK_FILTERS = {
    "cinematic": "curves=preset=medium_contrast,"
                 "colorbalance=rs=-0.04:bs=0.07:rm=0.02:bm=-0.02:rh=0.08:gh=0.02:bh=-0.07,"
                 "eq=saturation=1.06,vignette=PI/7",
    "warm": "colorbalance=rs=0.05:bs=-0.05:rm=0.05:bm=-0.05:rh=0.04:bh=-0.04",
    "cool": "colorbalance=rs=-0.05:bs=0.05:rm=-0.05:bm=0.05:rh=-0.04:bh=0.04",
    "vivid": "eq=saturation=1.28:contrast=1.08",
    "film": "curves=m='0/0.05 0.5/0.5 1/0.95',eq=saturation=0.92,vignette=PI/6",
}


def grade_filter(stats, look, strength=0.8):
    """Automatic correction from the measured footage, plus an optional look."""
    if look in (None, "", "off"):
        return ""
    parts = []
    if stats:
        y, low, high = stats["yavg"], stats["ylow"], stats["yhigh"]
        bright = _clamp((118 - y) / 255 * 0.55, -0.12, 0.12) * strength
        gamma = 1 + _clamp((118 - y) / 118 * 0.25, -0.18, 0.25) * strength
        spread = max(high - low, 20.0)
        contrast = 1 + (_clamp(185.0 / spread, 0.92, 1.22) - 1) * strength
        saturation = 1 + (_clamp(48.0 / max(stats["satavg"], 6.0), 0.88, 1.25) - 1) * strength
        parts.append(
            f"eq=brightness={bright:.3f}:contrast={contrast:.3f}:"
            f"saturation={saturation:.3f}:gamma={gamma:.3f}"
        )
        du, dv = stats["uavg"] - 128, stats["vavg"] - 128
        cast_r, cast_b = 1.402 * dv, 1.772 * du
        cast_g = -(0.344 * du + 0.714 * dv)
        if max(abs(cast_r), abs(cast_g), abs(cast_b)) > 4:
            gain = lambda c: 1 - _clamp(c / 255 * 0.7, -0.1, 0.1) * strength
            parts.append(
                f"colorchannelmixer=rr={gain(cast_r):.3f}:gg={gain(cast_g):.3f}:bb={gain(cast_b):.3f}"
            )
    if look in LOOK_FILTERS:
        parts.append(LOOK_FILTERS[look])
    return ",".join(parts)



# =========================================================
# AUTO MUSIC (stage 6): original music synthesised per mood, ducked under the voice
# =========================================================
#
# The tracks are generated here (chords, bass, drums, arpeggio), so there are no
# copyright issues. Edit events are snapped to the beat grid.

MUSIC_SR = 44100
MAJOR_SCALE = [0, 2, 4, 5, 7, 9, 11]
MINOR_SCALE = [0, 2, 3, 5, 7, 8, 10]
MOODS = {
    "upbeat": {"bpm": 124, "minor": False, "root": 261.63, "prog": [0, 4, 5, 3],
               "drums": "four", "arp": 2, "bass": "eighth", "pad": 0.55},
    "corporate": {"bpm": 100, "minor": False, "root": 233.08, "prog": [0, 5, 3, 4],
                  "drums": "light", "arp": 2, "bass": "quarter", "pad": 0.6},
    "chill": {"bpm": 84, "minor": False, "root": 220.0, "prog": [5, 3, 0, 4],
              "drums": "soft", "arp": 0, "bass": "half", "pad": 0.8},
    "lofi": {"bpm": 76, "minor": True, "root": 196.0, "prog": [0, 5, 2, 6],
             "drums": "lofi", "arp": 1, "bass": "half", "pad": 0.7},
    "cinematic": {"bpm": 70, "minor": True, "root": 174.61, "prog": [0, 5, 2, 6],
                  "drums": None, "arp": 0, "bass": "whole", "pad": 1.0},
}
MOOD_WORDS = {
    "upbeat": ("fitness", "sport", "workout", "motivation", "success", "energy", "party", "dance", "challenge"),
    "corporate": ("business", "money", "marketing", "startup", "tech", "finance", "sales", "product",
                  "company", "software", "app", "editing"),
    "chill": ("travel", "food", "cooking", "nature", "lifestyle", "family", "beauty", "fashion", "home", "vlog"),
    "lofi": ("study", "education", "learning", "tutorial", "school", "university", "language", "book", "course"),
    "cinematic": ("story", "history", "drama", "mystery", "documentary", "film", "movie", "legend", "journey"),
}
MUSIC_LEVEL_DB = {"low": -14.0, "normal": -10.0, "high": -6.0}


def chord_freqs(root, scale, degree):
    notes = []
    for step in (0, 2, 4):
        index = degree + step
        semis = scale[index % 7] + 12 * (index // 7)
        notes.append(root * 2 ** (semis / 12.0))
    return notes


def synth_music(mood, duration):
    """Mono float samples of an original track (needs numpy)."""
    cfg = MOODS[mood]
    sr = MUSIC_SR
    total = int(duration * sr)
    rng = np.random.default_rng(7)
    beat = 60.0 / cfg["bpm"]
    bar = beat * 4
    out = np.zeros(total, dtype=np.float64)
    scale = MINOR_SCALE if cfg["minor"] else MAJOR_SCALE

    def timeline(length):
        return np.arange(int(length * sr)) / sr

    def add(signal, moment, gain=1.0):
        start = int(moment * sr)
        if start < 0 or start >= total:
            return
        chunk = signal[: total - start]
        out[start:start + len(chunk)] += chunk * gain

    for index in range(int(duration / bar) + 2):
        freqs = chord_freqs(cfg["root"], scale, cfg["prog"][index % 4])
        t = timeline(bar + 0.6)
        env = np.minimum(1.0, t / 0.35) * np.clip((bar + 0.5 - t) / 0.5, 0.0, 1.0)
        pad = np.zeros_like(t)
        for f in freqs:
            for detune in (0.997, 1.003):
                pad += (np.sin(2 * np.pi * f * detune * t)
                        + 0.35 * np.sin(4 * np.pi * f * detune * t)
                        + 0.15 * np.sin(6 * np.pi * f * detune * t))
        add(pad * env * cfg["pad"] * 0.045, index * bar)

    if cfg["bass"]:
        step = {"eighth": beat / 2, "quarter": beat, "half": beat * 2, "whole": bar}[cfg["bass"]]
        moment, count = 0.0, 0
        while moment < duration:
            degree = cfg["prog"][int(moment // bar) % 4]
            f = chord_freqs(cfg["root"] / 2, scale, degree)[0]
            if cfg["bass"] == "eighth" and count % 2 == 1:
                f *= 1.5
            t = timeline(step + 0.1)
            env = np.exp(-t * (4.0 if step < beat else 1.6)) * np.minimum(1.0, t / 0.01)
            add((np.sin(2 * np.pi * f * t) + 0.3 * np.sin(4 * np.pi * f * t)) * env * 0.28, moment)
            moment += step
            count += 1

    if cfg["drums"]:
        t = timeline(0.35)
        kick = np.sin(2 * np.pi * (45 * t + 75 * (1 - np.exp(-30 * t)) / 30)) * np.exp(-t * 11)
        t = timeline(0.25)
        snare = (rng.standard_normal(len(t)) * 0.7 + np.sin(2 * np.pi * 190 * t) * 0.5) * np.exp(-t * 22)
        t = timeline(0.08)
        hat = np.diff(rng.standard_normal(len(t)), prepend=0.0) * np.exp(-t * 70)
        for index in range(4, int(duration / beat) + 1):
            moment, pos = index * beat, index % 4
            style = cfg["drums"]
            if style == "four":
                add(kick, moment, 0.55)
                add(hat, moment + beat / 2, 0.12)
                if pos in (1, 3):
                    add(snare, moment, 0.22)
            elif style == "light":
                if pos in (0, 2):
                    add(kick, moment, 0.5)
                if pos in (1, 3):
                    add(snare, moment, 0.16)
                add(hat, moment + beat / 2, 0.08)
            elif style == "soft":
                if pos in (0, 2):
                    add(kick, moment, 0.4)
                if pos in (1, 3):
                    add(snare, moment, 0.10)
            elif style == "lofi":
                if pos == 0:
                    add(kick, moment, 0.45)
                if pos == 2:
                    add(kick, moment + beat * 0.5, 0.3)
                if pos in (1, 3):
                    add(snare, moment, 0.12)
                add(hat, moment + beat / 2, 0.1)

    if cfg["arp"]:
        step = beat / cfg["arp"]
        moment, count = 0.0, 0
        while moment < duration:
            if moment >= bar:
                degree = cfg["prog"][int(moment // bar) % 4]
                freqs = chord_freqs(cfg["root"] * 2, scale, degree)
                f = [freqs[0], freqs[1], freqs[2], freqs[1]][count % 4]
                t = timeline(0.5)
                env = np.exp(-t * 8) * np.minimum(1.0, t / 0.004)
                tone = (np.sin(2 * np.pi * f * t) + 0.4 * np.sin(4 * np.pi * f * t)
                        + 0.2 * np.sin(6 * np.pi * f * t))
                add(tone * env * 0.1, moment)
            moment += step
            count += 1

    peak = float(np.max(np.abs(out))) or 1.0
    return out / peak * 0.8


def write_wav(path, samples):
    data = (np.clip(samples, -1.0, 1.0) * 32767).astype("<i2")
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(MUSIC_SR)
        handle.writeframes(data.tobytes())


async def make_music(mood, duration, work_dir):
    """Render the track for the mood; returns the file path or None."""
    if np is None or mood not in MOODS:
        return None
    work = Path(work_dir).resolve()
    raw, final = work / f"music_{mood}_raw.wav", work / f"music_{mood}.wav"
    try:
        samples = await asyncio.to_thread(synth_music, mood, duration)
        await asyncio.to_thread(write_wav, raw, samples)
        await run_command(
            ["ffmpeg", "-y", "-nostdin", "-hide_banner", "-loglevel", "error",
             "-i", str(raw),
             "-af", "lowpass=f=8500,aecho=0.7:0.6:70|130:0.25|0.15,"
                    "aformat=channel_layouts=stereo",
             str(final)],
            timeout=90, cwd=work,
        )
        return str(final)
    except Exception:
        logger.warning("Music synthesis failed", exc_info=True)
        return None


def mood_by_rules(topic, words, duration):
    text = (topic or "").lower()
    for mood, keys in MOOD_WORDS.items():
        if any(k in text for k in keys):
            return mood, "topic"
    pace = len(words) / max(duration, 1.0)
    if pace > 3.2:
        return "upbeat", "pace"
    if pace < 2.0:
        return "chill", "pace"
    return "corporate", "pace"


async def choose_mood(words, topic, duration, client=None):
    """{mood, reason}: the LLM picks by topic and tone, rules are the fallback."""
    mood, reason = mood_by_rules(topic, words, duration)
    result = {"mood": mood, "reason": reason, "bpm": MOODS[mood]["bpm"]}
    if not (LLM_API_KEY and LLM_CHAT_URL and KEYWORDS_LLM) or len(words) < 8:
        return result
    own = client is None
    if own:
        client = httpx.AsyncClient(timeout=20.0)
    try:
        sample = " ".join(w["word"] for w in words[:60])
        pace = len(words) / max(duration, 1.0)
        raw = await llm_json(
            client,
            "You choose background music for a short vertical video. Moods: upbeat "
            "(energetic, motivation, sport), corporate (business, tech, product), chill "
            "(lifestyle, travel, food, calm), lofi (study, tutorials, relaxed), cinematic "
            "(story, drama, history). Answer only with JSON: "
            '{"mood":"one of the five","reason":"short English reason"}.',
            f"Topic: {topic or 'unknown'}\nSpeaking pace: {pace:.1f} words per second\n"
            f"Start of the speech: {sample}",
        )
        if raw and raw.get("mood") in MOODS:
            result = {"mood": raw["mood"], "reason": str(raw.get("reason", ""))[:60] or "ai",
                      "bpm": MOODS[raw["mood"]]["bpm"]}
    except Exception:
        logger.warning("Mood choice failed, using rules", exc_info=True)
    finally:
        if own:
            await client.aclose()
    return result


def music_gain(level, voice_db, music_db):
    """Linear gain that puts the music (before ducking) below the voice level."""
    voice = voice_db if voice_db is not None else -20.0
    music = music_db if music_db is not None else -18.0
    target = voice + MUSIC_LEVEL_DB.get(level, -10.0)
    return max(0.02, min(2.0, 10 ** ((target - music) / 20.0)))


def snap_beat(moment, bpm, window=0.12):
    beat = 60.0 / bpm
    nearest = round(moment / beat) * beat
    return nearest if abs(nearest - moment) <= window else moment


def build_audio_graph(cues, kind_inputs, audio_src, gain, music=None):
    """Voice + sound effects (+ music ducked by the voice) -> [aout]."""
    kinds = sorted({kind for _, kind, _ in cues})
    counts = {k: sum(1 for _, kind, _ in cues if kind == k) for k in kinds}
    used = {k: 0 for k in kinds}
    parts = []
    for kind in kinds:
        index, count = kind_inputs[kind], counts[kind]
        if count == 1:
            parts.append(f"[{index}:a]anull[{kind}0]")
        else:
            parts.append(f"[{index}:a]asplit={count}" + "".join(f"[{kind}{i}]" for i in range(count)))
    parts.append(f"{audio_src}aformat=channel_layouts=stereo[a0]")
    mix, inputs = "[a0]", 1
    if music:
        length = music["duration"]
        parts.append("[a0]asplit=2[v1][vk]")
        parts.append(
            f"[{music['index']}:a]aformat=channel_layouts=stereo,atrim=0:{length:.2f},"
            f"volume={music['gain']:.4f},afade=t=in:st=0:d=1.2,"
            f"afade=t=out:st={max(0.0, length - 2.5):.2f}:d=2.5[m0]"
        )
        parts.append("[m0][vk]sidechaincompress=threshold=0.03:ratio=6:attack=15:release=350[mduck]")
        mix, inputs = "[v1][mduck]", 2
    for number, (moment, kind, strength) in enumerate(cues):
        slot = used[kind]
        used[kind] += 1
        ms = int(moment * 1000)
        volume = gain * SFX_BASE[kind] * strength
        parts.append(f"[{kind}{slot}]adelay={ms}|{ms},volume={volume:.4f}[d{number}]")
        mix += f"[d{number}]"
        inputs += 1
    parts.append(f"{mix}amix=inputs={inputs}:duration=first:normalize=0,alimiter=limit=0.95[aout]")
    return ";".join(parts)


# =========================================================
# PRO COVER MAKER (stage 6): best frames + a designed cover with text
# =========================================================

COVER_TEMPLATES = ("bold", "box", "split", "minimal")
COVER_SIZES = {"9:16": (1080, 1920), "16:9": (1280, 720), "1:1": (1080, 1080)}


def score_frame(path):
    """(sharpness, exposure, colourfulness) of a frame; needs numpy and Pillow."""
    image = Image.open(path).convert("RGB")
    rgb = np.asarray(image, dtype=np.float64)
    gray = rgb.mean(axis=2)
    h, w = gray.shape
    centre = gray[int(h * 0.2):int(h * 0.8), int(w * 0.2):int(w * 0.8)]
    lap = (4 * centre[1:-1, 1:-1] - centre[:-2, 1:-1] - centre[2:, 1:-1]
           - centre[1:-1, :-2] - centre[1:-1, 2:])
    exposure = 1.0 - abs(float(gray.mean()) - 125.0) / 125.0
    rg = rgb[..., 0] - rgb[..., 1]
    yb = 0.5 * (rgb[..., 0] + rgb[..., 1]) - rgb[..., 2]
    colour = float(np.sqrt(rg.std() ** 2 + yb.std() ** 2))
    return float(lap.var()), exposure, colour


def rank_frames(scores, times, limit=6):
    if not scores:
        return []
    sharp_max = max(s[0] for s in scores) or 1.0
    colour_max = max(s[2] for s in scores) or 1.0
    order = sorted(
        range(len(scores)),
        key=lambda i: -(0.6 * scores[i][0] / sharp_max + 0.25 * max(scores[i][1], 0.0)
                        + 0.15 * scores[i][2] / colour_max),
    )
    chosen = []
    for index in order:
        if all(abs(times[index] - times[other]) >= 1.0 for other in chosen):
            chosen.append(index)
        if len(chosen) >= limit:
            break
    return chosen


def fallback_title(scenes_words):
    words = [clean for clean in (w.strip(".,!?…:;") for w in scenes_words) if clean]
    return " ".join(words[:5])


async def suggest_cover_title(words, topic, client=None):
    fallback = fallback_title([w["word"] for w in words[:8]])
    if not (LLM_API_KEY and LLM_CHAT_URL and KEYWORDS_LLM) or len(words) < 6:
        return fallback
    own = client is None
    if own:
        client = httpx.AsyncClient(timeout=20.0)
    try:
        raw = await llm_json(
            client,
            "You write a short cover title (2-6 words) for a vertical video. Use the same "
            "language as the speech (Tajik in Cyrillic or Russian). Make it a strong hook, "
            'no hashtags, no quotes. Answer only with JSON: {"title":"..."}.',
            f"Topic: {topic or 'unknown'}\nSpeech: " + " ".join(w["word"] for w in words[:70]),
        )
        title = str((raw or {}).get("title", "")).strip().strip("\"'«»")[:70]
        return title or fallback
    except Exception:
        return fallback
    finally:
        if own:
            await client.aclose()


async def prepare_cover(video_path, work_dir, duration, words, topic):
    """Candidate frames (sharp, well exposed) with thumbnails, and a title."""
    info = {"times": [], "thumbs": [], "title": ""}
    if np is None or Image is None:
        return info
    video_path, work_dir = Path(video_path).resolve(), Path(work_dir).resolve()
    probe = work_dir / "cover_probe"
    probe.mkdir(exist_ok=True)
    try:
        count = int(min(24, max(6, duration * 2)))
        rate = count / max(duration, 1.0)
        await run_command(
            ["ffmpeg", "-y", "-nostdin", "-hide_banner", "-loglevel", "error",
             "-i", str(video_path), "-vf", f"fps={rate:.4f},scale=360:-2",
             "-q:v", "4", str(probe / "%03d.jpg")],
            timeout=90, cwd=work_dir,
        )
        files = sorted(probe.glob("*.jpg"))
        times = [min(duration - 0.1, index / rate + 0.05) for index in range(len(files))]
        scores = await asyncio.to_thread(lambda: [score_frame(f) for f in files])
        for index in rank_frames(scores, times):
            data = files[index].read_bytes()
            info["times"].append(round(times[index], 2))
            info["thumbs"].append("data:image/jpeg;base64," + base64.b64encode(data).decode())
    except Exception:
        logger.warning("Cover candidates failed", exc_info=True)
    info["title"] = await suggest_cover_title(words, topic)
    return info


def font_path_for(family):
    names = [family] if family else []
    names += ["Noto Sans", "DejaVu Sans"]
    for name in names:
        try:
            result = subprocess.run(
                ["fc-match", "-f", "%{family}|%{file}", f"{name}:bold"],
                capture_output=True, text=True, timeout=10,
            )
            found, _, path = result.stdout.partition("|")
            if path and Path(path).exists() and name in [x.strip() for x in found.split(",")]:
                return path
        except Exception:
            continue
    return "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"


def _rgb(hex_color):
    hex_color = (hex_color or "FFE500").strip("#")
    return tuple(int(hex_color[i:i + 2], 16) for i in (0, 2, 4))


def _wrap(draw, words, font, max_width):
    lines, current = [], []
    for word in words:
        trial = " ".join(current + [word])
        if current and draw.textlength(trial, font=font) > max_width:
            lines.append(current)
            current = [word]
        else:
            current.append(word)
    if current:
        lines.append(current)
    return lines


def draw_cover(frame_path, out_path, text, template, accent, font_path, size):
    width, height = size
    base = Image.open(frame_path).convert("RGB").resize(size)
    canvas = base.convert("RGBA")
    probe = ImageDraw.Draw(canvas)
    words = text.split() or [""]
    max_width = int(width * 0.86)
    max_lines = 4 if height > width else 3
    start = int(width * (0.105 if height > width else 0.075))
    size_px = start
    while True:
        font = ImageFont.truetype(font_path, size_px)
        lines = _wrap(probe, words, font, max_width)
        fits = len(lines) <= max_lines and all(
            probe.textlength(" ".join(line), font=font) <= max_width for line in lines
        )
        if fits or size_px <= 24:
            break
        size_px = int(size_px * 0.93)
    colour = _rgb(accent)
    key = max((w for w in words if len(w) >= 4), key=len, default=None)
    spacing = int(size_px * 1.15)
    block = spacing * len(lines)

    def gradient(top, bottom, y0, y1, alpha_top, alpha_bottom):
        layer = Image.new("RGBA", size, (0, 0, 0, 0))
        pixels = layer.load()
        for y in range(max(0, y0), min(height, y1)):
            ratio = (y - y0) / max(1, y1 - y0)
            alpha = int(alpha_top + (alpha_bottom - alpha_top) * ratio)
            for x in range(width):
                pixels[x, y] = (0, 0, 0, alpha)
        return layer

    if template == "split":
        canvas = Image.alpha_composite(canvas, Image.new("RGBA", size, (0, 0, 0, 40)))
        panel = Image.new("RGBA", (width, int(block + size_px * 1.4)), (10, 10, 16, 215))
        canvas.alpha_composite(panel, (0, 0))
        y_text = int(size_px * 0.6)
        bar = Image.new("RGBA", (int(width * 0.28), max(6, size_px // 9)), colour + (255,))
        canvas.alpha_composite(bar, (int(width * 0.07), y_text + block + int(size_px * 0.15)))
        align = "left"
    elif template == "minimal":
        canvas = Image.alpha_composite(canvas, gradient(0, 0, int(height * 0.6), height, 0, 190))
        y_text = height - block - int(height * 0.07)
        bar = Image.new("RGBA", (int(width * 0.2), max(5, size_px // 12)), colour + (255,))
        canvas.alpha_composite(bar, (int(width * 0.07), y_text - int(size_px * 0.35)))
        align = "left"
    elif template == "box":
        y_text = int(height * 0.58 - block / 2)
        align = "center"
    else:  # bold
        canvas = Image.alpha_composite(canvas, gradient(0, 0, int(height * 0.42), height, 0, 235))
        y_text = int(height * 0.78 - block / 2)
        align = "center"

    draw = ImageDraw.Draw(canvas)
    for number, line in enumerate(lines):
        line_text = " ".join(line)
        line_width = draw.textlength(line_text, font=font)
        x = int((width - line_width) / 2) if align == "center" else int(width * 0.07)
        y = y_text + number * spacing
        if template == "box":
            pad = int(size_px * 0.22)
            draw.rounded_rectangle(
                [x - pad, y - int(pad * 0.4), x + line_width + pad, y + int(size_px * 1.05) + int(pad * 0.4)],
                radius=int(size_px * 0.22), fill=colour + (255,),
            )
        cursor = x
        for word in line:
            is_key = key is not None and word == key
            if template == "box":
                fill, stroke = (17, 17, 17, 255), 0
            else:
                fill, stroke = (colour + (255,)) if is_key else (255, 255, 255, 255), max(2, size_px // 11)
            draw.text((cursor, y), word, font=font, fill=fill,
                      stroke_width=stroke, stroke_fill=(0, 0, 0, 255))
            cursor += draw.textlength(word + " ", font=font)
    canvas.convert("RGB").save(out_path, "JPEG", quality=92)


async def make_cover(job, moment, template, text, fmt, accent, font_family, grade):
    width, height = COVER_SIZES[fmt]
    work = Path(job["dir"]).resolve()
    frame = work / "cover_frame.png"
    chain = f"scale={width}:{height}:force_original_aspect_ratio=increase,crop={width}:{height}"
    if grade:
        chain += "," + grade
    await run_command(
        ["ffmpeg", "-y", "-nostdin", "-hide_banner", "-loglevel", "error",
         "-ss", f"{moment:.2f}", "-i", str(job["input"]), "-frames:v", "1",
         "-vf", chain, str(frame)],
        timeout=60, cwd=work,
    )
    out = work / f"cover_{uuid.uuid4().hex[:8]}.jpg"
    await asyncio.to_thread(
        draw_cover, frame, out, text, template, accent, font_path_for(font_family),
        (width, height),
    )
    return out



def build_graph(words, base_filter, width, height, duration, options, ass_name,
                fonts_option, card_inputs, segments=None):
    """Return (filtergraph, has_audio_graph, sfx_times)."""
    cut_parts = cut_graph_parts(segments) if segments else []
    steps = [("[cvout]" if segments else "[0:v]") + base_filter]

    lines = make_lines(words)
    starts = [l["start"] for l in lines[1:] if 1.0 < l["start"] < duration - 0.5]
    if options.get("beat_bpm"):
        starts = [snap_beat(moment, options["beat_bpm"]) for moment in starts]

    cam_kind = options.get("camera_eff") or ("dynamic" if options.get("zoom") else "off")
    zoom = zoom_expression(starts) if cam_kind == "dynamic" else None
    c_zoom, c_sx, c_sy = camera_terms(
        cam_kind, options.get("scenes_final") or [], duration, width, height
    )
    t_zoom, t_shift, t_filters = transition_filters(options.get("trans_points") or [], width)
    extra_zoom = c_zoom + t_zoom
    if extra_zoom:
        zoom = (zoom[:-1] if zoom else "(1") + "".join(extra_zoom) + ")"
    if options.get("grade_filter"):
        steps[0] += "," + options["grade_filter"]
    if zoom:
        x_expr = f"(iw-{width})/2*(1{''.join(t_shift)})" + "".join(c_sx)
        y_expr = f"(ih-{height})/2" + "".join(c_sy)
        if t_shift or c_sx or c_sy:
            crop = (f"crop={width}:{height}:x='clip({x_expr},0,iw-{width})'"
                    f":y='clip({y_expr},0,ih-{height})'")
        else:
            crop = f"crop={width}:{height}"
        steps[0] += (
            f",scale=w='2*trunc({width}*{zoom}/2)':h='2*trunc({height}*{zoom}/2)'"
            f":eval=frame:flags=bilinear,{crop}"
        )

    cards = options.get("cards") or []
    marks = options.get("scene_marks")
    if marks is None:
        flash_times = [s for i, s in enumerate(starts) if i % 3 == 2] if options.get("fx") else []
    else:
        flash_times = list(marks) if options.get("fx") else []
    flash_times += [c["at"] for c in cards] if options.get("fx") else []
    flash_times = sorted(flash_times)[:12]
    if flash_times:
        enable = between_expression(flash_times, 0.10)
        steps[0] += (
            f",negate=enable='{enable}',"
            f"rgbashift=rh=-12:bh=12:enable='{enable}'"
        )

    label = "v0"
    if t_filters:
        steps[0] += "," + t_filters
    steps[0] += f"[{label}]"
    graph = cut_parts + [steps[0]]

    for number, card in enumerate(cards):
        start = card["at"]
        end = min(duration, start + card.get("len", 1.9))
        input_index = card_inputs[number]
        shift = f"setpts=PTS-STARTPTS+{start:.2f}/TB"
        new_label = f"v{number + 1}"
        if card.get("mode") == "full":
            if card.get("kind") == "photo":
                # Full-screen Ken Burns-style slow pan; the original speaker remains
                # audible and the still image is visibly integrated into the timeline.
                visual_scale = (
                    f"scale={int(width * 1.12)}:{int(height * 1.12)}:"
                    f"force_original_aspect_ratio=increase,"
                    f"crop={width}:{height}:"
                    f"x='(iw-ow)*(0.5+0.12*sin(t*1.1))':y='(ih-oh)*0.5'"
                )
            else:
                visual_scale = (
                    f"scale={width}:{height}:force_original_aspect_ratio=increase,"
                    f"crop={width}:{height}"
                )
            graph.append(
                f"[{input_index}:v]fps=30,{visual_scale},"
                f"setsar=1,format=yuva420p,{shift},"
                f"fade=t=in:st={start:.2f}:d=0.15:alpha=1,"
                f"fade=t=out:st={max(start, end - 0.15):.2f}:d=0.15:alpha=1[c{number}]"
            )
            graph.append(
                f"[{label}][c{number}]overlay=x=0:y=0:"
                f"enable='between(t,{start:.2f},{end:.2f})':format=auto[{new_label}]"
            )
            label = new_label
            continue
        card_w, card_h = int(width * 0.78), int(height * 0.46)
        radius = 30
        graph.append(
            f"[{input_index}:v]fps=30,scale=w={card_w}:h={card_h}:"
            f"force_original_aspect_ratio=decrease,format=yuva420p,"
            f"geq=lum='lum(X,Y)':cb='cb(X,Y)':cr='cr(X,Y)':"
            f"a='if(gt(abs(W/2-X),W/2-{radius})*gt(abs(H/2-Y),H/2-{radius}),"
            f"if(lte(hypot(abs(W/2-X)-(W/2-{radius}),abs(H/2-Y)-(H/2-{radius})),{radius}),255,0),255)',"
            f"{shift}[c{number}]"
        )
        # Keep the speaker/video visible as a soft, cinematic blurred backdrop
        # while the relevant photo appears sharply in the foreground.
        graph.append(
            f"[{label}]scale={width}:{height}:flags=bilinear,"
            f"gblur=sigma=22:steps=2,eq=brightness=-0.10:saturation=0.88,"
            f"drawbox=x=0:y=0:w=iw:h=ih:color=black@0.16:t=fill,"
            f"format=yuva420p[blur{number}]"
        )
        graph.append(
            f"[blur{number}][c{number}]overlay="
            f"x='(W-w)/2+W*pow(1-min(1,max(0,(t-{start:.2f})/0.32)),3)':"
            f"y='(H-h)/2':enable='between(t,{start:.2f},{end:.2f})':format=auto"
            f"[{new_label}]"
        )
        label = new_label

    if options.get("subtitles", True):
        graph.append(f"[{label}]ass={ass_name}{fonts_option}[vout]")
    else:
        graph.append(f"[{label}]null[vout]")

    return ";".join(graph), starts


async def render_video(
    input_path, output_path, work_dir, words, style_name, video_format,
    info, duration, report, options=None,
):
    options = options or {}
    bmode = options.get("broll", "auto")
    cards0 = [] if bmode == "off" else list(options.get("cards") or [])
    for card in cards0:
        card["mode"] = (("full" if card.get("kind") in ("video", "photo") else "card")
                        if bmode == "auto" else bmode)
        card["len"] = (min(3.0, max(1.8, card.get("dur") or 2.5))
                       if card.get("kind") == "video" else 1.9)
    options = dict(options, cards=cards0)
    input_path = Path(input_path).resolve()
    output_path = Path(output_path).resolve()
    work_dir = Path(work_dir).resolve()

    _, _, display_width, display_height = probe_summary(info)
    width, height = get_output_size(video_format, display_width, display_height)

    segments = None
    cut_points = []
    level = options.get("cut", "off")
    if level in CUT_LEVELS:
        plan = plan_cuts(words, duration, level)
        if plan:
            words = plan["words"]
            segments = plan["segments"]
            duration = plan["duration"]
            cursor = 0.0
            for a, b in plan["segments"][:-1]:
                cursor += b - a
                cut_points.append(cursor)
            options = dict(options, cards=[
                dict(c, at=plan["remap"](c["at"])) for c in (options.get("cards") or [])
            ])
            logger.info("Auto cut %s: %s segments, %.1fs", level, len(segments), duration)

    # BRANDLY: reserve the closing visual section for full-screen B-roll.
    # The original audio track remains underneath as voice-over.
    if style_name == "brandly":
        outro_cards = list(options.get("cards") or [])
        if outro_cards:
            outro_duration = min(11.0, max(4.0, duration * 0.55))
            outro_start = max(0.0, duration - outro_duration)
            slot = outro_duration / len(outro_cards)
            scheduled = []
            for index, card in enumerate(outro_cards):
                item = dict(card)
                item["at"] = round(outro_start + index * slot, 3)
                item["len"] = round(slot, 3)
                item["mode"] = "full"
                scheduled.append(item)
            options = dict(options, cards=scheduled, brandly_outro=True)
            logger.info("Brandly B-roll outro: %s scenes over %.1fs", len(scheduled), outro_duration)

    scenes_final = []
    for scene in options.get("scenes") or []:
        a, b = float(scene["start"]), float(scene["end"])
        if segments:
            a, b = plan["remap"](a), plan["remap"](b)
        a, b = max(0.0, a), min(duration, b)
        if b - a >= 0.6:
            scenes_final.append(dict(scene, start=a, end=b))
    camera = options.get("camera") or ""
    options = dict(
        options,
        scenes_final=scenes_final,
        camera_eff=camera if camera in ("auto", "zoom_in", "zoom_out", "push", "shake", "dynamic", "off") else "",
        scene_marks=([sc["start"] for sc in scenes_final[1:] if sc.get("importance", 0.5) >= 0.8]
                     if scenes_final else None),
        grade_filter=grade_filter(options.get("color"), options.get("grade", "off")),
        trans_points=plan_transitions(
            words, duration, options.get("trans", "off"), cut_points, scenes_final or None
        ),
    )

    mood = None
    music_setting = options.get("music") or "off"
    if np is not None:
        if music_setting in MOODS:
            mood = music_setting
        elif music_setting == "auto":
            suggested = (options.get("music_mood") or {}).get("mood")
            mood = suggested if suggested in MOODS else "corporate"
    if mood and options.get("music_sync", True):
        bpm = MOODS[mood]["bpm"]
        options = dict(
            options,
            beat_bpm=bpm,
            trans_points=[(snap_beat(t, bpm), k) for t, k in options.get("trans_points") or []],
            cards=[dict(c, at=snap_beat(c["at"], bpm)) for c in options.get("cards") or []],
            scene_marks=([snap_beat(t, bpm) for t in options["scene_marks"]]
                         if options.get("scene_marks") is not None else None),
        )

    ass_name = "captions.ass"
    base_filter = get_video_filter(video_format, width, height)
    anim_log = []
    if options.get("subtitles", True):
        build_ass(
            words, style_name, width, height, Path(work_dir) / ass_name,
            accent=options.get("accent"),
            size=options.get("size", "m"),
            position=options.get("position", "auto"),
            font=options.get("font"),
            font2=options.get("font2"),
            mix=options.get("fontmix", False),
            keys=options.get("keys", True),
            anim=options.get("anim"),
            anim_out=options.get("anim_out"),
            anim_log=anim_log,
        )
    fonts_option = ""
    if FONTS_DIR.is_dir() and any(FONTS_DIR.iterdir()):
        fonts_option = f":fontsdir={FONTS_DIR}"

    cards = options.get("cards") or []
    extra_inputs = []
    card_inputs = []
    for card in cards:
        card_inputs.append(1 + len(card_inputs))
        span = f"{card.get('len', 1.9) + 0.4:.2f}"
        if card.get("kind") == "video":
            extra_inputs += ["-stream_loop", "-1", "-t", span, "-i", card["file"]]
        else:
            extra_inputs += ["-loop", "1", "-framerate", "30", "-t", span, "-i", card["file"]]

    graph, starts = build_graph(
        words, base_filter, width, height, duration, options, ass_name,
        fonts_option, card_inputs, segments,
    )

    audio_map = ["-map", "[caout]"] if segments else ["-map", "0:a:0?"]
    audio_src = "[caout]" if segments else "[0:a]"
    level = options.get("sound") or ("normal" if options.get("sfx") else "off")
    cues = []
    if level in ("light", "normal", "strong"):
        cues = plan_sound(
            duration, words, starts, options.get("trans_points") or [], cards,
            anim_log, bool(options.get("zoom")), brandly=(style_name == "brandly"),
        )
    music_info = None
    mean_db = None
    if mood or cues:
        mean_db = await measure_voice_level(input_path, work_dir)
    if mood:
        music_file = await make_music(mood, duration + 1.0, work_dir)
        if music_file:
            music_mean = await measure_voice_level(music_file, work_dir)
            music_info = {
                "file": music_file, "duration": duration,
                "gain": music_gain(options.get("music_level", "normal"), mean_db, music_mean),
            }
    if cues or music_info:
        files = await make_sfx_files(sorted({kind for _, kind, _ in cues}), work_dir) if cues else {}
        kind_inputs = {}
        for kind, path in files.items():
            kind_inputs[kind] = 1 + len(card_inputs) + len(kind_inputs)
            extra_inputs += ["-i", path]
        if music_info:
            music_info["index"] = 1 + len(card_inputs) + len(kind_inputs)
            extra_inputs += ["-i", music_info["file"]]
        graph += ";" + build_audio_graph(
            cues, kind_inputs, audio_src, sfx_gain(level, mean_db), music_info
        )
        audio_map = ["-map", "[aout]"]
        logger.info(
            "Audio design: %s cues, music=%s, voice %.1f dB",
            len(cues), mood if music_info else "off", mean_db or 0.0,
        )

    command = [
        "ffmpeg", "-y", "-nostdin", "-hide_banner", "-loglevel", "error",
        "-progress", "pipe:1", "-nostats",
        "-threads", "1", "-filter_threads", "1", "-filter_complex_threads", "1",
        "-i", str(input_path),
        *extra_inputs,
        "-t", f"{min(float(MAX_DURATION), duration):.3f}",
        "-filter_complex", graph,
        "-map", "[vout]", *audio_map,
        "-c:v", "libx264", "-preset", FFMPEG_PRESET, "-crf", "27",
        "-threads", "1",
        "-c:a", "aac", "-b:a", "128k", "-ac", "2",
        "-pix_fmt", "yuv420p",
        "-movflags", "+faststart",
        "-max_muxing_queue_size", "1024",
        "-fs", str(MAX_OUTPUT_SIZE),
        str(output_path),
    ]

    with tempfile.TemporaryFile(mode="w+b") as error_file:
        process = await asyncio.create_subprocess_exec(
            *command,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=error_file,
            cwd=str(work_dir),
        )

        async def read_progress():
            while True:
                raw = await process.stdout.readline()
                if not raw:
                    break
                line = raw.decode("utf-8", errors="ignore").strip()
                if line.startswith(("out_time_us=", "out_time_ms=")):
                    try:
                        seconds = int(line.split("=", 1)[1]) / 1_000_000
                    except ValueError:
                        continue
                    ratio = min(0.99, seconds / max(duration, 0.1))
                    report(int(ratio * 100), "Монтируем видео")
            await process.wait()

        try:
            await asyncio.wait_for(read_progress(), timeout=900)
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()
            raise UserError(
                "Время обработки видео истекло. Попробуйте более короткое видео.",
                504,
            )

        error_file.seek(0, os.SEEK_END)
        size = error_file.tell()
        error_file.seek(max(0, size - 3000))
        error_text = error_file.read().decode("utf-8", errors="replace")

    if process.returncode != 0:
        logger.error("Render failed: %s", error_text[-3000:])
        raise UserError("Ошибка FFmpeg при монтаже. Попробуйте другой файл.", 500)

    output = Path(output_path)
    if not output.exists() or output.stat().st_size < 1000:
        raise UserError("Не удалось создать готовое видео.", 500)
    if output.stat().st_size >= MAX_OUTPUT_SIZE:
        raise UserError(
            "Готовое видео достигло лимита размера. Попробуйте видео короче.", 413
        )

    logger.info("Render complete: %s bytes", output.stat().st_size)


# =========================================================
# UPLOAD + INPUT CHECKS
# =========================================================

def clean_language(language):
    language = (language or "tg_ru").strip().lower()
    language = LANGUAGE_ALIASES.get(language, language)
    if language not in ALLOWED_LANGUAGES:
        raise HTTPException(status_code=400, detail="Неверно выбран язык.")
    return language


def clean_style(style):
    style = (style or "viral").strip().lower()
    if style not in ALLOWED_STYLES:
        raise HTTPException(status_code=400, detail="Неверный стиль субтитров.")
    return style


def clean_format(video_format):
    video_format = (video_format or "9:16").strip().lower()
    if video_format not in ALLOWED_FORMATS:
        raise HTTPException(status_code=400, detail="Неверный формат видео.")
    return video_format


def clean_options(accent, size, position, subtitles, zoom="1", fx="1", sfx="1",
                  font="", font2="", fontmix="0", cut="off", keys="1",
                  anim="", anim_out="", trans="off", broll="auto", sound="",
                  camera="", grade="off", music="", music_level="normal", music_sync="1"):
    accent = (accent or "").strip().lstrip("#").upper()
    if accent and not re.fullmatch(r"[0-9A-F]{6}", accent):
        raise HTTPException(status_code=400, detail="Неверный цвет.")
    size = (size or "m").strip().lower()
    position = (position or "auto").strip().lower()
    if size not in ("s", "m", "l") or position not in ("auto", "top", "center"):
        raise HTTPException(status_code=400, detail="Неверные параметры субтитров.")
    return {
        "accent": accent or None,
        "size": size,
        "position": position,
        "subtitles": str(subtitles).strip().lower() not in ("0", "false", "off"),
        "zoom": str(zoom).strip().lower() not in ("0", "false", "off"),
        "fx": str(fx).strip().lower() not in ("0", "false", "off"),
        "sfx": str(sfx).strip().lower() not in ("0", "false", "off"),
        "font": (resolve_font(font) or {}).get("family"),
        "font2": (resolve_font(font2) or {}).get("family"),
        "fontmix": str(fontmix).strip().lower() in ("1", "true", "on"),
        "cut": cut if cut in CUT_LEVELS else "off",
        "keys": str(keys).strip().lower() not in ("0", "false", "off"),
        "anim": anim if anim in ANIM_IN or anim in ("auto", "mix") else "",
        "anim_out": anim_out if anim_out in ANIM_OUT or anim_out == "auto" else "",
        "broll": broll if broll in ("off", "card", "full", "auto") else "auto",
        "camera": camera if camera in ("auto", "dynamic", "zoom_in", "zoom_out", "push", "shake", "off") else "",
        "grade": grade if grade in ("off", "auto", "cinematic", "warm", "cool", "vivid", "film") else "off",
        "music": music if music in MOODS or music in ("auto", "off") else "",
        "music_level": music_level if music_level in MUSIC_LEVEL_DB else "normal",
        "music_sync": str(music_sync).strip().lower() not in ("0", "false", "off"),
        "sound": sound if sound in ("off", "light", "normal", "strong") else "",
        "trans": trans if trans in TRANSITION_KINDS or trans == "auto" else "off",
    }


def new_work_dir():
    BASE_DIR.mkdir(parents=True, exist_ok=True)
    work_dir = BASE_DIR / uuid.uuid4().hex
    work_dir.mkdir(parents=True, exist_ok=True)
    return work_dir


async def save_upload(file, work_dir):
    # Accept common video containers/codecs supported by the installed FFmpeg.
    # Keep a sanitized original suffix so ffprobe/ffmpeg can recognize the container.
    raw_extension = Path((file.filename or "video.mp4").lower()).suffix
    extension = raw_extension if raw_extension in ALLOWED_EXTENSIONS else ".video"
    input_path = Path(work_dir) / f"input{extension}"
    total = 0

    with input_path.open("wb") as destination:
        while True:
            chunk = await file.read(UPLOAD_CHUNK_SIZE)
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_INPUT_SIZE:
                raise HTTPException(
                    status_code=413,
                    detail="Размер видео не должен превышать 100 MB.",
                )
            destination.write(chunk)

    if total == 0:
        raise HTTPException(status_code=400, detail="Видеофайл пуст.")

    logger.info("Upload completed: %s bytes", total)
    return input_path


async def inspect_video(input_path):
    info, duration = await get_video_info(input_path)
    has_video, has_audio, _, _ = probe_summary(info)

    if not has_video:
        raise UserError("В файле нет видеодорожки.")
    if duration > MAX_DURATION:
        raise UserError(
            f"Длительность видео не должна превышать {MAX_DURATION} секунд.", 413
        )
    if not has_audio:
        raise UserError("В видео нет звука, поэтому речь распознать нельзя.")

    return info, duration


# =========================================================
# JOBS
# =========================================================

JOBS = {}
JOB_TASKS = set()
ACTIVE_STATES = {"queued", "transcribing", "rendering"}


def remove_job(job_id):
    job = JOBS.pop(job_id, None)
    if job:
        shutil.rmtree(job["dir"], ignore_errors=True)


def purge_expired_jobs():
    now = time.time()
    for job_id, job in list(JOBS.items()):
        if job["status"] in ACTIVE_STATES:
            continue
        if now - job["updated"] > JOB_TTL_SECONDS:
            remove_job(job_id)


def touch(job, **values):
    job.update(values)
    job["updated"] = time.time()


def public_job(job):
    data = {
        "job_id": job["id"],
        "status": job["status"],
        "stage": job["stage"],
        "progress": job["progress"],
        "language": job["language"],
        "filename": job.get("filename", "Видео"),
        "updated": int(job.get("updated", time.time())),
        "duration": round(job["duration"], 2),
        "error": job["error"],
    }
    if job["status"] in ("transcribed", "rendering", "done") or job["lines"]:
        data["lines"] = job["lines"]
        data["detected"] = job["detected"]
        data["cut_preview"] = job.get("cut_preview", {})
        data["scenes"] = job.get("scenes", [])
        data["topic"] = job.get("topic", "")
        data["visual_cuts"] = job.get("visual_cuts", 0)
        stats = job.get("color")
        data["color"] = {k: round(v, 1) for k, v in stats.items()} if stats else None
        data["music"] = job.get("music_mood")
        cover = job.get("cover") or {}
        data["cover"] = {"times": cover.get("times", []), "title": cover.get("title", "")}
        data["cards"] = [public_card(c) for c in job["cards"]]
        data["broll_info"] = job.get("broll_info", {})
    if job["status"] == "done":
        data["download_url"] = f"/api/jobs/{job['id']}/download"
    return data


def get_job(job_id, user):
    job = JOBS.get(job_id)
    if not job or job["user_id"] != user["id"]:
        raise HTTPException(status_code=404, detail="Задача не найдена или истекла.")
    return job


def spawn(coroutine):
    task = asyncio.create_task(coroutine)
    JOB_TASKS.add(task)
    task.add_done_callback(JOB_TASKS.discard)


def fail_job(job, error):
    if isinstance(error, UserError):
        touch(job, status="error", stage="Ошибка", error=error.message)
    else:
        logger.error("Job %s failed", job["id"], exc_info=error)
        touch(job, status="error", stage="Ошибка", error="Внутренняя ошибка сервера.")


async def run_transcribe_job(job):
    try:
        async with PROCESS_LOCK:
            def report(progress, stage):
                touch(job, progress=progress, stage=stage)

            touch(job, status="transcribing", progress=10, stage="Извлекаем звук")
            work_dir = job["dir"]
            audio_path = work_dir / "audio.mp3"

            await extract_audio(job["input"], audio_path, work_dir)
            words, detected = await transcribe_video(
                audio_path, work_dir, job["language"], job["duration"], report,
                job.get("model"), job.get("glossary"),
            )

            report(95, "Подбираем карточки")
            report(96, "Анализируем сцены")
            analysis = await analyze_scenes(words, job["duration"])
            cuts = await detect_visual_cuts(job["input"], work_dir, job["duration"])
            job["scenes"] = merge_visual_cuts(analysis["scenes"], cuts)
            job["topic"] = analysis["topic"]
            job["visual_cuts"] = len(cuts)
            job["color"] = await analyze_color(job["input"], work_dir, job["duration"])
            job["music_mood"] = await choose_mood(words, job["topic"], job["duration"])
            job["cover"] = await prepare_cover(
                job["input"], work_dir, job["duration"], words, job["topic"]
            )
            job["cards"], job["broll_info"] = await suggest_broll(
                words, job["duration"], topic=job["topic"]
            )
            job["cut_preview"] = cut_preview(words, job["duration"])
            touch(
                job,
                words=words,
                detected=detected,
                lines=make_lines(words),
                status="transcribed",
                stage="Текст готов",
                progress=100,
            )

        # Continue on the server: do not wait for the Mini App to poll or
        # send a second /render request. All selected settings were saved
        # when the user pressed "Начать монтаж".
        if job.get("auto_render"):
            render_options = dict(job.get("render_options") or {})
            if render_options.get("broll", "auto") != "off":
                render_options["cards"] = list(job.get("cards") or [])
            await run_render_job(
                job,
                job.get("render_style", "dynamic"),
                job.get("render_format", "9:16"),
                None,
                render_options,
            )
    except Exception as error:
        fail_job(job, error)


async def run_render_job(job, style, video_format, lines, options):
    try:
        async with PROCESS_LOCK:
            touch(job, status="rendering", progress=0, stage="Монтируем видео")

            if lines:
                words = lines_to_words(lines, job["duration"])
            else:
                words = job["words"]

            output_path = job["dir"] / "gapcut_ai.mp4"
            if output_path.exists():
                output_path.unlink()

            def report(progress, stage):
                touch(job, progress=progress, stage=stage)

            if options.get("broll", "auto") != "off":
                options = dict(options, cards=await fetch_broll_assets(
                    options.get("cards") or [], job["dir"]
                ))
            await render_video(
                job["input"], output_path, job["dir"], words, style,
                video_format, job["info"], job["duration"], report, options,
            )

            touch(
                job,
                output=output_path,
                status="done",
                stage="Видео готово",
                progress=100,
            )
            try:
                chat_id = int(job["user_id"])
                if output_path.stat().st_size <= 49 * 1024 * 1024:
                    await bot.send_video(
                        chat_id=chat_id,
                        video=FSInputFile(str(output_path)),
                        caption="✅ Ваш ролик GAPCUT AI готов!",
                    )
                else:
                    await bot.send_message(
                        chat_id=chat_id,
                        text="✅ Ваш ролик GAPCUT AI готов! Откройте Mini App → «Мои ролики», чтобы скачать его.",
                        reply_markup=main_keyboard(),
                    )
            except Exception:
                logger.exception("Could not notify Telegram user for job %s", job["id"])
    except Exception as error:
        fail_job(job, error)


# =========================================================
# FASTAPI
# =========================================================

async def cleanup_loop():
    while True:
        try:
            await asyncio.sleep(60)
            purge_expired_jobs()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Cleanup loop error")


@asynccontextmanager
async def lifespan(app_instance):
    shutil.rmtree(BASE_DIR, ignore_errors=True)
    BASE_DIR.mkdir(parents=True, exist_ok=True)

    global AVAILABLE_FONTS
    AVAILABLE_FONTS = await asyncio.to_thread(detect_fonts)
    logger.info("Fonts with Tajik letters: %s", [f["family"] for f in AVAILABLE_FONTS])

    polling_task = asyncio.create_task(run_polling_forever())
    cleanup_task = asyncio.create_task(cleanup_loop())

    logger.info("GAPCUT AI %s starting on port %s", VERSION, PORT)
    try:
        yield
    finally:
        for task in (polling_task, cleanup_task):
            task.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await task
        with suppress(Exception):
            await bot.session.close()
        shutil.rmtree(BASE_DIR, ignore_errors=True)


app = FastAPI(title="GAPCUT AI API", version=VERSION, lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=False,
    allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
    allow_headers=["*"],
    expose_headers=["Content-Disposition"],
)


@app.get("/")
async def home():
    return {
        "status": "ok",
        "service": "GAPCUT AI",
        "version": VERSION,
        "languages": sorted(ALLOWED_LANGUAGES),
        "styles": sorted(ALLOWED_STYLES),
        "formats": sorted(ALLOWED_FORMATS),
        "max_input_mb": MAX_INPUT_SIZE // (1024 * 1024),
        "max_duration_seconds": MAX_DURATION,
        "processing_concurrency": 1,
    }


@app.get("/api/fonts")
async def list_fonts():
    return {"fonts": [f["family"] for f in AVAILABLE_FONTS]}


@app.post("/api/images/generate")
async def generate_standalone_image(
    prompt: str = Form(...),
    style: str = Form("realistic"),
    ratio: str = Form("9:16"),
    init_data: str = Form(""),
    x_init_data: str = Header(default=""),
):
    global IMAGE_GENERATION_LAST_REQUEST
    user = require_user(init_data, x_init_data)
    prompt = " ".join(str(prompt).split())[:1200]
    if len(prompt) < 4:
        raise HTTPException(status_code=400, detail="Введите описание изображения.")

    styles = {
        "realistic": "photorealistic, realistic professional photography, natural details",
        "cinematic": "cinematic composition, dramatic film lighting, rich depth, premium color grading",
        "premium": "luxury premium advertising photography, elegant studio lighting, polished composition",
        "3d": "high-end 3D render, detailed materials, realistic global illumination",
        "viral": "bold social media creative, dynamic composition, strong visual hook",
        "anime": "beautiful high-quality anime illustration, detailed clean artwork",
    }
    dimensions = {
        "9:16": (720, 1280),
        "1:1": (1024, 1024),
        "16:9": (1280, 720),
    }
    if style not in styles or ratio not in dimensions:
        raise HTTPException(status_code=400, detail="Неверный стиль или формат изображения.")

    width, height = dimensions[ratio]
    full_prompt = (
        f"{prompt}. {styles[style]}. Compose the main subject centrally with enough "
        "visual space around it for a clean crop to the requested aspect ratio. "
        "High detail, professional quality, natural lighting. No text, no letters, "
        "no captions, no logos, no watermark. Avoid unrelated extra objects, "
        "distorted anatomy, blurry details and low quality."
    )
    url = "https://stable-diffusion-xl.endpoints.kepler.ai.cloud.ovh.net/api/text2image"
    payload = {
        "prompt": full_prompt,
        "negative_prompt": (
            "text, letters, captions, watermark, logo, blurry, low quality, "
            "distorted anatomy, extra fingers, unrelated objects"
        ),
    }

    # OVHcloud's free anonymous SDXL endpoint is limited to 2 requests/minute/IP/model.
    # Keep a 31-second local cooldown so GAPCUT does not exceed that published limit.
    async with IMAGE_GENERATION_LOCK:
        now = time.monotonic()
        wait = 31.0 - (now - IMAGE_GENERATION_LAST_REQUEST)
        if IMAGE_GENERATION_LAST_REQUEST and wait > 0:
            raise HTTPException(
                status_code=429,
                detail=f"Лимити ройгони генератор: як аксро ҳар 31 сония сохтан мумкин аст. {int(wait) + 1} сония интизор шавед.",
                headers={"Retry-After": str(int(wait) + 1)},
            )
        IMAGE_GENERATION_LAST_REQUEST = time.monotonic()
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(180.0, connect=20.0)) as client:
                response = await client.post(
                    url,
                    json=payload,
                    headers={
                        "accept": "application/octet-stream",
                        "content-type": "application/json",
                    },
                )
            if response.status_code == 429:
                raise HTTPException(
                    status_code=429,
                    detail="Сервиси ройгон ҳоло маҳдудият гузошт. Каме баъдтар кӯшиш кунед.",
                    headers={"Retry-After": response.headers.get("Retry-After", "60")},
                )
            if response.status_code >= 400:
                logger.error(
                    "OVHcloud SDXL error HTTP %s: %s",
                    response.status_code,
                    response.text[:500],
                )
                raise HTTPException(
                    status_code=502,
                    detail="Сервиси ройгони сохтани акс муваққатан дастрас нест.",
                )
            if not response.content or len(response.content) > 20 * 1024 * 1024:
                raise HTTPException(status_code=502, detail="Андозаи акси сохташуда нодуруст аст.")

            # OVHcloud SDXL returns 1024x1024. Crop and resize to the selected output ratio.
            if Image is None:
                raise HTTPException(
                    status_code=503,
                    detail="Коркарди акс дар сервер дастрас нест.",
                )
            try:
                image = Image.open(io.BytesIO(response.content)).convert("RGB")
                target_ratio = width / height
                source_ratio = image.width / image.height
                if source_ratio > target_ratio:
                    crop_width = int(image.height * target_ratio)
                    left = (image.width - crop_width) // 2
                    image = image.crop((left, 0, left + crop_width, image.height))
                elif source_ratio < target_ratio:
                    crop_height = int(image.width / target_ratio)
                    top = (image.height - crop_height) // 2
                    image = image.crop((0, top, image.width, top + crop_height))
                resampling = getattr(Image, "Resampling", Image).LANCZOS
                image = image.resize((width, height), resampling)
                output = io.BytesIO()
                image.save(output, format="JPEG", quality=92, optimize=True)
                image_bytes = output.getvalue()
            except Exception:
                logger.exception("Could not decode or resize OVHcloud image")
                raise HTTPException(status_code=502, detail="Сервиси AI акси дуруст барнагардонд.")

            logger.info(
                "Free OVHcloud SDXL image generated for Telegram user %s (%s, %s)",
                user["id"], style, ratio,
            )
            return Response(
                content=image_bytes,
                media_type="image/jpeg",
                headers={
                    "Content-Disposition": 'inline; filename="GAPCUT_AI_photo.jpg"',
                    "Cache-Control": "no-store",
                },
            )
        except HTTPException:
            raise
        except httpx.TimeoutException:
            raise HTTPException(
                status_code=504,
                detail="Сохтани акс вақти зиёд гирифт. Боз кӯшиш кунед.",
            )
        except httpx.HTTPError:
            logger.exception("OVHcloud SDXL image request failed")
            raise HTTPException(
                status_code=502,
                detail="Ба генератори ройгони акс пайваст нашуд.",
            )


@app.api_route("/health", methods=["GET", "HEAD"])
async def health():
    return {"status": "healthy", "service": "GAPCUT AI", "version": VERSION}


# ---------------------------------------------------------
# One-shot endpoint (compatible with the existing Mini App)
# ---------------------------------------------------------

@app.post("/api/process")
async def api_process(
    file: UploadFile = File(...),
    style: str = Form("viral"),
    language: str = Form("tg_ru"),
    video_format: str = Form("9:16"),
    init_data: str = Form(""),
    x_init_data: str = Header(default=""),
):
    user = require_user(init_data, x_init_data)
    style = clean_style(style)
    language = clean_language(language)
    video_format = clean_format(video_format)

    if PROCESS_LOCK.locked():
        raise HTTPException(
            status_code=429,
            detail="Сервер сейчас обрабатывает другое видео. Попробуйте позже.",
            headers={"Retry-After": "30"},
        )

    work_dir = None
    try:
        async with PROCESS_LOCK:
            work_dir = new_work_dir()
            logger.info("Upload started. Telegram user ID: %s", user["id"])

            input_path = await save_upload(file, work_dir)
            info, duration = await inspect_video(input_path)

            audio_path = work_dir / "audio.mp3"
            await extract_audio(input_path, audio_path, work_dir)
            words, _ = await transcribe_video(
                audio_path, work_dir, language, duration, lambda p, s: None
            )

            output_path = work_dir / "gapcut_ai.mp4"
            await render_video(
                input_path, output_path, work_dir, words, style, video_format,
                info, duration, lambda p, s: None,
            )

        return FileResponse(
            path=str(output_path),
            media_type="video/mp4",
            filename="GAPCUT_AI.mp4",
            background=BackgroundTask(shutil.rmtree, str(work_dir), ignore_errors=True),
        )

    except HTTPException:
        if work_dir:
            shutil.rmtree(work_dir, ignore_errors=True)
        raise
    except UserError as error:
        if work_dir:
            shutil.rmtree(work_dir, ignore_errors=True)
        raise HTTPException(status_code=error.status_code, detail=error.message)
    except Exception:
        logger.exception("Video processing failed")
        if work_dir:
            shutil.rmtree(work_dir, ignore_errors=True)
        raise HTTPException(status_code=500, detail="Внутренняя ошибка сервера.")
    finally:
        await file.close()


# ---------------------------------------------------------
# Job endpoints (progress + editable transcript)
# ---------------------------------------------------------

@app.get("/api/jobs")
async def list_jobs(
    x_init_data: str = Header(default=""),
    init_data: str = Query(default=""),
):
    user = require_user(x_init_data, init_data)
    purge_expired_jobs()
    user_jobs = [job for job in JOBS.values() if job["user_id"] == user["id"]]
    user_jobs.sort(key=lambda job: job.get("updated", 0), reverse=True)
    return {"jobs": [public_job(job) for job in user_jobs[:20]]}


@app.post("/api/jobs", status_code=202)
async def create_job(
    file: UploadFile = File(...),
    language: str = Form("tg_ru"),
    quality: str = Form("fast"),
    glossary: str = Form(""),
    render_settings: str = Form("{}"),
    init_data: str = Form(""),
    x_init_data: str = Header(default=""),
):
    user = require_user(init_data, x_init_data)
    language = clean_language(language)
    model = GROQ_MODEL_BEST if str(quality).strip().lower() == "best" else GROQ_MODEL
    glossary_terms = clean_glossary(glossary)

    try:
        cfg = json.loads(render_settings or "{}")
        if not isinstance(cfg, dict):
            raise ValueError("render_settings must be an object")
        render_style = clean_style(cfg.get("style", "dynamic"))
        render_format = clean_format(cfg.get("video_format", "9:16"))
        render_options = clean_options(
            cfg.get("accent", ""), cfg.get("size", "m"),
            cfg.get("position", "auto"), cfg.get("subtitles", "1"),
            cfg.get("zoom", "1"), cfg.get("fx", "1"), cfg.get("sfx", "1"),
            cfg.get("font", ""), cfg.get("font2", ""),
            cfg.get("fontmix", "0"), cfg.get("cut", "off"),
            cfg.get("keys", "1"), cfg.get("anim", "auto"),
            cfg.get("anim_out", "auto"), cfg.get("trans", "auto"),
            cfg.get("broll", "auto"), cfg.get("sound", "normal"),
            cfg.get("camera", "auto"), cfg.get("grade", "auto"),
            cfg.get("music", "auto"), cfg.get("music_level", "low"),
            cfg.get("music_sync", "1"),
        )
    except (ValueError, TypeError, json.JSONDecodeError):
        raise HTTPException(status_code=400, detail="Неверные настройки монтажа.")

    purge_expired_jobs()

    for existing in JOBS.values():
        if existing["user_id"] == user["id"] and existing["status"] in ACTIVE_STATES:
            raise HTTPException(
                status_code=429,
                detail="У вас уже есть задача в обработке. Дождитесь её завершения.",
            )

    while len(JOBS) >= MAX_STORED_JOBS:
        completed = [job for job in JOBS.values() if job["status"] not in ACTIVE_STATES]
        if not completed:
            raise HTTPException(
                status_code=429,
                detail="Сервер занят. Попробуйте через несколько минут.",
                headers={"Retry-After": "60"},
            )
        oldest = min(completed, key=lambda item: item.get("updated", 0))
        remove_job(oldest["id"])

    work_dir = new_work_dir()
    try:
        input_path = await save_upload(file, work_dir)
        info, duration = await inspect_video(input_path)
    except UserError as error:
        shutil.rmtree(work_dir, ignore_errors=True)
        raise HTTPException(status_code=error.status_code, detail=error.message)
    except HTTPException:
        shutil.rmtree(work_dir, ignore_errors=True)
        raise
    except Exception:
        logger.exception("Upload failed")
        shutil.rmtree(work_dir, ignore_errors=True)
        raise HTTPException(status_code=500, detail="Внутренняя ошибка сервера.")
    finally:
        await file.close()

    job_id = uuid.uuid4().hex
    job = {
        "id": job_id,
        "user_id": user["id"],
        "filename": Path(file.filename or "video").name[:120],
        "dir": work_dir,
        "input": input_path,
        "info": info,
        "duration": duration,
        "language": language,
        "model": model,
        "glossary": glossary_terms,
        "status": "queued",
        "stage": "В очереди",
        "progress": 5,
        "error": None,
        "words": [],
        "lines": [],
        "detected": [],
        "cards": [],
        "cut_preview": {},
        "scenes": [],
        "topic": "",
        "visual_cuts": 0,
        "color": None,
        "music_mood": None,
        "cover": None,
        "output": None,
        # The whole edit is executed by the server after upload. The browser
        # may close at any time without cancelling transcription or rendering.
        "auto_render": True,
        "render_style": render_style,
        "render_format": render_format,
        "render_options": render_options,
        "updated": time.time(),
    }
    JOBS[job_id] = job
    spawn(run_transcribe_job(job))

    return public_job(job)


@app.get("/api/jobs/{job_id}")
async def job_status(
    job_id: str,
    x_init_data: str = Header(default=""),
    init_data: str = Query(default=""),
):
    user = require_user(x_init_data, init_data)
    return public_job(get_job(job_id, user))


@app.post("/api/jobs/{job_id}/render", status_code=202)
async def start_render(
    job_id: str,
    style: str = Form("viral"),
    video_format: str = Form("9:16"),
    lines: str = Form(""),
    accent: str = Form(""),
    size: str = Form("m"),
    position: str = Form("auto"),
    subtitles: str = Form("1"),
    zoom: str = Form("1"),
    fx: str = Form("1"),
    sfx: str = Form("1"),
    cards: str = Form("[]"),
    font: str = Form(""),
    font2: str = Form(""),
    fontmix: str = Form("0"),
    cut: str = Form("off"),
    keys: str = Form("1"),
    anim: str = Form(""),
    anim_out: str = Form(""),
    trans: str = Form("off"),
    broll: str = Form("auto"),
    sound: str = Form(""),
    camera: str = Form(""),
    grade: str = Form("off"),
    music: str = Form(""),
    music_level: str = Form("normal"),
    music_sync: str = Form("1"),
    init_data: str = Form(""),
    x_init_data: str = Header(default=""),
):
    user = require_user(init_data, x_init_data)
    job = get_job(job_id, user)
    options = clean_options(
        accent, size, position, subtitles, zoom, fx, sfx, font, font2, fontmix,
        cut, keys, anim, anim_out, trans, broll, sound, camera, grade,
        music, music_level, music_sync,
    )
    options["music_mood"] = job.get("music_mood")
    options["scenes"] = job.get("scenes", [])
    options["color"] = job.get("color")
    try:
        wanted = {}
        for entry in json.loads(cards or "[]"):
            if isinstance(entry, dict):
                wanted[int(entry["id"])] = int(entry.get("alt", 0))
            else:
                wanted[int(entry)] = 0
    except (ValueError, TypeError, KeyError):
        raise HTTPException(status_code=400, detail="Неверный список карточек.")
    options["cards"] = [
        dict(c, alt_index=wanted[c["id"]]) for c in job["cards"] if c["id"] in wanted
    ]

    parsed_lines = None
    if lines.strip():
        try:
            parsed_lines = json.loads(lines)
        except json.JSONDecodeError:
            raise HTTPException(status_code=400, detail="Неверный формат текста.")
        if not isinstance(parsed_lines, list):
            raise HTTPException(status_code=400, detail="Неверный формат текста.")
        try:
            lines_to_words(parsed_lines, job["duration"])
        except UserError as error:
            raise HTTPException(status_code=400, detail=error.message)

    touch(job, status="rendering", stage="В очереди", progress=0, error=None)
    spawn(run_render_job(job, style, video_format, parsed_lines, options))
    return public_job(job)


@app.get("/api/jobs/{job_id}/download")
async def download_job(
    job_id: str,
    x_init_data: str = Header(default=""),
    init_data: str = Query(default=""),
):
    user = require_user(x_init_data, init_data)
    job = get_job(job_id, user)

    output = job.get("output")
    if job["status"] != "done" or not output or not Path(output).exists():
        raise HTTPException(status_code=409, detail="Видео ещё не готово.")

    touch(job)
    return FileResponse(
        path=str(output),
        media_type="video/mp4",
        filename="GAPCUT_AI.mp4",
    )


@app.delete("/api/jobs/{job_id}")
async def delete_job(job_id: str, x_init_data: str = Header(default="")):
    user = require_user(x_init_data)
    job = get_job(job_id, user)
    if job["status"] in ACTIVE_STATES:
        raise HTTPException(status_code=409, detail="Задача ещё обрабатывается.")
    remove_job(job_id)
    return {"deleted": True}


@app.get("/api/jobs/{job_id}/cover/candidates")
async def cover_candidates(
    job_id: str,
    x_init_data: str = Header(default=""),
    init_data: str = Query(default=""),
):
    user = require_user(x_init_data, init_data)
    cover = get_job(job_id, user).get("cover") or {}
    return {
        "times": cover.get("times", []),
        "thumbs": cover.get("thumbs", []),
        "title": cover.get("title", ""),
    }


@app.post("/api/jobs/{job_id}/cover")
async def create_cover(
    job_id: str,
    t: float = Form(0.0),
    template: str = Form("bold"),
    text: str = Form(""),
    fmt: str = Form("9:16"),
    accent: str = Form(""),
    font: str = Form(""),
    grade: str = Form("off"),
    init_data: str = Form(""),
    x_init_data: str = Header(default=""),
):
    user = require_user(init_data, x_init_data)
    job = get_job(job_id, user)
    if np is None or Image is None:
        raise HTTPException(status_code=503, detail="Создание обложек недоступно на сервере.")
    if template not in COVER_TEMPLATES or fmt not in COVER_SIZES:
        raise HTTPException(status_code=400, detail="Неверный шаблон или формат обложки.")
    text = " ".join(text.split())[:80] or (job.get("cover") or {}).get("title", "")
    if not text:
        raise HTTPException(status_code=400, detail="Введите текст обложки.")
    accent = accent.strip().lstrip("#").upper()
    if accent and not re.fullmatch(r"[0-9A-F]{6}", accent):
        raise HTTPException(status_code=400, detail="Неверный цвет.")
    families = {f["family"] if isinstance(f, dict) else f for f in AVAILABLE_FONTS}
    family = font if font in families else ""
    grade_expr = grade_filter(job.get("color"), grade if grade in LOOK_FILTERS or grade == "auto" else "off")
    moment = max(0.0, min(float(t), job["duration"] - 0.1))
    try:
        path = await make_cover(job, moment, template, text, fmt, accent, family, grade_expr)
    except UserError as error:
        raise HTTPException(status_code=error.status_code, detail=error.message)
    except Exception:
        logger.exception("Cover failed")
        raise HTTPException(status_code=500, detail="Не удалось создать обложку.")
    previous = job.get("cover_file")
    if previous and previous != str(path):
        with suppress(OSError):
            Path(previous).unlink()
    job["cover_file"] = str(path)
    touch(job)
    return FileResponse(str(path), media_type="image/jpeg", filename="GAPCUT_cover.jpg")


# =========================================================
# ENTRY POINT
# =========================================================

if __name__ == "__main__":
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=PORT,
        log_level="info",
        timeout_keep_alive=30,
        limit_concurrency=20,
    )
