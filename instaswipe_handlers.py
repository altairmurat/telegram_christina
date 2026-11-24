"""
instaswipe_handlers.py
----------------------
Интеграция проекта InstaSwipe Vault (проверка подписчиков/невзаимных/призраков Instagram +
Tinder-подобный Telegram Mini App с Zero-Knowledge AES-256-GCM шифрованием) внутрь Кристины:
- FastAPI-маршруты Mini App (`/instaswipe` и `/api/instaswipe/*`) на том же порту и сервере, что и Кристина.
- Команды Telethon (`/insta`, `/instaswipe`, `/igstats`, `/igexport`, `/igwipe`) и inline-кнопки `igvault:*`.
- Автоматический приём `.zip`, `.json` и `.csv` выгрузок Instagram прямо в чате с Кристиной (через MTProto без лимита 20 МБ Bot API).
- Аналитический контекст в RAM для ответов ИИ Кристины на вопросы пользователя о его подписках/подписчиках.
"""

import asyncio
import csv
import datetime
import html
import io
import json
import os
import re
from typing import Any, Dict, List, Optional, Tuple

from fastapi import APIRouter, File, Header, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse
from telethon import events, Button, types

from env import OWNER_ID, WEBAPP_URL, REDIRECT_URI
import ig_crypto_db
import ig_parser
from ig_webapp_ui import MINIAPP_HTML

instaswipe_router = APIRouter()
_client_ref = None
MAX_IG_FILE_BYTES = 150 * 1024 * 1024  # до 150 МБ через Telethon MTProto


def get_public_base_url() -> str:
    """Находит публичный HTTPS адрес сервиса (WEBAPP_URL, RENDER_EXTERNAL_URL или домен из REDIRECT_URI)."""
    for candidate in (
        WEBAPP_URL,
        os.getenv("RENDER_EXTERNAL_URL", ""),
    ):
        c = (candidate or "").strip().rstrip("/")
        if c.startswith("https://") and "localhost" not in c and "127.0.0.1" not in c:
            return c
    redir = (REDIRECT_URI or "").strip()
    if redir.startswith("https://") and "/gmail/callback" in redir:
        return redir.split("/gmail/callback")[0].rstrip("/")
    port = os.getenv("PORT", "8000")
    return (WEBAPP_URL or f"http://localhost:{port}").strip().rstrip("/")


def build_instaswipe_webapp_url(tg_user_id: int | str) -> str:
    base = get_public_base_url()
    tok = ig_crypto_db.create_signed_session_token(tg_user_id)
    return f"{base}/instaswipe?token={tok}"


def make_instaswipe_keyboard(tg_user_id: int | str) -> list:
    app_url = build_instaswipe_webapp_url(tg_user_id)
    rows = []
    if app_url.startswith("https://"):
        rows.append([types.KeyboardButtonWebView("🚀 Открыть InstaSwipe Mini App", app_url)])
    else:
        rows.append([Button.url("🌐 Открыть InstaSwipe в браузере", app_url)])
    rows.append([
        Button.inline("📊 Мой Инста-Радар", data=b"igvault:stats"),
        Button.inline("📥 Скачать решения", data=b"igvault:export"),
    ])
    rows.append([
        Button.inline("📖 Как выгрузить из IG", data=b"igvault:howto"),
        Button.inline("🔥 Стереть из БД", data=b"igvault:wipe_ask"),
    ])
    return rows


# ============================================================================
# Работа с зашифрованным сейфом пользователя
# ============================================================================

def resolve_tg_user_id(
    x_telegram_init_data: Optional[str] = None,
    x_session_token: Optional[str] = None,
    query_token: Optional[str] = None,
) -> str:
    if x_telegram_init_data:
        uid = ig_crypto_db.verify_telegram_webapp_init_data(x_telegram_init_data)
        if uid:
            return uid
    tok = x_session_token or query_token
    if tok:
        uid = ig_crypto_db.verify_signed_session_token(tok)
        if uid:
            return uid
    return str(OWNER_ID)


