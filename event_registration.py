"""
event_registration.py
---------------------
Автоматизация подачи заявок на лекции/семинары DGIST с бесплатным ужином (и любые Google Forms):
1) Поиск ссылки на регистрацию в посте (Google Forms и др.) и парсинг всех полей формы.
2) Автозаполнение данных пользователя из сохранённого профиля UserProfile; если данных не хватает —
   бот спрашивает у пользователя, ждёт ответ (текстом или голосом) и запоминает личные данные на будущее.
3) Отправка пользователю предзаполненной ссылки (usp=pp_url), краткого описания, даты/времени,
   места и списка всех заполненных полей на проверку.
4) Ожидание решения пользователя (кнопками или текстом/голосом: одобрить / изменить / не записываться).
5) Отправка формы (HTTP formResponse + верификация + Playwright fallback) или отмена операции.
6) Уведомление после завершения: «Записал вас на ___ — дата и время, место».
"""

import asyncio
import datetime
import html
import json
import os
import re
import secrets
from typing import Any

from pydantic import BaseModel, Field
from google import genai
from google.genai import types
from telethon import events, Button

from database import SessionLocal, engine
import models
from env import GEMINI_API_KEY, OWNER_ID
from google_forms_auto import (
    parse_google_form,
    build_prefilled_google_form_url,
    submit_google_form,
    extract_registration_urls_from_post,
    is_google_form_url,
)

GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.5-flash-lite")
_genai_client = genai.Client(api_key=GEMINI_API_KEY) if GEMINI_API_KEY else None

PROFILE_LABELS_RU = {
    "full_name_en": "Имя и фамилия (EN)",
    "full_name_ko": "Имя (KO / 한글)",
    "student_id": "Номер студбилета (학번)",
    "phone": "Телефон (연락처)",
    "email": "Email (основной)",
    "dgist_email": "Email DGIST (@dgist.ac.kr)",
    "affiliation_dept": "Факультет / кафедра (소속/학과)",
    "role_status": "Статус (학부생/대학원생/학생)",
    "grade_year": "Курс / год обучения (학년)",
    "dietary_restrictions": "Питание / аллергии (식이 제한)",
    "nationality": "Гражданство / страна",
    "gender": "Пол",
    "dorm_room": "Общежитие / комната",
    "lab_advisor": "Лаборатория / научрук",
}

# ─────────────────────────────────────────────────────────────────────────────
# Pydantic-схемы для структурированного вывода Gemini (без произвольных dict)
# ─────────────────────────────────────────────────────────────────────────────

class ProfileKeyValue(BaseModel):
    key: str = Field(
        description=(
            "Standard profile key: full_name_en, full_name_ko, student_id, phone, email, dgist_email, "
            "affiliation_dept, role_status, grade_year, dietary_restrictions, nationality, gender, dorm_room, lab_advisor, "
            "or a short snake_case key for another reusable personal fact."
        )
    )
    value: str = Field(description="Clean value to save in the user's persistent personal profile")


class EventPostAnalysis(BaseModel):
    is_free_dinner_event: bool = Field(
        default=False,
        description=(
            "True ONLY if this notice is a lecture/seminar/talk/workshop/conference/event that provides "
            "FREE DINNER / evening meal (dinner, free dinner, supper, 저녁, 저녁 식사, 저녁 도시락, 석식, "
            "happy hour with dinner, or evening event >= 16:30 offering free meal/pizza/food). "
            "False for cafeteria weekly menus, lunch-only events, scholarships, or notices without free dinner."
        ),
    )
    event_title: str = Field(default="", description="Clean concise event/lecture title")
    event_short_desc: str = Field(default="", description="Short 1-2 sentence Russian description of the event and free dinner details")
    event_datetime: str = Field(default="", description="Date and time in Russian (e.g. '24 ноября 2026 (вт), 18:30')")
    event_venue: str = Field(default="", description="Event venue/location (e.g. 'DGIST E8-312' or 'E1 컨벤션홀')")
    registration_url: str = Field(default="", description="Exact registration link URL (especially Google Forms) if present, else empty")


class PlannedFieldAnswer(BaseModel):
    entry_id: str = Field(description="Exact entry_id from the form schema (e.g. '1479224308' or 'emailAddress')")
    value: str = Field(default="", description="Exact answer string for text/paragraph/email/radio/dropdown/scale/date(YYYY-MM-DD)/time(HH:MM)")
    values: list[str] = Field(default_factory=list, description="Exact list of selected option strings for checkbox fields")
    use_other: bool = Field(default=False, description="True ONLY if selecting Google Forms 'Other' option when has_other_option=true")
    other_text: str = Field(default="", description="Custom text for 'Other' option when use_other=true")
    label_ru: str = Field(default="", description="Short clear Russian label of this field for review display")


class MissingFieldPrompt(BaseModel):
    entry_id: str = Field(description="Exact entry_id of the required field that cannot be filled yet")
    field_title: str = Field(default="", description="Original title of the field")
    question_ru: str = Field(description="Clear, concise Russian question asking the user for this missing info (mention formatting/options if relevant)")
    options: list[str] = Field(default_factory=list, description="Exact options if choice field")
    is_personal_reusable: bool = Field(default=True, description="True if personal info reusable in future forms; False if event-specific")
    profile_key: str = Field(default="", description="Suggested standard UserProfile key if is_personal_reusable=true")


class FormFillPlan(BaseModel):
    filled_fields: list[PlannedFieldAnswer] = Field(default_factory=list)
    missing_fields: list[MissingFieldPrompt] = Field(default_factory=list)
    event_title_refined: str = Field(default="", description="Refined clean event title combining post and form title")
    event_datetime_refined: str = Field(default="", description="Refined date and time in Russian if found in post/form")
    event_venue_refined: str = Field(default="", description="Refined venue/location if found in post/form")
    event_short_desc_refined: str = Field(default="", description="Short 1-2 sentence Russian description of the event")


class UserReplyExtraction(BaseModel):
    is_cancel: bool = Field(
        default=False,
        description="True ONLY if user says they do NOT want to register / cancel / skip / not interested"
    )
    extracted_answers: list[PlannedFieldAnswer] = Field(default_factory=list)
    profile_updates: list[ProfileKeyValue] = Field(
        default_factory=list,
        description="ONLY reusable personal facts provided by the user (name, student_id, phone, email, department, role, year, dietary). Do NOT save event-specific answers here."
    )


class ApprovalOrEditDecision(BaseModel):
    decision: str = Field(
        default="unrelated",
        description=(
            "'approve' if user agrees/approves registration (e.g. 'одобряю', 'записывай', 'да', 'го', 'ок', 'подавай', 'yes'); "
            "'reject' if user refuses/cancels (e.g. 'не нужно записываться', 'не надо', 'отмена', 'нет', 'не записывай', 'cancel'); "
            "'edit' if user asks to change/correct one or more filled form fields; "
            "'unrelated' if user wrote something completely unrelated to this event registration."
        )
    )
    edited_fields: list[PlannedFieldAnswer] = Field(default_factory=list, description="Updated field answers when decision=='edit'")
    profile_updates: list[ProfileKeyValue] = Field(default_factory=list, description="Personal profile updates if user corrected a personal field")


class ProfileParseResult(BaseModel):
    updates: list[ProfileKeyValue] = Field(default_factory=list)


class CalendarDateParse(BaseModel):
    start_iso: str = Field(default="", description="Start datetime in YYYY-MM-DDTHH:MM:SS (Asia/Seoul) or empty if unknown")
    end_iso: str = Field(default="", description="End datetime in YYYY-MM-DDTHH:MM:SS (default +1.5 hours) or empty")


# ─────────────────────────────────────────────────────────────────────────────
# Работа с базой данных (UserProfile & EventRegistration)
# ─────────────────────────────────────────────────────────────────────────────

def _ensure_tables():
    try:
        models.Base.metadata.create_all(bind=engine)
    except Exception as ex:
        print(f"[EVREG] DB create_all warning: {ex}")


def load_user_profile(uid: int) -> dict[str, str]:
    _ensure_tables()
    with SessionLocal() as db:
        row = db.query(models.UserProfile).filter_by(telegram_user_id=int(uid)).first()
        if not row or not row.profile_json:
            return {}
        try:
            data = json.loads(row.profile_json)
            if isinstance(data, dict):
                return {str(k): str(v) for k, v in data.items() if v is not None and str(v).strip()}
        except Exception:
            pass
    return {}


def save_user_profile(uid: int, profile: dict[str, str]) -> dict[str, str]:
    _ensure_tables()
    clean = {str(k).strip(): str(v).strip() for k, v in (profile or {}).items() if k and v is not None and str(v).strip()}
    with SessionLocal() as db:
        row = db.query(models.UserProfile).filter_by(telegram_user_id=int(uid)).first()
        if not row:
            row = models.UserProfile(telegram_user_id=int(uid))
            db.add(row)
        row.profile_json = json.dumps(clean, ensure_ascii=False)
        row.updated_at = datetime.datetime.utcnow()
        db.commit()
    return clean


def merge_user_profile(uid: int, updates: dict[str, str]) -> dict[str, str]:
    cur = load_user_profile(uid)
    for k, v in (updates or {}).items():
        if k and v is not None and str(v).strip():
            cur[str(k).strip()] = str(v).strip()
    return save_user_profile(uid, cur)


def format_user_profile_text(profile: dict[str, str]) -> str:
    if not profile:
        return (
            "👤 <b>Твой профиль для автозаполнения форм пока пуст.</b>\n\n"
            "Ты можешь заполнить его сейчас (нажми кнопку ниже) — или просто оставить пустым: "
            "при первой заявке на лекцию я сама спрошу недостающие поля и запомню их навсегда!"
        )
    lines = ["👤 <b>Твои сохранённые данные для автозаполнения форм:</b>\n"]
    shown = set()
    for key, label in PROFILE_LABELS_RU.items():
        if key in profile:
            lines.append(f"• <b>{html.escape(label)}:</b> {html.escape(profile[key])}")
            shown.add(key)
    for key, val in profile.items():
        if key not in shown:
            lines.append(f"• <b>{html.escape(key)}:</b> {html.escape(val)}")
    return "\n".join(lines)


