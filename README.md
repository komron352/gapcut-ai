# GAPCUT AI

Telegram bot and Web Mini App for AI-assisted short-form video editing.

## Hybrid editing architecture

- FastAPI + Aiogram backend
- Groq Whisper word-level transcription
- Tajik/Russian transcript handling and subtitle generation
- FFmpeg proxy-based rendering with conservative CPU threading
- Optional Renderster provider with local FFmpeg fallback
- Deterministic Hybrid Director EDL built from transcript words and scene analysis

The existing Telegram Mini App is intentionally kept unchanged during backend engine work.

## Local backend checks

Python 3.11 is used by the Docker image.

```bash
pip install -r requirements.txt
BOT_TOKEN="123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi" python -m unittest discover -s tests -v
```

A real render also requires FFmpeg/FFprobe and configured service credentials. Never commit API keys or Telegram tokens.

## Rollout safety

Feature work is developed on a dedicated branch and reviewed through a draft pull request. No production Render deployment or merge to `main` is performed without explicit approval and real-media validation.
