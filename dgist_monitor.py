#!/usr/bin/env python3
"""DGIST Bulletin Board Monitor — конденсированная и надежная версия."""
import os, re, json, time, requests
from bs4 import BeautifulSoup
from datetime import datetime, timezone
from dotenv import load_dotenv
from google import genai
from google.genai import types
from pydantic import BaseModel, Field

load_dotenv()

BASE_URL = "https://stuecm.dgist.ac.kr"
CATEGORIES = {
    "_0031": ("PB_ST00220031", "Latest / Student Bulletin"),
    "_0014": ("PB_ST00220014", "Academic Notice"),
    "_0161": ("BD000240", "Scholarships"),
    "_0162": ("PB_ST00220162", "Career & Jobs"),
    "_0004": ("PB_ST00220004", "Student Support"),
}
SITE_KEY, LANGUAGE, POSTS_PER_CATEGORY, REQUEST_DELAY_SEC = "ST0022", "en", 20, 1.0
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(BASE_DIR, "seen_ids.json")
LOG_FILE = os.path.join(BASE_DIR, "summaries_log.jsonl")
SESSION_COOKIE_FILE = os.path.join(BASE_DIR, "session_cookie.txt")

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.5-flash-lite")
gemini_client = genai.Client(api_key=GEMINI_API_KEY) if GEMINI_API_KEY else None

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
    "Content-Type": "application/x-www-form-urlencoded",
    "Referer": BASE_URL,
}

class SessionExpiredError(Exception): pass

class NoticeSummary(BaseModel):
    important: bool = Field(description="True if notice contains deadlines, scholarships, rules, or required actions")
    summary: str = Field(description="Concise 1-2 sentence Russian summary of the notice")

def build_user_paths(uid: int) -> dict:
    base = os.path.join(BASE_DIR, "user_data", str(uid))
    os.makedirs(base, exist_ok=True)
    return {
        "state_file": os.path.join(base, "seen_ids.json"),
        "log_file": os.path.join(base, "summaries_log.jsonl"),
        "cookie_file": os.path.join(base, "session_cookie.txt"),
        "browser_profile_dir": os.path.join(base, "browser_profile"),
    }

def load_seen(path: str = STATE_FILE) -> dict:
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f: return json.load(f)
        except Exception: pass
    return {}

def save_seen(seen: dict, path: str = STATE_FILE):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f: json.dump(seen, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)

def append_log(entry: dict, path: str = LOG_FILE):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")

def looks_like_login_page(html: str, url: str = "") -> bool:
    low = (html or "").lower()
    u = (url or "").lower()
    if any(m in u for m in ("/login.html", "/login.do", "/sso/login", "isign.dgist.ac.kr")): return True
    markers = ('name="loginform"', 'id="loginform"', 'name="j_password"', 'id="password"', 'name="userpassword"')
    return sum(m in low for m in markers) >= 2 or ("sendurl" in low and "isign.dgist.ac.kr" in low)

def fetch_list(session: requests.Session, menu_key: str, board_id: str, page: int = 1) -> list[dict]:
    url = f"{BASE_URL}/s/potal_board/{menu_key}/listBbs.do"
    body = {"currentPage": page, "BOARD_ID": board_id, "SITE_KEY": SITE_KEY, "MENU_KEY": menu_key, "language": LANGUAGE}
    resp = session.post(url, data=body, timeout=30)
    resp.raise_for_status()
    html = resp.text
    if looks_like_login_page(html, resp.url):
        raise SessionExpiredError(f"Сессия истекла при запросе списка для {menu_key}.")
    soup = BeautifulSoup(html, "html.parser")
    rows = soup.select("table.boardList tr") or soup.select("table tr")
    posts = []
    for row in rows:
        link = row.select_one("a.selectListTitle") or row.select_one('a[href*="viewBbs.do"]')
        if not link: continue
        href = link.get("href", "") or link.get("onclick", "")
        m = re.search(r"NOTIWR_NO=(\d+)", href) or re.search(r"NOTIWR_NO['\"]?\s*[,=]\s*['\"]?(\d+)", str(row))
        if not m: continue
        cells = row.find_all("td")
        title = link.get_text(" ", strip=True)
        if not title: continue
        posts.append({
            "post_id": m.group(1), "title": title,
            "author": cells[-3].get_text(strip=True) if len(cells) >= 3 else "",
            "date": cells[-2].get_text(strip=True) if len(cells) >= 2 else "",
            "views": cells[-1].get_text(strip=True) if len(cells) >= 1 else ""
        })
    return posts[:POSTS_PER_CATEGORY]