def create_event_registration(
    uid: int,
    chat_id: int,
    form_url: str,
    post_id: str | None = None,
    post_url: str | None = None,
    event_title: str = "",
    event_description: str = "",
    event_datetime: str = "",
    event_venue: str = "",
    status: str = "queued",
) -> dict:
    _ensure_tables()
    job_id = secrets.token_hex(5)
    with SessionLocal() as db:
        row = models.EventRegistration(
            job_id=job_id,
            telegram_user_id=int(uid),
            chat_id=int(chat_id),
            post_id=str(post_id) if post_id else None,
            post_url=post_url or "",
            form_url=form_url,
            event_title=event_title or "Мероприятие DGIST",
            event_description=event_description or "",
            event_datetime=event_datetime or "",
            event_venue=event_venue or "",
            status=status,
            created_at=datetime.datetime.utcnow(),
            updated_at=datetime.datetime.utcnow(),
        )
        db.add(row)
        db.commit()
        return _row_to_job_dict(row)


def _row_to_job_dict(row: models.EventRegistration) -> dict:
    def _jload(s, default):
        if not s:
            return default
        try:
            return json.loads(s)
        except Exception:
            return default

    return {
        "id": row.id,
        "job_id": row.job_id,
        "uid": row.telegram_user_id,
        "chat_id": row.chat_id or row.telegram_user_id,
        "post_id": row.post_id,
        "post_url": row.post_url or "",
        "form_url": row.form_url or "",
        "prefilled_url": row.prefilled_url or "",
        "event_title": row.event_title or "",
        "event_description": row.event_description or "",
        "event_datetime": row.event_datetime or "",
        "event_venue": row.event_venue or "",
        "form_schema": _jload(row.form_schema_json, {}),
        "filled_answers": _jload(row.filled_answers_json, {}),
        "missing_fields": _jload(row.missing_fields_json, []),
        "status": row.status,
        "error_message": row.error_message or "",
    }


def get_event_registration(job_id: str) -> dict | None:
    _ensure_tables()
    with SessionLocal() as db:
        row = db.query(models.EventRegistration).filter_by(job_id=job_id).first()
        return _row_to_job_dict(row) if row else None


def update_event_registration(job_id: str, **kwargs) -> dict | None:
    _ensure_tables()
    with SessionLocal() as db:
        row = db.query(models.EventRegistration).filter_by(job_id=job_id).first()
        if not row:
            return None
        json_map = {
            "form_schema": "form_schema_json",
            "filled_answers": "filled_answers_json",
            "missing_fields": "missing_fields_json",
        }
        for k, v in kwargs.items():
            if k in json_map:
                setattr(row, json_map[k], json.dumps(v, ensure_ascii=False))
            elif hasattr(row, k):
                setattr(row, k, v)
        row.updated_at = datetime.datetime.utcnow()
        db.commit()
        return _row_to_job_dict(row)


def has_existing_registration_for_post(uid: int, post_id: str) -> bool:
    if not post_id:
        return False
    _ensure_tables()
    with SessionLocal() as db:
        row = db.query(models.EventRegistration).filter_by(
            telegram_user_id=int(uid),
            post_id=str(post_id),
        ).first()
        return row is not None


def get_active_registration_for_user(uid: int) -> dict | None:
    """Возвращает активную (waiting_info / awaiting_approval) заявку из БД (переживает рестарт Render)."""
    _ensure_tables()
    with SessionLocal() as db:
        row = (
            db.query(models.EventRegistration)
            .filter(
                models.EventRegistration.telegram_user_id == int(uid),
                models.EventRegistration.status.in_(["waiting_info", "awaiting_approval"]),
            )
            .order_by(models.EventRegistration.updated_at.desc())
            .first()
        )
        return _row_to_job_dict(row) if row else None


def get_next_queued_registration(uid: int) -> dict | None:
    _ensure_tables()
    with SessionLocal() as db:
        row = (
            db.query(models.EventRegistration)
            .filter_by(telegram_user_id=int(uid), status="queued")
            .order_by(models.EventRegistration.id.asc())
            .first()
        )
        return _row_to_job_dict(row) if row else None


def cancel_active_registration_for_user(uid: int) -> dict | None:
    job = get_active_registration_for_user(uid)
    if job:
        return update_event_registration(job["job_id"], status="cancelled")
    return None


# ─────────────────────────────────────────────────────────────────────────────
# Gemini AI-анализ постов, планирование заполнения и разбор ответов пользователя
# ─────────────────────────────────────────────────────────────────────────────

def _is_owner(uid: int | None) -> bool:
    return bool(uid and int(uid) == int(OWNER_ID))


def analyze_post_for_dinner_sync(post: dict) -> dict:
    """Анализирует пост DGIST на наличие лекции с бесплатным ужином и извлекает дату/время/место/ссылку."""
    # Если пост уже промаркирован свежим dgist_monitor и содержит нужные поля — используем их
    if "is_free_dinner_event" in post and (not post.get("is_free_dinner_event") or post.get("event_title")):
        urls = extract_registration_urls_from_post(post)
        reg_url = (post.get("registration_url") or "").strip() or (urls[0] if urls else "")
        return {
            "is_free_dinner_event": bool(post.get("is_free_dinner_event")),
            "event_title": (post.get("event_title") or post.get("title") or "").strip(),
            "event_short_desc": (post.get("event_short_desc") or post.get("summary") or "").strip(),
            "event_datetime": (post.get("event_datetime") or "").strip(),
            "event_venue": (post.get("event_venue") or "").strip(),
            "registration_url": reg_url,
        }

    title = post.get("title") or ""
    full_text = post.get("full_text") or post.get("summary") or ""
    links = post.get("links") or []
    urls = extract_registration_urls_from_post(post)
    links_txt = "\n".join(
        f"- {lk.get('label', '')}: {lk.get('url', '')}"
        for lk in links if isinstance(lk, dict) and lk.get("url")
    )

    if not _genai_client:
        low = (title + " " + full_text).lower()
        is_dinner = any(k in low for k in ("dinner", "저녁", "석식", "supper")) and "cafeteria" not in low
        return {
            "is_free_dinner_event": is_dinner,
            "event_title": title,
            "event_short_desc": (post.get("summary") or full_text[:250]).strip(),
            "event_datetime": "",
            "event_venue": "",
            "registration_url": urls[0] if urls else "",
        }

    prompt = (
        f"Заголовок поста DGIST: {title}\n"
        f"Дата публикации: {post.get('date', '')}\n"
        f"Ссылки в посте:\n{links_txt or 'нет'}\n"
        f"Полный текст поста:\n{full_text[:4500]}\n\n"
        "Определи, является ли это лекцией, семинаром, конференцией или студенческим мероприятием, "
        "где участникам предоставляют БЕСПЛАТНЫЙ УЖИН (dinner, free dinner, supper, 저녁, 저녁 식사, 저녁 도시락, 석식, "
        "happy hour с ужином, или вечерняя лекция >=16:30 с бесплатной едой/пиццей).\n"
        "НЕ считай ужином меню столовой (Cafeteria menu), объявления о стипендиях, скидки или события только с обедом (lunch/점심).\n"
        "Заполни event_title, event_short_desc (на русском, 1-2 предложения), event_datetime (на русском), event_venue и registration_url."
    )
    try:
        cfg = types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=EventPostAnalysis,
            temperature=0.1,
        )
        res = _genai_client.models.generate_content(model=GEMINI_MODEL, contents=prompt, config=cfg)
        parsed = EventPostAnalysis.model_validate_json(res.text or "{}")
        reg_url = (parsed.registration_url or "").strip() or (urls[0] if urls else "")
        return {
            "is_free_dinner_event": bool(parsed.is_free_dinner_event),
            "event_title": (parsed.event_title or title).strip(),
            "event_short_desc": (parsed.event_short_desc or post.get("summary") or "").strip(),
            "event_datetime": (parsed.event_datetime or "").strip(),
            "event_venue": (parsed.event_venue or "").strip(),
            "registration_url": reg_url,
        }
    except Exception as ex:
        print(f"[EVREG] analyze_post_for_dinner error: {ex}")
        return {
            "is_free_dinner_event": False,
            "event_title": title,
            "event_short_desc": (post.get("summary") or "").strip(),
            "event_datetime": "",
            "event_venue": "",
            "registration_url": urls[0] if urls else "",
        }


def _normalize_and_validate_answers(
    fields: list[dict],
    raw_filled: dict[str, dict],
    raw_missing: list[dict],
) -> tuple[dict[str, dict], list[dict]]:
    """
    Жёсткая Python-валидация плана заполнения:
    - Проверяет точное совпадение вариантов (radio / dropdown / checkbox / scale) с options формы.
    - Гарантирует, что НИ ОДНО обязательное поле не потеряно.
    """
    validated_filled: dict[str, dict] = {}
    missing_by_id: dict[str, dict] = {str(m["entry_id"]): dict(m) for m in (raw_missing or []) if m.get("entry_id")}

    def _match_option(candidate: str, options: list[str]) -> str | None:
        c = (candidate or "").strip()
        if not c:
            return None
        if c in options:
            return c
        c_low = c.lower()
        for opt in options:
            if opt.strip().lower() == c_low:
                return opt
        return None

    for f in fields:
        eid = str(f["entry_id"])
        ftype = f.get("field_type", "short_text")
        req = bool(f.get("required"))
        opts = list(f.get("options") or [])
        has_other = bool(f.get("has_other_option"))

        ans = raw_filled.get(eid)
        valid = False
        cleaned_entry: dict[str, Any] = {}

        if ans and isinstance(ans, dict):
            label_ru = (ans.get("label_ru") or f.get("title") or "").strip()
            use_other = bool(ans.get("use_other")) and has_other
            other_text = (ans.get("other_text") or "").strip()

            if ftype == "checkbox":
                raw_vals = ans.get("values") or ([ans.get("value")] if ans.get("value") else [])
                matched_vals = []
                for rv in raw_vals:
                    m = _match_option(str(rv), opts)
                    if m and m not in matched_vals:
                        matched_vals.append(m)
                if matched_vals or (use_other and other_text):
                    valid = True
                    cleaned_entry = {
                        "value": matched_vals,
                        "use_other": use_other and bool(other_text),
                        "other_text": other_text if use_other else "",
                        "label_ru": label_ru,
                    }
            elif ftype in ("radio", "dropdown", "scale"):
                if use_other and other_text:
                    valid = True
                    cleaned_entry = {
                        "value": "",
                        "use_other": True,
                        "other_text": other_text,
                        "label_ru": label_ru,
                    }
                else:
                    raw_val = str(ans.get("value") or "").strip()
                    m = _match_option(raw_val, opts)
                    if m:
                        valid = True
                        cleaned_entry = {
                            "value": m,
                            "use_other": False,
                            "other_text": "",
                            "label_ru": label_ru,
                        }
                    elif raw_val and has_other:
                        # Если модель дала кастомный ответ, а у поля есть Other — используем Other
                        valid = True
                        cleaned_entry = {
                            "value": "",
                            "use_other": True,
                            "other_text": raw_val,
                            "label_ru": label_ru,
                        }
            else:
                raw_val = str(ans.get("value") or "").strip()
                if raw_val:
                    valid = True
                    cleaned_entry = {
                        "value": raw_val,
                        "use_other": False,
                        "other_text": "",
                        "label_ru": label_ru,
                    }

        if valid:
            validated_filled[eid] = cleaned_entry
            missing_by_id.pop(eid, None)
        elif req:
            # Обязательное поле не заполнено — обязательно спрашиваем пользователя!
            if eid not in missing_by_id:
                q_ru = f.get("title") or f"Поле {eid}"
                if f.get("description"):
                    q_ru += f" ({f['description'][:120]})"
                missing_by_id[eid] = {
                    "entry_id": eid,
                    "field_title": f.get("title", ""),
                    "question_ru": q_ru,
                    "options": opts,
                    "is_personal_reusable": True,
                    "profile_key": "",
                }
            else:
                # Дополняем точными опциями из схемы формы
                if opts and not missing_by_id[eid].get("options"):
                    missing_by_id[eid]["options"] = opts

    # Сохраняем порядок полей как в форме
    ordered_missing = [missing_by_id[str(f["entry_id"])] for f in fields if str(f["entry_id"]) in missing_by_id]
    return validated_filled, ordered_missing


