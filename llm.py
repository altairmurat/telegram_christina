import os, re, asyncio, requests
from collections import defaultdict, deque
from google import genai
from google.genai import types
from pydantic import BaseModel, Field
from env import GEMINI_API_KEY

if not GEMINI_API_KEY:
    raise RuntimeError("GEMINI_API_KEY not found in .env")

client = genai.Client(api_key=GEMINI_API_KEY)
MODEL = os.getenv("GEMINI_MODEL", "gemini-3.5-flash-lite")
# 10 пар сообщений (10 от пользователя + 10 ответов ассистента = 20 элементов)
user_contexts = defaultdict(lambda: deque(maxlen=20))

class UserIntent(BaseModel):
    intent: str = Field(
        default="chat",
        description="'video' ONLY if user explicitly asks to generate/create/make a video or animate a photo; 'calendar' if user asks to add/create/schedule a plan, event, or reminder in Google Calendar OR asks to view/check upcoming plans/calendar; 'email' ONLY if user explicitly asks to write/send/compose an email to someone; 'pdf' ONLY if user explicitly asks to generate/create/make a PDF file or document; 'portal' ONLY if user asks to check the DGIST portal/announcements/notices; 'group_query' if user asks to check, find, summarize, or search messages, topics, announcements, or discussions in the Telegram group/topics/chat; 'chat' for everything else"
    )
    is_video: bool = Field(default=False, description="True if intent == 'video' or user asks to generate/make a video or animate an image")
    video_prompt_en: str = Field(default="", description="Detailed visual prompt in English for generating the video (translate user's video idea into vivid English visual description)")
    is_email: bool = Field(default=False, description="True if intent == 'email'")
    is_pdf: bool = Field(default=False, description="True if intent == 'pdf'")
    is_portal: bool = Field(default=False, description="True if intent == 'portal'")
    is_group_query: bool = Field(default=False, description="True if intent == 'group_query' or user asks about group messages/topics")
    is_calendar: bool = Field(default=False, description="True if intent == 'calendar' or user asks about calendar/plans/reminders")
    calendar_action: str = Field(default="create", description="'create' to add/schedule event or reminder, 'list' to view upcoming calendar plans")
    calendar_summary: str = Field(default="", description="Clean, concise title of the plan/task, e.g. 'Сделать домашку по физике'")
    calendar_start_iso: str = Field(default="", description="Start time in ISO 8601 (YYYY-MM-DDTHH:MM:SS), calculated using current_time and user prompt")
    calendar_end_iso: str = Field(default="", description="End time in ISO 8601 (YYYY-MM-DDTHH:MM:SS), default 1 hour after start if not specified")
    is_important: bool = Field(default=False, description="True if user mentioned 'оч важно', 'срочно', 'супер важно', 'важно', 'high priority'")
    reminder_minutes: list[int] = Field(default_factory=lambda: [15], description="Minutes before event to remind user, e.g. [15] or [10, 30, 60] if important")
    calendar_description: str = Field(default="", description="Optional additional details for the calendar event")
    group_topic_name: str = Field(default="", description="Name or title of the group topic if mentioned (e.g. 'important announcements', 'homework', 'general')")
    group_search_query: str = Field(default="", description="Key terms or topic to search within group messages")
    recipient_name: str = Field(default="", description="Name of recipient if email")
    topic: str = Field(default="", description="Topic of the email or detailed topic/instructions for the PDF")
    recipient_email: str = Field(default="", description="Email address if explicitly provided")
    sender_name: str = Field(default="", description="Sender name if specified")
    pdf_filename: str = Field(default="document.pdf", description="Short English filename ending with .pdf, e.g. electrodynamics.pdf")

EmailIntent = UserIntent

class EmailDraft(BaseModel):
    subject: str = Field(description="Email subject line")
    body: str = Field(description="Email body text")