def ensure_user_vault_loaded(
    tg_user_id: int | str, user_pin: str = ""
) -> Tuple[Dict[str, Any], Dict[str, Any], Dict[str, Any]]:
    raw_state, actions, pinfo = ig_crypto_db.vault_storage.load_vault(tg_user_id, user_pin=user_pin)
    if pinfo.get("locked"):
        return ig_parser.empty_raw_state(), {}, pinfo
    if raw_state is None:
        raw_state = ig_parser.empty_raw_state()
        actions = {}
    if isinstance(raw_state, dict) and "raw_state" in raw_state and "following" not in raw_state:
        raw_state = raw_state["raw_state"]
    return raw_state, actions, pinfo


def build_export_files(raw_state: Dict[str, Any], actions: Dict[str, Any]) -> Tuple[bytes, bytes]:
    """Генерирует в памяти `tinder_actions.json` и `unfollow_candidates.csv` (готовый для локального Playwright-скрипта)."""
    json_bytes = json.dumps(actions, ensure_ascii=False, indent=2).encode("utf-8")
    compiled = ig_parser.compile_dataset_from_raw(raw_state, actions)
    buf = io.StringIO()
    fieldnames = [
        "username", "full_name", "is_private", "is_verified", "user_id",
        "tab", "action", "tag", "followed_date", "interaction_score", "link", "unfollow",
    ]
    w = csv.DictWriter(buf, fieldnames=fieldnames)
    w.writeheader()
    for p in compiled["profiles"]:
        if not p["action"]:
            continue
        w.writerow({
            "username": p["username"],
            "full_name": p["full_name"],
            "is_private": p["is_private"],
            "is_verified": p["is_verified"],
            "user_id": p["user_id"],
            "tab": p["tab"],
            "action": p["action"],
            "tag": p.get("tag", ""),
            "followed_date": p["followed_date"],
            "interaction_score": p["interaction_score"],
            "link": p["link"],
            "unfollow": "yes" if p["action"] in ("unfollow_me", "mutual_break") else "",
        })
    csv_bytes = buf.getvalue().encode("utf-8-sig")
    return json_bytes, csv_bytes


def format_radar_message_html(tg_user_id: int | str) -> str:
    raw_state, actions, pinfo = ensure_user_vault_loaded(tg_user_id)
    if not pinfo.get("exists"):
        blind = html.escape(pinfo.get("blind_uid", "")[:22])
        return (
            "🔥 <b>InstaSwipe Vault — Проверка подписок Instagram</b>\n\n"
            "Твой зашифрованный сейф пока пуст!\n"
            f"🛡 <b>Анонимный Blind-ID:</b> <code>{blind}...</code>\n\n"
            "📥 <b>Скинь мне сюда в чат:</b>\n"
            "• <b>ZIP-архив</b> выгрузки Instagram (<i>Connections → Followers and following</i>)\n"
            "• Или отдельные JSON-файлы (<code>following.json</code>, <code>followers_1.json</code>, <code>close_friends.json</code>, <code>pending_follow_requests.json</code>)\n"
            "• Либо открой <b>InstaSwipe Mini App</b> кнопкой ниже и выбери целую распакованную папку!"
        )

    if pinfo.get("locked"):
        return (
            "🔒 <b>Твой сейф InstaSwipe защищён личным PIN-кодом.</b>\n"
            "Открой Mini App по кнопке ниже и введи свой PIN в окне «🔐 AES-256»."
        )

    compiled = ig_parser.compile_dataset_from_raw(raw_state, actions)
    s = compiled["summary"]
    chosen_cnt = sum(
        1 for v in actions.values()
        if isinstance(v, dict) and v.get("action") and v.get("action") != "keep"
    )
    lines = [
        "📊 <b>Инста-Радар твоего сейфа (AES-256-GCM)</b>\n",
        f"• Подписки: <b>{s['total_following']}</b> | Подписчики: <b>{s['total_followers']}</b>",
        f"• Индекс взаимности: <b>{s['reciprocity_rate']}%</b> | Карма: <b>{s['follower_ratio']}x</b>",
        f"• ❌ Не подписаны в ответ: <b>{s['not_back_count']}</b>",
        f"• 👻 Призраки (0% контакта): <b>{s['ghosts_count']}</b>",
        f"• 🤝 Взаимные: <b>{s['mutual_count']}</b> | 🌟 Фанаты: <b>{s['fans_count']}</b>",
        f"• ⏳ Висящие заявки: <b>{s['pending_count']}</b> | ★ Близкие друзья: <b>{s['close_friends_count']}</b>",
        f"• 🛒 В корзине на отписку: <b>{chosen_cnt}</b>",
    ]
    oldest = s.get("oldest_not_back") or []
    if oldest:
        top_str = ", ".join(f"<code>@{html.escape(o['username'])}</code> ({o['days']} дн.)" for o in oldest[:5])
        lines.append(f"\n🦣 <b>Самые старые невзаимные:</b>\n{top_str}")

    lines.append(f"\n🛡 <b>Blind-ID:</b> <code>{html.escape(pinfo['blind_uid'][:20])}...</code>")
    return "\n".join(lines)