def plan_form_answers_sync(
    event_info: dict,
    form_schema: dict,
    user_profile: dict[str, str],
    existing_answers: dict | None = None,
) -> dict:
    """Использует Gemini для умного сопоставления профиля пользователя и контекста поста с полями Google Forms."""
    fields = form_schema.get("fields") or []
    existing_answers = existing_answers or {}

    if not _genai_client:
        validated_filled, ordered_missing = _normalize_and_validate_answers(fields, existing_answers, [])
        return {
            "filled_answers": validated_filled,
            "missing_fields": ordered_missing,
            "event_title": event_info.get("event_title") or form_schema.get("form_title") or "",
            "event_datetime": event_info.get("event_datetime") or "",
            "event_venue": event_info.get("event_venue") or "",
            "event_description": event_info.get("event_description") or form_schema.get("form_description") or "",
        }

    schema_for_llm = [
        {
            "entry_id": f["entry_id"],
            "type": f["field_type"],
            "title": f["title"],
            "description": f.get("description", ""),
            "context": f.get("context", ""),
            "required": f["required"],
            "options": f.get("options", []),
            "has_other_option": f.get("has_other_option", False),
        }
        for f in fields
    ]

    prompt = (
        "Ты помощник для автоматического заполнения Google Forms регистрации на университетскую лекцию/мероприятие в DGIST.\n\n"
        f"ИНФОРМАЦИЯ О МЕРОПРИЯТИИ (из поста):\n"
        f"Заголовок: {event_info.get('event_title', '')}\n"
        f"Дата/время: {event_info.get('event_datetime', '')}\n"
        f"Место: {event_info.get('event_venue', '')}\n"
        f"Описание: {event_info.get('event_description', '')}\n"
        f"Текст поста (фрагмент): {(event_info.get('post_text') or '')[:2000]}\n\n"
        f"ЗАГОЛОВОК ФОРМЫ: {form_schema.get('form_title', '')}\n"
        f"ОПИСАНИЕ ФОРМЫ: {(form_schema.get('form_description') or '')[:1200]}\n\n"
        f"ИЗВЕСТНЫЙ ПРОФИЛЬ ПОЛЬЗОВАТЕЛЯ:\n{json.dumps(user_profile, ensure_ascii=False, indent=2)}\n\n"
        f"УЖЕ ЗАПОЛНЕННЫЕ РАНЕЕ ПОЛЯ ДЛЯ ЭТОЙ ФОРМЫ:\n{json.dumps(existing_answers, ensure_ascii=False, indent=2)}\n\n"
        f"ПОЛЯ GOOGLE FORM:\n{json.dumps(schema_for_llm, ensure_ascii=False, indent=2)}\n\n"
        "СТРОГИЕ ПРАВИЛА ЗАПОЛНЕНИЯ:\n"
        "1. НИКОГДА не выдумывай личные данные пользователя (имя, 학번/Student ID, телефон, email, факультет, курс, диету/аллергии), если их НЕТ в профиле или уже заполненных полях! "
        "Если обязательное (required=true) поле требует неизвестных личных данных — помести его в missing_fields с понятным вопросом на русском.\n"
        "2. СОБЛЮДАЙ формат, указанный в названии поля! Например, если написано '연락처(- 제외 숫자만)' или 'without dashes', убери дефисы из телефона (01012345678). Если просят с дефисами — оставь дефисы.\n"
        "3. АВТОМАТИЧЕСКИ заполняй очевидные поля:\n"
        "   - Согласие на обработку персональных данных (개인정보 수집 및 이용 동의 / Privacy consent) -> выбери точный вариант согласия (например, '동의합니다!').\n"
        "   - Вопросы про еду/ужин/обед (저녁 식사 희망 여부 / Dinner / Meal / Lunchbox) -> выбери вариант 'Да / 희망함 / Yes / 참석'.\n"
        "   - Выбор кампуса/семинара, если пост конкретно про DGIST (대구 / 대구경북과학기술원) -> выбери точный вариант DGIST.\n"
        "   - Статус/구분 (например, ['교원', '연구원', '학생', ...]): если в профиле указано, что пользователь студент (undergraduate/graduate/학부생/대학원생/학생) -> выбери соответствующий точный вариант.\n"
        "4. Для полей radio / dropdown / checkbox / scale значение в value / values ДОЛЖНО ПОСИМВОЛЬНО совпадать с одним из элементов options! Если нужного варианта нет, но has_other_option=true — поставь use_other=true и запиши текст в other_text.\n"
        "5. Необязательные (required=false) поля НЕ добавляй в missing_fields! Если знаешь ответ из профиля (например, факультет) — заполни; если это свободный вопрос типа 'Вопросы спикеру (선택)' — оставь пустым.\n"
        "6. Если обязательное поле требует выбора специфичного для ивента варианта, который НЕ очевиден (например, выбор меню 'пицца или бургер', размер футболки, выбор сессии/времени) — помести в missing_fields с перечислением вариантов и is_personal_reusable=false.\n"
        "7. Для каждого заполненного поля в filled_fields укажи короткую понятную подпись на русском в label_ru.\n"
        "8. Также уточни event_title_refined, event_datetime_refined, event_venue_refined, event_short_desc_refined на русском, если в описании формы есть более точные дата/время/место."
    )

    try:
        cfg = types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=FormFillPlan,
            temperature=0.1,
        )
        res = _genai_client.models.generate_content(model=GEMINI_MODEL, contents=prompt, config=cfg)
        plan = FormFillPlan.model_validate_json(res.text or "{}")

        merged_raw = dict(existing_answers)
        for item in plan.filled_fields:
            merged_raw[str(item.entry_id)] = {
                "value": item.values if item.values else item.value,
                "values": item.values,
                "use_other": item.use_other,
                "other_text": item.other_text,
                "label_ru": item.label_ru,
            }

        raw_missing = [m.model_dump() for m in plan.missing_fields]
        validated_filled, ordered_missing = _normalize_and_validate_answers(fields, merged_raw, raw_missing)

        return {
            "filled_answers": validated_filled,
            "missing_fields": ordered_missing,
            "event_title": (plan.event_title_refined or event_info.get("event_title") or form_schema.get("form_title") or "Мероприятие DGIST").strip(),
            "event_datetime": (plan.event_datetime_refined or event_info.get("event_datetime") or "уточняется в описании").strip(),
            "event_venue": (plan.event_venue_refined or event_info.get("event_venue") or "DGIST (см. описание)").strip(),
            "event_description": (plan.event_short_desc_refined or event_info.get("event_description") or form_schema.get("form_description") or "").strip(),
        }
    except Exception as ex:
        print(f"[EVREG] plan_form_answers error: {ex}")
        validated_filled, ordered_missing = _normalize_and_validate_answers(fields, existing_answers, [])
        return {
            "filled_answers": validated_filled,
            "missing_fields": ordered_missing,
            "event_title": event_info.get("event_title") or form_schema.get("form_title") or "Мероприятие DGIST",
            "event_datetime": event_info.get("event_datetime") or "",
            "event_venue": event_info.get("event_venue") or "",
            "event_description": event_info.get("event_description") or "",
        }


