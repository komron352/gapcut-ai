
import asyncio
import logging
import os
import re
import subprocess
import tempfile
from pathlib import Path

import httpx
from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart, Command
from aiogram.types import Message, FSInputFile
from dotenv import load_dotenv


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
# 3. BOT
# =====================================

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

user_busy = set()


# =====================================
# 4. FFMPEG COMMAND
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
# 5. VIDEO DURATION
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
# 6. DOWNLOAD VIDEO
# =====================================

async def download_video(message, destination):
    media = message.video

    if not media and message.document:
        if not (
            message.document.mime_type
            and message.document.mime_type.startswith("video/")
        ):
            raise ValueError("Лутфан файли видео фиристед.")

        media = message.document

    if not media:
        raise ValueError("Видео ёфт нашуд.")

    if media.file_size and media.file_size > MAX_INPUT_SIZE:
        raise ValueError("Ҳаҷми видео бояд аз 19 MB зиёд набошад.")

    file_info = await bot.get_file(media.file_id)

    await bot.download_file(
        file_info.file_path,
        destination=destination
    )


# =====================================
# 7. EXTRACT AUDIO
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
# 8. GROQ WHISPER
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
# 9. ASS SUBTITLE HELPERS
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
# 10. CREATE SUBTITLES
# =====================================

def make_ass_subtitles(transcription, output_path):

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

    events = []
    words = []

    # Ensure the response is a dictionary
    if not isinstance(transcription, dict):
        raise RuntimeError("Ҷавоби Groq нодуруст аст.")

    # First: get word timestamps directly from Groq
    raw_words = transcription.get("words") or []

    # If top-level words are missing, try segment words
    if not raw_words:
        segments = transcription.get("segments") or []

        if isinstance(segments, list):
            for segment in segments:
                if not isinstance(segment, dict):
                    continue

                segment_words = segment.get("words") or []

                if isinstance(segment_words, list):
                    raw_words.extend(segment_words)

    # Prepare timed words
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

    # Create karaoke subtitles
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

        # Fallback: use segment text if available
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

        # Last fallback: use full transcription text
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
# 11. RENDER FINAL VIDEO
# =====================================

def render_video(video_path, subtitle_path, output_path):

    subtitle_filter = (
        "ass="
        + str(subtitle_path).replace("\\", "/")
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


# =====================================
# 12. START COMMAND
# =====================================

@dp.message(CommandStart())
async def start_handler(message: Message):

    await message.answer(
        "🎬 Хуш омадед ба GapCut AI!\n\n"
        "Ман видеои шуморо автоматӣ таҳрир мекунам.\n\n"
        "✅ Шинохтани овоз\n"
        "✅ Сохтани субтитрҳо\n"
        "✅ Субтитрҳои калима ба калима\n"
        "✅ Сохтани видеои тайёр\n\n"
        "📤 Видеоро фиристед.\n\n"
        "⏱ Максимум: 3 дақиқа\n"
        "📦 Максимум: 19 MB"
    )


# =====================================
# 13. HELP COMMAND
# =====================================

@dp.message(Command("help"))
async def help_handler(message: Message):

    await message.answer(
        "📖 Дастури GapCut AI\n\n"
        "1. Видеоро фиристед.\n"
        "2. Бот овозро мешиносад.\n"
        "3. Субтитрҳо сохта мешаванд.\n"
        "4. Видеои тайёр бармегардад.\n\n"
        "Формат: видео\n"
        "Давомнокӣ: то 3 дақиқа\n"
        "Ҳаҷм: то 19 MB"
    )


# =====================================
# 14. VIDEO HANDLER
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
            audio_path = folder / "audio.wav"
            subtitle_path = folder / "subtitles.ass"
            output_path = folder / "gapcut_output.mp4"

            # Download video
            await download_video(
                message,
                video_path
            )

            # Check duration
            duration = await asyncio.to_thread(
                get_video_duration,
                video_path
            )

            if duration <= 0:
                raise ValueError(
                    "Давомнокии видео нодуруст аст."
                )

            if duration > MAX_DURATION:
                raise ValueError(
                    "Давомнокии видео бояд то 3 дақиқа бошад."
                )

            # Extract audio
            await message.answer(
                "🎧 Қадами 1/3: Гирифтани овоз..."
            )

            await asyncio.to_thread(
                extract_audio,
                video_path,
                audio_path
            )

            # Transcription
            await message.answer(
                "🧠 Қадами 2/3: Шинохтани овоз ва сохтани субтитрҳо..."
            )

            transcription = await transcribe_audio(
                audio_path
            )

            await asyncio.to_thread(
                make_ass_subtitles,
                transcription,
                subtitle_path
            )

            # Render video
            await message.answer(
                "🎞️ Қадами 3/3: Сохтани видеои ниҳоӣ..."
            )

            await asyncio.to_thread(
                render_video,
                video_path,
                subtitle_path,
                output_path
            )

            if not output_path.exists():
                raise RuntimeError(
                    "Видеои ниҳоӣ сохта нашуд."
                )

            output_size = output_path.stat().st_size

            if output_size > MAX_OUTPUT_SIZE:
                raise ValueError(
                    "Ҳаҷми видеои тайёр аз 49 MB зиёд шуд."
                )

            # Send result
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

        await message.answer(
            f"⚠️ {error}"
        )

    except Exception:

        logger.exception(
            "Video processing error"
        )

        await message.answer(
            "❌ Ҳангоми коркарди видео хатогӣ шуд.\n\n"
            "Лутфан баъдтар дубора кӯшиш кунед."
        )

    finally:
        user_busy.discard(user_id)


# =====================================
# 15. RENDER HTTP SERVER
# =====================================

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

            if line in (
                b"\r\n",
                b"\n",
                b""
            ):
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

        logger.debug(
            "HTTP connection closed"
        )

    finally:

        writer.close()

        try:
            await writer.wait_closed()
        except Exception:
            pass


# =====================================
# 16. MAIN
# =====================================

async def main():

    server = await asyncio.start_server(
        http_handler,
        "0.0.0.0",
        PORT
    )

    logger.info(
        f"GapCut AI HTTP server started on port {PORT}"
    )

    logger.info(
        "GapCut AI Telegram bot started"
    )

    async with server:

        await asyncio.gather(
            server.serve_forever(),
            dp.start_polling(bot)
        )


# =====================================
# 17. RUN BOT
# =====================================

if __name__ == "__main__":

    try:
        asyncio.run(main())

    except KeyboardInterrupt:
        logger.info(
            "GapCut AI stopped"
        )
