"""
google_forms_auto.py
--------------------
Парсинг, генерация предзаполненных ссылок (usp=pp_url) и автоматическая отправка
Google Forms (HTTP formResponse с верификацией + Playwright fallback для сложных
многостраничных форм с условными переходами).
"""

import re
import json
import urllib.parse
import requests
from bs4 import BeautifulSoup

CHROME_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/128.0.0.0 Safari/537.36"
)

DEFAULT_HEADERS = {
    "User-Agent": CHROME_UA,
    "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
}

FIELD_TYPE_MAP = {
    0: "short_text",
    1: "paragraph",
    2: "radio",
    3: "dropdown",
    4: "checkbox",
    5: "scale",
    7: "grid",
    9: "date",
    10: "time",
    13: "file_upload",
}

CLOSED_MARKERS = (
    "no longer accepting responses",
    "더 이상 응답을 받지",
    "ответы больше не принимаются",
    "freebirdformviewerviewresponseclosed",
)

CONFIRMATION_MARKERS = (
    "freebirdformviewerviewresponseconfirmationmessage",
    "vhw8k",
    "your response has been recorded",
    "응답이 기록되었습니다",
    "ответ записан",
    "submit another response",
    "다른 응답 제출",
    "отправить ещё один ответ",
    "отправить еще один ответ",
    "formresponse",
)

VALIDATION_ERROR_MARKERS = (
    "this is a required question",
    "필수 질문입니다",
    "это обязательный вопрос",
    "must be a valid email address",
    "올바른 이메일 주소",
    "freebirdformviewercomponentsquestionbasevalidationerror",
)


def _extract_balanced_json_array(html: str, marker: str = "FB_PUBLIC_LOAD_DATA_") -> str | None:
    """Надёжно вырезает JSON-массив после var FB_PUBLIC_LOAD_DATA_ = [...] с учётом строк и экранирования."""
    idx = html.find(marker)
    if idx == -1:
        return None
    start = html.find("[", idx)
    if start == -1:
        return None

    depth = 0
    in_string = False
    escape = False
    n = len(html)
    for i in range(start, n):
        ch = html[i]
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
        else:
            if ch == '"':
                in_string = True
            elif ch == "[":
                depth += 1
            elif ch == "]":
                depth -= 1
                if depth == 0:
                    return html[start : i + 1]
    return None


def _clean_viewform_url(url: str) -> str:
    """Приводит URL Google Forms к чистому .../viewform без мусорных query-параметров."""
    parsed = urllib.parse.urlparse(url)
    path = parsed.path
    if "/formResponse" in path:
        path = path.replace("/formResponse", "/viewform")
    elif not path.endswith("/viewform") and "/forms/d/" in path:
        path = path.rstrip("/") + "/viewform"
    return urllib.parse.urlunparse((parsed.scheme, parsed.netloc, path, "", "", ""))


def _extract_hidden_inputs(soup: BeautifulSoup) -> tuple[str, dict[str, str]]:
    """Извлекает action формы и все скрытые поля (fvv, fbzx, tag, partialResponse, sentinel и т.д.)."""
    form = soup.find("form")
    if not form:
        return "", {}
    action = (form.get("action") or "").strip()
    hidden: dict[str, str] = {}
    for inp in form.find_all("input"):
        itype = (inp.get("type") or "").lower()
        name = (inp.get("name") or "").strip()
        if itype == "hidden" and name:
            hidden[name] = inp.get("value") or ""
    return action, hidden


def is_google_form_url(url: str) -> bool:
    low = (url or "").strip().lower()
    return "docs.google.com/forms" in low or "forms.gle/" in low