def extract_user_reply_sync(
    missing_fields: list[dict],
    all_fields: list[dict],
    user_reply: str,
    current_profile: dict[str, str],
) -> dict:
    """Извлекает ответы на недостающие поля из свободного текста/голоса пользователя + сохраняемые личные данные."""
    low = (user_reply or "").strip().lower()
    if any(k in low for k in (
        "не нужно записываться", "не надо записываться", "не записывай", "отмена", "отмени", "пропусти", "cancel"
    )):
        return {"is_cancel": True, "extracted_answers": {}, "profile_updates": {}}

    if not _genai_client:
        out = {}
        if len(missing_fields) == 1:
            eid = str(missing_fields[0]["entry_id"])
            out[eid] = {"value": user_reply.strip(), "label_ru": missing_fields[0].get("field_title", "")}
        return {"is_cancel": False, "extracted_answers": out, "profile_updates": {}}

    fields_by_id = {str(f["entry_id"]): f for f in all_fields}
    missing_enriched = []
    for m in missing_fields:
        f = fields_by_id.get(str(m["entry_id"]), {})
        missing_enriched.append({
            "entry_id": str(m["entry_id"]),
            "type": f.get("field_type", "short_text"),
            "title": f.get("title", m.get("field_title", "")),
            "question_ru": m.get("question_ru", ""),
            "options": f.get("options", m.get("options", [])),
            "has_other_option": f.get("has_other_option", False),
            "is_personal_reusable": m.get("is_personal_reusable", True),
            "profile_key": m.get("profile_key", ""),
        })

    prompt = (
        "Пользователь ответил на запрос недостающих данных для заполнения Google Forms.\n\n"
        f"НЕДОСТАЮЩИЕ ПОЛЯ ФОРМЫ:\n{json.dumps(missing_enriched, ensure_ascii=False, indent=2)}\n\n"
        f"ТЕКУЩИЙ ПРОФИЛЬ ПОЛЬЗОВАТЕЛЯ:\n{json.dumps(current_profile, ensure_ascii=False, indent=2)}\n\n"
        f"ОТВЕТ ПОЛЬЗОВАТЕЛЯ:\n\"{user_reply}\"\n\n"
        "ИНСТРУКЦИЯ:\n"
        "1. Если пользователь отказывается записываться ('не надо', 'не нужно записываться', 'отмена', 'пропусти') — поставь is_cancel=true.\n"
        "2. Сопоставь данные из ответа пользователя с нужными entry_id в extracted_answers.\n"
        "   - Для полей с options выбери ТОЧНУЮ строку из options (посимвольно!). Если пользователь указал номер варианта (например '1' или '2') — подставь соответствующий элемент options.\n"
        "   - Соблюдай формат поля (например, если в title указано без дефисов — убери дефисы из телефона в extracted_answers).\n"
        "3. В profile_updates запиши ТОЛЬКО постоянные личные данные пользователя, которые пригодятся в будущих формах "
        "(full_name_en, full_name_ko, student_id, phone, email, dgist_email, affiliation_dept, role_status, grade_year, dietary_restrictions, nationality, gender, dorm_room, lab_advisor). "
        "НЕ сохраняй в profile_updates одноразовые ответы для конкретного ивента (выбор меню, выбор сессии, вопрос спикеру)."
    )

    try:
        cfg = types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=UserReplyExtraction,
            temperature=0.1,
        )
        res = _genai_client.models.generate_content(model=GEMINI_MODEL, contents=prompt, config=cfg)
        parsed = UserReplyExtraction.model_validate_json(res.text or "{}")
        if parsed.is_cancel:
            return {"is_cancel": True, "extracted_answers": {}, "profile_updates": {}}
        ans_map = {}
        for item in parsed.extracted_answers:
            ans_map[str(item.entry_id)] = {
                "value": item.values if item.values else item.value,
                "values": item.values,
                "use_other": item.use_other,
                "other_text": item.other_text,
                "label_ru": item.label_ru,
            }
        prof_upd = {kv.key: kv.value for kv in parsed.profile_updates if kv.key and kv.value}
        return {"is_cancel": False, "extracted_answers": ans_map, "profile_updates": prof_upd}
    except Exception as ex:
        print(f"[EVREG] extract_user_reply error: {ex}")
        return {"is_cancel": False, "extracted_answers": {}, "profile_updates": {}}


def classify_approval_or_edit_sync(
    user_reply: str,
    form_schema: dict,
    current_answers: dict,
) -> dict:
    """Распознаёт решение пользователя на этапе проверки: одобрить / отменить / изменить поле / не по теме."""
    txt = (user_reply or "").strip()
    low = txt.lower().rstrip(".!?,")

    # Быстрые детерминированные правила для однозначных фраз
    exact_approve = {
        "да", "одобряю", "записывай", "запиши", "подтверждаю", "ок", "окей", "го", "давай",
        "подавай", "отправляй", "согласен", "все верно", "всё верно", "норм", "yes", "ok", "approve",
        "да записывай", "да, записывай", "одобряю записывай", "записывай меня",
    }
    exact_reject = {
        "нет", "не надо", "не нужно", "не нужно записываться", "не надо записываться",
        "не записывай", "не записывайся", "отмена", "отмени", "пропусти", "не пойду",
        "не хочу", "no", "cancel", "reject", "skip",
    }
    if low in exact_approve:
        return {"decision": "approve", "edited_fields": {}, "profile_updates": {}}
    if low in exact_reject or "не нужно записываться" in low or "не надо записываться" in low:
        return {"decision": "reject", "edited_fields": {}, "profile_updates": {}}

    if not _genai_client:
        return {"decision": "unrelated", "edited_fields": {}, "profile_updates": {}}

    fields = form_schema.get("fields") or []
    schema_brief = [
        {"entry_id": f["entry_id"], "type": f["field_type"], "title": f["title"], "options": f.get("options", []), "has_other_option": f.get("has_other_option", False)}
        for f in fields
    ]

    prompt = (
        "Пользователь проверяет предзаполненную заявку Google Forms на лекцию.\n"
        f"ПОЛЯ ФОРМЫ:\n{json.dumps(schema_brief, ensure_ascii=False)}\n\n"
        f"ТЕКУЩИЕ ЗАПОЛНЕННЫЕ ОТВЕТЫ:\n{json.dumps(current_answers, ensure_ascii=False)}\n\n"
        f"СООБЩЕНИЕ ПОЛЬЗОВАТЕЛЯ:\n\"{txt}\"\n\n"
        "Определи decision:\n"
        "- 'approve': пользователь подтверждает/одобряет отправку заявки.\n"
        "- 'reject': пользователь отказывается/отменяет запись ('не нужно записываться', 'не пойду', 'отмена').\n"
        "- 'edit': пользователь просит исправить/поменять какое-то поле заявки -> верни обновлённые значения в edited_fields и при изменении личных данных — в profile_updates.\n"
        "- 'unrelated': сообщение вообще не относится к этой заявке (например, обычный вопрос боту на другую тему)."
    )
    try:
        cfg = types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=ApprovalOrEditDecision,
            temperature=0.1,
        )
        res = _genai_client.models.generate_content(model=GEMINI_MODEL, contents=prompt, config=cfg)
        parsed = ApprovalOrEditDecision.model_validate_json(res.text or "{}")
        edits = {}
        for item in parsed.edited_fields:
            edits[str(item.entry_id)] = {
                "value": item.values if item.values else item.value,
                "values": item.values,
                "use_other": item.use_other,
                "other_text": item.other_text,
                "label_ru": item.label_ru,
            }
        prof_upd = {kv.key: kv.value for kv in parsed.profile_updates if kv.key and kv.value}
        return {"decision": parsed.decision, "edited_fields": edits, "profile_updates": prof_upd}
    except Exception as ex:
        print(f"[EVREG] classify_approval error: {ex}")
        return {"decision": "unrelated", "edited_fields": {}, "profile_updates": {}}


def parse_freeform_profile_sync(user_text: str, current_profile: dict[str, str]) -> dict[str, str]:
    if not _genai_client:
        return {}
    prompt = (
        f"Текущий профиль пользователя:\n{json.dumps(current_profile, ensure_ascii=False)}\n\n"
        f"Сообщение пользователя для обновления профиля:\n\"{user_text}\"\n\n"
        "Извлеки все личные данные пользователя в updates (ключи: full_name_en, full_name_ko, student_id, phone, "
        "email, dgist_email, affiliation_dept, role_status, grade_year, dietary_restrictions, nationality, gender, dorm_room, lab_advisor)."
    )
    try:
        cfg = types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=ProfileParseResult,
            temperature=0.1,
        )
        res = _genai_client.models.generate_content(model=GEMINI_MODEL, contents=prompt, config=cfg)
        parsed = ProfileParseResult.model_validate_json(res.text or "{}")
        return {kv.key: kv.value for kv in parsed.updates if kv.key and kv.value}
    except Exception as ex:
        print(f"[EVREG] parse_freeform_profile error: {ex}")
        return {}


def parse_event_datetime_to_iso_sync(event_datetime: str, now_str: str) -> tuple[str, str]:
    if not _genai_client or not event_datetime:
        return "", ""
    prompt = (
        f"Текущее время (Asia/Seoul, UTC+9): {now_str}\n"
        f"Дата и время мероприятия: \"{event_datetime}\"\n"
        "Преобразуй в start_iso (YYYY-MM-DDTHH:MM:SS) и end_iso (YYYY-MM-DDTHH:MM:SS, если конец не указан — +1.5 часа)."
    )
    try:
        cfg = types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=CalendarDateParse,
            temperature=0.1,
        )
        res = _genai_client.models.generate_content(model=GEMINI_MODEL, contents=prompt, config=cfg)
        parsed = CalendarDateParse.model_validate_json(res.text or "{}")
        return (parsed.start_iso or "").strip(), (parsed.end_iso or "").strip()
    except Exception:
        return "", ""


# ─────────────────────────────────────────────────────────────────────────────
# Форматирование сообщений для Telegram (HTML-safe)
# ─────────────────────────────────────────────────────────────────────────────

def _format_answer_display(ans: Any) -> str:
    if isinstance(ans, dict):
        if ans.get("use_other") and ans.get("other_text"):
            return f"Другое: {ans['other_text']}"
        val = ans.get("value")
    else:
        val = ans
    if isinstance(val, list):
        return ", ".join(str(x) for x in val if x)
    return str(val or "").strip()


def build_missing_info_message(job: dict) -> tuple[str, list]:
    title = html.escape(job.get("event_title") or "Лекция DGIST")
    dt = html.escape(job.get("event_datetime") or "не указано")
    venue = html.escape(job.get("event_venue") or "не указано")
    desc = html.escape((job.get("event_description") or "")[:350])
    missing = job.get("missing_fields") or []

    lines = [
        "🍽 <b>Нашла лекцию — готовлю заявку!</b>\n",
        f"📌 <b>Мероприятие:</b> {title}",
        f"📅 <b>Дата и время:</b> {dt}",
        f"📍 <b>Место:</b> {venue}",
    ]
    if desc:
        lines.append(f"📝 <b>О чём:</b> {desc}")

    lines.append("\n❓ <b>Чтобы заполнить форму, мне не хватает следующих данных о тебе:</b>")
    for idx, m in enumerate(missing, 1):
        q = html.escape(m.get("question_ru") or m.get("field_title") or f"Поле {idx}")
        opts = m.get("options") or []
        opt_str = ""
        if opts:
            shown_opts = " / ".join(html.escape(o) for o in opts[:8])
            opt_str = f"\n   <i>Варианты: {shown_opts}</i>"
        lines.append(f"{idx}. {q}{opt_str}")

    lines.append(
        "\n💬 <i>Ответь текстом или голосовым (можно всё одним сообщением) — я запомню твои личные данные, "
        "чтобы в будущем заполнять формы автоматически!</i>"
    )
    buttons = [[Button.inline("❌ Не нужно записываться", data=f"evreg:cancel:{job['job_id']}".encode("utf-8"))]]
    return "\n".join(lines), buttons


