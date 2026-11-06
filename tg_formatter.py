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

import re
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
