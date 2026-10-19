import os, io, asyncio, base64, hashlib, secrets, json, re, urllib.parse
from collections import defaultdict
from email.message import EmailMessage
from fastapi import FastAPI, Request as FastAPIRequest
from fastapi.responses import RedirectResponse, HTMLResponse, JSONResponse
from pydantic import BaseModel
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from google.auth.exceptions import RefreshError
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build
from telethon import TelegramClient, events, Button, types, functions

from env import API_ID, API_HASH, BOT_TOKEN, GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET, REDIRECT_URI, WEBAPP_URL
from database import SessionLocal, engine
import models
from llm import (
    ask_gemini, transcribe_voice, process_telegram_images,
    parse_user_intent, generate_email_text, generate_pdf_from_prompt,
    answer_with_group_context, prepare_video_prompt_en, detect_photo_video_intent,
    moderate_and_prepare_video_prompt
)
from dgist_bot_handlers import (
    register_dgist_handlers, handle_dgist_conversation_step,
    run_portal_check, remember_group_topic, is_active_group_topic
)
from group_memory import (
    record_group_message, search_group_messages, list_known_topics,
    format_messages_for_prompt, get_default_chat_id
)
from google_calendar import (
    create_calendar_event, list_calendar_events, schedule_telegram_reminder,
    run_reminder_worker
)
from fal_video import (
    is_owner, is_video_admin, add_video_admins, remove_video_admins,
    list_video_admins, check_video_quota, record_video_generation,
    mark_generation_started, mark_generation_finished, _generate_video_fal_sync,
    check_hard_safety_filter, VIDEO_MODELS, DEFAULT_MODEL_KEY, DEFAULT_DURATION_SEC,
    get_admin_prefs, set_admin_prefs, extract_hints_from_text
)

os.environ["OAUTHLIB_RELAX_TOKEN_SCOPE"] = "1"
app = FastAPI()
client = TelegramClient('session_bot_korean', API_ID, API_HASH)

user_states, draft_cache, text_cache, pkce_store, video_job_cache = {}, {}, {}, {}, {}
pending_photos, pending_captions, photo_tasks = defaultdict(list), {}, {}

SCOPES = [
    "https://www.googleapis.com/auth/gmail.compose",
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/calendar.events",
    "https://www.googleapis.com/auth/calendar.readonly"
]
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
        try: await client.send_message(user_id, "✅ Google аккаунт (Календарь и Gmail) успешно подключён! Можешь просить меня записывать планы в календарь, напоминать о делах и писать письма.")
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

def format_video_admin_panel(job: dict) -> str:
    m_key = job["model"]
    dur = job["duration"]
    m_info = VIDEO_MODELS.get(m_key, VIDEO_MODELS[DEFAULT_MODEL_KEY])
    mode_str = "📸 Фото → Видео" if job.get("image_bytes") else "📝 Текст → Видео"
    return (
        f"🎬 **Панель генерации видео (Режим Админа)**\n"
        f"🔹 **Тип:** {mode_str}\n"
        f"🔹 **Сюжет:** _{job['prompt_en'][:220]}_\n\n"
        f"🤖 **Модель:** **{m_info['title']}** ({m_info['price_hint']})\n"
        f"⏱ **Длительность:** **{dur} сек**\n\n"
        f"Выбери модель и время (секунды) ниже, затем нажми **«🚀 Сгенерировать»**:"
    )

def make_video_admin_keyboard(job_id: str, model_key: str, duration_sec: int):
    model_keys = list(VIDEO_MODELS.keys())
    rows = []
    # Кнопки выбора модели (по 2 в ряд)
    for i in range(0, len(model_keys), 2):
        row = []
        for mk in model_keys[i:i + 2]:
            short_lbl = VIDEO_MODELS[mk]["short"]
            prefix = "✅ " if mk == model_key else ""
            row.append(Button.inline(f"{prefix}{short_lbl}", f"vmod:{job_id}:{mk}"))
        rows.append(row)

    # Кнопки выбора длительности (секунды), поддерживаемые выбранной моделью
    allowed_durs = VIDEO_MODELS.get(model_key, VIDEO_MODELS[DEFAULT_MODEL_KEY])["durations"]
    dur_row = []
    for d in allowed_durs:
        prefix = "✅ " if int(d) == int(duration_sec) else ""
        dur_row.append(Button.inline(f"{prefix}{d} сек", f"vdur:{job_id}:{d}"))
    rows.append(dur_row)

    # Кнопки запуска и отмены
    rows.append([
        Button.inline("🚀 Сгенерировать видео", f"vrun:{job_id}"),
        Button.inline("❌ Отмена", f"vcancel:{job_id}"),
    ])
    return rows