HOWTO_EXPORT_HTML = (
    "📖 <b>Как за 1 минуту выгрузить списки из Instagram (без ввода пароля):</b>\n\n"
    "1. Зайди в <b>Instagram → Настройки → Центр аккаунтов (Accounts Center)</b>.\n"
    "2. Выбери <b>Ваша информация и разрешения → Скачать информацию</b>.\n"
    "3. Нажми <b>Часть информации</b> → отметь галочкой <b>Подписчики и подписки (Followers and following)</b> "
    "(по желанию можно добавить <i>Лайки / Комментарии / Сообщения</i> для расчёта индекса близости).\n"
    "4. Период: <b>За всё время (All time)</b> · Формат: обязательно <b>JSON</b> (не HTML!).\n"
    "5. Когда придёт <code>.zip</code> архив — просто перешли его прямо сюда в чат с Кристиной!"
)


async def send_exports_via_telethon(chat_id: int, tg_user_id: int | str) -> bool:
    if not _client_ref:
        return False
    raw_state, actions, pinfo = await asyncio.to_thread(ensure_user_vault_loaded, tg_user_id)
    if pinfo.get("locked") or not actions:
        return False
    json_bytes, csv_bytes = await asyncio.to_thread(build_export_files, raw_state, actions)

    f_json = io.BytesIO(json_bytes)
    f_json.name = "tinder_actions.json"
    f_csv = io.BytesIO(csv_bytes)
    f_csv.name = "unfollow_candidates.csv"

    await _client_ref.send_file(chat_id, f_json, caption="📂 Твой файл решений `tinder_actions.json`")
    await _client_ref.send_file(
        chat_id,
        f_csv,
        caption="📋 Таблица кандидатов `unfollow_candidates.csv` (с колонкой `unfollow=yes`)",
    )
    return True