def extract_registration_urls_from_post(post: dict) -> list[str]:
    """
    Извлекает и ранжирует ссылки для регистрации из поста DGIST.
    Приоритет: Google Forms (docs.google.com/forms, forms.gle) -> популярные сервисы форм / сокращатели -> прочие ссылки регистрации.
    """
    candidates: list[tuple[int, str]] = []
    seen = set()

    def _add(url: str, label: str = ""):
        u = (url or "").strip().rstrip(".,;:!?)\"'<>]")
        if not u.startswith("http"):
            return
        if u in seen:
            return
        low = u.lower()
        # Исключаем ссылки на сам портал, YouTube-трансляции, Zoom и почтовые ссылки
        if any(skip in low for skip in (
            "stuecm.dgist.ac.kr/s/potal_board",
            "youtube.com", "youtu.be",
            "zoom.us", "instagram.com", "facebook.com",
            "mailto:", "sugang.dgist.ac.kr",
        )):
            return
        seen.add(u)
        lbl_low = (label or "").lower()
        if "docs.google.com/forms" in low or "forms.gle/" in low:
            candidates.append((1, u))
        elif any(d in low for d in (
            "naver.me/", "form.naver.com", "forms.office.com", "forms.microsoft.com",
            "tally.so", "typeform.com", "surveymonkey.com", "lu.ma/", "eventbrite.",
            "onoffmix.com", "festa.io", "event-us.kr", "moaform.com", "walla.my",
            "bit.ly/", "han.gl/", "tinyurl.com/", "me2.do/", "linktr.ee/", "url.kr/", "shorturl.at/",
        )):
            candidates.append((2, u))
        elif any(k in lbl_low or k in low for k in (
            "apply", "register", "registration", "form", "survey", "rsvp", "sign", "신청", "등록", "접수",
        )):
            candidates.append((3, u))

    # 1. Explicit registration_url if already detected
    if post.get("registration_url"):
        _add(post["registration_url"], "registration")

    # 2. Extracted links from HTML
    for lk in (post.get("links") or []):
        if isinstance(lk, dict):
            _add(lk.get("url", ""), lk.get("label", ""))
        elif isinstance(lk, str):
            _add(lk, "")

    # 3. Raw URLs from full_text / summary
    for field in ("full_text", "summary"):
        txt = post.get(field) or ""
        for m in re.finditer(r"https?://[^\s<>\"')\]}]+", txt):
            _add(m.group(0), "text")

    candidates.sort(key=lambda x: x[0])
    return [u for _, u in candidates]