async def handle_video_generation(
    event,
    user_id: int,
    username: str | None,
    user_text: str,
    prompt_en: str = "",
    image_bytes: bytes | None = None
):
    # 1. Быстрая жёсткая проверка текста по чёрному списку до любых запросов
    safe_txt, block_msg = check_hard_safety_filter(user_text)
    if not safe_txt:
        await event.reply(block_msg)
        return
    safe_en, block_msg_en = check_hard_safety_filter(prompt_en)
    if not safe_en:
        await event.reply(block_msg_en)
        return

    # 2. Проверка прав админа и суточного лимита
    is_adm = await asyncio.to_thread(is_video_admin, user_id, username)
    allowed, err_msg = await asyncio.to_thread(check_video_quota, user_id, username)
    if not allowed:
        await event.reply(err_msg)
        return

    status = await event.reply(
        "🎬 Подготавливаю панель генерации..." if is_adm
        else "🎬 Проверяю запрос и генерирую 1-секундное видео (~15–35 секунд)..."
    )

    try:
        # 3. ИИ-модерация безопасности идеи и прикреплённого фото (если есть) до отправки в fal.ai
        is_safe, final_prompt_en, reject_reason = await moderate_and_prepare_video_prompt(
            user_text, prompt_en, image_bytes=image_bytes
        )
        if not is_safe:
            await status.edit(reject_reason)
            return

        # Повторная проверка итогового промпта по стоп-словам
        safe_final, block_final = check_hard_safety_filter(final_prompt_en)
        if not safe_final:
            await status.edit(block_final)
            return

        # 4А. Если это АДМИН — открываем интерактивное меню выбора модели и секунд
        if is_adm:
            m_hint, d_hint = extract_hints_from_text(user_text)
            prefs = set_admin_prefs(user_id, m_hint, d_hint)
            job_id = secrets.token_hex(4)
            job = {
                "user_id": user_id,
                "username": username,
                "chat_id": event.chat_id,
                "reply_to": event.id,
                "user_text": user_text,
                "prompt_en": final_prompt_en,
                "image_bytes": image_bytes,
                "model": prefs["model"],
                "duration": prefs["duration"],
            }
            video_job_cache[job_id] = job
            panel_txt = format_video_admin_panel(job)
            kb = make_video_admin_keyboard(job_id, job["model"], job["duration"])
            await status.edit(panel_txt, buttons=kb)
            return

        # 4Б. Если это ОБЫЧНЫЙ ПОЛЬЗОВАТЕЛЬ — строго 1 секунда на самой слабой модели без выбора
        mark_generation_started(user_id)
        try:
            vid_bytes = await asyncio.to_thread(
                _generate_video_fal_sync,
                final_prompt_en,
                image_bytes,
                DEFAULT_MODEL_KEY,
                DEFAULT_DURATION_SEC,
                False,
            )
            await asyncio.to_thread(record_video_generation, user_id, username, user_text)

            vid_file = io.BytesIO(vid_bytes)
            vid_file.name = "video.mp4"
            await client.send_file(
                event.chat_id,
                vid_file,
                caption="🎬 Готово! Вот твоё видео.",
                reply_to=event.id,
                supports_streaming=True,
            )
            await status.delete()
        finally:
            mark_generation_finished(user_id)
    except Exception as ex:
        await status.edit(f"❌ Не удалось сгенерировать видео: {ex}")