def get_instagram_ai_context_for_user(uid: int, user_query: str = "") -> str:
    """
    Формирует текстовую аналитическую выжимку из сейфа пользователя (ТОЛЬКО в RAM),
    чтобы Кристина могла отвечать голосом/текстом на вопросы вида:
    «кто на меня не подписан взаимно?», «проверь @username в инсте», «сколько у меня призраков?».
    """
    raw_state, actions, pinfo = ensure_user_vault_loaded(uid)
    if not pinfo.get("exists"):
        return ""
    if pinfo.get("locked"):
        return "[Сейф Instagram пользователя защищён личным PIN-кодом в Mini App.]"

    compiled = ig_parser.compile_dataset_from_raw(raw_state, actions)
    s = compiled["summary"]
    profiles = compiled["profiles"]

    not_back = [p for p in profiles if p["tab"] == "not_back"]
    ghosts = [p for p in not_back if p["tie_tier"] == "ghost" and not p["is_close_friend"]]
    pending = [p for p in profiles if p["tab"] == "pending"]

    lines = [
        f"Сводка Instagram пользователя (InstaSwipe Vault):",
        f"- Всего подписок: {s['total_following']}, подписчиков: {s['total_followers']}, взаимность: {s['reciprocity_rate']}%, карма: {s['follower_ratio']}x",
        f"- Не подписаны в ответ (not_back): {s['not_back_count']} (из них призраков 0% контакта: {s['ghosts_count']})",
        f"- Взаимные: {s['mutual_count']}, подписаны только на пользователя (fans): {s['fans_count']}, висящие заявки: {s['pending_count']}",
    ]
    if not_back:
        sample_nb = ", ".join(
            f"@{p['username']} ({p['followed_date']}, близость {p['interaction_score']}%)"
            for p in not_back[:35]
        )
        lines.append(f"Примеры невзаимных подписок (первые 35): {sample_nb}")
    if pending:
        sample_p = ", ".join(f"@{p['username']} ({p['followed_date']})" for p in pending[:20])
        lines.append(f"Висящие непринятые заявки: {sample_p}")

    # Если в вопросе упомянут конкретный ник (@username или латинское слово) — найдём точную инфу по нему
    tokens = re.findall(r"@?([a-zA-Z0-9._]{3,30})", user_query or "")
    if tokens:
        low_tokens = {t.lower() for t in tokens}
        matched = [
            p for p in profiles
            if p["username"].lower() in low_tokens
            or any(t in p["username"].lower() for t in low_tokens if len(t) >= 4)
        ]
        if matched:
            lines.append("Найденные профили по запросу пользователя:")
            for mp in matched[:10]:
                lines.append(
                    f"  * @{mp['username']} ({mp['full_name']}): статус={mp['tab']}, "
                    f"я подписан с {mp['followed_date']}, он подписан с {mp['follower_date']}, "
                    f"кто первый={mp['who_first'] or '—'}, индекс близости={mp['interaction_score']}% ({mp['tie_tier']}), "
                    f"близкий друг={mp['is_close_friend']}, решение={mp['action'] or 'нет'}"
                )

    return "\n".join(lines)


# ============================================================================
# Автоматический приём .zip / .json / .csv файлов выгрузки Instagram в чате
# ============================================================================