def parse_google_form(url: str) -> dict:
    """
    Открывает ссылку (разворачивая редиректы forms.gle / bit.ly и др.),
    парсит структуру Google Forms из FB_PUBLIC_LOAD_DATA_ и возвращает полную схему.
    """
    session = requests.Session()
    session.headers.update(DEFAULT_HEADERS)
    resp = session.get(url, timeout=25, allow_redirects=True)
    final_url = resp.url
    low_url = final_url.lower()
    html = resp.text or ""
    low_html = html.lower()

    if "accounts.google.com" in low_url or "servicelogin" in low_url:
        return {
            "ok": False,
            "status": "login_required",
            "is_google_form": True,
            "original_url": url,
            "final_url": final_url,
            "error": "Форма требует входа в Google-аккаунт (например, корпоративный @dgist.ac.kr).",
        }

    if "docs.google.com/forms" not in low_url:
        return {
            "ok": False,
            "status": "not_google_form",
            "is_google_form": False,
            "original_url": url,
            "final_url": final_url,
            "error": f"Ссылка ведёт не на Google Forms ({final_url}).",
        }

    if "/closedform" in low_url or any(m in low_html for m in CLOSED_MARKERS):
        soup = BeautifulSoup(html, "html.parser")
        title_tag = soup.find("title")
        return {
            "ok": False,
            "status": "closed",
            "is_google_form": True,
            "original_url": url,
            "final_url": final_url,
            "form_title": title_tag.get_text(strip=True) if title_tag else "",
            "error": "Регистрация в этой Google-форме уже закрыта.",
        }

    raw_json = _extract_balanced_json_array(html)
    if not raw_json:
        return {
            "ok": False,
            "status": "parse_error",
            "is_google_form": True,
            "original_url": url,
            "final_url": final_url,
            "error": "Не удалось извлечь структуру полей Google Forms.",
        }

    data = json.loads(raw_json)
    form_meta = data[1] if len(data) > 1 and isinstance(data[1], list) else []
    form_desc = (form_meta[0] or "") if len(form_meta) > 0 else ""
    form_title = (form_meta[8] or "") if len(form_meta) > 8 else ""
    raw_items = form_meta[1] if (len(form_meta) > 1 and isinstance(form_meta[1], list)) else []

    soup = BeautifulSoup(html, "html.parser")
    form_action, hidden_inputs = _extract_hidden_inputs(soup)
    viewform_url = _clean_viewform_url(final_url)
    if not form_action:
        form_action = viewform_url.replace("/viewform", "/formResponse")

    fields: list[dict] = []
    # Проверяем встроенный сбор email (emailAddress)
    settings = form_meta[10] if (len(form_meta) > 10 and isinstance(form_meta[10], list)) else []
    email_setting = settings[6] if len(settings) > 6 else None
    has_email_input = bool(
        soup.find("input", {"type": "email"})
        or 'name="emailAddress"' in html
        or email_setting == 3
    )
    if has_email_input:
        fields.append({
            "entry_id": "emailAddress",
            "field_type": "email",
            "title": "Email (обязательный сбор почты формой Google)",
            "description": "",
            "required": True,
            "options": [],
            "has_other_option": False,
            "page": 0,
            "context": "",
        })

    current_page = 0
    pending_context = ""
    has_file_upload = False

    for item in raw_items:
        if not isinstance(item, list) or len(item) < 4:
            continue
        q_title = (item[1] or "").strip()
        q_desc = (item[2] or "").strip()
        q_type = item[3]
        q_meta = item[4] if len(item) > 4 else None

        # Заголовок / информационный блок (тип 6) или разрыв страницы (тип 8)
        if q_type == 8:
            current_page += 1
            sec_txt = " — ".join(x for x in (q_title, q_desc) if x)
            pending_context = sec_txt[:400]
            continue
        if q_type == 6:
            blk_txt = " — ".join(x for x in (q_title, q_desc) if x)
            pending_context = (pending_context + "\n" + blk_txt).strip()[:500]
            continue
        if q_type in (11, 12):  # Image / Video
            continue
        if q_type == 13:
            has_file_upload = True
            continue

        if not q_meta or not isinstance(q_meta, list):
            continue

        # Таблица (Grid, type == 7): каждая строка имеет свой entry_id
        if q_type == 7:
            for row in q_meta:
                if not isinstance(row, list) or not row:
                    continue
                eid = str(row[0])
                raw_opts = row[1] if len(row) > 1 and isinstance(row[1], list) else []
                opts = [str(o[0]) for o in raw_opts if isinstance(o, list) and o and o[0] is not None]
                req = bool(row[2]) if len(row) > 2 else False
                row_label = str(row[3][0]) if (len(row) > 3 and isinstance(row[3], list) and row[3]) else ""
                is_checkbox_grid = bool(len(row) > 11 and row[11])
                fields.append({
                    "entry_id": eid,
                    "field_type": "checkbox" if is_checkbox_grid else "radio",
                    "title": f"{q_title} [{row_label}]" if row_label else q_title,
                    "description": q_desc,
                    "required": req,
                    "options": opts,
                    "has_other_option": False,
                    "page": current_page,
                    "context": pending_context,
                })
            pending_context = ""
            continue

        sub = q_meta[0] if isinstance(q_meta[0], list) else None
        if not sub:
            continue
        eid = str(sub[0])
        raw_opts = sub[1] if (len(sub) > 1 and isinstance(sub[1], list)) else []
        req = bool(sub[2]) if len(sub) > 2 else False

        opts: list[str] = []
        has_other = False
        for o in raw_opts:
            if not isinstance(o, list) or not o:
                continue
            val = o[0]
            # В Google Forms опция "Другое" ("Other") закодирована как пустая строка "" или флаг o[4] == 1
            is_other_flag = (len(o) > 4 and o[4] == 1)
            if val == "" or val is None or is_other_flag:
                has_other = True
            else:
                opts.append(str(val))

        fields.append({
            "entry_id": eid,
            "field_type": FIELD_TYPE_MAP.get(q_type, "short_text"),
            "title": q_title,
            "description": q_desc,
            "required": req,
            "options": opts,
            "has_other_option": has_other,
            "page": current_page,
            "context": pending_context,
        })
        pending_context = ""

    return {
        "ok": True,
        "status": "open",
        "is_google_form": True,
        "original_url": url,
        "final_url": final_url,
        "viewform_url": viewform_url,
        "form_action": form_action,
        "form_title": form_title,
        "form_description": form_desc,
        "num_pages": current_page + 1,
        "has_file_upload": has_file_upload,
        "fields": fields,
        "hidden_inputs": hidden_inputs,
    }