def build_review_message(job: dict) -> tuple[str, list]:
    title = html.escape(job.get("event_title") or "Лекция DGIST")
    dt = html.escape(job.get("event_datetime") or "не указано")
    venue = html.escape(job.get("event_venue") or "не указано")
    desc = html.escape((job.get("event_description") or "")[:500])
    prefilled = job.get("prefilled_url") or job.get("form_url") or ""
    post_url = job.get("post_url") or ""
    fields = (job.get("form_schema") or {}).get("fields") or []
    answers = job.get("filled_answers") or {}

    lines = [
        "🍽 <b>Проверь заявку на мероприятие перед отправкой:</b>\n",
        f"📌 <b>Мероприятие:</b> {title}",
    ]
    if desc:
        lines.append(f"📝 <b>Описание:</b> {desc}")
    lines.append(f"📅 <b>Дата и время:</b> {dt}")
    lines.append(f"📍 <b>Место:</b> {venue}")

    if prefilled:
        link_url = prefilled if len(prefilled) <= 1800 else (job.get("form_url") or prefilled)
        lines.append(f"🔗 <b>Форма (с заполненными полями):</b> <a href=\"{html.escape(link_url, quote=True)}\">Открыть Google Form</a>")
    if post_url:
        lines.append(f"📰 <b>Пост на портале:</b> <a href=\"{html.escape(post_url, quote=True)}\">Открыть объявление</a>")

    lines.append("\n📋 <b>Заполненные поля:</b>")
    filled_count = 0
    for f in fields:
        eid = str(f["entry_id"])
        if eid not in answers:
            continue
        ans = answers[eid]
        disp = _format_answer_display(ans)
        if not disp:
            continue
        lbl = (ans.get("label_ru") if isinstance(ans, dict) else "") or f.get("title") or eid
        lines.append(f"• <b>{html.escape(lbl[:80])}:</b> {html.escape(disp[:250])}")
        filled_count += 1
    if filled_count == 0:
        lines.append("• <i>(Нет заполненных полей)</i>")

    lines.append(
        "\n✅ <i>Нажми кнопку ниже или просто напиши/скажи «одобряю» / «не нужно записываться» "
        "(можно также написать, какое поле исправить).</i>"
    )

    job_id = job["job_id"]
    buttons = []
    if prefilled and len(prefilled) <= 1800:
        buttons.append([Button.url("🔗 Открыть и проверить форму", prefilled)])
    elif job.get("form_url"):
        buttons.append([Button.url("🔗 Открыть форму", job["form_url"])])
    buttons.append([
        Button.inline("✅ Одобрить и записаться", data=f"evreg:approve:{job_id}".encode("utf-8")),
        Button.inline("❌ Не нужно записываться", data=f"evreg:cancel:{job_id}".encode("utf-8")),
    ])
    buttons.append([
        Button.inline("✏️ Изменить поле", data=f"evreg:edit:{job_id}".encode("utf-8")),
    ])

    text = "\n".join(lines)
    if len(text) > 4000:
        text = text[:3980] + "\n..."
    return text, buttons


# ─────────────────────────────────────────────────────────────────────────────
# Основной конвейер (Шаги 1 -> 2 -> 3 -> 4 -> 5 -> 6)
# ─────────────────────────────────────────────────────────────────────────────

async def _advance_user_queue(client, uid: int, user_states: dict):
    """Если в очереди есть ещё найденные лекции с ужином — запускает следующую."""
    next_job = await asyncio.to_thread(get_next_queued_registration, uid)
    if not next_job:
        return
    await _run_parse_and_plan_for_job(client, next_job, user_states, is_auto=True)


async def _run_parse_and_plan_for_job(
    client,
    job: dict,
    user_states: dict,
    reply_to: int | None = None,
    status_msg=None,
    is_auto: bool = False,
    post_text: str = "",
):
    uid = int(job["uid"])
    chat_id = int(job["chat_id"])
    job_id = job["job_id"]
    form_url = job["form_url"]

    # Шаг 1: Заходим по ссылке и парсим Google Form
    try:
        schema = await asyncio.to_thread(parse_google_form, form_url)
    except Exception as ex:
        await asyncio.to_thread(update_event_registration, job_id, status="failed", error_message=str(ex))
        if status_msg:
            await status_msg.edit(f"❌ Не удалось открыть форму регистрации: {ex}")
        elif not is_auto:
            await client.send_message(chat_id, f"❌ Не удалось открыть форму регистрации: {ex}", reply_to=reply_to)
        return

    if not schema.get("ok"):
        st = schema.get("status")
        err = schema.get("error") or "Не удалось обработать форму."
        await asyncio.to_thread(update_event_registration, job_id, status="failed", error_message=err)

        # Если авто-мониторинг наткнулся на уже закрытую форму — не спамим пользователя
        if is_auto and st == "closed":
            await _advance_user_queue(client, uid, user_states)
            return

        title = html.escape(job.get("event_title") or schema.get("form_title") or "Лекция DGIST")
        dt = html.escape(job.get("event_datetime") or "см. описание")
        venue = html.escape(job.get("event_venue") or "см. описание")
        desc = html.escape((job.get("event_description") or "")[:350])
        final_u = schema.get("final_url") or form_url
        note = (
            f"🍽 <b>{title}</b>\n"
            f"📅 <b>Дата и время:</b> {dt}\n"
            f"📍 <b>Место:</b> {venue}\n"
            + (f"📝 {desc}\n\n" if desc else "\n")
            + f"⚠️ <b>Автозаполнение невозможно:</b> {html.escape(err)}\n"
            + f"🔗 <a href=\"{html.escape(final_u, quote=True)}\">Открыть ссылку вручную</a>"
        )
        btns = [[Button.url("🔗 Открыть ссылку", final_u)]]
        if status_msg:
            await status_msg.edit(note, parse_mode="html", buttons=btns, link_preview=False)
        else:
            await client.send_message(chat_id, note, parse_mode="html", buttons=btns, link_preview=False, reply_to=reply_to)
        if is_auto:
            await _advance_user_queue(client, uid, user_states)
        return

    if schema.get("has_file_upload"):
        err = "В форме есть обязательная загрузка файла (требует вход в Google)."
        await asyncio.to_thread(update_event_registration, job_id, status="failed", error_message=err)
        msg_txt = f"⚠️ {err}\n🔗 <a href=\"{html.escape(schema['viewform_url'], quote=True)}\">Открыть форму вручную</a>"
        if status_msg:
            await status_msg.edit(msg_txt, parse_mode="html", link_preview=False)
        else:
            await client.send_message(chat_id, msg_txt, parse_mode="html", link_preview=False, reply_to=reply_to)
        if is_auto:
            await _advance_user_queue(client, uid, user_states)
        return

    # Шаг 2: Заполняем все известные данные о пользователе
    profile = await asyncio.to_thread(load_user_profile, uid)
    event_info = {
        "event_title": job.get("event_title") or "",
        "event_datetime": job.get("event_datetime") or "",
        "event_venue": job.get("event_venue") or "",
        "event_description": job.get("event_description") or "",
        "post_text": post_text,
    }
    plan = await asyncio.to_thread(
        plan_form_answers_sync,
        event_info,
        schema,
        profile,
        job.get("filled_answers") or {},
    )

    filled_answers = plan["filled_answers"]
    missing_fields = plan["missing_fields"]
    prefilled_url = build_prefilled_google_form_url(schema["viewform_url"], schema["fields"], filled_answers)

    updated = await asyncio.to_thread(
        update_event_registration,
        job_id,
        event_title=plan["event_title"],
        event_datetime=plan["event_datetime"],
        event_venue=plan["event_venue"],
        event_description=plan["event_description"],
        form_schema=schema,
        filled_answers=filled_answers,
        missing_fields=missing_fields,
        prefilled_url=prefilled_url,
        status="waiting_info" if missing_fields else "awaiting_approval",
    )

    if missing_fields:
        # Шаг 2б: Спрашиваем недостающие поля и ждём ответа
        user_states[uid] = ("waiting_for_event_form_info", job_id)
        txt, btns = build_missing_info_message(updated)
        if status_msg:
            await status_msg.edit(txt, parse_mode="html", buttons=btns, link_preview=False)
        else:
            await client.send_message(chat_id, txt, parse_mode="html", buttons=btns, link_preview=False, reply_to=reply_to)
    else:
        # Шаг 3: Все поля заполнены -> отправляем на проверку!
        user_states[uid] = ("waiting_for_event_approval", job_id)
        txt, btns = build_review_message(updated)
        if status_msg:
            await status_msg.edit(txt, parse_mode="html", buttons=btns, link_preview=False)
        else:
            await client.send_message(chat_id, txt, parse_mode="html", buttons=btns, link_preview=False, reply_to=reply_to)


async def start_event_registration_flow(
    client,
    chat_id: int,
    uid: int,
    user_states: dict,
    form_url: str,
    event_info: dict | None = None,
    post_id: str | None = None,
    post_url: str | None = None,
    post_text: str = "",
    reply_to: int | None = None,
    status_msg=None,
    is_auto: bool = False,
):
    """Запускает полный 6-шаговый процесс регистрации на лекцию/форму (с поддержкой очереди)."""
    event_info = event_info or {}

    if is_auto and post_id:
        already = await asyncio.to_thread(has_existing_registration_for_post, uid, str(post_id))
        if already:
            return None

    # Если это авто-обнаружение и пользователь уже отвечает на другую форму — ставим в очередь
    active_job = await asyncio.to_thread(get_active_registration_for_user, uid)
    if is_auto and active_job:
        return await asyncio.to_thread(
            create_event_registration,
            uid=uid,
            chat_id=chat_id,
            form_url=form_url,
            post_id=post_id,
            post_url=post_url,
            event_title=event_info.get("event_title") or "",
            event_description=event_info.get("event_short_desc") or event_info.get("event_description") or "",
            event_datetime=event_info.get("event_datetime") or "",
            event_venue=event_info.get("event_venue") or "",
            status="queued",
        )

    job = await asyncio.to_thread(
        create_event_registration,
        uid=uid,
        chat_id=chat_id,
        form_url=form_url,
        post_id=post_id,
        post_url=post_url,
        event_title=event_info.get("event_title") or "",
        event_description=event_info.get("event_short_desc") or event_info.get("event_description") or "",
        event_datetime=event_info.get("event_datetime") or "",
        event_venue=event_info.get("event_venue") or "",
        status="queued",
    )
    await _run_parse_and_plan_for_job(
        client,
        job,
        user_states,
        reply_to=reply_to,
        status_msg=status_msg,
        is_auto=is_auto,
        post_text=post_text,
    )
    return job


