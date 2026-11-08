"""
tts_helper.py
-------------
Генерация естественных и живых голосовых сообщений (голосовых заметок) для Telegram
с использованием сверхреалистичной нейросетевой модели Google Gemini (gemini-3.8-flash-tts)
и кастомного голосового профиля 20-летней девушки ("пикми-подруга", мягкий, естественный разговорный девичий голос).

- Первичный движок: Google Gemini 3.8 Flash TTS (Interactions API)
  - Голосовой профиль: voice_ldsq01tgm8mp (Designed Voice: нежный, живой девичий тембр)
  - Формат: конвертация WAV -> OGG Opus через soundfile для нативной поддержки в Telegram
- Резервный движок: edge-tts (ru-RU-SvetlanaNeural) при отсутствии сети или квоты
- Порог срабатывания: длинные ответы (>= 250 символов) озвучиваются голосовым сообщением,
  а короткие отправляются текстом.
"""

import os
import io
import re
import asyncio
import base64
import soundfile as sf
import edge_tts
from dotenv import load_dotenv

load_dotenv()

# Кастомный созданный голосовой профиль в Google GenAI
# (20-летняя русскоязычная девушка, мягкий, нежный, естественный девичий голос без робот-эффекта)
PRIMARY_GEMINI_VOICE_ID = os.getenv("GEMINI_VOICE_ID", "voice_ldsq01tgm8mp")
GEMINI_TTS_MODEL = "gemini-3.8-flash-tts"

# Резервные настройки Edge-TTS
DEFAULT_EDGE_VOICE = "ru-RU-SvetlanaNeural"
DEFAULT_EDGE_PITCH = "+5Hz"
DEFAULT_EDGE_RATE = "-2%"

# Порог длины текста (в символах без тегов), при превышении которого ответ отправляется голосовым сообщением
LONG_TEXT_VOICE_THRESHOLD = 250
# Максимальная длина озвучиваемого текста (~1.5 минуты живой речи)
MAX_VOICE_CHARS = 1200


def clean_text_for_speech(text: str) -> str:
    """Очищает текст от HTML-тегов, ссылок, блоков кода, эмодзи и markdown-артефактов для естественной озвучки."""
    if not text:
        return ""
    # 1. Удаляем блоки кода целиком (код отправляется текстом отдельно)
    s = re.sub(r"```[\s\S]*?```", "", text)
    s = re.sub(r"<pre\b[^>]*>[\s\S]*?</pre>", "", s, flags=re.I)
    s = re.sub(r"<code>[^<]*</code>", "", s, flags=re.I)
    # 2. Удаляем HTML-теги, сохраняя текст ссылок
    s = re.sub(r"<[^>]+>", "", s)
    # 3. Удаляем оставшиеся сырые URL-адреса
    s = re.sub(r"https?://\S+", "", s)
    # 4. Удаляем списочные маркеры и markdown-символы
    s = re.sub(r"^\s*[-*•\d]\.?\s*", "", s, flags=re.M)
    s = re.sub(r"[#*_~`>]", "", s)
    # 5. Удаляем эмодзи, чтобы синтезатор речи не читал их названия
    s = re.sub(r"[\U00010000-\U0010ffff\u2600-\u27bf\u2300-\u23ff]", "", s)
    # 6. Заменяем спецсущности
    s = s.replace("&amp;", "и").replace("&lt;", "<").replace("&gt;", ">").replace("&quot;", '"')
    # 7. Убираем лишние пробелы и переносы
    s = re.sub(r"\s+", " ", s).strip()
    return s


def is_long_for_voice(text: str, threshold: int = LONG_TEXT_VOICE_THRESHOLD) -> bool:
    """Проверяет, является ли ответ длинным и требующим отправки голосовым сообщением."""
    clean = clean_text_for_speech(text)
    return len(clean) >= threshold


def _wav_to_ogg_opus(wav_bytes: bytes) -> bytes:
    """Синхронная конвертация WAV PCM в нативный OGG Opus через soundfile."""
    data, samplerate = sf.read(io.BytesIO(wav_bytes))
    buf = io.BytesIO()
    sf.write(buf, data, samplerate, format="OGG", subtype="OPUS")
    return buf.getvalue()


async def generate_gemini_voice_bytes(cleaned_text: str) -> bytes | None:
    """
    Генерирует живой голос 20-летней девушки через Google Gemini 3.8 Flash TTS
    с кастомным голосовым профилем и конвертирует в OGG Opus для Telegram.
    """
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        return None

    try:
        from google import genai
        client = genai.Client(api_key=api_key)

        res = await client.aio.interactions.create(
            model=GEMINI_TTS_MODEL,
            input=[{
                "type": "user_input",
                "content": [{
                    "type": "text",
                    "text": cleaned_text,
                    "annotations": [{
                        "type": "speech_metadata",
                        "style": "casual, soft, natural conversational, cute and lively 20yo girl voice note to a friend",
                    }],
                }],
            }],
            response_format={"type": "audio"},
            generation_config={
                "speech_config": [
                    {"voice": PRIMARY_GEMINI_VOICE_ID}
                ]
            }
        )

        if res.output_audio and res.output_audio.data:
            wav_bytes = base64.b64decode(res.output_audio.data)
            ogg_bytes = await asyncio.to_thread(_wav_to_ogg_opus, wav_bytes)
            return ogg_bytes
        else:
            print("[TTS] Gemini TTS вернул пустой audio output, переключение на резервный...")
            return None
    except Exception as e:
        print(f"[TTS] Ошибка Gemini TTS ({e}), переключение на резервный синтезатор...")
        return None