def build_prefilled_google_form_url(viewform_url: str, fields: list[dict], answers: dict) -> str:
    """
    Формирует официальную предзаполненную ссылку Google Forms (?usp=pp_url&entry.XXX=...),
    чтобы пользователь мог открыть её и визуально проверить все заполненные поля.
    """
    base = _clean_viewform_url(viewform_url)
    params: list[tuple[str, str]] = [("usp", "pp_url")]
    fields_by_id = {str(f["entry_id"]): f for f in fields}

    for eid, ans in (answers or {}).items():
        eid = str(eid)
        f = fields_by_id.get(eid)
        if not f or ans is None:
            continue
        ftype = f.get("field_type", "short_text")

        if isinstance(ans, dict):
            val = ans.get("value")
            other_text = (ans.get("other_text") or "").strip()
            use_other = bool(ans.get("use_other"))
        else:
            val = ans
            other_text = ""
            use_other = False

        if eid == "emailAddress":
            s_val = str(val or "").strip()
            if s_val:
                params.append(("emailAddress", s_val))
            continue

        key = f"entry.{eid}"
        if ftype == "checkbox":
            vals = val if isinstance(val, list) else ([val] if val else [])
            for v in vals:
                sv = str(v).strip()
                if sv:
                    params.append((key, sv))
            if use_other and other_text:
                params.append((key, "__other_option__"))
                params.append((f"{key}.other_option_response", other_text))
        elif ftype in ("radio", "dropdown"):
            if use_other and other_text:
                params.append((key, "__other_option__"))
                params.append((f"{key}.other_option_response", other_text))
            else:
                sv = str(val or "").strip()
                if sv:
                    params.append((key, sv))
        else:
            sv = str(val or "").strip()
            if sv:
                params.append((key, sv))

    qs = urllib.parse.urlencode(params, doseq=True)
    return f"{base}?{qs}"


def build_form_response_payload(
    hidden_inputs: dict[str, str],
    fields: list[dict],
    answers: dict,
    num_pages: int = 1,
) -> list[tuple[str, str]]:
    """Собирает точный POST-payload для отправки в Google Forms /formResponse."""
    payload: list[tuple[str, str]] = []
    fields_by_id = {str(f["entry_id"]): f for f in fields}

    # 1. Пользовательские ответы
    for eid, ans in (answers or {}).items():
        eid = str(eid)
        f = fields_by_id.get(eid)
        if not f or ans is None:
            continue
        ftype = f.get("field_type", "short_text")

        if isinstance(ans, dict):
            val = ans.get("value")
            other_text = (ans.get("other_text") or "").strip()
            use_other = bool(ans.get("use_other"))
        else:
            val = ans
            other_text = ""
            use_other = False

        if eid == "emailAddress":
            sv = str(val or "").strip()
            if sv:
                payload.append(("emailAddress", sv))
            continue

        key = f"entry.{eid}"
        if ftype == "checkbox":
            vals = val if isinstance(val, list) else ([val] if val else [])
            for v in vals:
                sv = str(v).strip()
                if sv:
                    payload.append((key, sv))
            if use_other and other_text:
                payload.append((key, "__other_option__"))
                payload.append((f"{key}.other_option_response", other_text))
        elif ftype in ("radio", "dropdown"):
            if use_other and other_text:
                payload.append((key, "__other_option__"))
                payload.append((f"{key}.other_option_response", other_text))
            else:
                sv = str(val or "").strip()
                if sv:
                    payload.append((key, sv))
        elif ftype == "date":
            sv = str(val or "").strip()
            m = re.match(r"^(\d{4})-(\d{1,2})-(\d{1,2})$", sv)
            if m:
                payload.append((f"{key}_year", m.group(1)))
                payload.append((f"{key}_month", str(int(m.group(2)))))
                payload.append((f"{key}_day", str(int(m.group(3)))))
        elif ftype == "time":
            sv = str(val or "").strip()
            m = re.match(r"^(\d{1,2}):(\d{2})$", sv)
            if m:
                payload.append((f"{key}_hour", f"{int(m.group(1)):02d}"))
                payload.append((f"{key}_minute", m.group(2)))
        else:
            sv = str(val or "").strip()
            if sv:
                payload.append((key, sv))

    # 2. Скрытые системные поля Google Forms (fbzx, tag, partialResponse, sentinel и т.д.)
    seen_hidden = set()
    for hk, hv in (hidden_inputs or {}).items():
        if hk == "pageHistory":
            continue
        payload.append((hk, hv))
        seen_hidden.add(hk)

    if "fvv" not in seen_hidden:
        payload.append(("fvv", "1"))
    if "submissionTimestamp" not in seen_hidden:
        payload.append(("submissionTimestamp", "-1"))

    page_hist = ",".join(str(i) for i in range(max(1, int(num_pages))))
    payload.append(("pageHistory", page_hist))

    return payload


