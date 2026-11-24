"""
ig_cloud_actor.py
-----------------
Облачный исполнитель действий в Instagram (через зашифрованную куку `sessionid` пользователя):
- Поддерживает 3 действия из InstaSwipe Mini App:
  1) `unfollow_me` — отписаться самому (или отменить висящую заявку `pending`):
     POST https://www.instagram.com/api/v1/friendships/destroy/{user_id}/
  2) `remove_follower` — отписать от себя (удалить из подписчиков):
     POST https://www.instagram.com/api/v1/friendships/remove_follower/{user_id}/
  3) `mutual_break` — отписаться взаимно (сначала destroy, затем remove_follower).
- Автоматически определяет числовой `user_id` (`pk`) по `username`, если в ZIP-выгрузке не было ID.
- Распознаёт любой формат ввода куки: чистый `sessionid`, строка `Cookie: ...` из DevTools или JSON из Cookie-Editor.
- Детектирует `login_required`, `challenge_required` и `feedback_required` (Action Blocked), чтобы мгновенно ставить очередь на паузу и защищать аккаунт.
"""

import json
import re
import time
import urllib.parse
from typing import Any, Dict, Optional, Tuple

import requests

IG_APP_ID = "1217981644879628"
CHROME_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 16_6 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148 "
    "Instagram 275.0.0.27.98 (iPhone14,5; iOS 16_6; en_US; en-US; "
    "scale=3.00; 1170x2532; 458229237)"
)


def parse_ig_cookie_input(raw_text: str) -> Optional[Dict[str, str]]:
    """
    Принимает куку в любом удобном формате:
    1) JSON-массив из расширения Cookie-Editor / EditThisCookie
    2) Полная строка Cookie: `sessionid=...; csrftoken=...; ds_user_id=...`
    3) Просто само значение `sessionid` (например `582910482%3Axxxx%3A...`)
    Возвращает словарь cookies с гарантированным ключом `sessionid` или None.
    """
    txt = (raw_text or "").strip()
    if not txt:
        return None

    cookies: Dict[str, str] = {}

    # 1. Пробуем JSON (массив объектов [{name, value}] или словарь)
    if (txt.startswith("[") and txt.endswith("]")) or (txt.startswith("{") and txt.endswith("}")):
        try:
            parsed = json.loads(txt)
            if isinstance(parsed, list):
                for item in parsed:
                    if isinstance(item, dict) and item.get("name") and item.get("value") is not None:
                        cookies[str(item["name"]).strip()] = str(item["value"]).strip()
            elif isinstance(parsed, dict):
                for k, v in parsed.items():
                    if k and v is not None:
                        cookies[str(k).strip()] = str(v).strip()
        except Exception:
            pass

    # 2. Если в строке есть `sessionid=` (формат заголовка Cookie или key=value)
    if "sessionid" not in cookies and "sessionid=" in txt:
        raw_line = re.sub(r"(?i)^cookie\s*:\s*", "", txt).strip()
        for part in re.split(r"[;\n]+", raw_line):
            part = part.strip()
            if "=" in part:
                k, v = part.split("=", 1)
                k = k.strip()
                v = v.strip().strip('"').strip("'")
                if k and v:
                    cookies[k] = v

    # 3. Если пользователь вставил просто голое значение sessionid
    if "sessionid" not in cookies:
        candidate = txt.strip().strip('"').strip("'")
        # Настоящий sessionid Инстаграма содержит ID пользователя и двоеточия (%3A или :)
        if len(candidate) >= 20 and (" " not in candidate) and ("%3A" in candidate or "%3a" in candidate or ":" in candidate):
            cookies["sessionid"] = candidate

    sid = cookies.get("sessionid", "").strip()
    if not sid:
        return None

    # Вытаскиваем ds_user_id из префикса sessionid, если он не был передан явно
    if not cookies.get("ds_user_id"):
        unquoted_sid = urllib.parse.unquote(sid)
        first_part = unquoted_sid.split(":", 1)[0].strip()
        if first_part.isdigit():
            cookies["ds_user_id"] = first_part

    return cookies