async def process_free_dinner_posts_after_portal_check(
    client,
    uid: int,
    posts: list[dict],
    user_states: dict,
    notify_chat_id: int | None = None,
):
    """
    Вызывается автоматически после проверки портала DGIST (/checkportal и 33-часовой авто-монитор):
    находит все новые лекции с бесплатным ужином и запускает пошаговую автоподачу.
    """
    if not posts or not _is_owner(uid):
        return
    target_chat = int(notify_chat_id or uid)

    for p in posts:
        pid = str(p.get("post_id") or "")
        if pid and await asyncio.to_thread(has_existing_registration_for_post, uid, pid):
            continue

        info = await asyncio.to_thread(analyze_post_for_dinner_sync, p)
        if not info.get("is_free_dinner_event"):
            continue

        reg_url = (info.get("registration_url") or "").strip()
        if not reg_url:
            urls = extract_registration_urls_from_post(p)
            reg_url = urls[0] if urls else ""

        if not reg_url:
            # Лекция с бесплатным ужином без ссылки (вход свободный на месте) — всё равно уведомим!
            if pid:
                await asyncio.to_thread(
                    create_event_registration,
                    uid=uid,
                    chat_id=target_chat,
                    form_url=p.get("post_url") or "",
                    post_id=pid,
                    post_url=p.get("post_url") or "",
                    event_title=info.get("event_title") or p.get("title") or "",
                    event_description=info.get("event_short_desc") or "",
                    event_datetime=info.get("event_datetime") or "",
                    event_venue=info.get("event_venue") or "",
                    status="failed",
                )
            title = html.escape(info.get("event_title") or p.get("title") or "Лекция DGIST")
            dt = html.escape(info.get("event_datetime") or "см. пост")
            venue = html.escape(info.get("event_venue") or "см. пост")
            desc = html.escape((info.get("event_short_desc") or "")[:350])
            post_u = p.get("post_url") or ""
            msg = (
                f"🍽 <b>Нашла лекцию с бесплатным ужином (без формы предварительной записи):</b>\n\n"
                f"📌 <b>{title}</b>\n"
                f"📅 <b>Дата и время:</b> {dt}\n"
                f"📍 <b>Место:</b> {venue}\n"
                + (f"📝 {desc}\n" if desc else "")
                + (f"\n📰 <a href=\"{html.escape(post_u, quote=True)}\">Открыть объявление на портале</a>" if post_u else "")
            )
            try:
                await client.send_message(target_chat, msg, parse_mode="html", link_preview=False)
            except Exception as ex:
                print(f"[EVREG] notify no-link dinner error: {ex}")
            continue

        await start_event_registration_flow(
            client=client,
            chat_id=target_chat,
            uid=uid,
            user_states=user_states,
            form_url=reg_url,
            event_info=info,
            post_id=pid or None,
            post_url=p.get("post_url") or "",
            post_text=p.get("full_text") or "",
            is_auto=True,
        )


async def execute_submission_and_notify(client, job: dict, user_states: dict, status_msg=None, fallback_event=None):
    """Шаг 5 -> Шаг 6: Отправляет Google Form и уведомляет пользователя."""
    job_id = job["job_id"]
    uid = int(job["uid"])
    chat_id = int(job["chat_id"])
    title = job.get("event_title") or "Мероприятие DGIST"
    dt = job.get("event_datetime") or "дату из описания"
    venue = job.get("event_venue") or "место из описания"
    schema = job.get("form_schema") or {}
    fields = schema.get("fields") or []
    answers = job.get("filled_answers") or {}
    viewform_url = schema.get("viewform_url") or job["form_url"]
    num_pages = int(schema.get("num_pages") or 1)
    prefilled_url = job.get("prefilled_url") or build_prefilled_google_form_url(viewform_url, fields, answers)

    user_states.pop(uid, None)
    wait_txt = f"⏳ Отправляю заявку на <b>{html.escape(title)}</b>..."
    if status_msg:
        try:
            await status_msg.edit(wait_txt, parse_mode="html", buttons=None)
        except Exception:
            status_msg = await client.send_message(chat_id, wait_txt, parse_mode="html")
    elif fallback_event:
        status_msg = await fallback_event.reply(wait_txt, parse_mode="html")
    else:
        status_msg = await client.send_message(chat_id, wait_txt, parse_mode="html")

    res = await asyncio.to_thread(
        submit_google_form,
        viewform_url,
        fields,
        answers,
        num_pages,
        prefilled_url,
    )

    if res.get("ok"):
        await asyncio.to_thread(update_event_registration, job_id, status="submitted", error_message="")
        # Шаг 6: Точное уведомление в формате, который просил пользователь
        done_txt = (
            f"✅ <b>Записал вас на {html.escape(title)}</b> — "
            f"<b>{html.escape(dt)}</b>, место: <b>{html.escape(venue)}</b>"
        )
        btns = [[Button.inline("📅 Добавить в Google Календарь", data=f"evreg:cal:{job_id}".encode("utf-8"))]]
        await status_msg.edit(done_txt, parse_mode="html", buttons=btns, link_preview=False)
    else:
        err = res.get("error") or "Неизвестная ошибка отправки"
        await asyncio.to_thread(update_event_registration, job_id, status="failed", error_message=err)
        fail_txt = (
            f"❌ <b>Не удалось автоматически отправить форму на «{html.escape(title)}»:</b>\n"
            f"<code>{html.escape(err[:350])}</code>\n\n"
            f"🔗 Но все поля уже заполнены по этой ссылке — можешь нажать «Отправить» за 1 клик:\n"
            f"<a href=\"{html.escape(prefilled_url, quote=True)}\">Открыть предзаполненную Google Form</a>"
        )
        btns = []
        if len(prefilled_url) <= 1800:
            btns.append([Button.url("🔗 Открыть и отправить вручную", prefilled_url)])
        await status_msg.edit(fail_txt, parse_mode="html", buttons=btns or None, link_preview=False)

    await _advance_user_queue(client, uid, user_states)


async def execute_cancellation_and_notify(client, job: dict, user_states: dict, status_msg=None, fallback_event=None):
    """Шаг 5 (отмена): Отменяет заявку и переходит к следующей в очереди."""
    job_id = job["job_id"]
    uid = int(job["uid"])
    title = job.get("event_title") or "Мероприятие DGIST"
    user_states.pop(uid, None)
    await asyncio.to_thread(update_event_registration, job_id, status="cancelled")

    cancel_txt = f"❌ Операция отменена — не записываю вас на <b>{html.escape(title)}</b>."
    if status_msg:
        try:
            await status_msg.edit(cancel_txt, parse_mode="html", buttons=None)
        except Exception:
            await client.send_message(job["chat_id"], cancel_txt, parse_mode="html")
    elif fallback_event:
        await fallback_event.reply(cancel_txt, parse_mode="html")
    else:
        await client.send_message(job["chat_id"], cancel_txt, parse_mode="html")

    await _advance_user_queue(client, uid, user_states)


# ─────────────────────────────────────────────────────────────────────────────
# Ручной запуск: /dinner, /register, поиск по номеру поста или ссылке
# ─────────────────────────────────────────────────────────────────────────────

def _load_cached_portal_posts(uid: int) -> list[dict]:
    try:
        from dgist_accounts import load_dgist_seen_ids
        seen = load_dgist_seen_ids(uid)
        if isinstance(seen, dict):
            return list(seen.get("__recent_posts__") or [])
    except Exception:
        pass
    return []


async def scan_and_register_dinner_events(client, event, uid: int, user_states: dict):
    """Сканирует последние объявления DGIST на предмет лекций с бесплатным ужином и запускает подачу."""
    if not _is_owner(uid):
        await event.reply("⛔ Автоматический поиск лекций на портале DGIST доступен только владельцу бота.")
        return

    st = await event.reply("🔍 Ищу на портале DGIST лекции с бесплатным ужином и ссылками на регистрацию...")
    posts = await asyncio.to_thread(_load_cached_portal_posts, uid)
    if not posts:
        await st.edit("📭 Кэш объявлений DGIST пока пуст. Сначала запусти /checkportal, и я сразу проверю все новые лекции!")
        return

    dinner_open = []
    dinner_closed = []
    dinner_nolink = []

    for idx, p in enumerate(posts, 1):
        info = await asyncio.to_thread(analyze_post_for_dinner_sync, p)
        if not info.get("is_free_dinner_event"):
            continue
        reg_url = (info.get("registration_url") or "").strip()
        if not reg_url:
            urls = extract_registration_urls_from_post(p)
            reg_url = urls[0] if urls else ""
        if not reg_url:
            dinner_nolink.append((idx, p, info))
            continue
        if is_google_form_url(reg_url):
            try:
                sch = await asyncio.to_thread(parse_google_form, reg_url)
                if not sch.get("ok") and sch.get("status") == "closed":
                    dinner_closed.append((idx, p, info, reg_url))
                    continue
            except Exception:
                pass
        dinner_open.append((idx, p, info, reg_url))

    if not dinner_open:
        lines = ["🍽 <b>Открытых форм регистрации на лекции с бесплатным ужином среди последних объявлений не нашлось.</b>\n"]
        if dinner_nolink:
            lines.append("🚶 <b>Лекции с бесплатным ужином без онлайн-формы (вход на месте):</b>")
            for idx, p, info in dinner_nolink:
                lines.append(
                    f"• <b>#{idx}</b> {html.escape(info['event_title'])} — "
                    f"{html.escape(info['event_datetime'] or p.get('date', ''))}, {html.escape(info['event_venue'] or 'DGIST')}"
                )
        if dinner_closed:
            lines.append("\n🔒 <b>У этих лекций с ужином регистрация уже закрыта:</b>")
            for idx, p, info, _ in dinner_closed:
                lines.append(f"• <b>#{idx}</b> {html.escape(info['event_title'])} ({html.escape(info['event_datetime'] or '')})")
        lines.append("\n💡 <i>Когда на портале появится новая лекция с ужином — я сама поймаю её и сразу предложу записаться! Также можешь кинуть любую ссылку Google Forms командой /register &lt;ссылка&gt;.</i>")
        await st.edit("\n".join(lines), parse_mode="html", link_preview=False)
        return

    # Запускаем первую открытую лекцию прямо сейчас, остальные кладём в очередь
    first_idx, first_p, first_info, first_url = dinner_open[0]
    for _, p_q, info_q, url_q in dinner_open[1:]:
        await asyncio.to_thread(
            create_event_registration,
            uid=uid,
            chat_id=event.chat_id,
            form_url=url_q,
            post_id=str(p_q.get("post_id") or "") or None,
            post_url=p_q.get("post_url") or "",
            event_title=info_q.get("event_title") or p_q.get("title") or "",
            event_description=info_q.get("event_short_desc") or "",
            event_datetime=info_q.get("event_datetime") or "",
            event_venue=info_q.get("event_venue") or "",
            status="queued",
        )

    await st.edit(
        f"🍽 Нашла открытую лекцию с бесплатным ужином (#{first_idx})! Захожу в Google Forms и подготавливаю поля..."
    )
    await start_event_registration_flow(
        client=client,
        chat_id=event.chat_id,
        uid=uid,
        user_states=user_states,
        form_url=first_url,
        event_info=first_info,
        post_id=str(first_p.get("post_id") or "") or None,
        post_url=first_p.get("post_url") or "",
        post_text=first_p.get("full_text") or "",
        reply_to=event.id,
        status_msg=st,
        is_auto=False,
    )


