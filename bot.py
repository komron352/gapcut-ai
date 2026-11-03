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
import hashlib
import hmac
import json
import logging
import os
import re
import shutil
import tempfile
import time
import uuid
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from urllib.parse import parse_qsl

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
    WebAppInfo,
)
from fastapi import FastAPI, File, Form, Header, HTTPException, Query, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from starlette.background import BackgroundTask


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
GROQ_URL = "https://api.groq.com/openai/v1/audio/transcriptions"

# Render Free: conservative limits.
MAX_INPUT_SIZE = 100 * 1024 * 1024
MAX_OUTPUT_SIZE = 150 * 1024 * 1024
MAX_DURATION = 60
UPLOAD_CHUNK_SIZE = 256 * 1024
GROQ_MAX_AUDIO_SIZE = 24 * 1024 * 1024

JOB_TTL_SECONDS = int(os.getenv("JOB_TTL_SECONDS", "1800"))
MAX_STORED_JOBS = int(os.getenv("MAX_STORED_JOBS", "4"))
FFMPEG_PRESET = os.getenv("FFMPEG_PRESET", "ultrafast").strip() or "ultrafast"

BASE_DIR = Path(tempfile.gettempdir()) / "gapcut_ai"
FONTS_DIR = Path(os.getenv("FONTS_DIR", str(Path(__file__).resolve().parent / "fonts")))

ALLOWED_EXTENSIONS = {".mp4", ".mov", ".m4v", ".webm", ".mkv"}
ALLOWED_STYLES = {"viral", "classic", "cinema", "podcast", "karaoke", "creator"}
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

bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()