async def try_handle_instagram_file_upload(client, event, uid: int, user_states: dict) -> bool:
    """
    Проверяет присланный документ (.zip, .json, .csv).
    Если это выгрузка Instagram (или включён режим ожидания файлов `/insta`),
    парсит и шифрует её AES-256-GCM в Supabase PostgreSQL.
    """
    if not event.message or not event.message.file:
        return False

    fname = getattr(event.message.file, "name", "") or ""
    low_name = fname.lower()
    if not (low_name.endswith(".zip") or low_name.endswith(".json") or low_name.endswith(".csv")):
        return False

    in_ig_mode = (user_states.get(uid) == "waiting_for_ig_files")
    fsize = getattr(event.message.file, "size", 0) or 0

    if fsize > MAX_IG_FILE_BYTES:
        if in_ig_mode or ig_parser.looks_like_ig_filename(fname):
            await event.reply(
                "⚠️ Архив слишком большой (> 150 МБ, скорее всего с фото/видео).\n"
                "Открой <b>InstaSwipe Mini App</b> по команде /insta и выбери распакованную папку — "
                "браузер сам возьмёт только лёгкие <code>.json</code> файлы!",
                parse_mode="html",
            )
            return True
        return False

    # Если пользователь не в /insta режиме и у файла большое не-инстаграмное имя, не трогаем чужие ZIP > 25 МБ
    if not in_ig_mode and not ig_parser.looks_like_ig_filename(fname) and fsize > 25 * 1024 * 1024:
        return False

    st_msg = None
    if in_ig_mode or ig_parser.looks_like_ig_filename(fname):
        st_msg = await event.reply(
            f"🔐 Приняла <code>{html.escape(fname)}</code> — проверяю структуру Instagram и шифрую AES-256-GCM...",
            parse_mode="html",
        )

    raw_bytes = await event.message.download_media(file=bytes)
    if not raw_bytes:
        if st_msg:
            await st_msg.edit("❌ Не удалось скачать файл из Telegram.")
            return True
        return False

    def _ingest_sync():
        raw_state, actions, pinfo = ensure_user_vault_loaded(uid)
        if pinfo.get("locked"):
            return None, None, None, ["LOCKED"]
        # Копируем состояние, чтобы не портить сейф, если файл окажется не от Instagram
        state_copy = json.loads(json.dumps(raw_state))
        new_state, recognized = ig_parser.ingest_files_bundle([(fname, raw_bytes)], state_copy)
        if not recognized:
            return None, None, None, []
        compiled = ig_parser.compile_dataset_from_raw(new_state, actions)
        save_info = ig_crypto_db.vault_storage.save_vault(uid, new_state, actions)
        return compiled, save_info, actions, recognized

    compiled, save_info, _, recognized = await asyncio.to_thread(_ingest_sync)

    if recognized == ["LOCKED"]:
        msg = "🔒 Твой сейф защищён личным PIN-кодом. Сначала сними или введи PIN внутри Mini App (/insta)."
        if st_msg:
            await st_msg.edit(msg)
        else:
            await event.reply(msg)
        return True

    if not recognized:
        if st_msg:
            await st_msg.edit(
                f"🤔 В файле <code>{html.escape(fname)}</code> не найдено таблиц подписок/подписчиков Instagram.\n"
                "Убедись, что скачивал выгрузку в формате <b>JSON</b> (а не HTML)! Инструкция: /insta",
                parse_mode="html",
            )
            return True
        return False

    # Успешно распознана выгрузка Instagram!
    user_states.pop(uid, None)
    s = compiled["summary"]
    rec_str = html.escape(", ".join(recognized[:8]))
    if len(recognized) > 8:
        rec_str += f" (+{len(recognized) - 8})"

    reply_html = (
        f"✅ <b>Выгрузка Instagram зашифрована и сохранена в {html.escape(save_info['backend'])}!</b>\n\n"
        f"🛡 <b>Blind-ID:</b> <code>{html.escape(save_info['blind_uid'][:18])}...</code>\n"
        f"🔒 <b>Шифр в БД:</b> <code>{html.escape(save_info['cipher_preview'][:32])}...</code>\n"
        f"📂 <b>Распознано:</b> <code>{rec_str}</code>\n\n"
        f"📊 <b>Твой Инста-Радар:</b>\n"
        f"• ❌ Не подписаны в ответ: <b>{s['not_back_count']}</b> (призраков 0%: <b>{s['ghosts_count']}</b>)\n"
        f"• 🤝 Взаимные подписки: <b>{s['mutual_count']}</b> (взаимность <b>{s['reciprocity_rate']}%</b>)\n"
        f"• 🌟 Подписаны только на тебя: <b>{s['fans_count']}</b>\n"
        f"• ⏳ Висящие заявки: <b>{s['pending_count']}</b>\n\n"
        f"<i>Можешь докинуть ещё JSON-файлы в чат (они склеятся автоматически) или запускай Tinder-свайпалку ниже 👇</i>"
    )
    kb = make_instaswipe_keyboard(uid)
    if st_msg:
        try:
            await st_msg.edit(reply_html, parse_mode="html", buttons=kb)
        except Exception:
            # Fallback если кнопка WebView требует новый send_message или HTTP URL
            await st_msg.delete()
            await client.send_message(event.chat_id, reply_html, parse_mode="html", buttons=kb, reply_to=event.id)
    else:
        await client.send_message(event.chat_id, reply_html, parse_mode="html", buttons=kb, reply_to=event.id)
    return True