async def try_handle_email_intent(event, user_id: int, text: str) -> bool:
    intent = await parse_user_intent(text)
    if intent.is_video:
        sender = await event.get_sender()
        uname = getattr(sender, "username", None) if sender else None
        await handle_video_generation(event, user_id, uname, text, prompt_en=intent.video_prompt_en)
        return True

    if intent.is_portal:
        await run_portal_check(event)
        return True

    if intent.is_group_query:
        cid = event.chat_id if not event.is_private else get_default_chat_id()
        msgs = search_group_messages(
            query=intent.group_search_query or text,
            topic_name=intent.group_topic_name,
            chat_id=cid,
            limit=30
        )
        if not msgs and intent.group_topic_name:
            # Если по конкретному названию не нашлось, берем последние сообщения группы
            msgs = search_group_messages(chat_id=cid, limit=25)

        if not msgs:
            known = list_known_topics(cid)
            if not known:
                await event.reply(
                    "🔍 Я пока не сохранила сообщений из группы.\n\n"
                    "Чтобы я могла читать топики:\n"
                    "1. Добавь меня **Администратором** в группу (с правом читать сообщения) ИЛИ в @BotFather отключи `/setprivacy`.\n"
                    "2. После этого я автоматически буду сохранять и знать все сообщения каждого топика!"
                )
            else:
                top_names = ", ".join([f"«{t['topic_title']}»" for t in known])
                await event.reply(
                    f"🔍 Я не нашла сообщений по этому вопросу.\n"
                    f"Отслеживаемые топики в группе: {top_names}."
                )
            return True

        ctx = format_messages_for_prompt(msgs)
        ans = await answer_with_group_context(text, ctx, user_id=user_id)
        await event.reply(ans)
        return True

    if intent.is_pdf:
        status = await event.reply("📄 Генерирую PDF-файл, подожди пару секунд...")
        try:
            pdf_prompt = text
            if "групп" in text.lower() or "топик" in text.lower() or intent.group_topic_name:
                cid = event.chat_id if not event.is_private else get_default_chat_id()
                msgs = search_group_messages(topic_name=intent.group_topic_name, chat_id=cid, limit=20)
                if msgs:
                    pdf_prompt += f"\n\nДанные из сообщений группы:\n{format_messages_for_prompt(msgs)}"
            pdf_bytes = await generate_pdf_from_prompt(pdf_prompt, user_id=user_id)
            fname = (intent.pdf_filename or "document.pdf").strip()
            if not fname.lower().endswith(".pdf"): fname += ".pdf"
            pdf_file = io.BytesIO(pdf_bytes)
            pdf_file.name = fname
            await client.send_file(event.chat_id, pdf_file, caption=f"✅ Готово! Документ: **{fname}**", reply_to=event.id)
            await status.delete()
        except Exception as ex:
            await status.edit(f"❌ Не удалось сгенерировать PDF: {ex}")
        return True

    if intent.is_calendar:
        creds = await asyncio.to_thread(load_user_creds, user_id)
        if not creds:
            await event.reply(
                f"📅 Для работы с Google Календарём сначала подключи Google аккаунт:\n{build_auth_url(user_id)}"
            )
            return True

        if intent.calendar_action == "list":
            status_list = await event.reply("📅 Загружаю события из Google Календаря...")
            try:
                events_list = await asyncio.to_thread(list_calendar_events, creds, 10)
                if not events_list:
                    await status_list.edit("📅 На ближайшее время событий в Google Календаре не найдено.")
                    return True
                lines = ["📅 **Ближайшие планы в Google Календаре:**\n"]
                for idx, ev in enumerate(events_list, 1):
                    badge = "🔥 [Важно] " if ev.get("is_important") else ""
                    st = ev.get("start") or ""
                    if "T" in st:
                        d_part, t_part = st.split("T", 1)
                        st_clean = f"{d_part[5:]} в {t_part[:5]}"
                    else:
                        st_clean = st
                    lines.append(f"{idx}. {badge}**{ev['summary']}** — {st_clean}")
                    if ev.get("link"):
                        lines.append(f"   🔗 [Открыть]({ev['link']})")
                await status_list.edit("\n".join(lines), link_preview=False)
            except Exception as ex:
                err_str = str(ex).lower()
                if "disabled" in err_str or "not been used in project" in err_str or "accessnotconfigured" in err_str:
                    await status_list.edit(
                        "❌ **Google Calendar API не включён в твоём Google Cloud!**\n\n"
                        "Перейди по ссылке и нажми кнопку **«Включить» (Enable)**:\n"
                        "👉 https://console.cloud.google.com/apis/library/calendar-json.googleapis.com\n\n"
                        "После включения просто нажми /calendar снова (переподключать не нужно)!"
                    )
                elif "insufficient" in err_str or "invalid_grant" in err_str:
                    delete_user_token(user_id)
                    await status_list.edit(f"⚠️ Нужно обновить доступ к Google Календарю: {build_auth_url(user_id)}")
                else:
                    await status_list.edit(f"❌ Ошибка получения событий: {ex}")
            return True

        # calendar_action == "create"
        summary = intent.calendar_summary or "Новая задача"
        start_iso = intent.calendar_start_iso
        if not start_iso:
            from datetime import datetime, timedelta
            start_iso = (datetime.now() + timedelta(hours=1)).strftime("%Y-%m-%dT%H:00:00")

        status_msg = await event.reply("📅 Записываю событие в Google Календарь...")
        try:
            created_event = await asyncio.to_thread(
                create_calendar_event,
                creds,
                summary,
                start_iso,
                intent.calendar_end_iso,
                intent.calendar_description,
                intent.is_important,
                intent.reminder_minutes
            )
            event_link = created_event.get("htmlLink", "")

            # Планируем прямое напоминание в Telegram
            mins_before = 30 if intent.is_important else 15
            schedule_telegram_reminder(
                telegram_user_id=user_id,
                title=summary,
                start_iso=start_iso,
                is_important=intent.is_important,
                event_id=created_event.get("id"),
                minutes_before=mins_before
            )

            priority_str = "🔥 **Очень важно (Высокий приоритет)**" if intent.is_important else "Обычный"
            remind_str = "за 10, 30 и 60 мин (email + push)" if intent.is_important else f"за {mins_before} мин (push)"
            time_display = start_iso.replace("T", " в ")[:18]

            resp_text = (
                f"✅ **Событие добавлено в Google Календарь!**\n\n"
                f"📌 **Задача:** {summary}\n"
                f"🕒 **Время:** {time_display}\n"
                f"⚡ **Приоритет:** {priority_str}\n"
                f"🔔 **Напоминания:** {remind_str} + в Telegram за {mins_before} мин"
            )
            btns = []
            if event_link:
                btns.append([Button.url("📅 Открыть в Календаре", event_link)])
            await status_msg.edit(resp_text, buttons=btns if btns else None, link_preview=False)
        except Exception as ex:
            err_str = str(ex).lower()
            if "disabled" in err_str or "not been used in project" in err_str or "accessnotconfigured" in err_str:
                await status_msg.edit(
                    "❌ **Google Calendar API не включён в твоём Google Cloud!**\n\n"
                    "Перейди по ссылке и нажми синюю кнопку **«Включить» (Enable)**:\n"
                    "👉 https://console.cloud.google.com/apis/library/calendar-json.googleapis.com\n\n"
                    "После включения просто напиши мне свои планы ещё раз (аккаунт заново подключать не нужно)!"
                )
            elif "insufficient" in err_str or "invalid_grant" in err_str:
                delete_user_token(user_id)
                await status_msg.edit(f"⚠️ Нужно обновить доступ к Google Календарю: {build_auth_url(user_id)}")
            else:
                await status_msg.edit(f"❌ Ошибка добавления в календарь: {ex}")
        return True

    if not intent.is_email: return False
    gmail = await asyncio.to_thread(get_gmail_client_for, user_id)
    if not gmail:
        await event.reply(f"Сначала подключи Gmail: {build_auth_url(user_id)}")
        return True
    try:
        topic_text = intent.topic
        if "групп" in text.lower() or "топик" in text.lower() or intent.group_topic_name:
            cid = event.chat_id if not event.is_private else get_default_chat_id()
            msgs = search_group_messages(topic_name=intent.group_topic_name, chat_id=cid, limit=15)
            if msgs:
                topic_text += f"\n\nДанные из группы:\n{format_messages_for_prompt(msgs)}"
        em = intent.recipient_email or await asyncio.to_thread(find_email_in_history, gmail, intent.recipient_name)
        if not em:
            user_states[user_id] = ("waiting_for_manual_email", intent.recipient_name, topic_text, intent.sender_name)
            await event.reply(f"Не нашёл email для {intent.recipient_name}. Пришли его почту одним сообщением:")
            return True
        await send_draft_to_telegram(event, gmail, user_id, em, intent.recipient_name, topic_text, intent.sender_name)
    except RefreshError:
        delete_user_token(user_id)
        await event.reply(f"⚠️ Токен Gmail истёк: {build_auth_url(user_id)}")
    return True

