import asyncio
import logging
import os
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

import httpx
from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart, Command
from aiogram.types import (
    Message,
    ReplyKeyboardMarkup,
    KeyboardButton,
    FSInputFile,
)
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")

MAX_INPUT_SIZE = 19 * 1024 * 1024
MAX_OUTPUT_SIZE = 49 * 1024 * 1024
MAX_DURATION = 180

GROQ_URL = "https://api.groq.com/openai/v1/audio/transcriptions"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)
logger = logging.getLogger("gapcut")

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is missing")

if not GROQ_API_KEY:
    raise RuntimeError("GROQ_API_KEY is missing")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

user_busy = set()


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
        raise RuntimeError("FFmpeg processing failed")

    return result


def get_video_duration(path):
    result = run_command([
        "ffprobe",
        "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        str(path)
    ])

    return float(result.stdout.strip())


async def download_telegram_file(message, destination):
    media = message.video or message.document

    if not media:
        raise ValueError("Файл видео ёфт нашуд")

    if media.file_size and media.file_size > MAX_INPUT_SIZE:
        raise ValueError("Ҳаҷми видео бояд аз 19 MB зиёд набошад.")

    file_info = await bot.get_file(media.file_id)

    await bot.download_file(
        file_info.file_path,
        destination=destination
    )


async def transcribe_audio(audio_path):
    headers = {
        "Authorization": f"Bearer {GROQ_API_KEY}"
    }

    data = {
        "model": "whisper-large-v3-turbo",
        "response_format": "verbose_json"
    }

    with open(audio_path, "rb") as audio:
        files = {
            "file": (
                "audio.wav",
                audio,
                "audio/wav"
            )
        }

        async with httpx.AsyncClient(timeout=180) as client:
            response = await client.post(
                GROQ_URL,
                headers=headers,
                data=data,
                files=files
            )

    if response.status_code != 200:
        logger.error(response.text[:2000])
        raise RuntimeError("Хатогӣ ҳангоми шинохтани овоз")

    return response.json()


def detect_silences(video_path):
    command = [
        "ffmpeg",
        "-i", str(video_path),
        "-af", "silencedetect=noise=-35dB:d=0.7",
        "-f", "null",
        "-"
    ]

    result = subprocess.run(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True
    )

    silence_starts = []
    silence_ranges = []
    current_start = None

    for line in result.stderr.splitlines():
        start_match = re.search(
            r"silence_start:\s*([0-9.]+)", line
        )
        end_match = re.search(
            r"silence_end:\s*([0-9.]+)", line
        )

        if start_match:
            current_start = float(start_match.group(1))

        if end_match and current_start is not None:
            end = float(end_match.group(1))
            silence_ranges.append((current_start, end))
            current_start = None

    return silence_ranges


