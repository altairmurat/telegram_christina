"""
tg_formatter.py
---------------
Модуль для безопасной и красивой вёрстки сообщений в Telegram через HTML.
Поддерживает:
- <b>жирный</b>, <i>курсив</i>, <u>подчёркнутый</u>, <s>зачёркнутый</s>
- <code>код</code>, <pre><code class="language-python">блок кода</code></pre>
- <blockquote>цитата</blockquote>
- <a href="url">ссылки</a>
- <tg-spoiler>спойлеры</tg-spoiler>
- Автоматическую конвертацию Markdown (**bold**, *italic*, `code`, > quote, [link](url)) в валидный Telegram HTML
- Экранирование сырых символов (&, <, >), чтобы Telegram не падал с ошибками парсинга.
"""

import asyncio, re
from html import escape
from telethon.extensions import html as telethon_html

ALLOWED_TAGS = {
    "b", "strong", "i", "em", "u", "s", "del", "code", "pre", "blockquote", "tg-spoiler"
}

# Регулярка для поиска уже существующих валидных HTML-тегов Telegram
_TAG_TOKEN_RE = re.compile(
    r"(</?(?:b|strong|i|em|u|s|del|code|pre|blockquote|tg-spoiler)\b[^>]*>|<a\s+href=[\"'][^\"']*[\"'][^>]*>|</a\s*>)",
    re.IGNORECASE
)


def _convert_markdown_to_html(text: str) -> str:
    if not text:
        return ""

    # 1. Защищаем уже готовые блоки <pre>...</pre> и <code>...</code>
    protected_blocks = []
    def _save_block(m):
        idx = len(protected_blocks)
        protected_blocks.append(m.group(0))
        return f"___PROT_BLOCK_{idx}___"

    # Сначала сохраняем markdown code blocks: ```lang\ncode```
    def _replace_code_block(m):
        lang = m.group(1).strip()
        code_content = m.group(2)
        # Внутри кода экранируем < и >
        esc_code = code_content.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        if lang:
            tag = f'<pre><code class="language-{lang}">{esc_code}</code></pre>'
        else:
            tag = f'<pre><code>{esc_code}</code></pre>'
        idx = len(protected_blocks)
        protected_blocks.append(tag)
        return f"___PROT_BLOCK_{idx}___"

    s = re.sub(r"```([a-zA-Z0-9_-]*)\n?([\s\S]*?)```", _replace_code_block, text)

    # Сохраняем инлайн-код `code`
    def _replace_inline_code(m):
        content = m.group(1).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        idx = len(protected_blocks)
        protected_blocks.append(f"<code>{content}</code>")
        return f"___PROT_BLOCK_{idx}___"

    s = re.sub(r"`([^`\n]+)`", _replace_inline_code, s)

    # 2. Конвертируем заголовки Markdown (# Заголовок -> <b>Заголовок</b>)
    s = re.sub(r"^(#{1,6})\s+(.+)$", r"<b>\2</b>", s, flags=re.MULTILINE)

    # 3. Конвертируем жирный текст: **текст** или __текст__
    s = re.sub(r"\*\*([^*]+)\*\*", r"<b>\1</b>", s)
    s = re.sub(r"(?<![a-zA-Z0-9])__([^_]+)__(?![a-zA-Z0-9])", r"<b>\1</b>", s)

    # 4. Конвертируем курсив: *текст* или _текст_ (не затрагивая переменные вида user_id_val)
    s = re.sub(r"(?<!\*)\*([^*\n]+)\*(?!\*)", r"<i>\1</i>", s)
    s = re.sub(r"(?<=\s)_([^_]+)_(?=\s|[.,!?:;)]|$)", r"<i>\1</i>", s)

    # 5. Конвертируем зачёркнутый: ~~текст~~
    s = re.sub(r"~~([^~]+)~~", r"<s>\1</s>", s)

    # 6. Конвертируем ссылки [текст](url)
    s = re.sub(r"\[([^\]]+)\]\((https?://[^\s)]+)\)", r'<a href="\2">\1</a>', s)

    # 7. Конвертируем цитаты > текст в <blockquote>
    lines = []
    in_bq = False
    bq_buf = []
    for line in s.split("\n"):
        if line.startswith("&gt; ") or line.startswith("> "):
            in_bq = True
            bq_buf.append(line.split(" ", 1)[1])
        elif line.strip() in (">", "&gt;"):
            in_bq = True
            bq_buf.append("")
        else:
            if in_bq:
                lines.append(f"<blockquote>{chr(10).join(bq_buf)}</blockquote>")
                bq_buf = []
                in_bq = False
            lines.append(line)
    if in_bq:
        lines.append(f"<blockquote>{chr(10).join(bq_buf)}</blockquote>")
    s = "\n".join(lines)

    # 8. Восстанавливаем защищённые блоки кода
    for i, b in enumerate(protected_blocks):
        s = s.replace(f"___PROT_BLOCK_{i}___", b)

    return s