def fetch_detail(session: requests.Session, menu_key: str, board_id: str, post_id: str) -> dict:
    url = f"{BASE_URL}/s/potal_board/{menu_key}/viewBbs.do"
    params = {"NOTIWR_NO": post_id, "currentPage": 1, "BOARD_ID": board_id, "SITE_KEY": SITE_KEY, "MENU_KEY": menu_key, "language": LANGUAGE}
    resp = session.get(url, params=params, timeout=30)
    resp.raise_for_status()
    if looks_like_login_page(resp.text, resp.url):
        raise SessionExpiredError(f"Сессия истекла при открытии поста {post_id}.")
    soup = BeautifulSoup(resp.text, "html.parser")
    eng, kor, body = soup.select_one("tr#cntn_eng_view"), soup.select_one("tr#cntn_kor_view"), soup.select_one("td.boardViewBody")
    text = (eng.get_text("\n", strip=True) if eng and eng.get_text(strip=True) else
            kor.get_text("\n", strip=True) if kor and kor.get_text(strip=True) else
            body.get_text("\n", strip=True) if body else "")
    if not text:
        candidates = [el.get_text(" ", strip=True) for el in soup.select("td, div")]
        text = max(candidates, key=len, default="")
    attachments = []
    for a in soup.select("div.bbs_single_file a[onclick], div.bbs_file a[onclick]"):
        m = re.search(r"fnDownloadFile\(\s*['\"]?([^'\",)]+)['\"]?\s*,\s*['\"]?([^'\",)]+)['\"]?\s*\)", a.get("onclick", ""))
        if m: attachments.append({"conn_no": m.group(1), "seq_no": m.group(2), "label": a.get_text(strip=True)})
    return {"text": text, "attachments": attachments}

def summarize_with_gemini(title: str, text: str, category: str) -> dict:
    preview = (" ".join((text or "").split()))[:300] or "Описание объявления недоступно."
    if not gemini_client: return {"important": True, "summary": preview}
    prompt = f"Раздел: {category}\nЗаголовок: {title}\nТекст:\n{text[:4000]}\n\nСделай краткое описание на русском языке (максимум 2 предложения) и определи important=true (дедлайн, стипендия, важное изменение правил) или false."
    try:
        cfg = types.GenerateContentConfig(response_mime_type="application/json", response_schema=NoticeSummary, temperature=0.2)
        res = gemini_client.models.generate_content(model=GEMINI_MODEL, contents=prompt, config=cfg)
        parsed = NoticeSummary.model_validate_json(res.text or "{}")
        return {"important": parsed.important, "summary": parsed.summary or preview}
    except Exception as e:
        print(f"[GEMINI] Warning: {e}, using preview fallback")
        return {"important": True, "summary": preview}

def run_once(cookie: str = "", state_file: str = STATE_FILE, log_file: str = LOG_FILE) -> dict:
    if not cookie: raise RuntimeError("Не передана session cookie для DGIST.")
    session = requests.Session()
    session.headers.update(HEADERS)
    session.headers["Cookie"] = cookie
    seen = load_seen(state_file)
    is_first = not bool(seen)
    new_imp, new_min, processed_titles = [], [], set()

    for menu_key, (board_id, cat_name) in CATEGORIES.items():
        try: posts = fetch_list(session, menu_key, board_id)
        except SessionExpiredError as e: return {"new_important": [], "new_minor": [], "digest_top10": [], "error": str(e)}
        except Exception as e:
            print(f"[LIST ERROR] {menu_key}: {e}")
            continue
        time.sleep(REQUEST_DELAY_SEC)

        ex_ids = set(seen.get(menu_key, []))
        curr_ids = {p["post_id"] for p in posts}
        if is_first:
            ex_ids.update(curr_ids)
            seen[menu_key] = list(ex_ids)
            continue

        fresh = [p for p in posts if p["post_id"] not in ex_ids]
        for p in fresh:
            pid = p["post_id"]
            t_key = p["title"].strip().lower()
            if t_key in processed_titles:
                ex_ids.add(pid); continue
            try:
                detail = fetch_detail(session, menu_key, board_id, pid)
            except SessionExpiredError as e:
                seen[menu_key] = list(ex_ids); save_seen(seen, state_file)
                return {"new_important": new_imp, "new_minor": new_min, "digest_top10": [], "error": str(e)}
            except Exception as e:
                print(f"[DETAIL ERROR] post={pid}: {e}")
                ex_ids.add(pid); continue
            time.sleep(REQUEST_DELAY_SEC)

            verdict = summarize_with_gemini(p["title"], detail["text"], cat_name)
            processed_titles.add(t_key)
            entry = {"timestamp": datetime.now(timezone.utc).isoformat(), "category": cat_name, "post_id": pid,
                     "title": p["title"], "date": p["date"], "author": p["author"],
                     "important": verdict["important"], "summary": verdict["summary"], "attachments": detail["attachments"]}
            append_log(entry, log_file)
            (new_imp if entry["important"] else new_min).append(entry)
            ex_ids.add(pid)
        seen[menu_key] = list(ex_ids)

    save_seen(seen, state_file)
    return {"new_important": new_imp, "new_minor": new_min, "digest_top10": [], "error": None}

def run_once_for_user(uid: int, dgist_cookie=None) -> dict:
    paths = build_user_paths(uid)
    cookie = dgist_cookie if isinstance(dgist_cookie, str) else ""
    if not cookie and os.path.exists(paths["cookie_file"]):
        with open(paths["cookie_file"], "r", encoding="utf-8") as f: cookie = f.read().strip()
    if not cookie: raise RuntimeError(f"Не найдена cookie для пользователя {uid}")
    return run_once(cookie=cookie, state_file=paths["state_file"], log_file=paths["log_file"])

if __name__ == "__main__":
    c_file = os.path.join(BASE_DIR, "session_cookie.txt")
    c = open(c_file).read().strip() if os.path.exists(c_file) else ""
    print(json.dumps(run_once(cookie=c), ensure_ascii=False, indent=2))