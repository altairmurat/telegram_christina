import os
import re
import time
import base64
import datetime
import requests
from sqlalchemy import or_
from database import SessionLocal
import models
from env import FAL_KEY, OWNER_ID

# Каталог доступных видео-моделей на fal.ai
# Для обычных пользователей ВСЕГДА принудительно используется "minimax_turbo" и duration=1 (480P).
# Админы могут выбирать любую модель и длительность (1..10 сек).
VIDEO_MODELS = {
    "minimax_turbo": {
        "title": "⚡ Minimax Turbo (Слабая / Эконом)",
        "short": "⚡ Minimax Turbo",
        "t2v": "minimax/h3-max-turbo/text-to-video",
        "i2v": "minimax/h3-max-turbo/image-to-video",
        "durations": [1, 2, 3, 5, 8, 10],
        "price_hint": "~$0.016/сек",
    },
    "grok_lite": {
        "title": "🚀 Grok Video Lite (Быстрая)",
        "short": "🚀 Grok Lite",
        "t2v": "xai/grok-imagine-video/v1.5/lite/text-to-video",
        "i2v": "xai/grok-imagine-video/v1.5/lite/image-to-video",
        "durations": [1, 2, 3, 5, 8, 10],
        "price_hint": "~$0.02/сек",
    },
    "pixverse_v6": {
        "title": "🎨 PixVerse v6 (Креативная)",
        "short": "🎨 PixVerse v6",
        "t2v": "fal-ai/pixverse/v6/text-to-video",
        "i2v": "fal-ai/pixverse/v6/image-to-video",
        "durations": [1, 2, 3, 5, 8, 10],
        "price_hint": "~$0.025/сек",
    },
    "minimax_max": {
        "title": "🔥 Minimax H3 Max (Высокое качество)",
        "short": "🔥 Minimax Max",
        "t2v": "minimax/h3-max/text-to-video",
        "i2v": "minimax/h3-max/image-to-video",
        "durations": [1, 2, 3, 5, 8, 10],
        "price_hint": "~$0.033/сек",
    },
    "kling_pro": {
        "title": "👑 Kling 2.5 Turbo Pro (Кино-топ)",
        "short": "👑 Kling 2.5 Pro",
        "t2v": "fal-ai/kling-video/v2.5-turbo/pro/text-to-video",
        "i2v": "fal-ai/kling-video/v2.5-turbo/pro/image-to-video",
        "durations": [5, 10],
        "price_hint": "~$0.07/сек (только 5с / 10с)",
    },
}

DEFAULT_MODEL_KEY = "minimax_turbo"
DEFAULT_DURATION_SEC = 1

# Память последних настроек каждого админа в рамках работы бота: {user_id: {"model": str, "duration": int}}
admin_video_prefs: dict[int, dict] = {}

# Защита от одновременного запуска нескольких генераций одним пользователем
_active_video_generations: set[int] = set()