def format_tg_html(text: str) -> str:
    """
    Превращает текст (смесь Markdown и/или HTML) в строго валидный Telegram HTML:
    - Конвертирует markdown-разметку в HTML
    - Экранирует опасные сырые символы (&, <, >), если они не являются разрешёнными тегами
    - Проверяет валидность через парсер Telethon
    """
    if not text:
        return ""

    # Сначала конвертируем Markdown разметку
    converted = _convert_markdown_to_html(text)

    # Разбиваем текст на куски: [обычный текст, валидный тег, обычный текст, ...]
    parts = []
    last_idx = 0
    for match in _TAG_TOKEN_RE.finditer(converted):
        start, end = match.span()
        if start > last_idx:
            # Обычный текст — безопасно экранируем сырые <, > и &
            raw_chunk = converted[last_idx:start]
            esc_chunk = raw_chunk.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            parts.append(esc_chunk)
        # Сам тег оставляем как есть
        parts.append(match.group(0))
        last_idx = end

    if last_idx < len(converted):
        raw_chunk = converted[last_idx:]
        esc_chunk = raw_chunk.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        parts.append(esc_chunk)

    result = "".join(parts)

    # Проверяем парсером Telethon, что всё парсится без исключений
    try:
        telethon_html.parse(result)
        return result
    except Exception:
        # Если вдруг какой-то тег оказался незакрыт, экранируем всё целиком в безопасный текст
        return escape(text)


def split_tg_html(text: str, max_len: int = 4000) -> list[str]:
    """Разбивает длинный текст на части до max_len символов по строкам."""
    if not text or len(text) <= max_len:
        return [text] if text else [""]
    chunks = []
    lines = text.split("\n")
    cur = []
    cur_len = 0
    for l in lines:
        if cur_len + len(l) + 1 > max_len:
            if cur:
                chunks.append("\n".join(cur))
                cur = [l]
                cur_len = len(l)
            else:
                for i in range(0, len(l), max_len):
                    chunks.append(l[i:i + max_len])
                cur = []
                cur_len = 0
        else:
            cur.append(l)
            cur_len += len(l) + 1
    if cur:
        chunks.append("\n".join(cur))
    return chunks


async def safe_reply(event, text: str, **kwargs):
    """
    Безопасный ответ на сообщение с красивым Telegram HTML.
    Если текст длиннее 4000 символов, отправляет его аккуратными частями.
    Если парсинг HTML дал сбой, мягко откатывается на чистый текст без краша.
    """
    chunks = split_tg_html(text)
    last_msg = None
    for c in chunks:
        formatted = format_tg_html(c)
        try:
            last_msg = await event.reply(formatted, parse_mode="html", **kwargs)
        except Exception:
            clean = re.sub(r"<[^>]+>", "", formatted)
            last_msg = await event.reply(clean, parse_mode=None, **kwargs)
    return last_msg

def split_into_human_chunks(text: str, max_chars: int = 700, max_chunks: int = 4) -> list[str]:
    """
    Разбивает ответ ассистента на естественные реплики, как пишет живой человек:
    - Защищает блоки <pre>...</pre>, <code>...</code> и <blockquote>...</blockquote> от разрыва.
    - Делит текст по абзацам и смысловым блокам (вводная мысль, вариант 1, вариант 2, завершающий вопрос).
    - Не отрывает короткие заголовки (например <b>Вариант 1:</b>) от следующей за ними цитаты или текста.
    - Ограничивает количество сообщений до max_chunks во избежание спама.
    """
    text = (text or "").strip()
    if not text:
        return []

    # Защищаем парные HTML блоки: pre, blockquote
    protected = []
    def save_block(m):
        idx = len(protected)
        protected.append(m.group(0))
        return f"___HTML_PROT_{idx}___"

    pat = re.compile(r"(<pre\b[^>]*>[\s\S]*?</pre>|<blockquote\b[^>]*>[\s\S]*?</blockquote>)", re.IGNORECASE)
    masked = pat.sub(save_block, text)

    raw_paras = [p.strip() for p in masked.split("\n\n") if p.strip()]

    def restore_block(s):
        for idx, block in enumerate(protected):
            s = s.replace(f"___HTML_PROT_{idx}___", block)
        return s

    paras = [restore_block(p) for p in raw_paras]
    if len(paras) <= 1:
        return [text]

    chunks = []
    current_chunk = []
    current_len = 0

    for p in paras:
        p_len = len(p)
        has_heavy_block = any(tag in p for tag in ("<blockquote", "<pre", "```"))
        prev_has_heavy_block = bool(current_chunk and any(tag in current_chunk[-1] for tag in ("<blockquote", "<pre", "```")))
        is_short_lead_in = (p_len < 120 and (p.endswith(":</b>") or p.endswith(":") or p.startswith("<b>") or p.startswith("**")))

        if not current_chunk:
            current_chunk.append(p)
            current_len = p_len
            continue

        # Условие разбиения на отдельное сообщение:
        # - Накопился достаточный объём (> max_chars) и это не заголовок
        # - Либо в текущем абзаце тяжёлый блок (цитата/код), а в текущем уже есть вводная мысль
        # - Либо предыдущий абзац сам был тяжёлым блоком
        should_split = (
            (current_len + p_len > max_chars and not is_short_lead_in)
            or (has_heavy_block and current_len > 120 and not current_chunk[-1].endswith(":</b>") and not current_chunk[-1].endswith(":"))
            or prev_has_heavy_block
        )

        if should_split:
            chunks.append("\n\n".join(current_chunk))
            current_chunk = [p]
            current_len = p_len
        else:
            current_chunk.append(p)
            current_len += p_len + 2

    if current_chunk:
        chunks.append("\n\n".join(current_chunk))

    while len(chunks) > max_chunks:
        min_idx = 0
        min_sum = len(chunks[0]) + len(chunks[1])
        for k in range(1, len(chunks) - 1):
            s = len(chunks[k]) + len(chunks[k+1])
            if s < min_sum:
                min_sum = s
                min_idx = k
        chunks[min_idx] = chunks[min_idx] + "\n\n" + chunks[min_idx + 1]
        chunks.pop(min_idx + 1)

    return chunks