@client.on(events.CallbackQuery)
async def on_callback(event):
    data = event.data.decode()
    if ":" not in data: return
    parts = data.split(":")
    action, arg1 = parts[0], parts[1] if len(parts) > 1 else ""

    # Обработка кнопок админ-панели генерации видео
    if action in ("vmod", "vdur", "vrun", "vcancel"):
        job_id = arg1
        job = video_job_cache.get(job_id)
        if not job:
            return await event.answer("⚠️ Эта панель устарела или уже запущена.", alert=True)

        sender = await event.get_sender()
        uname = getattr(sender, "username", None) if sender else None
        is_adm = await asyncio.to_thread(is_video_admin, event.sender_id, uname)
        if not is_adm:
            return await event.answer("⛔ Выбор модели и длительности доступен только админам!", alert=True)

        if action == "vcancel":
            video_job_cache.pop(job_id, None)
            return await event.edit("❌ Генерация видео отменена.", buttons=None)

        if action == "vmod" and len(parts) > 2:
            new_model = parts[2]
            prefs = set_admin_prefs(event.sender_id, model_key=new_model)
            job["model"] = prefs["model"]
            job["duration"] = prefs["duration"]
            return await event.edit(
                format_video_admin_panel(job),
                buttons=make_video_admin_keyboard(job_id, job["model"], job["duration"])
            )

        if action == "vdur" and len(parts) > 2 and parts[2].isdigit():
            new_dur = int(parts[2])
            prefs = set_admin_prefs(event.sender_id, duration=new_dur)
            job["duration"] = prefs["duration"]
            return await event.edit(
                format_video_admin_panel(job),
                buttons=make_video_admin_keyboard(job_id, job["model"], job["duration"])
            )

        if action == "vrun":
            video_job_cache.pop(job_id, None)
            m_key = job["model"]
            dur = job["duration"]
            m_title = VIDEO_MODELS.get(m_key, VIDEO_MODELS[DEFAULT_MODEL_KEY])["short"]
            mark_generation_started(job["user_id"])
            await event.edit(
                f"🎬 **Генерирую видео...**\n"
                f"🤖 Модель: **{m_title}**\n"
                f"⏱ Длительность: **{dur} сек**\n\n"
                f"⏳ Подожди немного (~15–60 секунд)...",
                buttons=None
            )
            try:
                vid_bytes = await asyncio.to_thread(
                    _generate_video_fal_sync,
                    job["prompt_en"],
                    job.get("image_bytes"),
                    m_key,
                    dur,
                    True,
                )
                await asyncio.to_thread(
                    record_video_generation,
                    job["user_id"],
                    job.get("username"),
                    job["user_text"]
                )
                vid_file = io.BytesIO(vid_bytes)
                vid_file.name = "video.mp4"
                await client.send_file(
                    job["chat_id"],
                    vid_file,
                    caption=f"🎬 Готово! (**{m_title}**, **{dur} сек**)",
                    reply_to=job["reply_to"],
                    supports_streaming=True,
                )
                await event.delete()
            except Exception as ex:
                await event.edit(f"❌ Не удалось сгенерировать видео: {ex}", buttons=None)
            finally:
                mark_generation_finished(job["user_id"])
            return

    draft_id = arg1
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
    asyncio.create_task(run_reminder_worker(client))
    try:
        await client(functions.bots.SetBotCommandsRequest(
            scope=types.BotCommandScopeDefault(),
            lang_code="",
            commands=[
                types.BotCommand("help", "Все возможности и команды"),
                types.BotCommand("calendar", "Ближайшие планы в Google Календаре"),
                types.BotCommand("connectgoogle", "Подключить Google (Календарь и Gmail)"),
                types.BotCommand("topics", "Список отслеживаемых топиков"),
                types.BotCommand("checkportal", "Проверить объявления DGIST"),
                types.BotCommand("connectmyportal", "Подключить портал DGIST"),
                types.BotCommand("disconnectportal", "Отключить портал DGIST"),
                types.BotCommand("cancel", "Отменить действие"),
            ]
        ))
    except Exception as e:
        print(f"Set commands error: {e}")
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