async def parse_user_intent(text: str) -> UserIntent:
    """Определяет намерение пользователя (video / calendar / email / pdf / portal / group_query / обычный чат) через структурированный вывод Gemini."""
    from datetime import datetime
    now_str = datetime.now().strftime("%Y-%m-%dT%H:%M:%S+09:00 (%A)")
    prompt = f"Current time: {now_str} (Asia/Seoul timezone, UTC+9). Parse this user request:\n\"{text}\""
    cfg = types.GenerateContentConfig(response_mime_type="application/json", response_schema=UserIntent, temperature=0.1)
    res = await client.aio.models.generate_content(model=MODEL, contents=prompt, config=cfg)
    try:
        data = UserIntent.model_validate_json(res.text or "{}")
        if data.intent == "video": data.is_video = True
        elif data.intent == "email": data.is_email = True
        elif data.intent == "pdf": data.is_pdf = True
        elif data.intent == "portal": data.is_portal = True
        elif data.intent == "group_query": data.is_group_query = True
        elif data.intent == "calendar": data.is_calendar = True
        return data
    except Exception:
        return UserIntent(intent="chat")

async def answer_with_group_context(user_query: str, group_context_text: str, user_id: int | None = None) -> str:
    """Генерирует ответ пользователю на основе сообщений из группы."""
    hist = user_contexts[user_id]
    prompt = f"""Ниже приведены реальные сообщения из топиков Telegram-группы:
---
{group_context_text}
---

Вопрос пользователя: {user_query}

Инструкция:
1. Ответь пользователю чётко и прямо по существу, используя факты из сообщений выше.
2. Укажи автора, дату или топик, если это полезно.
3. Если информации недостаточно или в этих сообщениях её нет, прямо и честно скажи об этом."""

    hist.append(types.Content(role="user", parts=[types.Part.from_text(text=prompt)]))
    cfg = types.GenerateContentConfig(
        system_instruction="Ты персональный ассистент Кристина. Ты помогаешь ориентироваться в сообщениях и топиках группы Telegram. Отвечай кратко, понятно и дружелюбно.",
        temperature=0.4
    )
    res = await client.aio.models.generate_content(model=MODEL, contents=list(hist), config=cfg)
    reply = (res.text or "").strip()
    hist.append(types.Content(role="model", parts=[types.Part.from_text(text=reply)]))
    return reply

parse_email_intent = parse_user_intent

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
    """Асинхронный диалог с моделью с изолированной историей (10 пар сообщений) на каждого пользователя."""
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

ask_gpt = ask_gemini

async def transcribe_voice(voice_bytes: bytes) -> str:
    """Транскрибирует голосовое сообщение через Gemini аудио-модель."""
    part = types.Part.from_bytes(data=voice_bytes, mime_type="audio/ogg")
    res = await client.aio.models.generate_content(
        model=MODEL,
        contents=[part, "Transcribe this audio verbatim in its original language. Output only the transcription, no comments."]
    )
    return (res.text or "").strip()

async def process_telegram_images(messages, user_prompt: str, user_id: int | None = None) -> str:
    """Обработка от 1 до 10 изображений одновременно через мультимодальный Gemini с сохранением в контекст."""
    msg_list = messages if isinstance(messages, list) else [messages]
    raw_images = await asyncio.gather(*(m.download_media(file=bytes) for m in msg_list[:10]))
    parts = [types.Part.from_bytes(data=b, mime_type="image/jpeg") for b in raw_images if b]
    if not parts:
        return "Не удалось загрузить изображения."
    prompt_txt = user_prompt or "Опиши и проанализируй эти изображения"
    parts.append(types.Part.from_text(text=prompt_txt))

    hist = user_contexts[user_id]
    cfg = types.GenerateContentConfig(
        system_instruction="Ты персональный ассистент Кристина. Проанализируй все переданные изображения и ответь на запрос пользователя.",
        temperature=0.7
    )
    res = await client.aio.models.generate_content(
        model=MODEL,
        contents=[*list(hist), types.Content(role="user", parts=parts)],
        config=cfg
    )
    reply = (res.text or "Ответ пуст.").strip()
    hist.append(types.Content(role="user", parts=[types.Part.from_text(text=f"[Фото: {len(parts)-1} шт.] {prompt_txt}")]))
    hist.append(types.Content(role="model", parts=[types.Part.from_text(text=reply)]))
    return reply

