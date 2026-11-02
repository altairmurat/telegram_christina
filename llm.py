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
# Хранилище последних объявлений портала DGIST (полный текст + ссылки) для каждого пользователя
portal_context_store: dict[int, str] = {}

def format_portal_posts_for_context(posts: list[dict]) -> str:
    if not posts:
        return ""
    blocks = []
    for idx, p in enumerate(posts[:20], 1):
        links_list = p.get("links") or []
        links_str = "\n".join(
            f"   - {lk.get('label', 'Ссылка')}: {lk.get('url', '')}"
            for lk in links_list if isinstance(lk, dict) and lk.get("url")
        )
        post_url = p.get("post_url") or ""
        full_txt = (p.get("full_text") or p.get("summary") or "")[:2500]
        block = (
            f"Объявление #{idx}: {p.get('title', 'Без названия')}\n"
            f"Раздел: {p.get('category', 'DGIST')} | Дата: {p.get('date', '')}\n"
            f"Краткая суть: {p.get('summary', '')}\n"
        )
        if post_url:
            block += f"Прямая ссылка на пост на портале: {post_url}\n"
        if links_str:
            block += f"Извлечённые ссылки (регистрация / формы / сайты):\n{links_str}\n"
        if full_txt:
            block += f"Полный текст объявления:\n{full_txt}\n"
        blocks.append(block)
    return "\n---\n".join(blocks)

def store_portal_context(user_id: int | None, posts: list[dict]) -> None:
    """Сохраняет вытащенные объявления с портала (включая полный текст и ссылки на регистрацию) в контекст пользователя."""
    if not posts or not user_id:
        return
    ctx_text = format_portal_posts_for_context(posts)
    if not ctx_text:
        return
    uid = int(user_id)
    portal_context_store[uid] = ctx_text
    hist = user_contexts[uid]
    summary_lines = [
        f"#{i}. [{p.get('category', 'DGIST')}] {p.get('title', '')} — {p.get('summary', '')}"
        for i, p in enumerate(posts[:15], 1)
    ]
    hist.append(
        types.Content(
            role="model",
            parts=[
                types.Part.from_text(
                    text=(
                        "[Выгружены объявления с портала DGIST в журнал и сохранены в память со всеми ссылками и полным текстом:]\n"
                        + "\n".join(summary_lines)
                    )
                )
            ],
        )
    )

def _get_portal_context_for_user(user_id: int | None) -> str:
    if not user_id:
        return ""
    uid = int(user_id)
    ctx = portal_context_store.get(uid)
    if ctx:
        return ctx
    try:
        from dgist_accounts import load_dgist_seen_ids, get_dgist_creds
        if not get_dgist_creds(uid):
            return ""
        seen = load_dgist_seen_ids(uid)
        recent = seen.get("__recent_posts__", []) if isinstance(seen, dict) else []
        if recent:
            ctx = format_portal_posts_for_context(recent)
            portal_context_store[uid] = ctx
            return ctx
    except Exception:
        pass
    return ""


everytime_context_store: dict[int, str] = {}


def format_everytime_posts_for_context(posts: list[dict]) -> str:
    if not posts:
        return ""
    blocks = []
    for idx, p in enumerate(posts[:18], 1):
        kws = ", ".join(p.get("matched_keywords") or [])
        comms = "\n".join(f"   - {c}" for c in (p.get("comments_ru") or [])[:10])
        block = (
            f"[Everytime Пост #{idx}] (ID: {p.get('id', '')})\n"
            f"Ссылка: {p.get('url', '')}\n"
            f"Дата: {p.get('created_at', '')} | Лайки: {p.get('likes', 0)} | Ключевые слова: {kws}\n"
            f"Заголовок (RU): {p.get('title_ru', '')}\n"
            f"Текст (RU дословно): {p.get('text_ru', '')}\n"
            f"Оригинал (KO): {p.get('title_ko', '')} — {p.get('text_ko', '')}\n"
        )
        if comms:
            block += f"Комментарии (RU):\n{comms}\n"
        blocks.append(block)
    return "\n---\n".join(blocks)


def store_everytime_context(user_id: int | None, posts: list[dict]):
    if not posts or not user_id:
        return
    formatted = format_everytime_posts_for_context(posts)
    uid = int(user_id)
    everytime_context_store[uid] = formatted


def _get_everytime_context_for_user(user_id: int | None) -> str:
    if not user_id:
        return ""
    uid = int(user_id)
    try:
        from everytime_monitor import load_everytime_cookie, load_everytime_seen_data
        # Если у пользователя нет собственной подключённой куки Everytime — контекст недоступен
        if not load_everytime_cookie(uid):
            return ""
        ctx = everytime_context_store.get(uid)
        if ctx:
            return ctx
        seen = load_everytime_seen_data(uid)
        last_posts = (seen or {}).get("last_magazine_posts") or []
        if last_posts:
            ctx = format_everytime_posts_for_context(last_posts)
            everytime_context_store[uid] = ctx
            return ctx
    except Exception:
        pass
    return ""