def make_ass_subtitles(transcription, output_path):
    words = []

    for segment in transcription.get("segments", []):
        for word in segment.get("words", []):
            text = word.get("word", "").strip()

            if not text:
                continue

            words.append({
                "start": float(word["start"]),
                "end": float(word["end"]),
                "text": text
            })

    if not words:
        raise RuntimeError(
            "Матни субтитрҳо аз аудио гирифта нашуд"
        )

    header = """[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,Arial,58,&H00FFFFFF,&H0000FFFF,&H00000000,&H80000000,-1,0,1,4,1,2,70,70,250,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""

    def ass_time(seconds):
        hours = int(seconds // 3600)
        minutes = int((seconds % 3600) // 60)
        secs = seconds % 60
        return f"{hours}:{minutes:02d}:{secs:05.2f}"

    events = []

    for word in words:
        start = word["start"]
        end = max(word["end"], start + 0.12)

        safe_text = (
            word["text"]
            .replace("\\", "")
            .replace("{", "")
            .replace("}", "")
        )

        event = (
            f"Dialogue: 0,{ass_time(start)},"
            f"{ass_time(end)},Default,,0,0,0,,"
            f"{{\\c&H00FFFF&}}{safe_text}"
        )

        events.append(event)

    with open(output_path, "w", encoding="utf-8") as file:
        file.write(header + "\n".join(events))


def render_video(video_path, subtitle_path, output_path):
    subtitle_filter = (
        "ass=" + str(subtitle_path).replace("\\", "/")
    )

    run_command([
        "ffmpeg",
        "-y",
        "-i", str(video_path),
        "-vf", subtitle_filter,
        "-c:v", "libx264",
        "-preset", "ultrafast",
        "-crf", "28",
        "-c:a", "aac",
        "-b:a", "128k",
        "-movflags", "+faststart",
        str(output_path)
    ], timeout=600)


async def process_video(message: Message):
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
            "⏳ Лутфан интизор шавед..."
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            folder = Path(temp_dir)

            video_path = folder / "input.mp4"
            audio_path = folder / "audio.wav"
            subtitle_path = folder / "subtitles.ass"
            output_path = folder / "gapcut_output.mp4"

            await download_telegram_file(
                message,
                video_path
            )

            duration = get_video_duration(video_path)

            if duration > MAX_DURATION:
                raise ValueError(
                    "⛔ Давомнокии видео бояд то 3 дақиқа бошад."
                )

            if duration <= 0:
                raise ValueError("Давомнокии видео нодуруст аст.")

            await message.answer(
                "🎧 Қадами 1/3: Шинохтани овоз..."
            )

            run_command([
                "ffmpeg",
                "-y",
                "-i", str(video_path),
                "-vn",
                "-ac", "1",
                "-ar", "16000",
                "-c:a", "pcm_s16le",
                str(audio_path)
            ])

            transcription = await transcribe_audio(
                audio_path
            )

            await message.answer(
                "✂️ Қадами 2/3: Омода кардани субтитрҳо..."
            )

            make_ass_subtitles(
                transcription,
                subtitle_path
            )

            await message.answer(
                "🎞️ Қадами 3/3: Сохтани видеои ниҳоӣ..."
            )

            render_video(
                video_path,
                subtitle_path,
                output_path
            )

            if output_path.stat().st_size > MAX_OUTPUT_SIZE:
                raise ValueError(
                    "Ҳаҷми видеои тайёр аз 49 MB зиёд шуд."
                )

            await message.answer_video(
                FSInputFile(output_path),
                caption=(
                    "✅ GapCut AI — видео омода шуд!\n\n"
                    "🎬 Субтитрҳои автоматӣ илова шуданд."
                ),
                supports_streaming=True
            )

    except ValueError as error:
        await message.answer(f"⚠️ {error}")

    except Exception:
        logger.exception("Video processing error")
        await message.answer(
            "❌ Ҳангоми коркарди видео хатогӣ шуд.\n"
            "Лутфан видеои дигарро санҷед."
        )

    finally:
        user_busy.discard(user_id)
        async def http_handler(reader, writer):
    try:
        request_line = await asyncio.wait_for(
            reader.readline(),
            timeout=10
        )

        if not request_line:
            return

        parts = request_line.decode(
            "latin-1"
        ).strip().split()

        path = (
            parts[1].split("?")[0]
            if len(parts) >= 2
            else "/"
        )

        while True:
            line = await asyncio.wait_for(
                reader.readline(),
                timeout=10
            )

            if line in (b"\r\n", b"\n", b""):
                break

        if path in ("/", "/health"):
            status = "200 OK"
            body = b"GapCut AI is running!"
        else:
            status = "404 Not Found"
            body = b"Not found"

        response = (
            f"HTTP/1.1 {status}\r\n"
            "Content-Type: text/plain; charset=utf-8\r\n"
            f"Content-Length: {len(body)}\r\n"
            "Connection: close\r\n"
            "\r\n"
        ).encode("ascii") + body

        writer.write(response)
        await writer.drain()

    except Exception:
        logger.debug("HTTP connection closed")

    finally:
        writer.close()

        try:
            await writer.wait_closed()
        except Exception:
            pass


async def main():
    port = int(os.getenv("PORT", "10000"))

    server = await asyncio.start_server(
        http_handler,
        "0.0.0.0",
        port
    )

    logger.info(
        f"GapCut AI HTTP server started on port {port}"
    )

    async with server:
        await asyncio.gather(
            server.serve_forever(),
            dp.start_polling(bot)
        )


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("GapCut AI stopped")