def _build_ig_session(cookie_dict: Dict[str, str]) -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "User-Agent": CHROME_UA,
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.9,ru;q=0.8",
        "X-IG-App-ID": IG_APP_ID,
        "X-Requested-With": "XMLHttpRequest",
        "Origin": "https://www.instagram.com",
        "Referer": "https://www.instagram.com/",
    })
    for k, v in (cookie_dict or {}).items():
        if k and v:
            s.cookies.set(k, v, domain=".instagram.com")

    # Если csrftoken ещё не известен — делаем лёгкий GET к главной странице, чтобы Instagram выдал csrftoken
    if not s.cookies.get("csrftoken"):
        try:
            s.get("https://www.instagram.com/", timeout=12)
        except Exception:
            pass

    csrf = s.cookies.get("csrftoken") or cookie_dict.get("csrftoken") or "missing"
    s.headers["X-CSRFToken"] = csrf
    return s


def verify_ig_session_sync(cookie_dict: Dict[str, str]) -> Dict[str, Any]:
    """
    Проверяет работоспособность куки `sessionid` и возвращает информацию об аккаунте:
    {"ok": bool, "ds_user_id": str, "username": str, "cookies": dict, "error": str}
    """
    if not cookie_dict or not cookie_dict.get("sessionid"):
        return {"ok": False, "error": "Кука sessionid не найдена."}

    s = _build_ig_session(cookie_dict)
    ds_uid = s.cookies.get("ds_user_id") or cookie_dict.get("ds_user_id") or ""
    if not ds_uid:
        return {"ok": False, "error": "Не удалось определить ds_user_id из sessionid."}

    try:
        # Проверяем авторизацию запросом к текущему профилю / edit form
        r = s.get("https://www.instagram.com/api/v1/accounts/edit/web_form_data/", timeout=15)
        low_txt = (r.text or "").lower()
        if r.status_code == 200 and "form_data" in low_txt:
            data = r.json()
            uname = (data.get("form_data") or {}).get("username") or ""
            updated_cookies = {c.name: c.value for c in s.cookies}
            updated_cookies.update(cookie_dict)
            if s.cookies.get("csrftoken"):
                updated_cookies["csrftoken"] = s.cookies.get("csrftoken")
            return {
                "ok": True,
                "ds_user_id": str(ds_uid),
                "username": uname,
                "cookies": updated_cookies,
            }

        # Резервная проверка через user info по ds_user_id
        r2 = s.get(f"https://www.instagram.com/api/v1/users/{ds_uid}/info/", timeout=15)
        if r2.status_code == 200 and '"user"' in (r2.text or ""):
            u_obj = (r2.json().get("user") or {})
            uname = u_obj.get("username") or ""
            updated_cookies = {c.name: c.value for c in s.cookies}
            updated_cookies.update(cookie_dict)
            return {
                "ok": True,
                "ds_user_id": str(ds_uid),
                "username": uname,
                "cookies": updated_cookies,
            }

        if "checkpoint_required" in low_txt or "challenge_required" in low_txt:
            return {
                "ok": False,
                "error": "Instagram запросил подтверждение входа (Challenge). Зайди в приложение Instagram, нажми «Это был я» и попробуй снова.",
            }
        if r.status_code in (401, 403) or "login_required" in low_txt or "not-logged-in" in low_txt:
            return {"ok": False, "error": "Кука sessionid недействительна или устарела."}

        # Если оба эндпоинта вернули нестандартный ответ, но кука валидного формата и не 401 — сохраняем
        updated_cookies = {c.name: c.value for c in s.cookies}
        updated_cookies.update(cookie_dict)
        return {
            "ok": True,
            "ds_user_id": str(ds_uid),
            "username": f"id:{ds_uid}",
            "cookies": updated_cookies,
        }
    except Exception as ex:
        return {"ok": False, "error": f"Ошибка проверки сессии Instagram: {ex}"}