# Жёсткий стоп-лист корней и слов (1-й уровень фильтрации до обращения к API)
_BANNED_PATTERNS = [
    # 18+ / NSFW / Порнография / Обнажёнка
    r"порн", r"секс", r"эрот", r"интим", r"обнаж", r"\bгол(ый|ая|ые|ую|ого|ым)\b",
    r"сиськ", r"дойк", r"член", r"пенис", r"вагин", r"минет", r"орги[яий]", r"хента[йя]",
    r"шлюх", r"проститут", r"стриптиз", r"фетиш", r"бдсм", r"bdsm", r"извращ",
    r"\bporn", r"\bsex", r"\bnsfw\b", r"\bnude", r"\bnaked", r"\berotic", r"\bhentai",
    r"\bboobs?\b", r"\bbreasts?\b", r"\bpenis\b", r"\bvagina\b", r"\bdick\b", r"\bpussy\b",
    r"\bstripper", r"\bonlyfans\b", r"\bdeepfake\b", r"дипфейк",
    # Насилие / Кровь / Жесть / Убийства / Пытки / Расчленёнка
    r"расчлен", r"убийств", r"\bубить\b", r"\bубива", r"пытк", r"пытать", r"\bтруп",
    r"кишки", r"обезглав", r"отруба", r"отреза[тьл]", r"кровав", r"кроволи", r"лужа крови",
    r"изнасил", r"насили[ея]", r"казнь", r"казнит", r"повешен", r"повесить", r"застрел",
    r"зареза", r"поножовщ", r"истяза", r"живодер", r"увечь",
    r"\bgore\b", r"\bgory\b", r"\bmurder", r"\btorture", r"\bdecapitat", r"\bdismember",
    r"\bcorpse", r"\bdead body", r"\brape\b", r"\braping\b", r"\bslaughter", r"\bmutilat",
    r"\bexecution\b", r"\bbehead", r"\bmassacre", r"\bbloodbath",
    # Суицид / Селфхарм
    r"суицид", r"самоубий", r"вскрыть вены", r"селфхарм", r"порезать себя",
    r"\bsuicid", r"\bself[\s-]?harm", r"\bslit wrist",
    # Терроризм / Экстремизм / Нацизм / Стрельба
    r"террор", r"теракт", r"взрывчат", r"шахид", r"игил", r"свастик", r"гитлер",
    r"нацист", r"нацизм", r"фашист", r"холокост", r"колумбайн", r"скулшутинг",
    r"стрельба в школе", r"массовый расстрел", r"экстремиз", r"ку-клукс",
    r"\bterroris", r"\bisis\b", r"\bswastika", r"\bhitler", r"\bnazi", r"\bschool shooting",
    r"\bkkk\b", r"\bwhite supremac",
    # Несовершеннолетние / CSAM
    r"педофил", r"\bлоли\b", r"\bloli\b", r"\bshota\b", r"\bunderage", r"\bchild abuse",
    r"малолетк", r"детское порн",
    # Наркотики
    r"героин", r"мефедрон", r"кокаин", r"амфетамин", r"спайс", r"наркотик", r"передоз",
    r"употреблени[ея] нарко", r"\bheroin\b", r"\bcocaine\b", r"\bmethamphetamine\b", r"\bfentanyl\b",
    # Отвратительный / шок-контент
    r"копро", r"фекали", r"испражн", r"рвот[аы]", r"некрофил", r"зоофил",
    r"\bscat\b", r"\bnecrophil", r"\bzoophil", r"\bbestiality",
]
_COMPILED_BANNED = re.compile("|".join(_BANNED_PATTERNS), re.IGNORECASE)


def check_hard_safety_filter(text: str | None) -> tuple[bool, str | None]:
    """
    Быстрая проверка текста по чёрному списку опасных/запрещённых тем ДО вызова любых API.
    Возвращает (True, None), если текст чист, или (False, причина_отказа).
    """
    if not text:
        return True, None
    normalized = text.lower().replace("ё", "е")
    if _COMPILED_BANNED.search(normalized):
        return (
            False,
            "🛡️ **Запрос отклонён фильтром безопасности.**\n"
            "Генерация видео с насилием, жестокостью, 18+ / NSFW, экстремизмом или запрещёнными темами строго заблокирована.",
        )
    return True, None


def get_fal_key() -> str:
    return (os.getenv("FAL_KEY", "") or os.getenv("FAL_API_KEY", "") or FAL_KEY).strip()


def normalize_username(raw: str | None) -> str:
    if not raw:
        return ""
    s = raw.strip()
    s = re.sub(r"^https?://t\.me/", "", s, flags=re.IGNORECASE)
    s = s.lstrip("@").strip().lower()
    return s


def is_owner(user_id: int | None) -> bool:
    if not user_id:
        return False
    return int(user_id) == int(OWNER_ID)


def is_video_admin(user_id: int | None, username: str | None = None) -> bool:
    """Проверяет, есть ли у пользователя безлимитный доступ и права выбора модели/секунд."""
    if is_owner(user_id):
        return True

    uname = normalize_username(username)
    with SessionLocal() as db:
        filters = []
        if user_id:
            filters.append(models.VideoAdmin.telegram_user_id == int(user_id))
        if uname:
            filters.append(models.VideoAdmin.username == uname)
        if not filters:
            return False

        adm = db.query(models.VideoAdmin).filter(or_(*filters)).first()
        if adm:
            updated = False
            if user_id and not adm.telegram_user_id:
                adm.telegram_user_id = int(user_id)
                updated = True
            if uname and not adm.username:
                adm.username = uname
                updated = True
            if updated:
                try:
                    db.commit()
                except Exception:
                    db.rollback()
            return True
        return False