process_telegram_image = process_telegram_images

LATEX_SYSTEM_PROMPT = r"""You are an expert LaTeX typesetter. Generate a complete, well-structured, valid LaTeX document for xelatex based on the user request.
STRICT RULES:
1. Output ONLY raw LaTeX code starting with \documentclass[12pt,a4paper]{article} and ending with \end{document}. No markdown fences, no explanations.
2. Always use this exact preamble:
\documentclass[12pt,a4paper]{article}
\usepackage{fontspec}
\setmainfont{CMU Serif}
\setsansfont{CMU Sans Serif}
\setmonofont{CMU Typewriter Text}
\usepackage{amsmath,amssymb,amsfonts}
\usepackage{geometry}
\geometry{margin=2cm}
3. Do NOT use inputenc, fontenc, babel, external images (\includegraphics), tikz, or tcolorbox.
4. Make sure every \begin{...} matches its exact \end{...}.
5. Escape %, &, #, _ in plain text outside math mode ($...$).
6. Write the document content in the SAME language as the user's request (e.g. Russian if requested in Russian)."""

def _clean_latex(raw: str) -> str:
    tex = re.sub(r"^```(?:latex|tex)?\s*", "", raw.strip())
    tex = re.sub(r"\s*```$", "", tex).strip()
    if "\\documentclass" in tex:
        tex = tex[tex.index("\\documentclass"):]
    if "\\end{document}" in tex:
        tex = tex[:tex.rindex("\\end{document}") + len("\\end{document}")]
    return "\\nonstopmode\n" + tex

def _compile_xelatex_sync(tex_code: str) -> tuple[bool, bytes | str]:
    r = requests.post(
        "https://latex.ytotech.com/builds/sync",
        json={"compiler": "xelatex", "resources": [{"main": True, "content": tex_code}]},
        timeout=60
    )
    if r.status_code in (200, 201) and "application/pdf" in r.headers.get("Content-Type", ""):
        return True, r.content
    return False, r.text[:1500]

async def generate_pdf_from_prompt(prompt: str, user_id: int | None = None) -> bytes:
    """Генерирует LaTeX-код по запросу пользователя и компилирует его в готовый PDF-файл."""
    hist = list(user_contexts[user_id]) if user_id is not None else []
    cfg = types.GenerateContentConfig(system_instruction=LATEX_SYSTEM_PROMPT, temperature=0.3)
    contents = [*hist, types.Content(role="user", parts=[types.Part.from_text(text=f"Сгенерируй полный LaTeX документ по запросу: {prompt}")])]
    res = await client.aio.models.generate_content(model=MODEL, contents=contents, config=cfg)
    tex = _clean_latex(res.text or "")

    ok, result = await asyncio.to_thread(_compile_xelatex_sync, tex)
    if not ok:
        fix_prompt = f"Исправь ошибки компиляции XeLaTeX и верни ТОЛЬКО исправленный код LaTeX:\nОшибка:\n{result}\n\nКод:\n{tex}"
        res_fix = await client.aio.models.generate_content(model=MODEL, contents=fix_prompt, config=cfg)
        tex = _clean_latex(res_fix.text or "")
        ok, result = await asyncio.to_thread(_compile_xelatex_sync, tex)
        if not ok:
            raise RuntimeError(f"Ошибка компиляции LaTeX: {result[:300]}")

    if user_id is not None:
        user_contexts[user_id].append(types.Content(role="user", parts=[types.Part.from_text(text=prompt)]))
        user_contexts[user_id].append(types.Content(role="model", parts=[types.Part.from_text(text=f"[Сгенерирован PDF документ по теме: {prompt}]")]))
    return result