class UserIntent(BaseModel):
    intent: str = Field(
        default="chat",
        description=(
            "'video' ONLY if user explicitly asks to generate/create/make a video or animate a photo; "
            "'calendar' if user asks to add/create/schedule a plan, event, or reminder in Google Calendar OR asks to view/check upcoming plans/calendar; "
            "'email' ONLY if user explicitly asks to write/send/compose an email to someone; "
            "'pdf' ONLY if user explicitly asks to generate/create/make a PDF file or document; "
            "'portal' ONLY if user explicitly asks to run a check/scan of the official DGIST portal right now (e.g. 'чекни портал', 'проверь портал'); "
            "'everytime' ONLY if user explicitly asks to check/parse/scan Everytime / 에브리타임 / еверитайм (e.g. 'чекни еверитайм', 'что пишут на everytime', 'парсни еверитайм'); "
            "do NOT use 'portal' or 'everytime' if user asks follow-up questions about an already shown post, asks for a link, or asks 'what does this mean' (use 'chat' for those!); "
            "'group_query' if user asks to check, find, summarize, or search messages, topics, announcements, or discussions in the Telegram group/topics/chat; "
            "'chat' for everything else (including questions about previously shown photos, DGIST notices, or Everytime posts)"
        )
    )
    is_video: bool = Field(default=False, description="True if intent == 'video' or user asks to generate/make a video or animate an image")
    video_prompt_en: str = Field(default="", description="Detailed visual prompt in English for generating the video (translate user's video idea into vivid English visual description)")
    is_email: bool = Field(default=False, description="True if intent == 'email'")
    is_pdf: bool = Field(default=False, description="True if intent == 'pdf'")
    is_portal: bool = Field(default=False, description="True if intent == 'portal'")
    is_everytime: bool = Field(default=False, description="True if intent == 'everytime'")
    everytime_query: str = Field(default="", description="Optional specific search term if user asked to search Everytime for a specific topic, else empty for default keyword scan")
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
        elif data.intent == "everytime": data.is_everytime = True
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

async def process_pdf_document(
    pdf_bytes: bytes,
    user_prompt: str,
    filename: str = "document.pdf",
    user_id: int | None = None
) -> str:
    """Обрабатывает PDF-документ через Gemini с сохранением документа и диалога в контекст пользователя."""
    if not pdf_bytes:
        return "Не удалось загрузить PDF-файл (файл пуст)."
    if len(pdf_bytes) > 20 * 1024 * 1024:
        return "Файл слишком большой (> 20 МБ). Пожалуйста, отправь файл меньшего размера."

    pdf_part = types.Part.from_bytes(data=pdf_bytes, mime_type="application/pdf")
    prompt_txt = user_prompt or "Изучи этот PDF-документ и сделай краткое резюме его содержания."
    text_part = types.Part.from_text(text=f"[Прикреплённый PDF-документ: {filename}]\nЗапрос пользователя: {prompt_txt}")

    hist = user_contexts[user_id]
    user_turn = types.Content(role="user", parts=[pdf_part, text_part])
    cfg = types.GenerateContentConfig(
        system_instruction=(
            "Ты персональный ассистент Кристина. Внимательно изучи прикреплённый PDF-документ и ответь на запрос пользователя "
            "подробно, структурированно, точно и понятно на русском языке (или на языке запроса пользователя). "
            "Ты помнишь содержание этого документа и сможешь отвечать на последующие уточняющие вопросы пользователя по нему."
        ),
        temperature=0.4
    )
    res = await client.aio.models.generate_content(
        model=MODEL,
        contents=[*list(hist), user_turn],
        config=cfg
    )
    reply = (res.text or "Ответ пуст.").strip()
    # Сохраняем сам PDF, запрос пользователя и ответ модели в контекст пользователя
    hist.append(user_turn)
    hist.append(types.Content(role="model", parts=[types.Part.from_text(text=reply)]))
    return reply