def get_admin_prefs(user_id: int) -> dict:
    prefs = admin_video_prefs.get(int(user_id))
    if not prefs:
        prefs = {"model": DEFAULT_MODEL_KEY, "duration": DEFAULT_DURATION_SEC}
        admin_video_prefs[int(user_id)] = prefs
    return prefs


def set_admin_prefs(user_id: int, model_key: str | None = None, duration: int | None = None) -> dict:
    prefs = get_admin_prefs(user_id)
    if model_key and model_key in VIDEO_MODELS:
        prefs["model"] = model_key
        allowed_durs = VIDEO_MODELS[model_key]["durations"]
        if prefs["duration"] not in allowed_durs:
            prefs["duration"] = allowed_durs[0]
    if duration is not None:
        allowed_durs = VIDEO_MODELS[prefs["model"]]["durations"]
        # Выбираем ближайшую поддерживаемую моделью длительность
        closest = min(allowed_durs, key=lambda d: abs(d - int(duration)))
        prefs["duration"] = closest
    return prefs


def extract_hints_from_text(text: str) -> tuple[str | None, int | None]:
    """Извлекает подсказки по модели или длительности (в секундах) из текста запроса админа."""
    low = (text or "").lower()
    model_hint = None
    if "kling" in low or "клинг" in low:
        model_hint = "kling_pro"
    elif "pixverse" in low or "пиксверс" in low:
        model_hint = "pixverse_v6"
    elif "grok" in low or "грок" in low:
        model_hint = "grok_lite"
    elif "max" in low or "макс" in low:
        model_hint = "minimax_max"
    elif "turbo" in low or "турбо" in low or "слаб" in low:
        model_hint = "minimax_turbo"

    dur_hint = None
    m = re.search(r"\b(\d{1,2})\s*(?:сек|секунд|секунды|с\b|sec|seconds|s\b)", low)
    if m:
        val = int(m.group(1))
        if 1 <= val <= 15:
            dur_hint = val
    return model_hint, dur_hint


def add_video_admins(raw_text: str) -> list[str]:
    """Добавляет один или несколько юзернеймов (или ID) в список безлимитных видео-админов."""
    tokens = re.split(r"[\s,;]+", (raw_text or "").strip())
    added = []
    with SessionLocal() as db:
        for tok in tokens:
            if not tok or tok.startswith("/"):
                continue
            clean = normalize_username(tok)
            if not clean:
                continue

            if clean.isdigit():
                uid = int(clean)
                exists = db.query(models.VideoAdmin).filter_by(telegram_user_id=uid).first()
                if not exists:
                    db.add(models.VideoAdmin(telegram_user_id=uid))
                    db.commit()
                added.append(f"ID:{uid}")
            else:
                exists = db.query(models.VideoAdmin).filter_by(username=clean).first()
                if not exists:
                    db.add(models.VideoAdmin(username=clean))
                    db.commit()
                added.append(f"@{clean}")
    return added


def remove_video_admins(raw_text: str) -> tuple[list[str], list[str]]:
    """Удаляет один или несколько юзернеймов (или ID) из списка безлимитных видео-админов."""
    tokens = re.split(r"[\s,;]+", (raw_text or "").strip())
    removed, not_found = [], []
    with SessionLocal() as db:
        for tok in tokens:
            if not tok or tok.startswith("/"):
                continue
            clean = normalize_username(tok)
            if not clean:
                continue

            if clean.isdigit():
                uid = int(clean)
                row = db.query(models.VideoAdmin).filter_by(telegram_user_id=uid).first()
                if row:
                    db.delete(row)
                    db.commit()
                    removed.append(f"ID:{uid}")
                else:
                    not_found.append(f"ID:{uid}")
            else:
                row = db.query(models.VideoAdmin).filter_by(username=clean).first()
                if row:
                    db.delete(row)
                    db.commit()
                    removed.append(f"@{clean}")
                else:
                    not_found.append(f"@{clean}")
    return removed, not_found


def list_video_admins() -> list[str]:
    """Возвращает список всех добавленных безлимитных админов."""
    result = [f"👑 Владелец (ID: `{OWNER_ID}`) — всегда безлимит"]
    with SessionLocal() as db:
        rows = db.query(models.VideoAdmin).order_by(models.VideoAdmin.added_at.asc()).all()
        for r in rows:
            if r.username and r.telegram_user_id:
                result.append(f"• @{r.username} (`{r.telegram_user_id}`)")
            elif r.username:
                result.append(f"• @{r.username}")
            elif r.telegram_user_id:
                result.append(f"• ID: `{r.telegram_user_id}`")
    return result


