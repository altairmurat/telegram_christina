"""
tts_helper.py
-------------
Генерация естественных и живых голосовых сообщений (голосовых заметок) для Telegram
с использованием сверхреалистичной нейросетевой модели Google Gemini 3.8 Flash TTS.

- Первичный голос: Leda (встроенный студийный голос Google: молодая, милая 20-летняя девушка с естественной русской речью, быстрый отклик ~7 сек)
- Поддержка кастомного голоса через env GEMINI_VOICE_ID (с автоматическим откатом на Leda при недоступности)
- Конвертация: WAV -> OGG Opus через soundfile для нативной поддержки голосовых в Telegram
- Резервный движок: edge-tts при отсутствии сети или квоты
- Порог срабатывания:
  * Длинные ответы (>= 130 символов)
  * Запросы на рассказ / анекдот / голосовое (ключевые слова: анекдот, расскажи, голос, гс, скажи)
  * Короткие реплики (< 130 символов) отправляются быстрым живым текстом.
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

# Первичный студийный голос в Gemini 3.8 Flash TTS
# Leda — официальный студийный голос Google ("Youthful", милая 20-летняя девушка с живым русским произношением)
PRIMARY_GEMINI_VOICE = os.getenv("GEMINI_VOICE_NAME", "Leda")
GEMINI_TTS_MODEL = "gemini-3.8-flash-tts"

# Резервные настройки Edge-TTS
DEFAULT_EDGE_VOICE = "ru-RU-SvetlanaNeural"
DEFAULT_EDGE_PITCH = "+5Hz"
DEFAULT_EDGE_RATE = "-2%"

# Порог длины текста (в символах без тегов) для автоматической озвучки
LONG_TEXT_VOICE_THRESHOLD = 130
MAX_VOICE_CHARS = 1200

VOICE_INTENT_KEYWORDS = (
    "анекдот", "голос", "голосовое", "гс", "расскажи", "озвучь", "скажи", "поболтай", "спой", "историю"
)


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


def is_long_for_voice(text: str, user_text: str = "", threshold: int = LONG_TEXT_VOICE_THRESHOLD) -> bool:
    """Проверяет, требуется ли отправка ответа голосовым сообщением (ТОЛЬКО если пользователь прямо попросил об этом).
    Теперь отправляем ГС строго при наличии ключевых слов типа 'по гс', 'голосовым', 'войс', 'озвучь', 'расскажи по гс' и т.п.
    """
    clean = clean_text_for_speech(text)
    if not clean or len(clean) < 15:
        return False
    u_low = (user_text or "").lower()
    voice_triggers = ("по гс", "голосовое", "войс", "voice", "гс", "озвучь", "надиктони", "скажи голосом")
    has_explicit_request = any(t in u_low for t in voice_triggers) or any(k in u_low for k in VOICE_INTENT_KEYWORDS)
    return bool(has_explicit_request and len(clean) >= 15)


def _wav_to_ogg_opus(wav_bytes: bytes) -> bytes:
    """Синхронная конвертация WAV PCM в нативный OGG Opus через soundfile."""
    data, samplerate = sf.read(io.BytesIO(wav_bytes))
    buf = io.BytesIO()
    sf.write(buf, data, samplerate, format="OGG", subtype="OPUS")
    return buf.getvalue()


def _sync_call_gemini_tts(cleaned_text: str, voice_name: str) -> bytes | None:
    """Синхронный вызов Gemini Interactions API в отдельном потоке (без зависаний SSL asyncio)."""
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        return None
    from google import genai
    client = genai.Client(api_key=api_key)

    res = client.interactions.create(
        model=GEMINI_TTS_MODEL,
        input=[{
            "type": "user_input",
            "content": [{
                "type": "text",
                "text": cleaned_text,
                "annotations": [{
                    "type": "speech_metadata",
                    "style": "natural, calm, youthful, pleasant and casual 20yo Russian girl voice note to a friend",
                }],

            }],
        }],
        response_format={"type": "audio"},
        generation_config={
            "speech_config": [
                {"voice": voice_name}
            ]
        }
    )

    if res.output_audio and res.output_audio.data:
        wav_bytes = base64.b64decode(res.output_audio.data)
        return _wav_to_ogg_opus(wav_bytes)
    return None


async def generate_gemini_voice_bytes(cleaned_text: str) -> bytes | None:
    """Генерирует живой голос 20-летней девушки через Google Gemini 3.8 Flash TTS со студийным голосом Leda."""
    try:
        ogg_data = await asyncio.to_thread(_sync_call_gemini_tts, cleaned_text, PRIMARY_GEMINI_VOICE)
        if ogg_data:
            return ogg_data
    except Exception as e:
        print(f"[TTS] Ошибка Gemini TTS ({e}), откат на резервный синтезатор...")
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
    2. Пытается сгенерировать ультра-естественный голос 20-летней девушки через Gemini 3.8 Flash TTS (Leda / Custom).
    3. При сетевой ошибке делает откат на резервный Edge TTS.
    """
    cleaned = clean_text_for_speech(text)
    if not cleaned or len(cleaned) < 10:
        return None

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


async def send_response_smart(client, event, ans: str, threshold: int = LONG_TEXT_VOICE_THRESHOLD, user_text: str = ""):
    """
    Умная отправка ответа пользователю:
    - Если ответ короткий (< 130 символов) -> отправляет компактный текст в виде аккуратных реплик.
    - Если ответ длинный (>= 130 символов) или запрос на голос/анекдот -> отправляет ГОЛОСОВОЕ СООБЩЕНИЕ (ГС) живым девичьим голосом,
      НЕ отправляя перед ним простыню текста.
    - Если в ответе был код (```...```), он аккуратно отправляется текстом под ГС.
    - При любой сетевой ошибке генерации речи мягко откатывается на текстовую отправку.
    """
    if not ans or not ans.strip():
        return None

    chat_id = getattr(event, "chat_id", None)
    is_private = getattr(event, "is_private", True)
    reply_to = None if is_private else getattr(event, "id", None)

    # Извлекаем текст входящего сообщения, если не передан явно
    if not user_text:
        user_text = getattr(event, "raw_text", "") or getattr(event, "text", "")

    if is_long_for_voice(ans, user_text=user_text, threshold=threshold):
        code_blocks = re.findall(r"(```[\s\S]*?```|<pre\b[^>]*>[\s\S]*?</pre>)", ans)
        voice_sent = False
        try:
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

                if code_blocks:
                    from tg_formatter import safe_reply
                    code_text = "\n\n".join(code_blocks)
                    await safe_reply(event, f"💻 <b>Код:</b>\n\n{code_text}")
        except Exception as e:
            print(f"[VOICE] Ошибка отправки ГС, откат на текст: {e}")
            voice_sent = False

        if voice_sent:
            return

    # Текстовая отправка репликами
    from tg_formatter import safe_reply_human_streamed
    return await safe_reply_human_streamed(event, ans)