async def ask_gemini(chat_text: str, user_id: int | None = None) -> str:
    """Асинхронный диалог с моделью с изолированной историей (10 пар сообщений), фото-памятью, контекстом PDF и портала DGIST."""
    from datetime import datetime
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S+09:00 (%A)")
    hist = user_contexts[user_id]
    hist.append(types.Content(role="user", parts=[types.Part.from_text(text=chat_text)]))

    portal_ctx = await asyncio.to_thread(_get_portal_context_for_user, user_id)
    et_ctx = await asyncio.to_thread(_get_everytime_context_for_user, user_id)
    sys_inst = (
        f"Current time and date: {now_str} (Asia/Seoul timezone, UTC+9).\n"
        "Ты персональный ассистент Кристина. Отвечай кратко, понятно и дружелюбно на языке пользователя. "
        "Ты помнишь текущие дату и время, все отправленные пользователем фотографии, прикреплённые PDF-документы и предыдущие сообщения в диалоге, "
        "поэтому ты можешь понимать, когда пользователь обращается к тебе спустя некоторое время."
    )
    if portal_ctx:
        sys_inst += (
            "\n\nВ твоей памяти также хранятся последние объявления с портала DGIST (включая их полные тексты и ссылки на регистрацию / формы). "
            "Если пользователь спрашивает про какую-то новость, просит скинуть ссылку на регистрацию/подачу заявки или спрашивает «а что это значит?», "
            "используй данные ниже, дай точную ссылку и подробно объясни суть:\n---\n"
            + portal_ctx
        )
    if et_ctx:
        sys_inst += (
            "\n\nВ твоей памяти также хранятся спарсенные посты и комментарии со студенческого форума DGIST Everytime (에브리타임) с их дословным переводом и оригиналом. "
            "Если пользователь спрашивает про какой-то пост с Everytime, просит перевести комментарии или рассказать подробности:\n---\n"
            + et_ctx
        )

    cfg = types.GenerateContentConfig(
        system_instruction=sys_inst,
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
    """Обработка от 1 до 10 изображений одновременно через мультимодальный Gemini с полным сохранением самих фото и текста в контекст."""
    msg_list = messages if isinstance(messages, list) else [messages]
    raw_images = await asyncio.gather(*(m.download_media(file=bytes) for m in msg_list[:10]))
    parts = [types.Part.from_bytes(data=b, mime_type="image/jpeg") for b in raw_images if b]
    if not parts:
        return "Не удалось загрузить изображения."
    prompt_txt = user_prompt or "Опиши и проанализируй эти изображения"
    parts.append(types.Part.from_text(text=prompt_txt))

    hist = user_contexts[user_id]
    user_turn = types.Content(role="user", parts=parts)
    cfg = types.GenerateContentConfig(
        system_instruction="Ты персональный ассистент Кристина. Проанализируй все переданные изображения и ответь на запрос пользователя.",
        temperature=0.7
    )
    res = await client.aio.models.generate_content(
        model=MODEL,
        contents=[*list(hist), user_turn],
        config=cfg
    )
    reply = (res.text or "Ответ пуст.").strip()
    # Сохраняем сами изображения + вопрос пользователя + ответ модели в контекст, чтобы помнить фото в следующих сообщениях!
    hist.append(user_turn)
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
        default=True,
        description=(
            "Set to False ONLY for extreme/illegal content: (1) explicit pornography, nudity, or sexual acts (18+ NSFW), "
            "(2) graphic gore, dismemberment, torture, or murder of humans, (3) child sexual abuse/exploitation (CSAM), "
            "(4) suicide or self-harm, (5) terrorism/ISIS/Nazi propaganda or mass shootings. "
            "Set to True for EVERYTHING ELSE, including wild animals chasing/running, action scenes, humor, memes, scary/thriller scenes, etc."
        )
    )
    rejection_reason_ru: str = Field(
        default="",
        description="If is_safe is False, a short explanation in Russian why the request was blocked."
    )
    prompt_en: str = Field(
        default="",
        description="If is_safe is True, a concise, vivid English visual description (1-2 sentences) for the video generator, incorporating details from the attached image if present."
    )

async def moderate_and_prepare_video_prompt(
    user_text: str,
    existing_en: str = "",
    image_bytes: bytes | None = None
) -> tuple[bool, str, str]:
    """
    Проверяет запрос и изображение (если есть) только на жёсткий запрещённый контент (18+, расчленёнка, терроризм, суицид, CSAM)
    и формирует английский визуальный промпт с учётом фото.
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
                "Rules:\n"
                "1. Be permissive! ALLOW (is_safe=true) all normal, funny, creative, action, thriller, wild animals (e.g. a boar running toward the camera), memes, and cinematic scenes.\n"
                "2. ONLY reject (is_safe=false) if the request or image contains extreme violations: explicit porn/nudity (18+ NSFW), graphic gore/murder/torture, child abuse (CSAM), suicide/self-harm, or terrorism/Nazi propaganda.\n"
                "3. If an image is attached and the user refers to objects/animals/people in it (like 'это животное'), identify what is shown in the image and write a clear 1-2 sentence English visual prompt for the video model."
            )
        )
    )
    cfg = types.GenerateContentConfig(
        response_mime_type="application/json",
        response_schema=VideoModerationResult,
        temperature=0.2,
    )
    res = await client.aio.models.generate_content(model=MODEL, contents=parts, config=cfg)
    try:
        mod = VideoModerationResult.model_validate_json(res.text or "{}")
        if not mod.is_safe:
            reason = mod.rejection_reason_ru or "Запрос содержит строго запрещённый контент (18+ / жесть)."
            return False, "", f"🛡️ **Запрос отклонён фильтром безопасности:** {reason}"
        final_en = (mod.prompt_en or existing_en or user_text).strip()
        return True, final_en, ""
    except Exception:
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