def check_video_quota(user_id: int, username: str | None = None) -> tuple[bool, str | None]:
    """
    Проверяет, может ли пользователь сейчас сгенерировать видео.
    Возвращает (True, None) если можно, иначе (False, текст_ошибки).
    """
    if user_id in _active_video_generations:
        return False, "⏳ Твоё видео уже генерируется прямо сейчас, подожди немного!"

    if is_video_admin(user_id, username):
        return True, None

    now = datetime.datetime.utcnow()
    cutoff = now - datetime.timedelta(hours=24)
    uname = normalize_username(username)

    with SessionLocal() as db:
        filters = [models.VideoGenerationLog.telegram_user_id == int(user_id)]
        if uname:
            filters.append(models.VideoGenerationLog.username == uname)

        last_log = (
            db.query(models.VideoGenerationLog)
            .filter(
                or_(*filters),
                models.VideoGenerationLog.created_at >= cutoff,
            )
            .order_by(models.VideoGenerationLog.created_at.desc())
            .first()
        )

        if last_log:
            next_allowed = last_log.created_at + datetime.timedelta(hours=24)
            rem_sec = max(60, int((next_allowed - now).total_seconds()))
            hours = rem_sec // 3600
            minutes = (rem_sec % 3600) // 60
            if hours > 0:
                time_str = f"{hours} ч. {minutes} мин."
            else:
                time_str = f"{minutes} мин."
            return (
                False,
                f"⛔ Лимит генерации видео: **1 видео в день** (раз в 24 часа).\n"
                f"⏳ Ты сможешь сгенерировать следующее видео через **{time_str}**.",
            )

    return True, None


def record_video_generation(user_id: int, username: str | None, prompt: str):
    """Записывает успешную генерацию видео в БД для отсчёта суточного лимита."""
    uname = normalize_username(username)
    with SessionLocal() as db:
        db.add(
            models.VideoGenerationLog(
                telegram_user_id=int(user_id),
                username=uname or None,
                prompt=(prompt or "")[:500],
                created_at=datetime.datetime.utcnow(),
            )
        )
        db.commit()


def mark_generation_started(user_id: int):
    _active_video_generations.add(int(user_id))


def mark_generation_finished(user_id: int):
    _active_video_generations.discard(int(user_id))


def _build_model_payload(model_key: str, prompt_en: str, duration_sec: int, data_uri: str | None) -> tuple[str, dict]:
    info = VIDEO_MODELS.get(model_key, VIDEO_MODELS[DEFAULT_MODEL_KEY])
    endpoint = info["i2v"] if data_uri else info["t2v"]
    dur = int(duration_sec)

    if model_key == "minimax_turbo":
        payload = {
            "prompt": prompt_en,
            "duration": dur,
            "resolution": "480P",
            "prompt_expansion_mode": "disabled",
            "enable_safety_checker": True,
        }
    elif model_key == "minimax_max":
        payload = {
            "prompt": prompt_en,
            "duration": dur,
            "resolution": "768P",
            "prompt_expansion_mode": "balanced",
            "enable_safety_checker": True,
        }
    elif model_key == "grok_lite":
        payload = {
            "prompt": prompt_en,
            "duration": dur,
            "resolution": "480p",
            "enable_safety_checker": True,
        }
    elif model_key == "pixverse_v6":
        payload = {
            "prompt": prompt_en,
            "duration": dur,
            "resolution": "540p" if dur > 1 else "360p",
            "enable_safety_checker": True,
        }
    elif model_key == "kling_pro":
        kling_dur = "10" if dur >= 8 else "5"
        payload = {
            "prompt": prompt_en,
            "duration": kling_dur,
            "enable_safety_checker": True,
        }
    else:
        payload = {
            "prompt": prompt_en,
            "duration": 1,
            "resolution": "480P",
            "enable_safety_checker": True,
        }

    if data_uri:
        payload["image_url"] = data_uri
    return endpoint, payload