def _verify_google_form_response_html(status_code: int, html: str, final_url: str = "") -> tuple[bool, str]:
    """Проверяет, действительно ли Google Forms приняла и сохранила ответ."""
    low = (html or "").lower()
    if status_code != 200:
        return False, f"HTTP статус {status_code}"
    if any(m in low for m in CLOSED_MARKERS) or "/closedform" in (final_url or "").lower():
        return False, "Форма уже закрылась для приёма ответов."
    if "accounts.google.com" in (final_url or "").lower():
        return False, "Форма потребовала авторизацию в Google."
    if any(err in low for err in VALIDATION_ERROR_MARKERS):
        return False, "Google Forms вернула ошибку валидации обязательного поля."
    # Google Forms показывает страницу подтверждения с vHW8K / freebirdFormviewerViewResponseConfirmationMessage
    # или текстом подтверждения; при ошибке оставляет form с полями ввода
    has_confirm = any(m in low for m in CONFIRMATION_MARKERS)
    still_has_submit_form = 'action="https://docs.google.com/forms/' in low and "formresponse" in low and 'type="submit"' in low
    if has_confirm and not still_has_submit_form:
        return True, "OK"
    # На некоторых локалях проверяем наличие блока подтверждения без input entry.*
    if "formresponse" in (final_url or "").lower() and "entry." not in low and "fb_public_load_data_" not in low:
        return True, "OK"
    return False, "Не удалось подтвердить сохранение ответа на странице Google Forms."