# ============================================================================
# Команды и inline-кнопки Telethon
# ============================================================================

async def send_instaswipe_hub(client, event, uid: int, user_states: Optional[dict] = None):
    if user_states is not None:
        # Включаем мягкий режим готовности принять любой .zip/.json, не мешая обычным текстам
        pass
    msg_html = await asyncio.to_thread(format_radar_message_html, uid)
    kb = make_instaswipe_keyboard(uid)
    try:
        await client.send_message(event.chat_id, msg_html, parse_mode="html", buttons=kb, reply_to=event.id)
    except Exception:
        # На случай невалидного домена WebView откатываемся на обычную URL-кнопку
        fallback_kb = [
            [Button.url("🌐 Открыть InstaSwipe", build_instaswipe_webapp_url(uid))],
            [
                Button.inline("📊 Мой Инста-Радар", data=b"igvault:stats"),
                Button.inline("📥 Скачать решения", data=b"igvault:export"),
            ],
            [
                Button.inline("📖 Как выгрузить из IG", data=b"igvault:howto"),
                Button.inline("🔥 Стереть из БД", data=b"igvault:wipe_ask"),
            ],
        ]
        await client.send_message(event.chat_id, msg_html, parse_mode="html", buttons=fallback_kb, reply_to=event.id)


def register_instaswipe_handlers(client, user_states: dict):
    global _client_ref
    _client_ref = client

    @client.on(events.NewMessage(pattern=r'(?i)^/(insta|instaswipe|instagram|ig|unfollow)(@\w+)?(\s|$)'))
    async def insta_cmd(e):
        await send_instaswipe_hub(client, e, e.sender_id, user_states)

    @client.on(events.NewMessage(pattern=r'(?i)^/igstats(@\w+)?(\s|$)'))
    async def igstats_cmd(e):
        await send_instaswipe_hub(client, e, e.sender_id, user_states)

    @client.on(events.NewMessage(pattern=r'(?i)^/igexport(@\w+)?(\s|$)'))
    async def igexport_cmd(e):
        ok = await send_exports_via_telethon(e.chat_id, e.sender_id)
        if not ok:
            await e.reply("🛒 Корзина решений пока пуста (или сейф под PIN-кодом). Открой /insta и выбери профили в Mini App!")

    @client.on(events.NewMessage(pattern=r'(?i)^/igwipe(@\w+)?(\s|$)'))
    async def igwipe_cmd(e):
        btns = [[
            Button.inline("🔥 Да, стереть навсегда", data=b"igvault:wipe_yes"),
            Button.inline("Отмена", data=b"igvault:wipe_no"),
        ]]
        await e.reply("⚠️ Точно полностью удалить твой зашифрованный сейф Instagram из базы данных?", buttons=btns)

    @client.on(events.CallbackQuery(pattern=rb"^igvault:"))
    async def on_igvault_callback(event):
        data = event.data.decode("utf-8", errors="ignore")
        act = data.split(":", 1)[1] if ":" in data else ""
        uid = event.sender_id

        if act == "stats":
            await event.answer()
            msg_html = await asyncio.to_thread(format_radar_message_html, uid)
            kb = make_instaswipe_keyboard(uid)
            try:
                await event.edit(msg_html, parse_mode="html", buttons=kb)
            except Exception:
                await client.send_message(event.chat_id, msg_html, parse_mode="html", buttons=kb)
        elif act == "export":
            await event.answer("Готовлю файлы...")
            ok = await send_exports_via_telethon(event.chat_id, uid)
            if not ok:
                await event.respond("🛒 Корзина решений пока пуста. Открой Mini App и свайпни кандидатов!")
        elif act == "howto":
            await event.answer()
            await event.respond(HOWTO_EXPORT_HTML, parse_mode="html")
        elif act == "wipe_ask":
            await event.answer()
            btns = [[
                Button.inline("🔥 Да, стереть навсегда", data=b"igvault:wipe_yes"),
                Button.inline("Отмена", data=b"igvault:wipe_no"),
            ]]
            await event.respond("⚠️ Точно полностью стереть твои зашифрованные данные Instagram из Supabase?", buttons=btns)
        elif act == "wipe_yes":
            await asyncio.to_thread(ig_crypto_db.vault_storage.wipe_vault, uid)
            await event.answer("Данные стёрты")
            await event.edit("🔥 Все твои зашифрованные данные Instagram полностью удалены из базы данных.", buttons=None)
        elif act == "wipe_no":
            await event.answer("Отменено")
            await event.delete()