async def register_from_post_ref_or_url(
    client,
    event,
    uid: int,
    user_states: dict,
    post_ref: str = "",
    form_url: str = "",
):
    """Запускает автозаполнение формы по прямой ссылке Google Forms ИЛИ по номеру/названию поста из журнала DGIST."""
    clean_url = (form_url or "").strip()
    if not clean_url and post_ref:
        m_url = re.search(r"https?://[^\s<>\"')\]}]+", post_ref)
        if m_url:
            clean_url = m_url.group(0).rstrip(".,;:!?")

    # 1. Если передана прямая ссылка — сразу открываем её
    if clean_url:
        st = await event.reply("🔍 Открываю форму регистрации и анализирую поля...")
        await start_event_registration_flow(
            client=client,
            chat_id=event.chat_id,
            uid=uid,
            user_states=user_states,
            form_url=clean_url,
            event_info={},
            reply_to=event.id,
            status_msg=st,
            is_auto=False,
        )
        return

    # 2. Иначе ищем пост в сохранённых постах портала DGIST (по номеру #N или ключевым словам)
    if not _is_owner(uid):
        await event.reply("Пришли ссылку на Google Forms (например: `/register https://forms.gle/...`).")
        return

    posts = await asyncio.to_thread(_load_cached_portal_posts, uid)
    if not posts:
        await event.reply("📭 Список постов DGIST пуст. Сначала запусти /checkportal или пришли прямую ссылку на Google Forms!")
        return

    target_post = None
    ref = (post_ref or "").strip()
    m_num = re.search(r"(?:#|№|пост\s*|номер\s*|новость\s*)?(\d{1,2})\b", ref, re.I)
    if m_num:
        idx = int(m_num.group(1))
        if 1 <= idx <= len(posts):
            target_post = posts[idx - 1]

    if not target_post and ref:
        ref_low = ref.lower()
        for p in posts:
            hay = ((p.get("title") or "") + " " + (p.get("summary") or "") + " " + (p.get("full_text") or "")).lower()
            if ref_low in hay:
                target_post = p
                break
        if not target_post:
            tokens = [t for t in re.split(r"\W+", ref_low) if len(t) >= 3]
            if tokens:
                best_score, best_p = 0, None
                for p in posts:
                    hay = ((p.get("title") or "") + " " + (p.get("summary") or "")).lower()
                    score = sum(1 for t in tokens if t in hay)
                    if score > best_score:
                        best_score, best_p = score, p
                if best_score > 0:
                    target_post = best_p

    if not target_post:
        await event.reply(
            "🤔 Не смогла понять, на какой именно пост записаться.\n"
            "Укажи номер поста из журнала (например: `/register #24` или *«запиши меня на пост #24»*), "
            "скинь ссылку на Google Forms, или нажми /dinner для поиска лекций с бесплатным ужином!"
        )
        return

    urls = extract_registration_urls_from_post(target_post)
    if not urls:
        await event.reply(
            f"😕 В посте «<b>{html.escape(target_post.get('title', ''))}</b>» не найдена ссылка на форму регистрации.",
            parse_mode="html",
        )
        return

    st = await event.reply("🔍 Нашла ссылку в посте! Открываю форму и заполняю твои данные...")
    info = await asyncio.to_thread(analyze_post_for_dinner_sync, target_post)
    await start_event_registration_flow(
        client=client,
        chat_id=event.chat_id,
        uid=uid,
        user_states=user_states,
        form_url=urls[0],
        event_info=info,
        post_id=str(target_post.get("post_id") or "") or None,
        post_url=target_post.get("post_url") or "",
        post_text=target_post.get("full_text") or "",
        reply_to=event.id,
        status_msg=st,
        is_auto=False,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Обработка диалоговых шагов (текст + голосовые) и inline-кнопок
# ─────────────────────────────────────────────────────────────────────────────

async def handle_event_reg_conversation_step(client, event, uid: int, state, user_states: dict) -> bool:
    """
    Обрабатывает ответы пользователя (текст ИЛИ голос) на этапах:
    - waiting_for_event_form_info (Шаг 2: пользователь отвечает на недостающие поля)
    - waiting_for_event_approval (Шаг 4: пользователь текстом/голосом говорит «одобряю» / «не нужно записываться» / просит правку)
    - waiting_for_event_edit (пользователь присылает правку поля)
    - waiting_for_profile_edit (пользователь обновляет /myprofile)
    Также восстанавливает активную сессию из БД, если сервер перезапускался (state is None).
    """
    # Проверяем восстановление из БД после рестарта Render, если RAM user_states пуст
    if state is None:
        db_active = await asyncio.to_thread(get_active_registration_for_user, uid)
        if db_active:
            if db_active["status"] == "waiting_info":
                state = ("waiting_for_event_form_info", db_active["job_id"])
                user_states[uid] = state
            elif db_active["status"] == "awaiting_approval":
                state = ("waiting_for_event_approval", db_active["job_id"])
                user_states[uid] = state

    if not state:
        return False

    is_profile_edit = (state == "waiting_for_profile_edit")
    is_event_tuple = (
        isinstance(state, tuple)
        and len(state) >= 2
        and state[0] in ("waiting_for_event_form_info", "waiting_for_event_approval", "waiting_for_event_edit")
    )
    if not is_profile_edit and not is_event_tuple:
        return False

    # Извлекаем текст (поддержка и текста, и голосовых сообщений!)
    user_txt = ""
    if event.text:
        user_txt = event.text.strip()
    elif event.message and event.message.voice:
        try:
            from llm import transcribe_voice
            vb = await event.message.download_media(file=bytes)
            if vb:
                user_txt = (await transcribe_voice(vb)).strip()
        except Exception as ex:
            print(f"[EVREG] voice transcribe error: {ex}")

    if not user_txt:
        return False

    # 1. Редактирование профиля /myprofile
    if is_profile_edit:
        user_states.pop(uid, None)
        cur_prof = await asyncio.to_thread(load_user_profile, uid)
        updates = await asyncio.to_thread(parse_freeform_profile_sync, user_txt, cur_prof)
        if not updates:
            await event.reply("🤔 Не удалось распознать данные профиля. Напиши, например: `Имя Altair Murat, 학번 2024..., телефон 010-...`")
            return True
        new_prof = await asyncio.to_thread(merge_user_profile, uid, updates)
        await event.reply(
            "✅ <b>Профиль обновлён!</b>\n\n" + format_user_profile_text(new_prof),
            parse_mode="html",
        )
        return True

    mode, job_id = state[0], state[1]
    job = await asyncio.to_thread(get_event_registration, job_id)
    if not job or job.get("status") not in ("waiting_info", "awaiting_approval"):
        user_states.pop(uid, None)
        return False

    # 2. Шаг 2: Пользователь прислал недостающие данные для формы
    if mode == "waiting_for_event_form_info":
        st = await event.reply("⏳ Заполняю поля формы твоими данными...")
        profile = await asyncio.to_thread(load_user_profile, uid)
        schema = job.get("form_schema") or {}
        fields = schema.get("fields") or []
        missing = job.get("missing_fields") or []

        ext = await asyncio.to_thread(extract_user_reply_sync, missing, fields, user_txt, profile)
        if ext.get("is_cancel"):
            await execute_cancellation_and_notify(client, job, user_states, status_msg=st)
            return True

        # Сохраняем постоянные личные данные в профиль пользователя навсегда!
        prof_updates = ext.get("profile_updates") or {}
        if prof_updates:
            profile = await asyncio.to_thread(merge_user_profile, uid, prof_updates)

        # Объединяем уже заполненные поля с новыми ответами и перепроверяем форму
        merged_answers = dict(job.get("filled_answers") or {})
        merged_answers.update(ext.get("extracted_answers") or {})

        event_info = {
            "event_title": job.get("event_title") or "",
            "event_datetime": job.get("event_datetime") or "",
            "event_venue": job.get("event_venue") or "",
            "event_description": job.get("event_description") or "",
        }
        plan = await asyncio.to_thread(plan_form_answers_sync, event_info, schema, profile, merged_answers)
        filled_answers = plan["filled_answers"]
        still_missing = plan["missing_fields"]
        prefilled_url = build_prefilled_google_form_url(schema.get("viewform_url") or job["form_url"], fields, filled_answers)

        updated = await asyncio.to_thread(
            update_event_registration,
            job_id,
            filled_answers=filled_answers,
            missing_fields=still_missing,
            prefilled_url=prefilled_url,
            status="waiting_info" if still_missing else "awaiting_approval",
        )

        if still_missing:
            user_states[uid] = ("waiting_for_event_form_info", job_id)
            txt, btns = build_missing_info_message(updated)
            await st.edit("👍 Часть данных записала! Осталось уточнить:\n\n" + txt, parse_mode="html", buttons=btns, link_preview=False)
        else:
            # Все данные собраны -> Шаг 3: отправляем на проверку!
            user_states[uid] = ("waiting_for_event_approval", job_id)
            txt, btns = build_review_message(updated)
            await st.edit(txt, parse_mode="html", buttons=btns, link_preview=False)
        return True

    # 3. Шаг 4: Ожидание решения (одобрить / не нужно записываться / изменить поле)
    if mode in ("waiting_for_event_approval", "waiting_for_event_edit"):
        schema = job.get("form_schema") or {}
        current_answers = job.get("filled_answers") or {}

        dec = await asyncio.to_thread(classify_approval_or_edit_sync, user_txt, schema, current_answers)
        action = dec.get("decision")

        if action == "approve":
            await execute_submission_and_notify(client, job, user_states, fallback_event=event)
            return True

        if action == "reject":
            await execute_cancellation_and_notify(client, job, user_states, fallback_event=event)
            return True

        if action == "edit" or mode == "waiting_for_event_edit":
            edits = dec.get("edited_fields") or {}
            prof_upd = dec.get("profile_updates") or {}
            if prof_upd:
                await asyncio.to_thread(merge_user_profile, uid, prof_upd)
            if edits:
                fields = schema.get("fields") or []
                merged = dict(current_answers)
                merged.update(edits)
                validated, _ = _normalize_and_validate_answers(fields, merged, [])
                prefilled_url = build_prefilled_google_form_url(schema.get("viewform_url") or job["form_url"], fields, validated)
                updated = await asyncio.to_thread(
                    update_event_registration,
                    job_id,
                    filled_answers=validated,
                    prefilled_url=prefilled_url,
                    status="awaiting_approval",
                )
                user_states[uid] = ("waiting_for_event_approval", job_id)
                txt, btns = build_review_message(updated)
                await event.reply("✏️ Поля обновлены!\n\n" + txt, parse_mode="html", buttons=btns, link_preview=False)
                return True
            if mode == "waiting_for_event_edit":
                await event.reply("🤔 Не поняла, какое поле изменить. Напиши, например: `поменяй телефон на 010-1234-5678` (или `/cancel`).")
                return True

        # Если пользователь написал что-то совершенно на другую тему во время ожидания подтверждения —
        # НЕ блокируем обычный чат; заявка остаётся доступной по inline-кнопкам!
        return False

    return False


def register_event_handlers(client, user_states: dict):
    """Регистрирует команды /dinner, /register, /myprofile и callback-кнопки evreg:*."""

    @client.on(events.NewMessage(pattern=r'(?i)^/(dinner|freedinner|lectures)(@\w+)?(\s|$)'))
    async def dinner_cmd(e):
        await scan_and_register_dinner_events(client, e, e.sender_id, user_states)

    @client.on(events.NewMessage(pattern=r'(?i)^/(register|apply|reg)(@\w+)?(?:\s+([\s\S]*))?$'))
    async def register_cmd(e):
        arg = (e.pattern_match.group(3) or "").strip()
        if not arg:
            await e.reply(
                "📝 <b>Автозаполнение и подача заявок на лекции</b>\n\n"
                "• <code>/dinner</code> — найти лекции с бесплатным ужином на портале DGIST и подготовить заявку\n"
                "• <code>/register #24</code> — подать заявку на конкретный пост из журнала портала\n"
                "• <code>/register https://forms.gle/...</code> — заполнить любую Google-форму\n"
                "• <code>/myprofile</code> — посмотреть или изменить сохранённые личные данные",
                parse_mode="html",
            )
            return
        await register_from_post_ref_or_url(client, e, e.sender_id, user_states, post_ref=arg)

    @client.on(events.NewMessage(pattern=r'(?i)^/(myprofile|profile)(@\w+)?(?:\s+([\s\S]*))?$'))
    async def myprofile_cmd(e):
        uid = e.sender_id
        arg = (e.pattern_match.group(3) or "").strip()
        if arg:
            cur = await asyncio.to_thread(load_user_profile, uid)
            upd = await asyncio.to_thread(parse_freeform_profile_sync, arg, cur)
            if upd:
                new_p = await asyncio.to_thread(merge_user_profile, uid, upd)
                await e.reply("✅ <b>Профиль обновлён!</b>\n\n" + format_user_profile_text(new_p), parse_mode="html")
                return
        prof = await asyncio.to_thread(load_user_profile, uid)
        btns = [
            [Button.inline("✏️ Добавить / изменить данные", data=b"evreg:profedit:1")],
            [Button.inline("🗑 Очистить профиль", data=b"evreg:profclear:1")],
        ]
        await e.reply(format_user_profile_text(prof), parse_mode="html", buttons=btns)

    @client.on(events.CallbackQuery(pattern=rb"^evreg:"))
    async def on_evreg_callback(event):
        data = event.data.decode("utf-8", errors="ignore")
        parts = data.split(":")
        if len(parts) < 3:
            return
        _, action, arg = parts[0], parts[1], parts[2]
        uid = event.sender_id

        # Профиль
        if action == "profedit":
            user_states[uid] = "waiting_for_profile_edit"
            await event.answer()
            await event.respond(
                "✏️ Пришли свои данные одним сообщением (или голосом) — например:\n"
                "<i>Имя: Altair Murat, 학번: 2024..., телефон: 010-..., email: ...@dgist.ac.kr, "
                "факультет: School of Undergraduate Studies, студент (학부생), аллергий нет</i>\n\n"
                "(или /cancel для отмены)",
                parse_mode="html",
            )
            return

        if action == "profclear":
            await asyncio.to_thread(save_user_profile, uid, {})
            await event.answer("Профиль очищен")
            await event.edit("🗑 Профиль для автозаполнения очищен.", buttons=None)
            return

        # Быстрая кнопка «📝 Записаться» прямо из журнала портала DGIST
        if action == "postreg":
            if not _is_owner(uid):
                return await event.answer("⛔ Доступно только владельцу бота.", alert=True)
            post_id = arg
            posts = await asyncio.to_thread(_load_cached_portal_posts, uid)
            target_post = next((p for p in posts if str(p.get("post_id")) == str(post_id)), None)
            if not target_post:
                return await event.answer("Пост устарел — обнови журнал командой /checkportal", alert=True)
            urls = extract_registration_urls_from_post(target_post)
            if not urls:
                return await event.answer("В этом посте не найдена ссылка для регистрации.", alert=True)
            await event.answer("Открываю форму...")
            st = await event.respond("🔍 Захожу в форму регистрации и подготавливаю твои данные...")
            info = await asyncio.to_thread(analyze_post_for_dinner_sync, target_post)
            await start_event_registration_flow(
                client=client,
                chat_id=event.chat_id,
                uid=uid,
                user_states=user_states,
                form_url=urls[0],
                event_info=info,
                post_id=str(target_post.get("post_id") or "") or None,
                post_url=target_post.get("post_url") or "",
                post_text=target_post.get("full_text") or "",
                status_msg=st,
                is_auto=False,
            )
            return

        # Действия с конкретной заявкой job_id
        job = await asyncio.to_thread(get_event_registration, arg)
        if not job:
            return await event.answer("⚠️ Заявка не найдена или устарела.", alert=True)
        if int(job["uid"]) != int(uid):
            return await event.answer("⛔ Это заявка другого пользователя.", alert=True)

        if action == "approve":
            if job["status"] == "submitted":
                return await event.answer("Ты уже записан на это мероприятие!", alert=True)
            if job["status"] == "cancelled":
                return await event.answer("Эта заявка уже была отменена.", alert=True)
            await event.answer("Отправляю форму...")
            await execute_submission_and_notify(client, job, user_states, status_msg=event)
            return

        if action == "cancel":
            if job["status"] == "submitted":
                return await event.answer("Заявка уже была отправлена ранее.", alert=True)
            await event.answer("Запись отменена")
            await execute_cancellation_and_notify(client, job, user_states, status_msg=event)
            return

        if action == "edit":
            if job["status"] != "awaiting_approval":
                return await event.answer("Редактирование доступно только до отправки.", alert=True)
            user_states[uid] = ("waiting_for_event_edit", job["job_id"])
            await event.answer()
            await event.respond(
                "✏️ Напиши (или скажи голосом), какое поле нужно изменить и на какое значение.\n"
                "Например: <i>«поменяй телефон на 010-9999-8888»</i> или <i>«укажи статус 대학원생»</i>.",
                parse_mode="html",
            )
            return

        if action == "cal":
            await event.answer("Добавляю в Google Календарь...")
            try:
                from main import load_user_creds
                from google_calendar import create_calendar_event, schedule_telegram_reminder
                from timezone_helper import get_user_now
                creds = await asyncio.to_thread(load_user_creds, uid)
                if not creds:
                    await event.respond("📅 Сначала подключи Google Календарь через /connectgoogle.")
                    return
                now_dt, tz_name, tz_offset = get_user_now(uid)
                now_str = now_dt.strftime(f"%Y-%m-%dT%H:%M:%S{tz_offset} (%A)")
                start_iso, end_iso = await asyncio.to_thread(
                    parse_event_datetime_to_iso_sync,
                    job.get("event_datetime") or "",
                    now_str,
                )
                if not start_iso:
                    await event.respond("⚠️ Не удалось точно распознать дату/время для календаря — добавь вручную фразой «запиши в календарь ...».")
                    return
                desc = f"Место: {job.get('event_venue', '')}\n{job.get('event_description', '')}\nФорма: {job.get('form_url', '')}"
                created = await asyncio.to_thread(
                    create_calendar_event,
                    creds,
                    job.get("event_title") or "Лекция DGIST",
                    start_iso,
                    end_iso,
                    desc,
                    False,
                    [30],
                    tz_name,
                )
                schedule_telegram_reminder(
                    telegram_user_id=uid,
                    title=f"{job.get('event_title', 'Лекция DGIST')} ({job.get('event_venue', '')})",
                    start_iso=start_iso,
                    is_important=False,
                    event_id=created.get("id"),
                    minutes_before=30,
                    tz_name=tz_name,
                )
                ev_link = created.get("htmlLink", "")
                await event.respond(
                    f"📅 Добавила «<b>{html.escape(job.get('event_title', ''))}</b>» в Google Календарь и поставила напоминание за 30 минут!"
                    + (f"\n🔗 <a href=\"{html.escape(ev_link, quote=True)}\">Открыть событие</a>" if ev_link else ""),
                    parse_mode="html",
                    link_preview=False,
                )
            except Exception as ex:
                await event.respond(f"❌ Не удалось добавить в календарь: {ex}")
            return