def _submit_google_form_playwright(prefilled_url: str, fields: list[dict], answers: dict) -> tuple[bool, str]:
    """
    Резервная отправка через headless Chromium (Playwright):
    открывает предзаполненную ссылку (usp=pp_url), дозаполняет emailAddress при наличии,
    проходит все страницы (Next / 다음 / Далее) и нажимает Submit (제출 / Отправить).
    """
    try:
        from playwright.sync_api import sync_playwright
    except Exception as ex:
        return False, f"Playwright недоступен: {ex}"

    email_val = ""
    if "emailAddress" in (answers or {}):
        raw_em = answers["emailAddress"]
        email_val = (raw_em.get("value") if isinstance(raw_em, dict) else raw_em) or ""

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
                "--no-zygote",
            ],
        )
        ctx = browser.new_context(user_agent=CHROME_UA, locale="ko-KR")
        try:
            page = ctx.new_page()
            page.goto(prefilled_url, wait_until="networkidle", timeout=35000)
            if "accounts.google.com" in page.url.lower():
                return False, "Форма требует вход в Google-аккаунт."
            if "/closedform" in page.url.lower():
                return False, "Форма уже закрыта."

            for _step in range(10):
                # Если есть поле emailAddress — заполняем
                if email_val:
                    em_loc = page.locator('input[type="email"]')
                    if em_loc.count() > 0 and em_loc.first.is_visible():
                        try:
                            cur = em_loc.first.input_value()
                            if not cur.strip():
                                em_loc.first.fill(str(email_val).strip())
                        except Exception:
                            pass

                # Ищем кнопку отправки (Submit / 제출 / Отправить)
                submit_btn = page.locator(
                    'div[role="button"]:has-text("제출"), '
                    'div[role="button"]:has-text("Submit"), '
                    'div[role="button"]:has-text("Отправить")'
                )
                if submit_btn.count() > 0 and submit_btn.first.is_visible():
                    submit_btn.first.click()
                    page.wait_for_timeout(3000)
                    html = page.content()
                    ok, msg = _verify_google_form_response_html(200, html, page.url)
                    if ok:
                        return True, "OK (Playwright)"
                    return False, msg

                # Иначе ищем кнопку перехода на следующую страницу (Next / 다음 / Далее)
                next_btn = page.locator(
                    'div[role="button"]:has-text("다음"), '
                    'div[role="button"]:has-text("Next"), '
                    'div[role="button"]:has-text("Далее")'
                )
                if next_btn.count() > 0 and next_btn.first.is_visible():
                    next_btn.first.click()
                    page.wait_for_timeout(1500)
                    continue

                break

            return False, "Не найдена кнопка отправки на странице Google Forms."
        except Exception as ex:
            return False, f"Ошибка Playwright при отправке: {ex}"
        finally:
            try:
                ctx.close()
            except Exception:
                pass
            try:
                browser.close()
            except Exception:
                pass


def submit_google_form(
    viewform_url: str,
    fields: list[dict],
    answers: dict,
    num_pages: int = 1,
    prefilled_url: str = "",
) -> dict:
    """
    Отправляет заполненную Google Form:
    1) Свежий GET для получения актуальных cookies и скрытых токенов (fbzx, tag, partialResponse).
    2) HTTP POST в /formResponse + строгая проверка страницы подтверждения.
    3) Если HTTP POST не прошёл (например, условные переходы между секциями), автоматический Playwright fallback.
    """
    clean_url = _clean_viewform_url(viewform_url)
    session = requests.Session()
    session.headers.update(DEFAULT_HEADERS)

    try:
        get_resp = session.get(clean_url, timeout=25, allow_redirects=True)
        if "accounts.google.com" in get_resp.url.lower():
            return {"ok": False, "method": "http", "error": "Форма требует входа в Google-аккаунт."}
        if "/closedform" in get_resp.url.lower() or any(m in get_resp.text.lower() for m in CLOSED_MARKERS):
            return {"ok": False, "method": "http", "error": "К сожалению, форма уже закрыта."}

        soup = BeautifulSoup(get_resp.text, "html.parser")
        form_action, hidden_inputs = _extract_hidden_inputs(soup)
        if not form_action:
            form_action = clean_url.replace("/viewform", "/formResponse")

        payload = build_form_response_payload(hidden_inputs, fields, answers, num_pages=num_pages)
        post_headers = {
            **DEFAULT_HEADERS,
            "Content-Type": "application/x-www-form-urlencoded",
            "Referer": get_resp.url,
            "Origin": "https://docs.google.com",
        }
        post_resp = session.post(form_action, data=payload, headers=post_headers, timeout=25, allow_redirects=True)
        ok, verify_msg = _verify_google_form_response_html(post_resp.status_code, post_resp.text, post_resp.url)
        if ok:
            return {"ok": True, "method": "http", "error": None}
        http_err = verify_msg
    except Exception as ex:
        http_err = str(ex)

    # Fallback: Playwright на предзаполненной ссылке
    pp_url = prefilled_url or build_prefilled_google_form_url(clean_url, fields, answers)
    pw_ok, pw_msg = _submit_google_form_playwright(pp_url, fields, answers)
    if pw_ok:
        return {"ok": True, "method": "playwright", "error": None}
    return {
        "ok": False,
        "method": "http+playwright",
        "error": f"HTTP: {http_err} | Browser: {pw_msg}",
    }