HELP_TEXT = """🤖 **Что умеет Кристина:**

📌 **Команды:**
• /help — список возможностей
• /calendar — ближайшие планы в календаре
• /connectgoogle — подключить Google (Календарь и Gmail)
• /topics — список топиков в группе
• /checkportal — проверить объявления DGIST
• /connectmyportal — подключить портал DGIST (в ЛС)
• /disconnectportal — отключить портал DGIST
• /cancel — отменить текущее действие

✨ **Возможности (просто напиши или скажи голосом):**
• 💬 **Ответы на любые вопросы** — просто напиши мне (в группе тегни через `@` или напиши *«Кристина, ...»*).
• 🎬 **Генерация видео** — напиши *«Сгенерируй видео на тему...»* или отправь фото с подписью *«Сгенерируй видео на...»*, и я создам короткий видеоролик (доступно 1 видео в день).
• 📅 **Google Календарь и напоминания** — напиши, например: *«Запиши в календарь: сегодня в 19:00 созвон, это оч важно, напомни»* или *«Что у меня по планам?»* — я добавлю событие в календарь, настрою push-уведомления и пришлю напоминание прямо в Telegram!
• 📂 **Поиск по топикам группы** — напиши *«Что писали в топике Important Announcements?»* или *«Найди в группе информацию про дедлайн»*.
• 🎓 **Объявления DGIST** — напиши *«чекни портал»* или *«что нового на портале»*, и я пришлю свежие новости.
• 📄 **Создание PDF-файлов** — напиши, например, *«Сделай мне пдф по электродинамике»*, и я сгенерирую готовый PDF по нужной теме.
• 📸 **Работа с фото (до 10 штук)** — отправь от 1 до 10 картинок с вопросом, и я разберу их все сразу.
• 🎙 **Голосовые сообщения** — отправь войс, и я запишу план в календарь, отвечу на вопрос, сделаю PDF или подготовлю письмо.
• 📧 **Отправка Email** — напиши *«Напиши письмо профессору Киму о встрече»*, и я создам готовый черновик с кнопками отправки и редактирования."""