async def simulate_typing(client, chat_id: int, duration: float):
    """Показывает статус 'печатает...' в чате заданное количество секунд."""
    if not client or not chat_id:
        await asyncio.sleep(duration)
        return
    try:
        async with client.action(chat_id, "typing"):
            await asyncio.sleep(duration)
    except Exception:
        await asyncio.sleep(duration)


async def safe_reply_human_streamed(event, text: str, max_chunks: int = 4, **kwargs):
    """
    Отправляет ответ пользователю несколькими естественными репликами, как реальный человек:
    1. Первое сообщение отправляется реплаем на вопрос пользователя.
    2. Перед отправкой каждого последующего сообщения в чате отображается статус 'печатает...' (typing).
    3. Последующие сообщения отправляются как продолжение диалога (без дублирования плашки реплая в ЛС).
    """
    if not text or not text.strip():
        return None

    chunks = split_into_human_chunks(text, max_chunks=max_chunks)
    if not chunks:
        return await safe_reply(event, text, **kwargs)

    client = getattr(event, "client", None)
    chat_id = getattr(event, "chat_id", None)
    is_private = getattr(event, "is_private", True)

    # 1. Отправляем первый чанк ответом на сообщение
    last_msg = await safe_reply(event, chunks[0], **kwargs)

    # 2. Если чанков несколько — отправляем последующие с естественной задержкой и статусом "печатает..."
    for chunk in chunks[1:]:
        # Имитируем реальную скорость набора текста человеком (~20-30 символов в сек)
        typing_delay = min(max(len(chunk) * 0.016, 1.2), 3.2)
        await simulate_typing(client, chat_id, typing_delay)

        fmt = format_tg_html(chunk)
        try:
            if is_private:
                last_msg = await client.send_message(chat_id, fmt, parse_mode="html", **kwargs)
            else:
                reply_to = getattr(event, "id", None)
                last_msg = await client.send_message(chat_id, fmt, reply_to=reply_to, parse_mode="html", **kwargs)
        except Exception:
            clean = re.sub(r"<[^>]+>", "", fmt)
            if is_private:
                last_msg = await client.send_message(chat_id, clean, parse_mode=None, **kwargs)
            else:
                reply_to = getattr(event, "id", None)
                last_msg = await client.send_message(chat_id, clean, reply_to=reply_to, parse_mode=None, **kwargs)

    return last_msg

safe_reply_streamed = safe_reply_human_streamed


async def safe_send_message(client, entity, text: str, **kwargs):
    """Безопасная отправка сообщения с parse_mode='html'."""
    chunks = split_tg_html(text)
    last_msg = None
    for c in chunks:
        formatted = format_tg_html(c)
        try:
            last_msg = await client.send_message(entity, formatted, parse_mode="html", **kwargs)
        except Exception:
            clean = re.sub(r"<[^>]+>", "", formatted)
            last_msg = await client.send_message(entity, clean, parse_mode=None, **kwargs)
    return last_msg


async def safe_edit_message(msg, text: str, **kwargs):
    """Безопасное редактирование сообщения с parse_mode='html' и досылкой длинных хвостов."""
    chunks = split_tg_html(text)
    first_chunk = chunks[0] if chunks else ""
    formatted = format_tg_html(first_chunk)
    last_msg = msg
    try:
        last_msg = await msg.edit(formatted, parse_mode="html", **kwargs)
    except Exception:
        clean = re.sub(r"<[^>]+>", "", formatted)
        last_msg = await msg.edit(clean, parse_mode=None, **kwargs)

    for extra in chunks[1:]:
        extra_fmt = format_tg_html(extra)
        try:
            await msg.respond(extra_fmt, parse_mode="html")
        except Exception:
            extra_clean = re.sub(r"<[^>]+>", "", extra_fmt)
            await msg.respond(extra_clean, parse_mode=None)

    return last_msg
