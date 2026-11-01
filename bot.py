import asyncio
import json
import logging
import os
import re
import shutil
import tempfile
from pathlib import Path

import httpx
from dotenv import load_dotenv
from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart
from aiogram.types import Message, FSInputFile, ReplyKeyboardMarkup, KeyboardButton

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")

MAX_VIDEO_SIZE = 19 * 1024 * 1024
MAX_OUTPUT_SIZE = 49 * 1024 * 1024
MAX_DURATION = 180

if not BOT_TOKEN or not GROQ_API_KEY:
    raise RuntimeError("BOT_TOKEN ё GROQ_API_KEY дар Environment гузошта нашудааст.")

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("gapcut")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
job_lock = asyncio.Lock()

keyboard = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="🎬 Монтаж кардани видео")],
        [KeyboardButton(text="ℹ️ Дастур / Инструкция")],
    ],
    resize_keyboard=True
)


async def run_command(command):
    process = await asyncio.create_subprocess_exec(
        *command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE
    )
    stdout, stderr = await process.communicate()

    if process.returncode != 0:
        raise RuntimeError(stderr.decode("utf-8", errors="ignore")[-2500:])

    return stdout.decode("utf-8", errors="ignore"), stderr.decode(
        "utf-8", errors="ignore"
    )


async def get_video_info(video_path):
    stdout, _ = await run_command([
        "ffprobe", "-v", "error",
        "-show_entries", "format=duration:stream=codec_type",
        "-of", "json", str(video_path)
    ])

    data = json.loads(stdout)
    duration = float(data["format"]["duration"])
    has_audio = any(
        stream.get("codec_type") == "audio"
        for stream in data.get("streams", [])
    )

    return duration, has_audio


async def transcribe_audio(audio_path):
    url = "https://api.groq.com/openai/v1/audio/transcriptions"

    headers = {"Authorization": f"Bearer {GROQ_API_KEY}"}
    data = [
        ("model", "whisper-large-v3-turbo"),
        ("response_format", "verbose_json"),
        ("timestamp_granularities[]", "word"),
        ("temperature", "0"),
    ]

    with open(audio_path, "rb") as audio_file:
        files = {
            "file": (audio_path.name, audio_file, "audio/mpeg")
        }

        async with httpx.AsyncClient(timeout=180) as client:
            response = await client.post(
                url, headers=headers, data=data, files=files
            )

    if response.status_code != 200:
        raise RuntimeError(
            f"Groq API: {response.status_code} — {response.text[:1000]}"
        )

    result = response.json()
    words = result.get("words", [])

    if not words:
        raise RuntimeError(
            "Groq матнро баргардонд, аммо вақти калимаҳо дастрас нест."
        )

    return words


async def detect_silences(video_path, duration):
    _, stderr = await run_command([
        "ffmpeg", "-hide_banner", "-i", str(video_path),
        "-af", "silencedetect=noise=-38dB:d=0.65",
        "-f", "null", "-"
    ])

    events = []
    starts = re.findall(r"silence_start:\s*([0-9.]+)", stderr)
    ends = re.findall(r"silence_end:\s*([0-9.]+)", stderr)

    for index, start in enumerate(starts):
        end = float(ends[index]) if index < len(ends) else duration
        events.append((max(0, float(start) - 0.12),
                       min(duration, end + 0.12)))

    return events


def make_keep_segments(duration, silences):
    segments = []
    cursor = 0.0

    for start, end in silences:
        if start > cursor + 0.25:
            segments.append((cursor, start))
        cursor = max(cursor, end)

    if duration > cursor + 0.25:
        segments.append((cursor, duration))

    return segments or [(0.0, duration)]


def compressed_time(original_time, silences):
    removed = 0.0

    for start, end in silences:
        if original_time >= end:
            removed += end - start
        elif original_time > start:
            return None

    return max(0.0, original_time - removed)


def ass_time(seconds):
    seconds = max(0, seconds)
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = seconds % 60
    return f"{hours}:{minutes:02}:{secs:05.2f}"


def clean_ass_text(value):
    value = str(value)
    return (
        value.replace("\\", "\\\\")
        .replace("{", "\\{")
        .replace("}", "\\}")
        .replace("\n", " ")
    )


