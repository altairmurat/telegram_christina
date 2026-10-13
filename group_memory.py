import asyncio
import os
import json
from datetime import datetime
from sqlalchemy import func
from database import SessionLocal
import models

GROUP_TARGET_FILE = os.path.join("user_data", "group_target.json")
_topic_title_cache: dict[tuple[int, int], str] = {}

def get_topic_id(event) -> int | None:
    """Извлекает ID топика (thread_id) из события Telethon."""
    r = getattr(event.message, "reply_to", None)
    if r and getattr(r, "forum_topic", False):
        return getattr(r, "reply_to_top_id", None) or getattr(r, "reply_to_msg_id", None)
    return None

def save_or_update_topic(chat_id: int, topic_id: int, title: str):
    """Сохраняет или обновляет название топика в БД и кэше."""
    if not title or not title.strip():
        return
    title = title.strip()
    _topic_title_cache[(chat_id, topic_id)] = title
    with SessionLocal() as db:
        top = db.query(models.GroupTopic).filter_by(chat_id=chat_id, topic_id=topic_id).first()
        if not top:
            top = models.GroupTopic(chat_id=chat_id, topic_id=topic_id, topic_title=title)
            db.add(top)
        else:
            top.topic_title = title
            top.updated_at = datetime.utcnow()
        db.commit()

async def resolve_topic_title(client, chat_id: int, topic_id: int | None) -> str:
    """Определяет понятное название топика (через кэш, БД или запрос к первому сообщению)."""
    if topic_id is None:
        return "General"
    
    cached = _topic_title_cache.get((chat_id, topic_id))
    if cached:
        return cached

    with SessionLocal() as db:
        top = db.query(models.GroupTopic).filter_by(chat_id=chat_id, topic_id=topic_id).first()
        if top and top.topic_title:
            _topic_title_cache[(chat_id, topic_id)] = top.topic_title
            return top.topic_title

    # Пытаемся получить service message о создании топика
    try:
        top_msg = await client.get_messages(chat_id, ids=topic_id)
        action = getattr(top_msg, "action", None)
        if action and hasattr(action, "title") and action.title:
            t = action.title.strip()
            save_or_update_topic(chat_id, topic_id, t)
            return t
    except Exception:
        pass

    fallback = f"Topic #{topic_id}"
    _topic_title_cache[(chat_id, topic_id)] = fallback
    return fallback

async def record_group_message(client, event):
    """
    Автоматически сохраняет и индексирует входящее сообщение из группы/топика в базу данных.
    Работает для любых сообщений в группе (когда бот админ или отключен Privacy Mode).
    """
    if event.is_private:
        return

    chat_id = event.chat_id
    topic_id = get_topic_id(event)

    # Проверяем служебные действия создания/переименования топиков
    action = getattr(event.message, "action", None)
    if action and hasattr(action, "title") and action.title:
        tid = topic_id or event.message.id
        save_or_update_topic(chat_id, tid, action.title)
        return

    text = (event.raw_text or "").strip()
    if not text and event.message.media:
        if hasattr(event.message, "file") and getattr(event.message.file, "name", None):
            text = f"[Файл: {event.message.file.name}]"
        elif getattr(event.message, "photo", None):
            text = "[Фотография]"
        elif getattr(event.message, "voice", None):
            text = "[Голосовое сообщение]"
        elif getattr(event.message, "document", None):
            text = "[Документ]"

    if not text:
        return

    topic_title = await resolve_topic_title(client, chat_id, topic_id)

    # Получаем имя отправителя
    s_name = "Участник"
    try:
        sender = await event.get_sender()
        if sender:
            s_name = getattr(sender, "first_name", "") or getattr(sender, "title", "") or getattr(sender, "username", "") or "Участник"
            if getattr(sender, "last_name", None):
                s_name = f"{s_name} {sender.last_name}".strip()
    except Exception:
        pass

    try:
        with SessionLocal() as db:
            existing = db.query(models.GroupMessage).filter_by(
                chat_id=chat_id, message_id=event.message.id
            ).first()
            if existing:
                existing.text = text
                existing.topic_title = topic_title
                existing.topic_id = topic_id
            else:
                msg = models.GroupMessage(
                    message_id=event.message.id,
                    chat_id=chat_id,
                    topic_id=topic_id,
                    topic_title=topic_title,
                    sender_id=event.sender_id,
                    sender_name=s_name,
                    text=text,
                    created_at=datetime.utcnow()
                )
                db.add(msg)
            db.commit()
    except Exception as e:
        print(f"Error recording group message: {e}")

def get_default_chat_id() -> int | None:
    """Возвращает ID последней известной группы."""
    if os.path.exists(GROUP_TARGET_FILE):
        try:
            with open(GROUP_TARGET_FILE, "r", encoding="utf-8") as f:
                d = json.load(f)
                if d.get("chat_id"):
                    return d["chat_id"]
        except Exception:
            pass
    with SessionLocal() as db:
        latest = db.query(models.GroupMessage).order_by(models.GroupMessage.created_at.desc()).first()
        if latest:
            return latest.chat_id
    return None

def list_known_topics(chat_id: int | None = None) -> list[dict]:
    """Возвращает список всех известных топиков группы с количеством сохранённых сообщений."""
    cid = chat_id or get_default_chat_id()
    with SessionLocal() as db:
        q = db.query(
            models.GroupMessage.topic_id,
            models.GroupMessage.topic_title,
            func.count(models.GroupMessage.id)
        )
        if cid:
            q = q.filter(models.GroupMessage.chat_id == cid)
        q = q.group_by(models.GroupMessage.topic_id, models.GroupMessage.topic_title)
        rows = q.all()

        results = []
        for tid, title, cnt in rows:
            results.append({
                "topic_id": tid,
                "topic_title": title or ("General" if tid is None else f"Topic #{tid}"),
                "message_count": cnt
            })
        return results

def search_group_messages(
    query: str | None = None,
    topic_name: str | None = None,
    chat_id: int | None = None,
    limit: int = 30
) -> list[dict]:
    """Ищет сообщения в БД по названию топика и/или ключевым словам."""
    cid = chat_id or get_default_chat_id()
    with SessionLocal() as db:
        q = db.query(models.GroupMessage)
        if cid:
            q = q.filter(models.GroupMessage.chat_id == cid)
        if topic_name and topic_name.strip():
            tn = topic_name.strip()
            q = q.filter(models.GroupMessage.topic_title.ilike(f"%{tn}%"))
        if query and query.strip():
            words = [w for w in query.strip().split() if len(w) > 2]
            for w in words[:3]:
                q = q.filter(models.GroupMessage.text.ilike(f"%{w}%"))
        
        q = q.order_by(models.GroupMessage.created_at.desc()).limit(limit)
        rows = q.all()
        return [
            {
                "id": m.message_id,
                "topic": m.topic_title or "General",
                "sender": m.sender_name or "Участник",
                "text": m.text,
                "date": m.created_at.strftime("%Y-%m-%d %H:%M") if m.created_at else ""
            }
            for m in reversed(rows)
        ]

def format_messages_for_prompt(messages: list[dict]) -> str:
    """Форматирует список сообщений в компактный текст для Gemini."""
    if not messages:
        return "Нет сообщений."
    lines = []
    for m in messages:
        dt = m.get("date") or ""
        top = m.get("topic") or "General"
        snd = m.get("sender") or "Участник"
        txt = (m.get("text") or "").strip()
        lines.append(f"[{dt}] [Топик: {top}] {snd}: {txt}")
    return "\n".join(lines)
