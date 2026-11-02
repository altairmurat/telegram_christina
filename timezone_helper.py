import re
import datetime
from zoneinfo import ZoneInfo
from database import SessionLocal
import models

DEFAULT_TIMEZONE = "Asia/Seoul"

COMMON_TIMEZONES = {
    "seoul": "Asia/Seoul",
    "корея": "Asia/Seoul",
    "сеул": "Asia/Seoul",
    "тэгу": "Asia/Seoul",
    "daegu": "Asia/Seoul",
    "almaty": "Asia/Almaty",
    "алматы": "Asia/Almaty",
    "астана": "Asia/Almaty",
    "astana": "Asia/Almaty",
    "казахстан": "Asia/Almaty",
    "kazakhstan": "Asia/Almaty",
    "tashkent": "Asia/Tashkent",
    "ташкент": "Asia/Tashkent",
    "узбекистан": "Asia/Tashkent",
    "bishkek": "Asia/Bishkek",
    "бишкек": "Asia/Bishkek",
    "кыргызстан": "Asia/Bishkek",
    "moscow": "Europe/Moscow",
    "москва": "Europe/Moscow",
    "питер": "Europe/Moscow",
    "спб": "Europe/Moscow",
    "россия": "Europe/Moscow",
    "kyiv": "Europe/Kyiv",
    "киев": "Europe/Kyiv",
    "украина": "Europe/Kyiv",
    "minsk": "Europe/Minsk",
    "минск": "Europe/Minsk",
    "беларусь": "Europe/Minsk",
    "baku": "Asia/Baku",
    "баку": "Asia/Baku",
    "азербайджан": "Asia/Baku",
    "tbilisi": "Asia/Tbilisi",
    "тбилиси": "Asia/Tbilisi",
    "грузия": "Asia/Tbilisi",
    "yerevan": "Asia/Yerevan",
    "ереван": "Asia/Yerevan",
    "армения": "Asia/Yerevan",
    "london": "Europe/London",
    "лондон": "Europe/London",
    "berlin": "Europe/Berlin",
    "берлин": "Europe/Berlin",
    "варшава": "Europe/Warsaw",
    "warsaw": "Europe/Warsaw",
    "прага": "Europe/Prague",
    "prague": "Europe/Prague",
    "new york": "America/New_York",
    "нью-йорк": "America/New_York",
    "los angeles": "America/Los_Angeles",
    "лос-анджелес": "America/Los_Angeles",
    "chicago": "America/Chicago",
    "чикаго": "America/Chicago",
    "tokyo": "Asia/Tokyo",
    "токио": "Asia/Tokyo",
    "япония": "Asia/Tokyo",
    "beijing": "Asia/Shanghai",
    "пекин": "Asia/Shanghai",
    "китай": "Asia/Shanghai",
    "utc": "UTC",
    "gmt": "UTC",
}

_tz_cache: dict[int, str] = {}


def resolve_timezone_name(raw: str | None) -> str | None:
    if not raw:
        return None
    s = raw.strip()
    low = s.lower().replace("_", " ").replace("-", " ")
    if low in COMMON_TIMEZONES:
        return COMMON_TIMEZONES[low]

    # Проверяем напрямую IANA-имя (например Asia/Almaty, Europe/Moscow)
    try:
        ZoneInfo(s)
        return s
    except Exception:
        pass

    # Проверяем формат смещения: +5, -4, UTC+5, GMT+3, +05:00
    m = re.match(r"^(?:utc|gmt)?\s*([+-]?\d{1,2})(?::?(\d{2}))?$", s, re.IGNORECASE)
    if m:
        hrs = int(m.group(1))
        offset_presets = {
            9: "Asia/Seoul",
            5: "Asia/Almaty",
            6: "Asia/Bishkek",
            3: "Europe/Moscow",
            4: "Asia/Tbilisi",
            2: "Europe/Kyiv",
            1: "Europe/Berlin",
            0: "UTC",
            -5: "America/New_York",
            -6: "America/Chicago",
            -8: "America/Los_Angeles",
        }
        if hrs in offset_presets:
            return offset_presets[hrs]
        if -12 <= hrs <= 14:
            etc_sign = "-" if hrs > 0 else "+"
            return f"Etc/GMT{etc_sign}{abs(hrs)}"
    return None


def get_user_timezone(uid: int | None) -> str:
    if not uid:
        return DEFAULT_TIMEZONE
    if uid in _tz_cache:
        return _tz_cache[uid]

    try:
        with SessionLocal() as db:
            row = db.query(models.UserSetting).filter_by(telegram_user_id=uid).first()
            if row and row.timezone:
                valid = resolve_timezone_name(row.timezone)
                if valid:
                    _tz_cache[uid] = valid
                    return valid
    except Exception:
        pass

    _tz_cache[uid] = DEFAULT_TIMEZONE
    return DEFAULT_TIMEZONE


def set_user_timezone(uid: int, tz_name: str) -> str:
    valid = resolve_timezone_name(tz_name) or DEFAULT_TIMEZONE
    _tz_cache[uid] = valid
    try:
        with SessionLocal() as db:
            row = db.query(models.UserSetting).filter_by(telegram_user_id=uid).first()
            if not row:
                row = models.UserSetting(telegram_user_id=uid, timezone=valid)
                db.add(row)
            else:
                row.timezone = valid
                row.updated_at = datetime.datetime.utcnow()
            db.commit()
    except Exception as ex:
        print(f"[TIMEZONE] Error saving timezone for {uid}: {ex}")
    return valid


def sync_google_calendar_timezone(uid: int, creds) -> str | None:
    """Пытается определить часовой пояс из основного календаря Google Calendar."""
    if not creds:
        return None
    try:
        from googleapiclient.discovery import build
        service = build("calendar", "v3", credentials=creds)
        cal = service.calendars().get(calendarId="primary").execute()
        tz = cal.get("timeZone")
        if tz:
            valid = resolve_timezone_name(tz)
            if valid:
                set_user_timezone(uid, valid)
                return valid
    except Exception as ex:
        print(f"[TIMEZONE] Failed to sync timezone from Google Calendar for {uid}: {ex}")
    return None


def get_user_now(uid: int | None) -> tuple[datetime.datetime, str, str]:
    """Возвращает (now_dt, tz_name, offset_str), например (datetime, 'Asia/Almaty', '+05:00')."""
    tz_name = get_user_timezone(uid)
    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        tz_name = DEFAULT_TIMEZONE
        tz = ZoneInfo(DEFAULT_TIMEZONE)
    now_dt = datetime.datetime.now(tz)
    offset_str = now_dt.strftime("%z")
    if offset_str and len(offset_str) == 5:
        offset_str = f"{offset_str[:3]}:{offset_str[3:]}"
    return now_dt, tz_name, offset_str or "+09:00"