async def generate_edge_voice_bytes(
    cleaned_text: str,
    voice: str = DEFAULT_EDGE_VOICE,
    pitch: str = DEFAULT_EDGE_PITCH,
    rate: str = DEFAULT_EDGE_RATE
) -> bytes | None:
    """Резервная генерация через Edge TTS при сбое Gemini API."""
    try:
        communicate = edge_tts.Communicate(cleaned_text, voice=voice, pitch=pitch, rate=rate)
        audio_stream = bytearray()
        async for chunk in communicate.stream():
            if chunk.get("type") == "audio":
                audio_stream.extend(chunk.get("data", b""))
        return bytes(audio_stream) if audio_stream else None
    except Exception as e:
        print(f"[TTS] Ошибка резервного Edge TTS: {e}")
        return None


async def generate_voice_bytes(text: str) -> bytes | None:
    """
    Основная точка входа генерации аудио:
    1. Очищает текст и обрезает слишком длинные монологи.
    2. Пытается сгенерировать ультра-естественный голос 20-летней девушки через Gemini 3.8 Flash TTS.
    3. При сетевой ошибке делает откат на резервный Edge TTS.
    """
    cleaned = clean_text_for_speech(text)
    if not cleaned or len(cleaned) < 10:
        return None

    # Если текст очень длинный, аккуратно обрезаем по последнему завершённому предложению
    if len(cleaned) > MAX_VOICE_CHARS:
        cut = cleaned[:MAX_VOICE_CHARS]
        last_punct = max(cut.rfind("."), cut.rfind("!"), cut.rfind("?"))
        if last_punct > 350:
            cleaned = cut[: last_punct + 1]
        else:
            cleaned = cut + "..."

    # 1. Пробуем Gemini 3.8 Flash TTS
    audio_data = await generate_gemini_voice_bytes(cleaned)
    if audio_data:
        return audio_data

    # 2. Резервный откат на Edge TTS
    print("[TTS] Используется резервный синтезатор Edge TTS")
    return await generate_edge_voice_bytes(cleaned)


async def send_response_smart(client, event, ans: str, threshold: int = LONG_TEXT_VOICE_THRESHOLD):
    """
    Умная отправка ответа пользователю:
    - Если ответ короткий (< 250 символов) -> отправляет компактный текст в виде аккуратных реплик.
    - Если ответ длинный (>= 250 символов) -> отправляет ГОЛОСОВОЕ СООБЩЕНИЕ (ГС) живым голосом 20-летней девушки,
      НЕ отправляя перед ним простыню текста.
    - Если в длинном ответе был код (```...```) или важные ссылки, они аккуратно отправляются текстом под ГС.
    - При любой сетевой ошибке генерации речи мягко откатывается на текстовую отправку.
    """
    if not ans or not ans.strip():
        return None

    chat_id = getattr(event, "chat_id", None)
    is_private = getattr(event, "is_private", True)
    reply_to = None if is_private else getattr(event, "id", None)

    if is_long_for_voice(ans, threshold=threshold):
        # Ищем блоки кода в ответе, чтобы продублировать их текстом ниже голосового сообщения
        code_blocks = re.findall(r"(```[\s\S]*?```|<pre\b[^>]*>[\s\S]*?</pre>)", ans)
        voice_sent = False
        try:
            # Показываем статус "записывает голосовое сообщение..." в процессе генерации
            try:
                async with client.action(chat_id, "record-audio"):
                    voice_data = await generate_voice_bytes(ans)
            except Exception:
                voice_data = await generate_voice_bytes(ans)

            if voice_data:
                voice_buf = io.BytesIO(voice_data)
                voice_buf.name = "voice.ogg"

                await client.send_file(
                    chat_id,
                    voice_buf,
                    voice_note=True,
                    reply_to=reply_to,
                )
                voice_sent = True

                # Если в ответе был код, отправляем его отдельным аккуратным сообщением под ГС
                if code_blocks:
                    from tg_formatter import safe_reply
                    code_text = "\n\n".join(code_blocks)
                    await safe_reply(event, f"💻 <b>Код:</b>\n\n{code_text}")
        except Exception as e:
            print(f"[VOICE] Ошибка отправки ГС, откат на текст: {e}")
            voice_sent = False

        if voice_sent:
            return

    # Короткий ответ или безопасный откат при ошибке TTS: отправляем естественными текстовыми сообщениями
    from tg_formatter import safe_reply_human_streamed
    return await safe_reply_human_streamed(event, ans)
