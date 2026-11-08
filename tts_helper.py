"""
tts_helper.py
-------------
Генерация естественных голосовых сообщений (голосовых заметок) для Telegram через Edge TTS (ru-RU-SvetlanaNeural).
Настроено на яркий, милый и естественный голос 20-летней девушки (вайб дружелюбной пикми-девчонки):
- Повышенный питч (+22Hz) для молодого, звонкого и нежного звучания
- Увеличенный темп (+8%) для живой, естественной разговорной речи
"""

import io
import re
import asyncio
import edge_tts

DEFAULT_VOICE = "ru-RU-SvetlanaNeural"
DEFAULT_PITCH = "+5Hz"
DEFAULT_RATE = "-2%"

# Порог длины текста (в символах без тегов), при превышении которого ответ отправляется голосовым сообщением
LONG_TEXT_VOICE_THRESHOLD = 250
# Максимальная длина озвучиваемого текста (~1.5 минуты живой речи)
MAX_VOICE_CHARS = 1200


def clean_text_for_speech(text: str) -> str:
    """Очищает текст от HTML-тегов, ссылок, блоков кода, эмодзи и markdown-артефактов для естественной озвучки."""
    if not text:
        return ""
    # 1. Удаляем блоки кода
    s = re.sub(r"```[\s\S]*?```", "код приведён в сообщении ниже", text)
    s = re.sub(r"<pre\b[^>]*>[\s\S]*?</pre>", "код приведён в сообщении ниже", s, flags=re.I)
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


async def generate_voice_bytes(
    text: str,
    voice: str = DEFAULT_VOICE,
    pitch: str = DEFAULT_PITCH,
    rate: str = DEFAULT_RATE
) -> bytes | None:
    """Генерирует байты аудиозаписи речи через edge-tts с голосом 20-летней девушки."""
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

    try:
        communicate = edge_tts.Communicate(cleaned, voice=voice, pitch=pitch, rate=rate)
        audio_stream = bytearray()
        async for chunk in communicate.stream():
            if chunk.get("type") == "audio":
                audio_stream.extend(chunk.get("data", b""))
        return bytes(audio_stream) if audio_stream else None
    except Exception as e:
        print(f"[TTS] Ошибка генерации голосового сообщения: {e}")
        return None


async def send_response_smart(client, event, ans: str, threshold: int = LONG_TEXT_VOICE_THRESHOLD):
    """
    Умная отправка ответа пользователю:
    - Если ответ короткий (< 250 символов) -> отправляет компактный текст в виде аккуратных реплик.
    - Если ответ длинный (>= 250 символов) -> отправляет ГОЛОСОВОЕ СООБЩЕНИЕ (ГС) голосом 20-летней девушки,
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
        # Ищем блоки кода в ответе
        code_blocks = re.findall(r"(```[\s\S]*?```|<pre\b[^>]*>[\s\S]*?</pre>)", ans)
        voice_sent = False
        try:
            voice_data = await generate_voice_bytes(ans)
            if voice_data:
                voice_buf = io.BytesIO(voice_data)
                voice_buf.name = "voice.ogg"

                # Показываем статус "записывает голосовое сообщение..."
                try:
                    async with client.action(chat_id, "record-audio"):
                        await asyncio.sleep(1.0)
                except Exception:
                    pass

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
