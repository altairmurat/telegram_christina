import os, asyncio, base64, hashlib, secrets, json, re, urllib.parse
from email.message import EmailMessage
from fastapi import FastAPI, Request as FastAPIRequest
from fastapi.responses import RedirectResponse, HTMLResponse, JSONResponse
from pydantic import BaseModel
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from google.auth.exceptions import RefreshError
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build
from telethon import TelegramClient, events, Button, types

from env import API_ID, API_HASH, BOT_TOKEN, GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET, REDIRECT_URI, WEBAPP_URL
from database import SessionLocal, engine
import models
from llm import ask_gemini, transcribe_voice, process_telegram_image, parse_email_intent, generate_email_text
from dgist_bot_handlers import register_dgist_handlers, handle_dgist_conversation_step

os.environ["OAUTHLIB_RELAX_TOKEN_SCOPE"] = "1"
app = FastAPI()
client = TelegramClient('session_bot_korean', API_ID, API_HASH)

user_states, pending_photos, draft_cache, text_cache, pkce_store = {}, {}, {}, {}, {}

SCOPES = ["https://www.googleapis.com/auth/gmail.compose", "https://www.googleapis.com/auth/gmail.readonly"]
CLIENT_CONFIG = {"web": {"client_id": GOOGLE_CLIENT_ID, "client_secret": GOOGLE_CLIENT_SECRET,
                         "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                         "token_uri": "https://oauth2.googleapis.com/token", "redirect_uris": [REDIRECT_URI]}}

def build_auth_url(user_id: int) -> str:
    flow = Flow.from_client_config(CLIENT_CONFIG, scopes=SCOPES, redirect_uri=REDIRECT_URI)
    ver = base64.urlsafe_b64encode(secrets.token_bytes(64)).rstrip(b"=").decode()
    chal = base64.urlsafe_b64encode(hashlib.sha256(ver.encode()).digest()).rstrip(b"=").decode()
    pkce_store[user_id] = ver
    url, _ = flow.authorization_url(access_type="offline", prompt="consent", state=str(user_id),
                                    code_challenge=chal, code_challenge_method="S256")
    return url

def delete_user_token(user_id: int):
    with SessionLocal() as db:
        acc = db.query(models.GoogleAccount).filter_by(telegram_user_id=user_id).first()
        if acc:
            acc.token_json = None
            db.commit()

def save_user_token(user_id: int, creds: Credentials):
    with SessionLocal() as db:
        acc = db.query(models.GoogleAccount).filter_by(telegram_user_id=user_id).first()
        if not acc:
            acc = models.GoogleAccount(telegram_user_id=user_id)
            db.add(acc)
        acc.token_json = creds.to_json()
        db.commit()

def load_user_creds(user_id: int) -> Credentials | None:
    with SessionLocal() as db:
        acc = db.query(models.GoogleAccount).filter_by(telegram_user_id=user_id).first()
        if not acc or not acc.token_json:
            return None
        creds = Credentials.from_authorized_user_info(json.loads(acc.token_json))
        if creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
                acc.token_json = creds.to_json()
                db.commit()
            except RefreshError:
                delete_user_token(user_id)
                return None
        return creds

def get_gmail_client_for(user_id: int):
    creds = load_user_creds(user_id)
    return build("gmail", "v1", credentials=creds) if creds else None

@app.get("/gmail/connect/{user_id}")
async def gmail_connect(user_id: int):
    delete_user_token(user_id)
    return RedirectResponse(build_auth_url(user_id))

@app.get("/gmail/callback")
async def gmail_callback(request: FastAPIRequest):
    try:
        user_id = int(request.query_params.get("state"))
        flow = Flow.from_client_config(CLIENT_CONFIG, scopes=SCOPES, redirect_uri=REDIRECT_URI)
        flow.code_verifier = pkce_store.pop(user_id, None)
        flow.fetch_token(code=request.query_params.get("code"))
        save_user_token(user_id, flow.credentials)
        try: await client.send_message(user_id, "✅ Gmail подключен! Можешь просить меня писать письма.")
        except Exception: pass
        return HTMLResponse("<h2>Готово! Закрой эту вкладку и вернись в Telegram.</h2>")
    except Exception as e:
        return HTMLResponse(f"<h2>Ошибка: {e}</h2>", status_code=500)

def find_email_in_history(gmail, name: str) -> str | None:
    if not name or not name.strip(): return None
    first_name = name.strip().split()[0].lower()
    my_email = ""
    try: my_email = (gmail.users().getProfile(userId="me").execute().get("emailAddress") or "").lower()
    except Exception: pass
    try:
        res = gmail.users().messages().list(userId="me", q=f'"{name.strip()}"', maxResults=5).execute()
        for m in res.get("messages", []):
            msg = gmail.users().messages().get(userId="me", id=m["id"], format="metadata", metadataHeaders=["To", "From"]).execute()
            headers = {h["name"]: h["value"] for h in msg["payload"]["headers"]}
            for h in (headers.get("To", ""), headers.get("From", "")):
                if not h: continue
                if re.search(rf"\b{re.escape(first_name)}\b", h, re.IGNORECASE):
                    for em in re.findall(r"[\w.+-]+@[\w.-]+", h):
                        if em.lower() != my_email: return em
    except Exception: pass
    return None

def create_gmail_draft(gmail, to: str, subject: str, body: str) -> str:
    msg = EmailMessage()
    msg['To'], msg['Subject'] = to, subject
    msg.set_content(body, charset='utf-8')
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    return gmail.users().drafts().create(userId="me", body={"message": {"raw": raw}}).execute()["id"]

def is_valid_webapp_url(url: str) -> bool:
    if not url: return False
    u = url.strip().lower()
    return u.startswith("https://") and "localhost" not in u and "127.0.0.1" not in u

def make_draft_keyboard(draft_id: str, message_id: int, user_id: int = 0):
    base_url = WEBAPP_URL.strip().rstrip("/")
    btns = [
        [Button.inline("✅ Отправить", f"send:{draft_id}"), Button.inline("❌ Отмена", f"cancel:{draft_id}")]
    ]
    if is_valid_webapp_url(base_url):
        q = f"draft_id={urllib.parse.quote(draft_id)}&message_id={message_id}" + (f"&user_id={user_id}" if user_id else "")
        btns.append([types.KeyboardButtonWebView("✏️ Изменить текст", f"{base_url}/webapp?{q}")])
    else:
        btns.append([Button.inline("✏️ Изменить текст", f"edit:{draft_id}:{message_id}")])
    return btns

async def apply_draft_update(draft_id: str, new_text: str, user_id: int, message_id: int = 0):
    text_cache[draft_id] = new_text
    uid = user_id or draft_cache.get(draft_id)
    gmail = await asyncio.to_thread(get_gmail_client_for, uid) if uid else None
    to_val, subj_val = "", "Без темы"
    if gmail:
        try:
            info = await asyncio.to_thread(gmail.users().drafts().get(userId="me", id=draft_id, format="full").execute)
            hdrs = info.get("message", {}).get("payload", {}).get("headers", [])
            to_val = next((h["value"] for h in hdrs if h.get("name", "").lower() == "to"), "")
            subj_val = next((h["value"] for h in hdrs if h.get("name", "").lower() == "subject"), "Без темы")
            msg = EmailMessage(); msg['To'], msg['Subject'] = to_val, subj_val
            msg.set_content(new_text, charset='utf-8')
            raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
            await asyncio.to_thread(gmail.users().drafts().update(userId="me", id=draft_id, body={"message": {"raw": raw}}).execute)
        except Exception as e: print(f"Update draft error: {e}")
    if message_id != 0 and uid:
        try:
            btn = make_draft_keyboard(draft_id, message_id, uid)
            await client.edit_message(uid, message_id, f"📧 Черновик для {to_val}\n\nТема: {subj_val}\n\n{new_text}", buttons=btn)
        except Exception as e: print(f"Edit msg error: {e}")

async def send_draft_to_telegram(event, gmail, user_id: int, to: str, name: str, topic: str, s_name: str):
    subj, body = await generate_email_text(topic, name, s_name)
    draft_id = await asyncio.to_thread(create_gmail_draft, gmail, to, subj, body)
    draft_cache[draft_id], text_cache[draft_id] = user_id, body
    txt = f"📧 Черновик для {name} ({to})\n\nТема: {subj}\n\n{body}"
    msg = await event.reply(txt)
    try:
        await msg.edit(txt, buttons=make_draft_keyboard(draft_id, msg.id, user_id))
    except Exception:
        fallback_btns = [
            [Button.inline("✅ Отправить", f"send:{draft_id}"), Button.inline("❌ Отмена", f"cancel:{draft_id}")],
            [Button.inline("✏️ Изменить текст", f"edit:{draft_id}:{msg.id}")]
        ]
        await msg.edit(txt, buttons=fallback_btns)

async def try_handle_email_intent(event, user_id: int, text: str) -> bool:
    intent = await parse_email_intent(text)
    if not intent.is_email: return False
    gmail = await asyncio.to_thread(get_gmail_client_for, user_id)
    if not gmail:
        await event.reply(f"Сначала подключи Gmail: {build_auth_url(user_id)}")
        return True
    try:
        em = intent.recipient_email or await asyncio.to_thread(find_email_in_history, gmail, intent.recipient_name)
        if not em:
            user_states[user_id] = ("waiting_for_manual_email", intent.recipient_name, intent.topic, intent.sender_name)
            await event.reply(f"Не нашёл email для {intent.recipient_name}. Пришли его почту одним сообщением:")
            return True
        await send_draft_to_telegram(event, gmail, user_id, em, intent.recipient_name, intent.topic, intent.sender_name)
    except RefreshError:
        delete_user_token(user_id)
        await event.reply(f"⚠️ Токен Gmail истёк: {build_auth_url(user_id)}")
    return True

@client.on(events.CallbackQuery)
async def on_callback(event):
    data = event.data.decode()
    if ":" not in data: return
    parts = data.split(":")
    action, draft_id = parts[0], parts[1] if len(parts) > 1 else ""
    msg_id = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else (event.message_id or 0)
    uid = draft_cache.get(draft_id) or event.sender_id
    if action == "edit":
        user_states[uid] = ("waiting_for_draft_text", draft_id, msg_id)
        return await event.respond("✏️ Пришли новый текст для этого письма (или напиши /cancel):")
    gmail = await asyncio.to_thread(get_gmail_client_for, uid) if uid else None
    if not gmail:
        return await event.edit(f"❌ Gmail не подключён. Авторизуйся: {build_auth_url(event.sender_id)}")
    try:
        if action == "send":
            await asyncio.to_thread(gmail.users().drafts().send(userId="me", body={"id": draft_id}).execute)
            await event.edit("✅ Отправлено")
        elif action == "cancel":
            await asyncio.to_thread(gmail.users().drafts().delete(userId="me", id=draft_id).execute)
            await event.edit("❌ Отменено")
    except Exception as e:
        await event.edit("⚠️ Черновик уже отправлен или удалён." if "404" in str(e) else f"Ошибка: {e}")
    finally:
        if action in ("send", "cancel"):
            draft_cache.pop(draft_id, None)
            text_cache.pop(draft_id, None)

@app.on_event("startup")
async def startup_event():
    try: models.Base.metadata.create_all(bind=engine)
    except Exception as e: print(f"DB init error: {e}")
    await client.start(bot_token=BOT_TOKEN)
    register_dgist_handlers(client, user_states)
    asyncio.create_task(client.run_until_disconnected())

@app.on_event("shutdown")
async def shutdown_event():
    await client.disconnect()

@app.get("/")
async def ping(): return {"status": "ok"}

def save_communication(username, usermessage):
    with SessionLocal() as db:
        db.add(models.Communication(username=username, usermessage=usermessage))
        db.commit()

class EditDraftModel(BaseModel):
    chat_id: int; message_id: int; draft_id: str; new_text: str

@app.get("/get-text")
async def get_text(draft_id: str):
    return {"text": text_cache.get(draft_id, "Текст не найден или устарел")}

@app.get("/webapp", response_class=HTMLResponse)
async def get_webapp(draft_id: str = "", message_id: int = 0, user_id: int = 0):
    return f"""<!DOCTYPE html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<script src="https://telegram.org/js/telegram-web-app.js"></script><style>
body{{font-family:sans-serif;margin:0;padding:15px;display:flex;flex-direction:column;height:90vh;background:var(--tg-theme-bg-color,#fff);color:var(--tg-theme-text-color,#000)}}
textarea{{width:100%;flex-grow:1;padding:10px;border-radius:8px;border:1px solid #ccc;font-size:15px;box-sizing:border-box;resize:none}}
button{{margin-top:12px;padding:12px;background:#2481cc;color:#fff;border:none;border-radius:8px;font-size:16px;font-weight:bold;cursor:pointer}}
</style></head><body><h3>Редактирование письма</h3><textarea id="t" placeholder="Загрузка..."></textarea><button id="b">Сохранить</button>
<script>
const tg=window.Telegram.WebApp;tg.ready();tg.expand();
const p=new URLSearchParams(location.search),did=p.get('draft_id')||"",mid=parseInt(p.get('message_id')||"0"),uid=parseInt(p.get('user_id')||"0");
fetch('/get-text?draft_id='+encodeURIComponent(did)).then(r=>r.json()).then(d=>document.getElementById('t').value=d.text);
document.getElementById('b').onclick=async()=>{{
  const ok=(await fetch('/update-draft',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{chat_id:tg.initDataUnsafe.user?.id||uid||0,message_id:mid,draft_id:did,new_text:document.getElementById('t').value}})}})).ok;
  if(ok)tg.close();else alert('Ошибка сохранения!');
}};
</script></body></html>"""

@app.post("/update-draft")
async def update_draft(data: EditDraftModel):
    uid = draft_cache.get(data.draft_id) or (data.chat_id if data.chat_id != 0 else 0)
    await apply_draft_update(data.draft_id, data.new_text, uid, data.message_id)
    return {"status": "success"}

@client.on(events.NewMessage(pattern='/start'))
async def start_msg(e):
    await e.respond('Привет! Я Кристина — твой личный ассистент на базе Google Gemini. Помогаю писать email, работать с фото/голосом и отслеживать портал DGIST!')

@client.on(events.NewMessage(pattern=r'^/(connectgmail|reconnect)'))
async def connect_gmail(e):
    delete_user_token(e.sender_id)
    await e.respond(f"⚠️ **Подключение Gmail**\n\nАвторизуйся по ссылке:\n{build_auth_url(e.sender_id)}")

@client.on(events.NewMessage(pattern='/sendShak'))
async def send_shak(e):
    user_states[e.sender_id] = "waiting_for_submitmessage"
    await e.respond('Напиши сообщение в формате: `!username/текст`')

@client.on(events.NewMessage)
async def necessary_task_handler(event):
    if not event.is_private or (event.text and event.text.startswith("/")): return
    uid, sender = event.sender_id, await event.get_sender()
    state = user_states.get(uid)

    if await handle_dgist_conversation_step(event, uid, state, user_states): return

    if isinstance(state, tuple) and state[0] == "waiting_for_manual_email":
        _, name, topic, s_name = state
        m = re.search(r"[\w.+-]+@[\w.-]+", event.text or "")
        if m:
            gmail = await asyncio.to_thread(get_gmail_client_for, uid)
            user_states.pop(uid, None)
            if gmail: await send_draft_to_telegram(event, gmail, uid, m.group(0), name, topic, s_name)
            else: await event.reply(f"Сессия не найдена. Авторизуйся: {build_auth_url(uid)}")
        else: await event.reply("Это не похоже на email. Попробуй ещё раз или введи /cancel")
        return

    if isinstance(state, tuple) and state[0] == "waiting_for_draft_text":
        _, draft_id, msg_id = state
        new_text = (event.text or "").strip()
        user_states.pop(uid, None)
        if not new_text:
            await event.reply("Текст не может быть пустым.")
            return
        await apply_draft_update(draft_id, new_text, uid, msg_id)
        await event.reply("✅ Текст черновика обновлён!")
        return

    if event.text and event.text.startswith("!") and state == "waiting_for_submitmessage":
        parts = event.text[1:].split("/", 1)
        if len(parts) == 2 and parts[0].strip() and parts[1].strip():
            try:
                await client.send_message(parts[0].strip(), parts[1].strip())
                await event.reply("✅ Сообщение отправлено!")
            except Exception as ex: await event.reply(f"❌ Ошибка отправки: {ex}")
            finally: user_states.pop(uid, None)
        else: await event.reply("Формат: `!username/текст сообщения`")
        return

    if event.message.voice:
        try:
            vb = await event.message.download_media(file=bytes)
            if not vb: return await event.reply("Не удалось скачать аудио.")
            txt = await transcribe_voice(vb)
            if txt:
                if not await try_handle_email_intent(event, uid, txt):
                    ans = await ask_gemini(txt, user_id=uid)
                    await event.respond(ans)
                    await asyncio.to_thread(save_communication, sender.username if sender else None, txt)
            else: await event.reply("Не удалось распознать речь.")
        except Exception as ex: await event.reply(f"Ошибка аудио: {ex}")
        finally: user_states.pop(uid, None)

    elif event.photo:
        pending_photos[uid] = event.message
        user_states[uid] = "waiting_for_imageprompt"
        await event.respond("Получила фотку! Что с ней сделать?")

    elif state == "waiting_for_imageprompt" and event.text:
        pm = pending_photos.pop(uid, None)
        user_states.pop(uid, None)
        if pm:
            await event.respond("Обрабатываю...")
            try: await event.respond(await process_telegram_image(pm, event.text))
            except Exception as ex: await event.respond(f"Ошибка: {ex}")

    elif event.text:
        try:
            if not await try_handle_email_intent(event, uid, event.text):
                ans = await ask_gemini(event.text, user_id=uid)
                await event.respond(ans)
                await asyncio.to_thread(save_communication, sender.username if sender else None, event.text)
        except Exception as ex: await event.respond(f"Ошибка: {ex}")