@client.on(events.NewMessage(pattern=r'^/start(@\w+)?(\s|$)'))
async def start_msg(e):
    remember_group_topic(e)
    await e.reply('Привет! Я Кристина — твой личный ассистент.\nНажми /help, чтобы посмотреть все мои команды и возможности!')

@client.on(events.NewMessage(pattern=r'^/help(@\w+)?(\s|$)'))
async def help_msg(e):
    remember_group_topic(e)
    await e.reply(HELP_TEXT)

@client.on(events.NewMessage(pattern=r'^/topics(@\w+)?(\s|$)'))
async def show_topics(e):
    cid = e.chat_id if not e.is_private else get_default_chat_id()
    topics = list_known_topics(cid)
    if not topics:
        await e.reply("📭 Я пока ещё не зафиксировала топиков или сообщений в группе.\n\nУбедись, что я добавлена в группу администратором (с правом чтения сообщений).")
        return
    lines = ["📋 **Топики в группе, которые я отслеживаю:**\n"]
    for idx, t in enumerate(topics, 1):
        lines.append(f"{idx}. 📌 **{t['topic_title']}** (сообщений: {t['message_count']})")
    lines.append("\n💡 Спроси меня: *«Что писали в топике Important Announcements?»* или *«Найди в группе информацию про дедлайн»*.")
    await e.reply("\n".join(lines))

@client.on(events.NewMessage(pattern=r'^/calendar(@\w+)?(\s|$)'))
async def show_calendar_cmd(e):
    creds = await asyncio.to_thread(load_user_creds, e.sender_id)
    if not creds:
        await e.reply(f"📅 Сначала подключи Google аккаунт:\n{build_auth_url(e.sender_id)}")
        return
    status_list = await e.reply("📅 Загружаю события из Google Календаря...")
    try:
        events_list = await asyncio.to_thread(list_calendar_events, creds, 10)
        if not events_list:
            await status_list.edit("📅 На ближайшее время событий в твоем Google Календаре не найдено.")
            return
        lines = ["📅 **Твои ближайшие планы в Google Календаре:**\n"]
        for idx, ev in enumerate(events_list, 1):
            badge = "🔥 [Важно] " if ev.get("is_important") else ""
            st = ev.get("start") or ""
            if "T" in st:
                d_part, t_part = st.split("T", 1)
                st_clean = f"{d_part[5:]} в {t_part[:5]}"
            else:
                st_clean = st
            lines.append(f"{idx}. {badge}**{ev['summary']}** — {st_clean}")
            if ev.get("link"):
                lines.append(f"   🔗 [Открыть]({ev['link']})")
        await status_list.edit("\n".join(lines), link_preview=False)
    except Exception as ex:
        err_str = str(ex).lower()
        if "disabled" in err_str or "not been used in project" in err_str or "accessnotconfigured" in err_str:
            await status_list.edit(
                "❌ **Google Calendar API не включён в твоём Google Cloud!**\n\n"
                "Перейди по ссылке и нажми кнопку **«Включить» (Enable)**:\n"
                "👉 https://console.cloud.google.com/apis/library/calendar-json.googleapis.com\n\n"
                "После включения просто нажми /calendar снова (переподключать не нужно)!"
            )
        elif "insufficient" in err_str or "invalid_grant" in err_str:
            delete_user_token(e.sender_id)
            await status_list.edit(f"⚠️ Нужно обновить доступ к Google Календарю: {build_auth_url(e.sender_id)}")
        else:
            await status_list.edit(f"❌ Ошибка получения событий: {ex}")

@client.on(events.NewMessage(pattern=r'^/(connectgoogle|connectcalendar|connectgmail|reconnect)(@\w+)?(\s|$)'))
async def connect_gmail(e):
    delete_user_token(e.sender_id)
    await e.reply(f"⚠️ **Подключение Google (Календарь и Gmail)**\n\nАвторизуйся по ссылке:\n{build_auth_url(e.sender_id)}")