def resolve_ig_user_id_sync(
    session: requests.Session,
    username: str,
    known_user_id: str = "",
) -> Tuple[Optional[str], Optional[str]]:
    """
    Возвращает `(numeric_user_id, error_reason)`.
    Если `known_user_id` уже известен — сразу возвращает его без лишнего запроса.
    Использует `/api/v1/users/search/?q=` и `/api/v1/fbsearch/topsearch_flat/`, которые стабильно
    работают с облачных IP (в отличие от `web_profile_info`, часто отдающего пустой HTTP 429).
    Если аккаунт не найден ни в поиске, ни в OG-разметке страницы — возвращает `(None, "not_found")`
    (аккаунт удалён, деактивирован или сменил ник со времён старой ZIP-выгрузки).
    """
    clean_known = str(known_user_id or "").strip()
    if clean_known and clean_known.isdigit():
        return clean_known, None

    u = (username or "").strip().lstrip("@")
    if not u:
        return None, "Пустой username"

    u_low = u.lower()
    session.headers["Referer"] = f"https://www.instagram.com/{u}/"

    # 1. Основной быстрый поиск `/api/v1/users/search/?q=...` (работает с X-IG-App-ID 1217981644879628 без 429)
    search_succeeded = False
    try:
        r_s = session.get(
            f"https://www.instagram.com/api/v1/users/search/?q={urllib.parse.quote(u)}",
            timeout=14,
        )
        low_s = (r_s.text or "").lower()
        if "login_required" in low_s or "checkpoint_required" in low_s or "challenge_required" in low_s:
            return None, "auth_error"
        if r_s.status_code == 429 or "feedback_required" in low_s:
            return None, "rate_limit"
        if r_s.status_code == 200:
            search_succeeded = True
            data_s = r_s.json()
            for usr in data_s.get("users", []):
                if isinstance(usr, dict) and str(usr.get("username", "")).lower() == u_low:
                    pk = str(usr.get("pk") or usr.get("id") or "").strip()
                    if pk.isdigit():
                        return pk, None
    except Exception:
        pass

    # 2. Резервный эндпоинт `/api/v1/fbsearch/topsearch_flat/`
    try:
        r_f = session.get(
            f"https://www.instagram.com/api/v1/fbsearch/topsearch_flat/?search_surface=top_search_page&query={urllib.parse.quote(u)}",
            timeout=14,
        )
        if r_f.status_code == 200:
            search_succeeded = True
            data_f = r_f.json()
            for item in data_f.get("list", []):
                usr = (item or {}).get("user") or {}
                if str(usr.get("username", "")).lower() == u_low:
                    pk = str(usr.get("pk") or usr.get("id") or "").strip()
                    if pk.isdigit():
                        return pk, None
    except Exception:
        pass

    # 3. Если поиск отработал со статусом 200, но точного совпадения username нет — проверяем, существует ли страница вообще
    try:
        r_og = requests.get(
            f"https://www.instagram.com/{urllib.parse.quote(u)}/",
            headers={"User-Agent": "Mozilla/5.0 (compatible; Discordbot/2.0; +https://discordapp.com)"},
            timeout=10,
        )
        if r_og.status_code == 404 or 'property="og:title"' not in (r_og.text or ""):
            return None, "not_found"
    except Exception:
        if search_succeeded:
            return None, "not_found"

    # 4. На случай если профиль существует, но скрыт из поиска — пробуем web_profile_info
    try:
        r_w = session.get(
            f"https://www.instagram.com/api/v1/users/web_profile_info/?username={urllib.parse.quote(u)}",
            timeout=12,
        )
        if r_w.status_code == 200:
            user_obj = ((r_w.json().get("data") or {}).get("user") or {})
            pk = str(user_obj.get("id") or "").strip()
            if pk.isdigit():
                return pk, None
    except Exception:
        pass

    if search_succeeded:
        return None, "not_found"

    return None, "Не удалось получить ID профиля в Instagram"