def create_ass(words, silences, output_path):
    header = """[Script Info]
ScriptType: v4.00+
PlayResX: 720
PlayResY: 1280
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,DejaVu Sans,54,&H00FFFFFF,&H0000D5FF,&H00101010,&H80000000,-1,0,0,0,100,100,0,0,1,4,1,2,45,45,85,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""

    prepared = []

    for item in words:
        try:
            start = float(item["start"])
            end = float(item["end"])
            word = clean_ass_text(item["word"].strip())
        except (KeyError, TypeError, ValueError):
            continue

        new_start = compressed_time(start, silences)
        new_end = compressed_time(end, silences)

        if new_start is None or new_end is None or not word:
            continue

        if new_end <= new_start:
            continue

        prepared.append((new_start, new_end, word))

    lines = []

    for index in range(0, len(prepared), 6):
        group = prepared[index:index + 6]
        if not group:
            continue

        start = group[0][0]
        end = group[-1][1]
        karaoke = []

        for word_start, word_end, word in group:
            duration_cs = max(1, int((word_end - word_start) * 100))
            karaoke.append(f"{{\\k{duration_cs}}}{word}")

        text = " ".join(karaoke)
        lines.append(
            f"Dialogue: 0,{ass_time(start)},{ass_time(end)},"
            f"Default,,0,0,0,,{text}"
        )

    output_path.write_text(
        header + "\n".join(lines) + "\n",
        encoding="utf-8"
    )


async def render_video(video_path, output_path, ass_path, segments):
    filters = []

    for index, (start, end) in enumerate(segments):
        filters.append(
            f"[0:v]trim=start={start:.3f}:end={end:.3f},"
            f"setpts=PTS-STARTPTS[v{index}]"
        )
        filters.append(
            f"[0:a]atrim=start={start:.3f}:end={end:.3f},"
            f"asetpts=PTS-STARTPTS[a{index}]"
        )

    concat_inputs = "".join(
        f"[v{i}][a{i}]" for i in range(len(segments))
    )

    filters.append(
        f"{concat_inputs}concat=n={len(segments)}:v=1:a=1[v][a]"
    )

    ass_filter = (
        f"[v]scale=720:-2:force_original_aspect_ratio=decrease,"
        f"pad=720:ceil(ih/2)*2:(ow-iw)/2:(oh-ih)/2,"
        f"ass={ass_path}[vout]"
    )
    filters.append(ass_filter)

    await run_command([
        "ffmpeg", "-y", "-hide_banner",
        "-i", str(video_path),
        "-filter_complex", ";".join(filters),
        "-map", "[vout]", "-map", "[a]",
        "-c:v", "libx264", "-preset", "veryfast",
        "-crf", "27", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "96k",
        "-movflags", "+faststart",
        str(output_path)
    ])


@dp.message(CommandStart())
async def start_handler(message: Message):
    await message.answer(
        "Салом! 👋 Ба GapCut AI хуш омадӣ.\n\n"
        "Видео фирист — бот хомӯшиҳои дарозро кӯтоҳ мекунад, "
        "суханро мешиносад ва субтитрҳои калима ба калима месозад.\n\n"
        "Привет! Отправь видео, и я автоматически смонтирую его.",
        reply_markup=keyboard
    )


@dp.message(F.text.in_({"ℹ️ Дастур / Инструкция"}))
async def help_handler(message: Message):
    await message.answer(
        "🎬 Дастур:\n"
        "1. Видео фирист.\n"
        "2. То анҷоми кор интизор шав.\n"
        "3. Видеои тайёрро гир.\n\n"
        "Маҳдудиятҳо:\n"
        "• Ҳаҷм: то 19 MB\n"
        "• Давомнокӣ: то 3 дақиқа\n\n"
        "Инструкция:\n"
        "Отправь видео до 19 MB и длительностью до 3 минут."
    )


@dp.message(F.video | F.document)
async def video_handler(message: Message):
    video = message.video

    if message.document:
        if not (message.document.mime_type or "").startswith("video/"):
            await message.answer("Лутфан файли видеоӣ фирист.")
            return
        video = message.document

    if not video:
        await message.answer("Видео ёфт нашуд.")
        return

    if video.file_size and video.file_size > MAX_VIDEO_SIZE:
        await message.answer("Ҳаҷми видео бояд аз 19 MB зиёд набошад.")
        return

    if job_lock.locked():
        await message.answer(
            "Ҳоло як видео коркард шуда истодааст. Каме интизор шав."
        )
        return

    async with job_lock:
        work_dir = Path(tempfile.mkdtemp(prefix="gapcut_"))

        try:
            await message.answer("📥 Видео қабул шуд. Коркард оғоз шуд...")

            input_path = work_dir / "input_video.mp4"
            audio_path = work_dir / "audio.mp3"
            ass_path = work_dir / "subtitles.ass"
            output_path = work_dir / "gapcut_output.mp4"

            telegram_file = await bot.get_file(video.file_id)
            await bot.download_file(
                telegram_file.file_path,
                destination=input_path
            )

            duration, has_audio = await get_video_info(input_path)

            if duration > MAX_DURATION:
                await message.answer("Давомнокии видео бояд то 3 дақиқа бошад.")
                return

            if not has_audio:
                await message.answer("Дар видео садо ёфт нашуд.")
                return

            await message.answer("🎧 Садо ва суханро таҳлил мекунам...")

            await run_command([
                "ffmpeg", "-y", "-i", str(input_path),
                "-vn", "-ac", "1", "-ar", "16000",
                "-b:a", "64k", str(audio_path)
            ])

            if audio_path.stat().st_size > 24 * 1024 * 1024:
                await message.answer(
                    "Файли садо барои API хеле калон шуд. "
                    "Лутфан видеои кӯтоҳтар фирист."
                )
                return

            words = await transcribe_audio(audio_path)

            await message.answer("✂️ Хомӯшиҳои дарозро кӯтоҳ мекунам...")

            silences = await detect_silences(input_path, duration)
            segments = make_keep_segments(duration, silences)

            create_ass(words, silences, ass_path)

            await message.answer("🎬 Субтитрҳоро мегузорам ва видео месозам...")

            await render_video(
                input_path, output_path, ass_path, segments
            )

            if output_path.stat().st_size > MAX_OUTPUT_SIZE:
                await message.answer(
                    "Ҳаҷми видеои тайёр аз 49 MB зиёд шуд. "
                    "Видеои кӯтоҳтар ё сабуктар фирист."
                )
                return

            await message.answer_video(
                FSInputFile(output_path),
                caption="✅ GapCut AI — видеои тайёр!"
            )

        except Exception as error:
            logger.exception("Video processing failed")
            await message.answer(
                "❌ Ҳангоми коркарди видео хато рӯй дод.\n"
                "Бори дигар кӯшиш кун. Агар такрор шавад, ба администратор хабар деҳ."
            )
        finally:
            shutil.rmtree(work_dir, ignore_errors=True)


async def main():
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