@client.on(events.NewMessage(pattern=r'(?i)^/sendShak(@\w+)?(\s|$)'))
async def send_shak(e):
    user_states[e.sender_id] = "waiting_for_submitmessage"
    await e.reply('Напиши сообщение в формате: `!username/текст`')

@client.on(events.NewMessage(pattern=r'(?i)^/addAdminShak(@\w+)?(?:\s+(.*))?$'))
async def add_admin_shak_cmd(e):
    if not is_owner(e.sender_id):
        await e.reply("⛔ Эта команда доступна только главному администратору.")
        return
    args = (e.pattern_match.group(2) or "").strip()
    if args:
        added = await asyncio.to_thread(add_video_admins, args)
        all_admins = await asyncio.to_thread(list_video_admins)
        await e.reply(
            f"✅ Добавлены в безлимитные видео-админы: **{', '.join(added) if added else 'никто'}**\n\n"
            f"📋 **Текущий список безлимита:**\n" + "\n".join(all_admins)
        )
    else:
        user_states[e.sender_id] = "waiting_for_add_admin_shak"
        all_admins = await asyncio.to_thread(list_video_admins)
        await e.reply(
            "👑 **Добавление безлимитного доступа к видео**\n\n"
            "📋 **Сейчас в списке:**\n" + "\n".join(all_admins) + "\n\n"
            "Напиши `@username` (или несколько через пробел), кого добавить (или /cancel):"
        )

@client.on(events.NewMessage(pattern=r'(?i)^/deleteAdminShak(@\w+)?(?:\s+(.*))?$'))
async def delete_admin_shak_cmd(e):
    if not is_owner(e.sender_id):
        await e.reply("⛔ Эта команда доступна только главному администратору.")
        return
    args = (e.pattern_match.group(2) or "").strip()
    if args:
        removed, not_found = await asyncio.to_thread(remove_video_admins, args)
        all_admins = await asyncio.to_thread(list_video_admins)
        msg_parts = []
        if removed:
            msg_parts.append(f"🗑 Удалены из безлимита: **{', '.join(removed)}**")
        if not_found:
            msg_parts.append(f"⚠️ Не найдены в списке: {', '.join(not_found)}")
        msg_parts.append("\n📋 **Текущий список безлимита:**\n" + "\n".join(all_admins))
        await e.reply("\n".join(msg_parts))
    else:
        user_states[e.sender_id] = "waiting_for_del_admin_shak"
        all_admins = await asyncio.to_thread(list_video_admins)
        await e.reply(
            "🗑 **Удаление из безлимитного доступа к видео**\n\n"
            "📋 **Сейчас в списке:**\n" + "\n".join(all_admins) + "\n\n"
            "Напиши `@username` (или несколько через пробел), кого удалить (или /cancel):"
        )

@client.on(events.NewMessage(pattern=r'(?i)^/listAdminShak(@\w+)?(\s|$)'))
async def list_admin_shak_cmd(e):
    if not is_owner(e.sender_id):
        await e.reply("⛔ Эта команда доступна только главному администратору.")
        return
    all_admins = await asyncio.to_thread(list_video_admins)
    await e.reply("📋 **Список безлимитных видео-админов:**\n" + "\n".join(all_admins))

@client.on(events.MessageEdited)
async def on_message_edited(event):
    if not event.is_private:
        try:
            await record_group_message(client, event)
        except Exception:
            pass