def _run_fal_queue_job(model_id: str, payload: dict, headers: dict, max_wait_sec: int = 120) -> str:
    """
    Отправляет задачу в асинхронную очередь queue.fal.run и опрашивает статус каждые 2.5 сек,
    чтобы соединение никогда не зависало по ReadTimeout. Возвращает URL готового видео.
    """
    submit_url = f"https://queue.fal.run/{model_id}"
    sub_resp = requests.post(submit_url, headers=headers, json=payload, timeout=25)
    if sub_resp.status_code not in (200, 201, 202):
        raise RuntimeError(f"Ошибка запуска ({sub_resp.status_code}): {sub_resp.text[:300]}")

    sub_data = sub_resp.json()
    if isinstance(sub_data.get("video"), dict) and sub_data["video"].get("url"):
        return sub_data["video"]["url"]

    status_url = sub_data.get("status_url")
    response_url = sub_data.get("response_url")
    cancel_url = sub_data.get("cancel_url")
    if not status_url or not response_url:
        raise RuntimeError(f"Некорректный ответ очереди fal.ai: {str(sub_data)[:250]}")

    deadline = time.time() + max_wait_sec
    while time.time() < deadline:
        time.sleep(2.5)
        st_resp = requests.get(status_url, headers=headers, timeout=15)
        if st_resp.status_code not in (200, 202):
            continue
        st_json = st_resp.json()
        status = st_json.get("status", "")
        if status == "COMPLETED":
            res_resp = requests.get(response_url, headers=headers, timeout=30)
            if res_resp.status_code not in (200, 201):
                raise RuntimeError(f"Ошибка получения видео ({res_resp.status_code}): {res_resp.text[:300]}")
            res_data = res_resp.json()
            video_url = (res_data.get("video") or {}).get("url")
            if not video_url:
                raise RuntimeError("fal.ai не вернул ссылку на видео (возможно, сработал фильтр безопасности).")
            return video_url
        if status in ("FAILED", "ERROR", "CANCELLED"):
            raise RuntimeError(f"Генерация отклонена или завершилась ошибкой: {str(st_json)[:250]}")

    if cancel_url:
        try:
            requests.put(cancel_url, headers=headers, timeout=10)
        except Exception:
            pass
    raise TimeoutError(f"Модель {model_id} слишком долго в очереди (> {max_wait_sec} сек)")


def _generate_video_fal_sync(
    prompt_en: str,
    image_bytes: bytes | None = None,
    model_key: str = DEFAULT_MODEL_KEY,
    duration_sec: int = DEFAULT_DURATION_SEC,
    is_admin_req: bool = False,
) -> bytes:
    api_key = get_fal_key()
    if not api_key:
        raise RuntimeError("Ключ FAL_KEY не задан в настройках (.env / Render Environment).")

    # Жёсткая защита: если пользователь НЕ админ — принудительно только самая слабая модель и 1 секунда!
    if not is_admin_req:
        model_key = DEFAULT_MODEL_KEY
        duration_sec = DEFAULT_DURATION_SEC

    headers = {
        "Authorization": f"Key {api_key}",
        "Content-Type": "application/json",
    }

    clean_prompt = (prompt_en or "Cinematic smooth motion, high quality").strip()
    data_uri = None
    if image_bytes:
        b64_img = base64.b64encode(image_bytes).decode("ascii")
        data_uri = f"data:image/jpeg;base64,{b64_img}"

    endpoint, payload = _build_model_payload(model_key, clean_prompt, duration_sec, data_uri)
    wait_limit = 180 if (is_admin_req and (duration_sec > 2 or model_key in ("kling_pro", "minimax_max"))) else 80

    video_url = None
    try:
        video_url = _run_fal_queue_job(endpoint, payload, headers, max_wait_sec=wait_limit)
    except Exception as primary_err:
        # Авто-фоллбэк на резервную модель того же уровня длительности, если основная очередь перегружена
        fallback_key = "grok_lite" if model_key != "grok_lite" else "minimax_turbo"
        fallback_dur = duration_sec if is_admin_req else 1
        print(f"[FAL] Primary model ({endpoint}) error: {primary_err}. Falling back to {fallback_key} ({fallback_dur}s)")
        fb_endpoint, fb_payload = _build_model_payload(fallback_key, clean_prompt, fallback_dur, data_uri)
        video_url = _run_fal_queue_job(fb_endpoint, fb_payload, headers, max_wait_sec=100)

    vid_resp = requests.get(video_url, timeout=60)
    vid_resp.raise_for_status()
    return vid_resp.content
