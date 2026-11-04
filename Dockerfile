FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# ffmpeg + base fonts with Cyrillic/Tajik glyphs (ғ ӣ қ ӯ ҳ ҷ)
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    fontconfig \
    fonts-dejavu-core \
    fonts-noto-core \
    && rm -rf /var/lib/apt/lists/*

# Extra display fonts. A missing package never breaks the build; the app
# only offers fonts that really contain the Tajik letters.
RUN apt-get update && \
    for p in fonts-montserrat fonts-oswald fonts-roboto fonts-inter fonts-open-sans fonts-ubuntu fonts-comfortaa fonts-lobster fonts-bebas-neue; do \
      apt-get install -y --no-install-recommends "$p" || echo "skip $p"; \
    done && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Your own fonts: put .ttf/.otf files into the "fonts" folder of the repo.
RUN mkdir -p /usr/local/share/fonts/gapcut && \
    (cp -r /app/fonts/. /usr/local/share/fonts/gapcut/ 2>/dev/null || true) && \
    fc-cache -f

CMD ["python", "bot.py"]
