import os
import asyncio
from datetime import datetime, timedelta, timezone
from googleapiclient.discovery import build
from database import SessionLocal
import models

DEFAULT_TIMEZONE = "Asia/Seoul"
DEFAULT_TZ_OFFSET = "+09:00"

def _ensure_rfc3339(dt_str: str, tz_offset: str = DEFAULT_TZ_OFFSET) -> str:
    """Обеспечивает формат даты RFC3339 с часовым поясом."""
    if not dt_str:
        return ""
    s = dt_str.strip()
    # Если уже есть таймзона (+, -, Z)
    if "+" in s[10:] or "-" in s[10:] or s.endswith("Z"):
        return s
    return f"{s}{tz_offset}"

def _parse_to_utc(dt_str: str) -> datetime | None:
    """Парсит строку даты в UTC datetime для точного планирования напоминания."""
    if not dt_str:
        return None
    s = dt_str.strip()
    try:
        # Убираем секунды и миллисекунды для гибкости, если есть
        # Заменяем +09:00 на +0900 для strptime если нужно, или используем fromisoformat
        dt = datetime.fromisoformat(s)
        return dt.astimezone(timezone.utc).replace(tzinfo=None)
    except Exception:
        try:
            # Fallback для YYYY-MM-DDTHH:MM:SS
            naive = datetime.strptime(s[:19], "%Y-%m-%dT%H:%M:%S")
            # По умолчанию считаем, что это Asia/Seoul (UTC+9)
            return naive - timedelta(hours=9)
        except Exception:
            return None

def build_calendar_service(creds):
    """Создает Google Calendar API сервис из учетных данных."""
    return build("calendar", "v3", credentials=creds)

def create_calendar_event(
    creds,
    summary: str,
    start_iso: str,
    end_iso: str = "",
    description: str = "",
    is_important: bool = False,
    reminder_minutes: list[int] | None = None,
    tz_name: str = DEFAULT_TIMEZONE
) -> dict:
    """Создает событие в Google Календаре пользователя с гибкими напоминаниями."""
    service = build_calendar_service(creds)
    
    start_rfc = _ensure_rfc3339(start_iso)
    if not end_iso:
        st_dt = _parse_to_utc(start_rfc)
        if st_dt:
            end_rfc = _ensure_rfc3339((st_dt + timedelta(hours=10)).strftime("%Y-%m-%dT%H:%M:%S"))
        else:
            end_rfc = start_rfc
    else:
        end_rfc = _ensure_rfc3339(end_iso)

    priority_label = "🔥 Высокий (Очень важно)" if is_important else "Обычный"
    full_desc = (description or "").strip()
    full_desc += f"\n\n---\nСоздано ассистентом Кристиной\nПриоритет: {priority_label}"

    body = {
        "summary": summary,
        "description": full_desc.strip(),
        "start": {
            "dateTime": start_rfc,
            "timeZone": tz_name,
        },
        "end": {
            "dateTime": end_rfc,
            "timeZone": tz_name,
        },
        "colorId": "11" if is_important else "1",  # 11 - Красный (Flamingo) для важных дел
        "reminders": {
            "useDefault": False,
            "overrides": []
        }
    }

    if is_important:
        body["reminders"]["overrides"] = [
            {"method": "popup", "minutes": 10},
            {"method": "popup", "minutes": 30},
            {"method": "popup", "minutes": 60},
            {"method": "email", "minutes": 60}
        ]
    else:
        mins = reminder_minutes if reminder_minutes else [15]
        body["reminders"]["overrides"] = [{"method": "popup", "minutes": m} for m in mins]

    event = service.events().insert(calendarId="primary", body=body).execute()
    return event

def list_calendar_events(
    creds,
    max_results: int = 10,
    time_min_iso: str = None,
    time_max_iso: str = None,
    tz_name: str = DEFAULT_TIMEZONE
) -> list[dict]:
    """Возвращает список ближайших событий из Google Календаря."""
    service = build_calendar_service(creds)
    now_utc = datetime.utcnow().isoformat() + "Z"
    t_min = _ensure_rfc3339(time_min_iso) if time_min_iso else now_utc

    kwargs = {
        "calendarId": "primary",
        "timeMin": t_min,
        "maxResults": max_results,
        "singleEvents": True,
        "orderBy": "startTime"
    }
    if time_max_iso:
        kwargs["timeMax"] = _ensure_rfc3339(time_max_iso)

    res = service.events().list(**kwargs).execute()
    items = res.get("items", [])
    
    events_list = []
    for it in items:
        st = it.get("start", {}).get("dateTime") or it.get("start", {}).get("date") or ""
        en = it.get("end", {}).get("dateTime") or it.get("end", {}).get("date") or ""
        color = it.get("colorId", "1")
        events_list.append({
            "id": it.get("id"),
            "summary": it.get("summary", "Без названия"),
            "start": st,
            "end": en,
            "link": it.get("htmlLink", ""),
            "is_important": (color == "11") or ("важн" in (it.get("description", "")).lower())
        })
    return events_list

def schedule_telegram_reminder(
    telegram_user_id: int,
    title: str,
    start_iso: str,
    is_important: bool,
    event_id: str = None,
    minutes_before: int = 15
):
    """Планирует отправку прямого напоминания в Telegram за N минут до события."""
    utc_start = _parse_to_utc(_ensure_rfc3339(start_iso))
    if not utc_start:
        return

    remind_at = utc_start - timedelta(minutes=minutes_before)
    # Если событие прямо сейчас или в ближайшие минуты — напоминаем через 10 секунд
    now_utc = datetime.utcnow()
    if remind_at < now_utc:
        remind_at = now_utc + timedelta(seconds=10)

    try:
        # Форматируем красивую дату для текста напоминания (по времени пользователя UTC+9)
        local_time = utc_start + timedelta(hours=9)
        time_str = local_time.strftime("%d.%m в %H:%M")

        with SessionLocal() as db:
            rem = models.CalendarReminder(
                telegram_user_id=telegram_user_id,
                event_id=event_id,
                title=title,
                event_time_str=time_str,
                remind_at=remind_at,
                is_important=is_important,
                is_sent=False
            )
            db.add(rem)
            db.commit()
    except Exception as e:
        print(f"Error scheduling Telegram reminder: {e}")

async def run_reminder_worker(client):
    """
    Фоновый воркер: каждые 30 секунд проверяет базу на наступление времени напоминаний
    и отправляет сообщения пользователям в Telegram.
    """
    while True:
        try:
            now_utc = datetime.utcnow()
            due_reminders = []
            with SessionLocal() as db:
                due_reminders = db.query(models.CalendarReminder).filter(
                    models.CalendarReminder.remind_at <= now_utc,
                    models.CalendarReminder.is_sent == False
                ).all()

                for rem in due_reminders:
                    priority_text = "⚡ **Приоритет: Очень важно!**" if rem.is_important else "📌 **План из календаря**"
                    msg_text = (
                        f"⏰ **НАПОМИНАНИЕ!**\n\n"
                        f"{priority_text}\n"
                        f"🎯 **Задача:** {rem.title}\n"
                        f"🕒 **Время:** {rem.event_time_str or 'Скоро'}\n\n"
                        f"Не забудь выполнить!"
                    )
                    try:
                        await client.send_message(rem.telegram_user_id, msg_text)
                    except Exception as ex:
                        print(f"Error sending reminder to {rem.telegram_user_id}: {ex}")
                    rem.is_sent = True
                
                if due_reminders:
                    db.commit()
        except Exception as e:
            print(f"Reminder worker error: {e}")

        await asyncio.sleep(30)