def _classify_ig_post_response(resp: requests.Response) -> Tuple[str, str]:
    """
    Классифицирует ответ POST-запроса Instagram API:
    Возвращает `(status_code_str, message)`:
    - `'ok'` — успешно выполнено
    - `'auth_error'` — слетела сессия или нужен Challenge («Это были вы?»)
    - `'rate_limit'` — временный лимит скорости (Action Blocked / 429)
    - `'error'` — прочая ошибка
    """
    txt = resp.text or ""
    low = txt.lower()

    if "checkpoint_required" in low or "challenge_required" in low:
        return "auth_error", "Instagram запросил подтверждение входа (Challenge в приложении IG)"
    if resp.status_code in (401, 403) and ("login_required" in low or "not-logged-in" in low):
        return "auth_error", "Сессия Instagram истекла — обнови куку через /connectig"
    if resp.status_code == 429 or "feedback_required" in low or "try again later" in low or "please wait a few minutes" in low:
        return "rate_limit", "Instagram включил временный анти-спам лимит (Action Blocked)"

    if resp.status_code == 200:
        try:
            j = resp.json()
            if j.get("status") == "ok" or "friendship_status" in j:
                return "ok", "OK"
        except Exception:
            pass

    return "error", f"HTTP {resp.status_code}: {txt[:140]}"


def execute_ig_action_sync(
    cookie_dict: Dict[str, str],
    username: str,
    action: str,
    known_user_id: str = "",
) -> Dict[str, Any]:
    """
    Выполняет действие в Instagram:
    - `unfollow_me`: POST /api/v1/friendships/destroy/{pk}/
    - `remove_follower`: POST /api/v1/friendships/remove_follower/{pk}/
    - `mutual_break`: destroy/{pk}/ + remove_follower/{pk}/
    Возвращает `{"ok": bool, "status": "ok"|"auth_error"|"rate_limit"|"not_found"|"error", "user_id": str, "message": str}`.
    """
    if action not in ("unfollow_me", "remove_follower", "mutual_break"):
        return {"ok": True, "status": "skipped", "user_id": known_user_id, "message": "Не требует отписки"}

    session = _build_ig_session(cookie_dict)
    csrf = session.cookies.get("csrftoken") or cookie_dict.get("csrftoken") or ""
    if csrf:
        session.headers["X-CSRFToken"] = csrf

    target_uid, err_code = resolve_ig_user_id_sync(session, username, known_user_id=known_user_id)
    if not target_uid:
        if err_code == "not_found":
            # Профиль удалён или переименован в IG — помечаем как выполненный/пропущенный, чтобы не застревал
            return {"ok": True, "status": "not_found", "user_id": "", "message": "Профиль не найден в IG (удалён)"}
        if err_code in ("auth_error", "rate_limit"):
            return {"ok": False, "status": err_code, "user_id": "", "message": err_code}
        return {"ok": False, "status": "error", "user_id": "", "message": err_code or "ID не найден"}

    session.headers["Referer"] = f"https://www.instagram.com/{username.strip().lstrip('@')}/"
    session.headers["Content-Type"] = "application/x-www-form-urlencoded"
    if session.cookies.get("csrftoken"):
        session.headers["X-CSRFToken"] = session.cookies.get("csrftoken")

    try:
        if action in ("unfollow_me", "mutual_break"):
            r1 = session.post(
                f"https://www.instagram.com/api/v1/friendships/destroy/{target_uid}/",
                data={"container_module": "profile", "user_id": target_uid},
                timeout=15,
            )
            st1, msg1 = _classify_ig_post_response(r1)
            if st1 != "ok":
                return {"ok": False, "status": st1, "user_id": target_uid, "message": msg1}

        if action == "mutual_break":
            time.sleep(1.8)

        if action in ("remove_follower", "mutual_break"):
            r2 = session.post(
                f"https://www.instagram.com/api/v1/friendships/remove_follower/{target_uid}/",
                data={"container_module": "profile", "user_id": target_uid},
                timeout=15,
            )
            st2, msg2 = _classify_ig_post_response(r2)
            if st2 != "ok":
                return {"ok": False, "status": st2, "user_id": target_uid, "message": msg2}

        return {"ok": True, "status": "ok", "user_id": target_uid, "message": "Выполнено в Instagram"}
    except Exception as ex:
        return {"ok": False, "status": "error", "user_id": target_uid, "message": str(ex)}
