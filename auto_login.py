#!/usr/bin/env python3
"""DGIST Auto-Login с 2FA через почту (Playwright) — конденсированная версия."""
import os, sys, time, re, imaplib, email
from email.header import decode_header
from email.utils import parsedate_to_datetime
from playwright.sync_api import sync_playwright
from dotenv import load_dotenv

load_dotenv()

PORTAL_LOGIN_URL = "https://my.dgist.ac.kr/com/portal/index.do?language=en"
ECM_URL = "https://stuecm.dgist.ac.kr/site/ecmSiteList/index.do"
USERNAME_SEL, PASS_SEL = "input#loginID", "input#password"
LOGIN_BTN, ALERT_BTN = "button[onclick='passwordLogin();']", "button#alert_btn"
CODE_SEL, CODE_SUBMIT = "input#code", "button[onclick='ok();']"
USER_MARKER = "div.user-info span.name"
CODE_REGEX = r"\b(\d{6})\b"

def _decode(val):
    if not val: return ""
    return "".join(t.decode(e or "utf-8", "ignore") if isinstance(t, bytes) else str(t) for t, e in decode_header(val))

def _get_body(msg) -> str:
    if msg.is_multipart():
        plain, html = [], []
        for p in msg.walk():
            if "attachment" in str(p.get("Content-Disposition", "")): continue
            ct = p.get_content_type()
            try:
                pay = p.get_payload(decode=True)
                if not pay: continue
                txt = pay.decode(p.get_content_charset() or "utf-8", "ignore")
                (plain if ct == "text/plain" else html if ct == "text/html" else []).append(txt)
            except Exception: pass
        if plain: return "\n".join(plain)
        if html: return "\n".join(html)
    try:
        pay = msg.get_payload(decode=True)
        if pay: return pay.decode(msg.get_content_charset() or "utf-8", "ignore")
    except Exception: pass
    return str(msg.get_payload() or "")

def fetch_verification_code(min_timestamp: float, email_addr: str, email_pass: str, imap_host: str = "imap.gmail.com") -> str:
    deadline = time.time() + 90
    since_date = time.strftime("%d-%b-%Y", time.gmtime(min_timestamp - 86400))

    while time.time() < deadline:
        try:
            M = imaplib.IMAP4_SSL(imap_host, 993)
            try:
                M.login(email_addr, email_pass)
                M.select("INBOX", readonly=True)
                _, data = M.search(None, "SINCE", since_date, "FROM", '"no-reply@dgist.ac.kr"')
                if data and data[0]:
                    candidates = []
                    for mid in data[0].split():
                        _, hdr_data = M.fetch(mid, "(BODY.PEEK[HEADER])")
                        if not hdr_data or not hdr_data[0]: continue
                        hdr = email.message_from_bytes(hdr_data[0][1])
                        if "인증" not in _decode(hdr.get("Subject")): continue
                        try: ts = parsedate_to_datetime(hdr.get("Date")).timestamp()
                        except Exception: ts = 0
                        if ts >= min_timestamp - 120: candidates.append((ts, mid))

                    if candidates:
                        candidates.sort(key=lambda x: x[0], reverse=True)
                        _, body_data = M.fetch(candidates[0][1], "(BODY.PEEK[])")
                        if body_data and body_data[0]:
                            text = re.sub(r"<[^>]+>", " ", _get_body(email.message_from_bytes(body_data[0][1])))
                            m = (re.search(r"(?:인증번호|인증\s*код|code|verification)[\s:=*#\[\]]*([0-9]{6})", text, re.I)
                                 or re.search(CODE_REGEX, text))
                            if m: return m.group(1)
            finally:
                try: M.logout()
                except Exception: pass
        except Exception: pass
        time.sleep(3)
    raise TimeoutError("Не дождался письма с кодом подтверждения.")

def wait_for_sso_to_finish(page, timeout_ms: int = 120000) -> bool:
    deadline = time.time() + (timeout_ms / 1000)
    while time.time() < deadline:
        url = page.url
        if "my.dgist.ac.kr" in url and "login" not in url.lower(): return True
        try: page.wait_for_timeout(1000)
        except Exception: break
    return "my.dgist.ac.kr" in page.url and "login" not in page.url.lower()

def open_ecm(page) -> bool:
    for _ in range(3):
        try: page.goto(ECM_URL, wait_until="domcontentloaded", timeout=60000)
        except Exception: pass
        page.wait_for_timeout(4000)
        if "stuecm.dgist.ac.kr" in page.url and "isign" not in page.url: return True
        if "isign.dgist.ac.kr" in page.url or "savetoken" in page.url.lower():
            wait_for_sso_to_finish(page, timeout_ms=60000)
            page.wait_for_timeout(3000)
            if "stuecm.dgist.ac.kr" in page.url and "isign" not in page.url: return True
    return False

def save_cookies(context, out_file: str) -> str:
    cookies = context.cookies()
    seen = set()
    parts = []
    for c in cookies:
        if "dgist.ac.kr" in c.get("domain", ""):
            key = (c["domain"], c["name"])
            if key not in seen and c.get("name") and c.get("value"):
                seen.add(key)
                parts.append(f"{c['name']}={c['value']}")
    cookie_str = "; ".join(parts)
    os.makedirs(os.path.dirname(out_file) or ".", exist_ok=True)
    with open(out_file, "w", encoding="utf-8") as f: f.write(cookie_str)
    return cookie_str

def login_and_get_cookie(dgist_user, dgist_pass, email_addr, email_pass, profile_dir, cookie_file, imap_host="imap.gmail.com"):
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            profile_dir, headless=True,
            args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage", "--disable-gpu"]
        )
        try:
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            page.goto(PORTAL_LOGIN_URL, wait_until="networkidle", timeout=60000)
            try:
                page.wait_for_selector(USER_MARKER, timeout=5000)
                alive = True
            except Exception: alive = False

            if not alive:
                page.fill(USERNAME_SEL, dgist_user)
                page.fill(PASS_SEL, dgist_pass)
                click_ts = time.time()
                page.click(LOGIN_BTN)
                page.wait_for_selector(ALERT_BTN, timeout=15000)
                page.click(ALERT_BTN)
                page.wait_for_selector(CODE_SEL, timeout=15000)
                code = fetch_verification_code(click_ts, email_addr, email_pass, imap_host)
                page.fill(CODE_SEL, code)
                page.click(CODE_SUBMIT)
                if not wait_for_sso_to_finish(page, 120000):
                    raise RuntimeError(f"SSO не завершился: {page.url}")

            if not open_ecm(page):
                raise RuntimeError(f"Не удалось открыть ECM: {page.url}")

            return save_cookies(ctx, cookie_file)
        finally:
            try: ctx.close()
            except Exception: pass

def login_for_telegram_user(uid: int, dgist_user, dgist_pass, email_addr, email_pass, imap_host="imap.gmail.com"):
    from dgist_monitor import build_user_paths
    p = build_user_paths(uid)
    return login_and_get_cookie(dgist_user, dgist_pass, email_addr, email_pass, p["browser_profile_dir"], p["cookie_file"], imap_host)

if __name__ == "__main__":
    u, p = os.getenv("DGIST_USERNAME"), os.getenv("DGIST_PASSWORD")
    em, ep = os.getenv("EMAIL_ADDRESS"), os.getenv("EMAIL_APP_PASSWORD")
    if u and p:
        login_and_get_cookie(u, p, em, ep, "browser_profile", "session_cookie.txt", os.getenv("EMAIL_IMAP_HOST", "imap.gmail.com"))