# Only one heavy job (transcription or render) at a time on a small server.
PROCESS_LOCK = asyncio.Lock()


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
                    text="🎬 Монтаж",
                    web_app=WebAppInfo(url=MINI_APP_URL),
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
        "Нажмите «Монтаж», чтобы начать."
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
    """Run a command; stderr goes to a temp file (small RAM footprint)."""
    with tempfile.TemporaryFile(mode="w+b") as error_file:
        process = await asyncio.create_subprocess_exec(
            *[str(part) for part in command],
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

TG_PROMPT = "Салом! Имрӯз мо бо забони тоҷикӣ сӯҳбат мекунем."
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


async def groq_transcribe(client, audio_path, language, prompt):
    data = {
        "model": GROQ_MODEL,
        "response_format": "verbose_json",
        "temperature": "0",
        "timestamp_granularities[]": ["word", "segment"],
    }
    if language:
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
        cleaned.append({"word": word, "start": start, "end": max(end, start + 0.04)})

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


async def transcribe_video(audio_path, work_dir, language, audio_duration, report):
    """Return (words, detected_ranges)."""
    if not GROQ_API_KEY:
        raise UserError("GROQ_API_KEY не настроен на сервере.", 503)

    timeout = httpx.Timeout(180.0, connect=20.0, write=60.0, read=180.0)

    async with httpx.AsyncClient(timeout=timeout) as client:
        if language in ("tg", "ru"):
            prompt = TG_PROMPT if language == "tg" else RU_PROMPT
            report(30, "Распознаём речь")
            result = await groq_transcribe(client, audio_path, language, prompt)
            parsed = parse_groq_result(result, 0.0, prompt)
            words = parsed["words"]
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

                tg_raw, ru_raw = await asyncio.gather(
                    groq_transcribe(client, chunk_path, "tg", TG_PROMPT),
                    groq_transcribe(client, chunk_path, "ru", RU_PROMPT),
                )
                tg_parsed = parse_groq_result(tg_raw, start, TG_PROMPT)
                ru_parsed = parse_groq_result(ru_raw, start, RU_PROMPT)

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

                words.extend(chosen["words"])
                detected.append({"start": start, "end": end, "lang": lang})

    words = normalize_words(words)
    if not words:
        raise UserError("Не удалось распознать речь в видео.", 422)

    report(90, "Готовим текст для редактирования")
    return words, detected


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
            "words": [
                {
                    "word": w["word"],
                    "start": round(w["start"], 2),
                    "end": round(w["end"], 2),
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
                    words.append({
                        "word": token,
                        "start": float(source.get("start", start)),
                        "end": float(source.get("end", end)),
                    })
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


def build_ass(words, style_name, width, height, output_path,
              accent=None, size="m", position="auto"):
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
        f"Style: Default,{style['font']},{font_size},"
        f"{ass_color(style['primary'])},{ass_color(style['secondary'])},"
        f"{ass_color(style['outline'], outline_alpha)},{back_color},"
        f"{style['bold']},0,0,0,100,100,{spacing:.1f},0,"
        f"{style['border_style']},{outline_w},{shadow},{alignment},"
        f"{margin_h},{margin_h},{margin_v},1\n\n"
        "[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, "
        "Effect, Text\n"
    )

    def display(token):
        text = escape_ass_text(token)
        return text.upper() if style["upper"] else text

    phrases = split_phrases(words, style["max_words"], style["max_chars"])
    events = []

    for p_index, phrase in enumerate(phrases):
        phrase_start = phrase[0]["start"]
        phrase_end = phrase[-1]["end"] + 0.15
        if p_index + 1 < len(phrases):
            phrase_end = min(phrase_end, phrases[p_index + 1][0]["start"] - 0.02)
        phrase_end = max(phrase_end, phrase_start + 0.15)

        mode = style["mode"]

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
                                "{\\1c" + active +
                                "\\fscx112\\fscy112"
                                "\\t(0,110,\\fscx100\\fscy100)}"
                            )
                        else:
                            tag = "{\\1c" + active + "}"
                        parts.append(tag + text + "{\\r}")
                    else:
                        parts.append(text)

                events.append(
                    f"Dialogue: 0,{ass_time(start)},{ass_time(end)},Default,,"
                    f"0,0,0,,{' '.join(parts)}"
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
                f"Default,,0,0,0,,{' '.join(parts)}"
            )

        elif mode == "fade":
            text = " ".join(display(t["word"]) for t in phrase)
            events.append(
                f"Dialogue: 0,{ass_time(phrase_start)},{ass_time(phrase_end)},"
                f"Default,,0,0,0,,{{\\fad(250,200)}}{text}"
            )

        else:
            text = " ".join(display(t["word"]) for t in phrase)
            events.append(
                f"Dialogue: 0,{ass_time(phrase_start)},{ass_time(phrase_end)},"
                f"Default,,0,0,0,,{text}"
            )

    Path(output_path).write_text(header + "\n".join(events) + "\n", encoding="utf-8")


# =========================================================
# RENDER
# =========================================================

async def render_video(
    input_path, output_path, work_dir, words, style_name, video_format,
    info, duration, report, options=None,
):
    options = options or {}
    input_path = Path(input_path).resolve()
    output_path = Path(output_path).resolve()
    work_dir = Path(work_dir).resolve()

    _, _, display_width, display_height = probe_summary(info)
    width, height = get_output_size(video_format, display_width, display_height)

    ass_name = "captions.ass"
    filter_chain = get_video_filter(video_format, width, height)
    if options.get("subtitles", True):
        build_ass(
            words, style_name, width, height, Path(work_dir) / ass_name,
            accent=options.get("accent"),
            size=options.get("size", "m"),
            position=options.get("position", "auto"),
        )
        filter_chain += f",ass={ass_name}"
        if FONTS_DIR.is_dir() and any(FONTS_DIR.iterdir()):
            filter_chain += f":fontsdir={FONTS_DIR}"

    command = [
        "ffmpeg", "-y", "-nostdin", "-hide_banner", "-loglevel", "error",
        "-progress", "pipe:1", "-nostats",
        "-threads", "1", "-filter_threads", "1",
        "-i", str(input_path),
        "-t", str(MAX_DURATION),
        "-vf", filter_chain,
        "-map", "0:v:0", "-map", "0:a:0?",
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


def clean_options(accent, size, position, subtitles):
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
    }


def new_work_dir():
    BASE_DIR.mkdir(parents=True, exist_ok=True)
    work_dir = BASE_DIR / uuid.uuid4().hex
    work_dir.mkdir(parents=True, exist_ok=True)
    return work_dir


async def save_upload(file, work_dir):
    extension = Path((file.filename or "video.mp4").lower()).suffix
    if extension not in ALLOWED_EXTENSIONS:
        raise HTTPException(
            status_code=400,
            detail="Поддерживаются MP4, MOV, M4V, WEBM и MKV.",
        )

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
        if job["status"] in ACTIVE_STATES and now - job["updated"] < 1800:
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
        "duration": round(job["duration"], 2),
        "error": job["error"],
    }
    if job["status"] in ("transcribed", "rendering", "done") or job["lines"]:
        data["lines"] = job["lines"]
        data["detected"] = job["detected"]
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
                audio_path, work_dir, job["language"], job["duration"], report
            )

            touch(
                job,
                words=words,
                detected=detected,
                lines=make_lines(words),
                status="transcribed",
                stage="Текст готов",
                progress=100,
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

@app.post("/api/jobs", status_code=202)
async def create_job(
    file: UploadFile = File(...),
    language: str = Form("tg_ru"),
    init_data: str = Form(""),
    x_init_data: str = Header(default=""),
):
    user = require_user(init_data, x_init_data)
    language = clean_language(language)

    purge_expired_jobs()

    for existing_id, existing in list(JOBS.items()):
        if existing["user_id"] != user["id"]:
            continue
        if existing["status"] in ACTIVE_STATES:
            raise HTTPException(
                status_code=429,
                detail="У вас уже есть задача в обработке. Дождитесь её завершения.",
            )
        remove_job(existing_id)

    if len(JOBS) >= MAX_STORED_JOBS:
        raise HTTPException(
            status_code=429,
            detail="Сервер занят. Попробуйте через несколько минут.",
            headers={"Retry-After": "60"},
        )

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
        "dir": work_dir,
        "input": input_path,
        "info": info,
        "duration": duration,
        "language": language,
        "status": "queued",
        "stage": "В очереди",
        "progress": 5,
        "error": None,
        "words": [],
        "lines": [],
        "detected": [],
        "output": None,
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
    init_data: str = Form(""),
    x_init_data: str = Header(default=""),
):
    user = require_user(init_data, x_init_data)
    job = get_job(job_id, user)
    options = clean_options(accent, size, position, subtitles)

    style = clean_style(style)
    video_format = clean_format(video_format)

    if job["status"] in ("queued", "transcribing", "rendering"):
        raise HTTPException(status_code=409, detail="Задача ещё обрабатывается.")
    if job["status"] == "error" and not job["words"]:
        raise HTTPException(status_code=409, detail="Распознавание не удалось.")
    if not job["words"]:
        raise HTTPException(status_code=409, detail="Текст ещё не готов.")

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