# ============================================================================
# FastAPI маршруты Telegram Mini App (/instaswipe и /api/instaswipe/*)
# ============================================================================

@instaswipe_router.get("/instaswipe", response_class=HTMLResponse)
@instaswipe_router.get("/instaswipe/", response_class=HTMLResponse)
async def instaswipe_page():
    return MINIAPP_HTML


@instaswipe_router.get("/api/instaswipe/data")
@instaswipe_router.get("/api/data")
async def api_ig_get_data(
    request: Request,
    x_telegram_init_data: Optional[str] = Header(default=None),
    x_session_token: Optional[str] = Header(default=None),
    x_vault_pin: Optional[str] = Header(default=""),
):
    uid = resolve_tg_user_id(x_telegram_init_data, x_session_token, request.query_params.get("token"))
    raw_state, actions, pinfo = await asyncio.to_thread(ensure_user_vault_loaded, uid, x_vault_pin or "")
    if pinfo.get("locked"):
        return JSONResponse({"profiles": [], "summary": {}, "privacy": pinfo})
    compiled = await asyncio.to_thread(ig_parser.compile_dataset_from_raw, raw_state, actions)
    return JSONResponse({
        "profiles": compiled["profiles"],
        "summary": compiled["summary"],
        "privacy": pinfo,
    })


@instaswipe_router.post("/api/instaswipe/action")
@instaswipe_router.post("/api/action")
async def api_ig_save_action(
    request: Request,
    x_telegram_init_data: Optional[str] = Header(default=None),
    x_session_token: Optional[str] = Header(default=None),
    x_vault_pin: Optional[str] = Header(default=""),
):
    uid = resolve_tg_user_id(x_telegram_init_data, x_session_token, request.query_params.get("token"))
    body = await request.json()
    username = str(body.get("username", "")).strip()
    action = str(body.get("action", "")).strip()
    tag = str(body.get("tag", "")).strip()

    def _save():
        raw_state, actions, _ = ensure_user_vault_loaded(uid, user_pin=x_vault_pin or "")
        now = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
        if action == "reset" and not tag:
            actions.pop(username, None)
        else:
            actions[username] = {"action": action, "tag": tag, "updated_at": now}
        if not ig_crypto_db.vault_storage.update_actions_only(uid, actions, user_pin=x_vault_pin or ""):
            ig_crypto_db.vault_storage.save_vault(uid, raw_state, actions, user_pin=x_vault_pin or "")

    await asyncio.to_thread(_save)
    return {"ok": True}


@instaswipe_router.post("/api/instaswipe/batch_action")
@instaswipe_router.post("/api/batch_action")
async def api_ig_batch_action(
    request: Request,
    x_telegram_init_data: Optional[str] = Header(default=None),
    x_session_token: Optional[str] = Header(default=None),
    x_vault_pin: Optional[str] = Header(default=""),
):
    uid = resolve_tg_user_id(x_telegram_init_data, x_session_token, request.query_params.get("token"))
    body = await request.json()
    usernames = body.get("usernames", [])
    action = str(body.get("action", "unfollow_me")).strip()

    def _batch():
        raw_state, actions, _ = ensure_user_vault_loaded(uid, user_pin=x_vault_pin or "")
        now = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
        for u in usernames:
            if u:
                actions[str(u)] = {"action": action, "tag": "", "updated_at": now}
        if not ig_crypto_db.vault_storage.update_actions_only(uid, actions, user_pin=x_vault_pin or ""):
            ig_crypto_db.vault_storage.save_vault(uid, raw_state, actions, user_pin=x_vault_pin or "")

    await asyncio.to_thread(_batch)
    return {"ok": True, "count": len(usernames)}


