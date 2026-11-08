"""
tts_helper.py
-------------
Генерация естественных голосовых сообщений (голосовых заметок) для Telegram через Edge TTS (ru-RU-SvetlanaNeural).
Позволяет ассистенту Кристине отвечать живым женским голосом, когда ответ подробный или длинный.
"""

import io
import re
import asyncio
import edge_tts

DEFAULT_VOICE = "ru-RU-SvetlanaNeural"
# Порог длины текста (в символах без тегов), при превышении которого отправляется голосовое сообщение
LONG_TEXT_VOICE_THRESHOLD = 280
# Максимальная длина озвучиваемого текста (~1.5 минуты живой речи)
MAX_VOICE_CHARS = 1200


def clean_text_for_speech(text: str) -> str:
    """Очищает текст от HTML-тегов, ссылок, блоков кода и markdown-артефактов для естественной озвучки."""
    if not text:
        return ""
    # 1. Удаляем блоки кода
    s = re.sub(r"```[\s\S]*?```", "код приведён в сообщении выше", text)
    s = re.sub(r"<pre\b[^>]*>[\s\S]*?</pre>", "код приведён в сообщении выше", s, flags=re.I)
    s = re.sub(r"<code>[^<]*</code>", "", s, flags=re.I)
    # 2. Удаляем HTML-теги, сохраняя текст ссылок (например <a href="...">ссылка</a> -> ссылка)
    s = re.sub(r"<[^>]+>", "", s)
    # 3. Удаляем оставшиеся сырые URL-адреса
    s = re.sub(r"https?://\S+", "", s)
    # 4. Удаляем markdown-символы
    s = re.sub(r"[#*_~`>]", "", s)
    # 5. Заменяем спецсущности
    s = s.replace("&amp;", "и").replace("&lt;", "<").replace("&gt;", ">").replace("&quot;", '"')
    # 6. Убираем лишние пробелы и переносы
    s = re.sub(r"\s+", " ", s).strip()
    return s


def is_long_for_voice(text: str, threshold: int = LONG_TEXT_VOICE_THRESHOLD) -> bool:
    """Проверяет, является ли ответ длинным и требующим сопровождения голосовым сообщением."""
    clean = clean_text_for_speech(text)
    return len(clean) >= threshold


async def generate_voice_bytes(text: str, voice: str = DEFAULT_VOICE) -> bytes | None:
    """Генерирует байты аудиозаписи речи через edge-tts."""
    cleaned = clean_text_for_speech(text)
    if not cleaned or len(cleaned) < 15:
        return None

    # Если текст очень длинный, аккуратно обрезаем по последнему завершённому предложению до MAX_VOICE_CHARS
    if len(cleaned) > MAX_VOICE_CHARS:
        cut = cleaned[:MAX_VOICE_CHARS]
        last_punct = max(cut.rfind("."), cut.rfind("!"), cut.rfind("?"))
        if last_punct > 400:
            cleaned = cut[: last_punct + 1]
        else:
            cleaned = cut + "..."

    try:
        communicate = edge_tts.Communicate(cleaned, voice=voice)
        audio_stream = bytearray()
        async for chunk in communicate.stream():
            if chunk.get("type") == "audio":
                audio_stream.extend(chunk.get("data", b""))
        return bytes(audio_stream) if audio_stream else None
    except Exception as e:
        print(f"[TTS] Ошибка генерации голосового сообщения: {e}")
        return None


async def send_voice_if_long(client, event, text: str, threshold: int = LONG_TEXT_VOICE_THRESHOLD):
    """
    Если ответ длинный (превышает threshold символов), генерирует голосовое сообщение
    с приятным женским голосом Кристины и отправляет его в чат Telegram с атрибутом voice_note=True.
    """
    if not client or not event or not is_long_for_voice(text, threshold):
        return None

    try:
        voice_data = await generate_voice_bytes(text)
        if not voice_data:
            return None

        chat_id = getattr(event, "chat_id", None)
        if not chat_id:
            return None

        is_private = getattr(event, "is_private", True)
        reply_to = None if is_private else getattr(event, "id", None)

        voice_buf = io.BytesIO(voice_data)
        voice_buf.name = "voice.ogg"

        # Имитируем статус записи голосового сообщения
        try:
            async with client.action(chat_id, "record-audio"):
                await asyncio.sleep(1.2)
        except Exception:
            pass

        return await client.send_file(
            chat_id,
            voice_buf,
            voice_note=True,
            reply_to=reply_to,
        )
    except Exception as e:
        print(f"[TTS] Ошибка отправки голосового сообщения: {e}")
        return None
