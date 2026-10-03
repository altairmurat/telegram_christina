import os
from collections import defaultdict, deque
from google import genai
from google.genai import types
from pydantic import BaseModel, Field
from env import GEMINI_API_KEY

if not GEMINI_API_KEY:
    raise RuntimeError("GEMINI_API_KEY not found in .env")

client = genai.Client(api_key=GEMINI_API_KEY)
MODEL = os.getenv("GEMINI_MODEL", "gemini-3.5-flash-lite")
user_contexts = defaultdict(lambda: deque(maxlen=10))

class EmailIntent(BaseModel):
    is_email: bool = Field(description="True only if user explicitly asks to write, send, compose, or draft an email/message to someone")
    recipient_name: str = Field(default="", description="Name of recipient")
    topic: str = Field(default="", description="Topic or purpose of the email")
    recipient_email: str = Field(default="", description="Email address if explicitly provided")
    sender_name: str = Field(default="", description="Sender name if specified")

class EmailDraft(BaseModel):
    subject: str = Field(description="Email subject line")
    body: str = Field(description="Email body text")

async def parse_email_intent(text: str) -> EmailIntent:
    """Определяет намерение написать email через структурированный вывод Gemini."""
    cfg = types.GenerateContentConfig(response_mime_type="application/json", response_schema=EmailIntent, temperature=0.1)
    res = await client.aio.models.generate_content(model=MODEL, contents=text, config=cfg)
    try:
        return EmailIntent.model_validate_json(res.text or "{}")
    except Exception:
        return EmailIntent(is_email=False)

async def generate_email_text(topic: str, recipient: str, sendername: str) -> tuple[str, str]:
    """Генерирует тему и текст письма через Gemini."""
    prompt = f"Напиши email письмо. Получатель: {recipient}. Тема/суть: {topic}. Отправитель: {sendername or 'пользователь'}."
    cfg = types.GenerateContentConfig(response_mime_type="application/json", response_schema=EmailDraft, temperature=0.7)
    res = await client.aio.models.generate_content(model=MODEL, contents=prompt, config=cfg)
    try:
        data = EmailDraft.model_validate_json(res.text or "{}")
        return data.subject, data.body
    except Exception:
        return topic[:50] or "Без темы", res.text or ""

async def ask_gemini(chat_text: str, user_id: int | None = None) -> str:
    """Асинхронный диалог с моделью с изолированной историей на каждого пользователя."""
    hist = user_contexts[user_id]
    hist.append(types.Content(role="user", parts=[types.Part.from_text(text=chat_text)]))
    cfg = types.GenerateContentConfig(
        system_instruction="Ты персональный ассистент Кристина. Отвечай кратко, понятно и дружелюбно на языке пользователя.",
        temperature=0.7
    )
    res = await client.aio.models.generate_content(model=MODEL, contents=list(hist), config=cfg)
    reply = (res.text or "").strip()
    hist.append(types.Content(role="model", parts=[types.Part.from_text(text=reply)]))
    return reply

ask_gpt = ask_gemini  # обратная совместимость

async def transcribe_voice(voice_bytes: bytes) -> str:
    """Транскрибирует голосовое сообщение через Gemini аудио-модель."""
    part = types.Part.from_bytes(data=voice_bytes, mime_type="audio/ogg")
    res = await client.aio.models.generate_content(
        model=MODEL,
        contents=[part, "Transcribe this audio verbatim in its original language. Output only the transcription, no comments."]
    )
    return (res.text or "").strip()

async def process_telegram_image(message, user_prompt: str) -> str:
    """Обработка изображений через мультимодальный Gemini."""
    image_bytes = await message.download_media(file=bytes)
    if not image_bytes:
        return "Не удалось загрузить изображение."
    part = types.Part.from_bytes(data=image_bytes, mime_type="image/jpeg")
    res = await client.aio.models.generate_content(model=MODEL, contents=[part, user_prompt or "Опиши это изображение"])
    return (res.text or "Ответ пуст.").strip()
