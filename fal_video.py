import os
import re
import base64
import datetime
import requests
from sqlalchemy import or_
from database import SessionLocal
import models
from env import FAL_KEY, OWNER_ID

# Самая дешёвая и лёгкая видео-модель на fal.ai (~$0.02 за 1 сек видео)
FAL_T2V_ENDPOINT = "https://fal.run/fal-ai/ltxv-13b-098-distilled"
FAL_I2V_ENDPOINT = "https://fal.run/fal-ai/ltxv-13b-098-distilled/image-to-video"

# Жёсткие ограничения для генерации ровно 1-секундного видео в минимальном разрешении
STRICT_1S_PARAMS = {
    "resolution": "480p",
    "num_frames": 25,                    # 25 кадров при 24 fps ≈ 1.04 сек (минимум для 1 сек по формуле 8k+1)
    "frame_rate": 24,
    "first_pass_num_inference_steps": 8, # минимальное число шагов дистиллированной модели
    "enable_detail_pass": False,         # отключаем второй проход для максимальной экономии
    "expand_prompt": False,
}

# Защита от одновременного запуска нескольких генераций одним пользователем
_active_video_generations: set[int] = set()


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
    """Проверяет, есть ли у пользователя безлимитный доступ к генерации видео."""
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
            # Привязываем telegram_user_id или username, если они появились впервые
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
        return False, "⏳ Твоё видео уже генерируется прямо сейчас, подожди несколько секунд!"

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


def _generate_video_fal_sync(prompt_en: str, image_bytes: bytes | None = None) -> bytes:
    api_key = get_fal_key()
    if not api_key:
        raise RuntimeError("Ключ FAL_KEY не задан в настройках (.env / Render Environment).")

    headers = {
        "Authorization": f"Key {api_key}",
        "Content-Type": "application/json",
    }

    payload = {
        **STRICT_1S_PARAMS,
        "prompt": (prompt_en or "Cinematic motion, high quality, smooth movement").strip(),
    }

    if image_bytes:
        b64_img = base64.b64encode(image_bytes).decode("ascii")
        payload["image_url"] = f"data:image/jpeg;base64,{b64_img}"
        endpoint = FAL_I2V_ENDPOINT
    else:
        endpoint = FAL_T2V_ENDPOINT

    resp = requests.post(endpoint, headers=headers, json=payload, timeout=180)
    if resp.status_code not in (200, 201):
        detail = resp.text[:400]
        raise RuntimeError(f"Ошибка fal.ai ({resp.status_code}): {detail}")

    data = resp.json()
    video_url = (data.get("video") or {}).get("url")
    if not video_url:
        raise RuntimeError(f"fal.ai не вернул ссылку на видео: {str(data)[:300]}")

    vid_resp = requests.get(video_url, timeout=120)
    vid_resp.raise_for_status()
    return vid_resp.content