@instaswipe_router.post("/api/instaswipe/upload")
@instaswipe_router.post("/api/upload")
async def api_ig_upload_files(
    request: Request,
    files: List[UploadFile] = File(...),
    x_telegram_init_data: Optional[str] = Header(default=None),
    x_session_token: Optional[str] = Header(default=None),
    x_vault_pin: Optional[str] = Header(default=""),
):
    uid = resolve_tg_user_id(x_telegram_init_data, x_session_token, request.query_params.get("token"))
    bundle: List[Tuple[str, bytes]] = []
    for f in files:
        content = await f.read()
        bundle.append((f.filename or "file.json", content))

    def _process():
        raw_state, actions, _ = ensure_user_vault_loaded(uid, user_pin=x_vault_pin or "")
        raw_state, recognized = ig_parser.ingest_files_bundle(bundle, raw_state)
        compiled = ig_parser.compile_dataset_from_raw(raw_state, actions)
        save_info = ig_crypto_db.vault_storage.save_vault(uid, raw_state, actions, user_pin=x_vault_pin or "")
        return recognized, len(compiled["profiles"]), save_info

    recognized, total_profiles, save_info = await asyncio.to_thread(_process)
    return {
        "ok": True,
        "recognized_files": recognized,
        "recognized_count": len(recognized),
        "total_profiles": total_profiles,
        "privacy": save_info,
    }


@instaswipe_router.post("/api/instaswipe/set_pin")
@instaswipe_router.post("/api/set_pin")
async def api_ig_set_pin(
    request: Request,
    x_telegram_init_data: Optional[str] = Header(default=None),
    x_session_token: Optional[str] = Header(default=None),
):
    uid = resolve_tg_user_id(x_telegram_init_data, x_session_token, request.query_params.get("token"))
    body = await request.json()
    new_pin = str(body.get("pin", "")).strip()
    old_pin = str(body.get("old_pin", "")).strip()

    def _rekey():
        raw_state, actions, _ = ensure_user_vault_loaded(uid, user_pin=old_pin)
        return ig_crypto_db.vault_storage.save_vault(uid, raw_state, actions, user_pin=new_pin)

    info = await asyncio.to_thread(_rekey)
    return {"ok": True, "privacy": info}


@instaswipe_router.post("/api/instaswipe/wipe")
@instaswipe_router.post("/api/wipe")
async def api_ig_wipe(
    request: Request,
    x_telegram_init_data: Optional[str] = Header(default=None),
    x_session_token: Optional[str] = Header(default=None),
):
    uid = resolve_tg_user_id(x_telegram_init_data, x_session_token, request.query_params.get("token"))
    deleted = await asyncio.to_thread(ig_crypto_db.vault_storage.wipe_vault, uid)
    return {"ok": True, "deleted": deleted}


@instaswipe_router.post("/api/instaswipe/send_export_to_bot")
@instaswipe_router.post("/api/send_export_to_bot")
async def api_ig_send_export_to_bot(
    request: Request,
    x_telegram_init_data: Optional[str] = Header(default=None),
    x_session_token: Optional[str] = Header(default=None),
):
    uid = resolve_tg_user_id(x_telegram_init_data, x_session_token, request.query_params.get("token"))
    if not uid or not str(uid).lstrip("-").isdigit():
        return {"ok": False, "error": "Открой Mini App через кнопку в Telegram-боте"}
    ok = await send_exports_via_telethon(int(uid), uid)
    if not ok:
        return {"ok": False, "error": "Корзина пуста или клиент Telegram не готов"}
    return {"ok": True}
