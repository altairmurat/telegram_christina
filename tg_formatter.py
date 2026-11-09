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

_AMP_ESCAPE_RE = re.compile(r"&(?!amp;|lt;|gt;|quot;|apos;|#\d+;|#x[0-9a-fA-F]+;)")

def _escape_href_amps(match):
    quote = match.group(1)
    val = match.group(2)
    val_fixed = _AMP_ESCAPE_RE.sub("&amp;", val)
    return "href=" + quote + val_fixed + quote


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

    # 6. Конвертируем ссылки [текст](url) с обязательным экранированием & в &amp; в href
    def _replace_md_link(m):
        link_text = m.group(1)
        raw_url = m.group(2)
        esc_url = _AMP_ESCAPE_RE.sub("&amp;", raw_url)
        return f'<a href="{esc_url}">{link_text}</a>'

    s = re.sub(r"\[([^\]]+)\]\((https?://[^\s)]+)\)", _replace_md_link, s)

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
        # Сам тег: если это <a href="...">, экранируем сырые & внутри href в &amp;,
        # чтобы парсер Telethon/HTMLParser не превращал &curren в знак валюты ¤
        tag_str = match.group(0)
        if tag_str.lower().startswith("<a "):
            tag_str = re.sub(r'href=(["\'])(.*?)\1', _escape_href_amps, tag_str, flags=re.I)
        parts.append(tag_str)
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

def split_into_human_chunks(text: str, max_chunks: int = 5) -> list[str]:
    """
    Разбивает ответ ассистента на естественные реплики, как пишет живой человек:
    - Защищает блоки <pre>...</pre>, <code>...</code> и <blockquote>...</blockquote> от разрыва.
    - Делит текст по абзацам (\n\n) — каждый отдельный абзац/мысль становится отдельным сообщением.
    - Длинные абзацы без кода (> 380 символов) аккуратно делит по границам предложений.
    - Не отрывает короткие заголовки/лид-ины (например <b>Вариант 1:</b>, «Вот пример:») от следующего за ними контента.
    - Ограничивает суммарное число реплик до max_chunks (по умолчанию 5) во избежание спама, склеивая самые короткие соседние реплики.
    """
    text = (text or "").strip()
    if not text:
        return []

    # 1. Защищаем парные HTML блоки: pre, blockquote, а также ```markdown```
    protected = []
    def save_block(m):
        idx = len(protected)
        protected.append(m.group(0))
        return f"___HTML_PROT_{idx}___"

    pat = re.compile(r"(<pre\b[^>]*>[\s\S]*?</pre>|<blockquote\b[^>]*>[\s\S]*?</blockquote>|```[\s\S]*?```)", re.IGNORECASE)
    masked = pat.sub(save_block, text)

    raw_paras = [p.strip() for p in masked.split("\n\n") if p.strip()]
    if not raw_paras:
        return [text]

    # 2. Если отдельный абзац слишком длинный (> 380 символов), делим его по границам предложений
    expanded_paras = []
    for p in raw_paras:
        if len(p) > 380 and "___HTML_PROT_" not in p and not re.search(r"^\s*[-*•\d]\.?\s", p, re.M):
            sentences = re.split(r"(?<=[.!?])\s+(?=[А-ЯЁA-Z])", p)
            if len(sentences) > 1:
                cur_buf = []
                cur_len = 0
                for s in sentences:
                    if cur_len + len(s) > 280 and cur_buf:
                        expanded_paras.append(" ".join(cur_buf))
                        cur_buf = [s]
                        cur_len = len(s)
                    else:
                        cur_buf.append(s)
                        cur_len += len(s) + 1
                if cur_buf:
                    expanded_paras.append(" ".join(cur_buf))
                continue
        expanded_paras.append(p)

    def is_lead_in(p_str: str) -> bool:
        clean = re.sub(r"<[^>]+>", "", p_str).strip()
        # Заголовок или короткая вводка до 90 символов
        if len(clean) <= 90:
            if clean.endswith(":"):
                return True
            if re.match(r"^(вариант\s+\d+|шаг\s+\d+|способ\s+\d+|пример\s*\d*|\d+[\.\)])", clean, re.I):
                return True
            if (p_str.startswith("<b>") and p_str.endswith("</b>")) or (p_str.startswith("**") and p_str.endswith("**")):
                return True
        return False

    # 3. Объединяем короткие заголовки с их следующим содержимым
    grouped = []
    i = 0
    while i < len(expanded_paras):
        cur = expanded_paras[i]
        if is_lead_in(cur) and i + 1 < len(expanded_paras):
            grouped.append(cur + "\n" + expanded_paras[i+1])
            i += 2
        else:
            grouped.append(cur)
            i += 1

    # 4. Если сообщений больше max_chunks — склеиваем наименьшие соседние части
    while len(grouped) > max_chunks:
        best_idx = 0
        best_len = len(grouped[0]) + len(grouped[1])
        for k in range(1, len(grouped) - 1):
            combo = len(grouped[k]) + len(grouped[k+1])
            if combo < best_len:
                best_len = combo
                best_idx = k
        grouped[best_idx] = grouped[best_idx] + "\n\n" + grouped[best_idx + 1]
        grouped.pop(best_idx + 1)

    # 5. Восстанавливаем защищённые блоки
    def restore_block(s):
        for idx, block in enumerate(protected):
            s = s.replace(f"___HTML_PROT_{idx}___", block)
        return s

    return [restore_block(c) for c in grouped]


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


async def safe_reply_human_streamed(event, text: str, max_chunks: int = 5, **kwargs):
    """
    Отправляет ответ пользователю несколькими естественными репликами, как реальный человек:
    1. Первое сообщение отправляется реплаем на вопрос пользователя.
    2. Перед отправкой каждого последующего сообщения в чате отображается статус 'печатает...' (typing).
    3. В ЛС последующие сообщения идут без повторения цитаты, а в группах/топиках сохраняется reply_to для маршрутизации.
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
        # Имитируем реальную скорость набора текста человеком (~25-35 символов в сек, от 1.0 до 2.6 сек)
        typing_delay = min(max(len(chunk) * 0.012, 1.0), 2.6)
        await simulate_typing(client, chat_id, typing_delay)

        fmt = format_tg_html(chunk)
        if is_private:
            try:
                last_msg = await client.send_message(chat_id, fmt, parse_mode="html", **kwargs)
            except Exception:
                clean = re.sub(r"<[^>]+>", "", fmt)
                last_msg = await client.send_message(chat_id, clean, parse_mode=None, **kwargs)
        else:
            last_msg = await safe_reply(event, chunk, **kwargs)

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