class VideoModerationResult(BaseModel):
    is_safe: bool = Field(
        description=(
            "False if the user request OR attached image involves ANY of: nudity, sexual/erotic/NSFW content, "
            "violence, murder, gore, blood, torture, weapons aimed at people, self-harm, suicide, cruelty to animals/humans, "
            "terrorism, extremism, hate symbols (swastika, Nazi, etc.), hate speech, bullying/humiliation/deepfakes of real people, "
            "illegal drugs, child exploitation/safety risks, or disgusting/shocking/gruesome themes. True ONLY if completely safe and benign."
        )
    )
    rejection_reason_ru: str = Field(
        default="",
        description="If is_safe is False, a short polite explanation in Russian why the request was blocked by the safety filter."
    )
    prompt_en: str = Field(
        default="",
        description="If is_safe is True, a concise, vivid English visual description (1-2 sentences) for the video generator."
    )

async def moderate_and_prepare_video_prompt(
    user_text: str,
    existing_en: str = "",
    image_bytes: bytes | None = None
) -> tuple[bool, str, str]:
    """
    Проверяет запрос и изображение (если есть) через ИИ-модератор безопасности.
    Возвращает (is_safe, prompt_en, rejection_reason_ru).
    """
    parts = []
    if image_bytes:
        parts.append(types.Part.from_bytes(data=image_bytes, mime_type="image/jpeg"))
    parts.append(
        types.Part.from_text(
            text=(
                f"User video generation request: \"{user_text}\"\n"
                f"Draft English prompt: \"{existing_en}\"\n\n"
                "Strictly evaluate safety. Reject ANY NSFW, sexual, violent, gory, horrific, cruel, hateful, extremist, "
                "self-harm, illegal, or humiliating content (in either text or attached image). "
                "If safe, provide a clean 1-2 sentence English visual prompt."
            )
        )
    )
    cfg = types.GenerateContentConfig(
        response_mime_type="application/json",
        response_schema=VideoModerationResult,
        temperature=0.1,
    )
    res = await client.aio.models.generate_content(model=MODEL, contents=parts, config=cfg)
    try:
        mod = VideoModerationResult.model_validate_json(res.text or "{}")
        if not mod.is_safe:
            reason = mod.rejection_reason_ru or "Запрос содержит недопустимый или небезопасный контент."
            return False, "", f"🛡️ **Запрос отклонён фильтром безопасности:** {reason}"
        final_en = (mod.prompt_en or existing_en or user_text).strip()
        return True, final_en, ""
    except Exception:
        # Если сама Gemini заблокировала ответ из-за встроенных safety-фильтров
        return (
            False,
            "",
            "🛡️ **Запрос отклонён фильтром безопасности:** генерация подобного контента запрещена.",
        )

async def prepare_video_prompt_en(user_text: str, existing_en: str = "") -> str:
    """Переводит и адаптирует запрос пользователя в короткий англоязычный визуальный промпт для генерации видео."""
    is_safe, prompt_en, reason = await moderate_and_prepare_video_prompt(user_text, existing_en)
    if not is_safe:
        raise ValueError(reason)
    return prompt_en

async def detect_photo_video_intent(prompt_text: str) -> tuple[bool, str]:
    """Проверяет, просит ли пользователь сгенерировать видео по присланному фото."""
    if not prompt_text:
        return False, ""
    low = prompt_text.lower()
    video_triggers = ("видео", "видос", "ролик", "video", "оживи", "оживить", "анимируй", "анимировать", "animate")
    if not any(t in low for t in video_triggers):
        return False, ""
    intent = await parse_user_intent(prompt_text)
    if intent.is_video or any(t in low for t in ("сгенерируй видео", "сделай видео", "создай видео", "оживи", "анимируй", "generate video", "make a video", "видео на")):
        return True, (intent.video_prompt_en or "")
    return False, ""