@client.on(events.NewMessage)
async def necessary_task_handler(event):
    if not event.is_private:
        try:
            await record_group_message(client, event)
        except Exception as ex:
            print(f"Record group msg error: {ex}")

    if event.text and event.text.startswith("/"): return
    uid, sender = event.sender_id, await event.get_sender()
    state = user_states.get(uid)

    if not event.is_private:
        raw_l = (event.raw_text or "").lower()
        mentioned = bool(
            event.mentioned or "@" in raw_l or "кристин" in raw_l
            or "christin" in raw_l or "портал" in raw_l or "portal" in raw_l
            or "топик" in raw_l or "групп" in raw_l
            or "сгенерируй видео" in raw_l or "сделай видео" in raw_l
        )
        if mentioned:
            remember_group_topic(event)
        elif state is None and not is_active_group_topic(event):
            if event.is_reply:
                rmsg = await event.get_reply_message()
                if not (rmsg and rmsg.out): return
            else:
                return

    if await handle_dgist_conversation_step(event, uid, state, user_states): return

    if state == "waiting_for_add_admin_shak" and event.text:
        user_states.pop(uid, None)
        added = await asyncio.to_thread(add_video_admins, event.text)
        all_admins = await asyncio.to_thread(list_video_admins)
        await event.reply(
            f"✅ Добавлены в безлимитные видео-админы: **{', '.join(added) if added else 'никто'}**\n\n"
            f"📋 **Текущий список безлимита:**\n" + "\n".join(all_admins)
        )
        return

    if state == "waiting_for_del_admin_shak" and event.text:
        user_states.pop(uid, None)
        removed, not_found = await asyncio.to_thread(remove_video_admins, event.text)
        all_admins = await asyncio.to_thread(list_video_admins)
        msg_parts = []
        if removed:
            msg_parts.append(f"🗑 Удалены из безлимита: **{', '.join(removed)}**")
        if not_found:
            msg_parts.append(f"⚠️ Не найдены в списке: {', '.join(not_found)}")
        msg_parts.append("\n📋 **Текущий список безлимита:**\n" + "\n".join(all_admins))
        await event.reply("\n".join(msg_parts))
        return

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
                    await event.reply(ans)
                    await asyncio.to_thread(save_communication, sender.username if sender else None, txt)
            else: await event.reply("Не удалось распознать речь.")
        except Exception as ex: await event.reply(f"Ошибка аудио: {ex}")
        finally: user_states.pop(uid, None)

    elif event.photo:
        prev_task = photo_tasks.get(uid)
        if state != "waiting_for_imageprompt" and (not prev_task or prev_task.done()):
            pending_photos[uid].clear()
            pending_captions.pop(uid, None)
        if prev_task and not prev_task.done():
            prev_task.cancel()
        if len(pending_photos[uid]) < 10:
            pending_photos[uid].append(event.message)
        if event.raw_text and event.raw_text.strip():
            pending_captions[uid] = re.sub(r"@\w+", "", event.raw_text).strip() or event.raw_text.strip()

        async def _flush_photos(u=uid, ev=event):
            await asyncio.sleep(0.8)
            cap = pending_captions.pop(u, None)
            if cap:
                msgs = list(pending_photos.pop(u, []))
                user_states.pop(u, None)
                if msgs:
                    is_vid, vid_prompt_en = await detect_photo_video_intent(cap)
                    if is_vid:
                        snd = await ev.get_sender()
                        uname = getattr(snd, "username", None) if snd else None
                        img_bytes = await msgs[0].download_media(file=bytes)
                        await handle_video_generation(ev, u, uname, cap, prompt_en=vid_prompt_en, image_bytes=img_bytes)
                        return
                    st = await ev.reply(f"⏳ Обрабатываю фото ({len(msgs)} шт.)...")
                    try:
                        ans = await process_telegram_images(msgs, cap, user_id=u)
                        await st.edit(ans)
                    except Exception as ex:
                        await st.edit(f"❌ Ошибка: {ex}")
            else:
                user_states[u] = "waiting_for_imageprompt"
                await ev.reply(f"📸 Получила фото ({len(pending_photos[u])}/10 шт.)! Что с ними сделать?")

        photo_tasks[uid] = asyncio.create_task(_flush_photos())

    elif state == "waiting_for_imageprompt" and event.text:
        prev_task = photo_tasks.pop(uid, None)
        if prev_task and not prev_task.done():
            prev_task.cancel()
        msgs = list(pending_photos.pop(uid, []))
        pending_captions.pop(uid, None)
        user_states.pop(uid, None)
        if msgs:
            clean_txt = re.sub(r"@\w+", "", event.text).strip() or event.text
            is_vid, vid_prompt_en = await detect_photo_video_intent(clean_txt)
            if is_vid:
                uname = getattr(sender, "username", None) if sender else None
                img_bytes = await msgs[0].download_media(file=bytes)
                await handle_video_generation(event, uid, uname, clean_txt, prompt_en=vid_prompt_en, image_bytes=img_bytes)
                return
            st = await event.reply(f"⏳ Обрабатываю фото ({len(msgs)} шт.)...")
            try:
                ans = await process_telegram_images(msgs, clean_txt, user_id=uid)
                await st.edit(ans)
            except Exception as ex:
                await st.edit(f"❌ Ошибка: {ex}")

    elif event.text:
        clean_txt = re.sub(r"@\w+", "", event.text).strip() or event.text
        try:
            if not await try_handle_email_intent(event, uid, clean_txt):
                ans = await ask_gemini(clean_txt, user_id=uid)
                await event.reply(ans)
                await asyncio.to_thread(save_communication, sender.username if sender else None, clean_txt)
        except Exception as ex: await event.reply(f"Ошибка: {ex}")