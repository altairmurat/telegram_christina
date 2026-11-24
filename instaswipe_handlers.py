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
import random
import re
import time
from typing import Any, Dict, List, Optional, Tuple

from fastapi import APIRouter, File, Header, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse
from telethon import events, Button
from telethon.tl import types as tl_types

from env import OWNER_ID, WEBAPP_URL, REDIRECT_URI
import ig_cloud_actor
import ig_crypto_db
import ig_parser
from ig_webapp_ui import MINIAPP_HTML

instaswipe_router = APIRouter()
_client_ref = None
_handlers_registered = False
_last_ig_error = ""
MAX_IG_FILE_BYTES = 150 * 1024 * 1024  # до 150 МБ через Telethon MTProto

# Очередь автоматических действий в Instagram (по одному таску на пользователя)
_ig_worker_tasks: Dict[str, asyncio.Task] = {}
_ig_last_exec_ts: Dict[str, float] = {}
EXECUTABLE_IG_ACTIONS = ("unfollow_me", "remove_follower", "mutual_break")
ACTION_RU_LABELS = {
    "unfollow_me": "Отписаться самому",
    "remove_follower": "Удалить из подписчиков",
    "mutual_break": "Взаимный разрыв (отписаться + удалить)",
}


def _clean_url_str(val: Optional[str]) -> str:
    return (val or "").strip().strip('"').strip("'").strip().rstrip("/")


def get_public_base_url() -> str:
    """Находит публичный HTTPS адрес сервиса (WEBAPP_URL, RENDER_EXTERNAL_URL или домен из REDIRECT_URI)."""
    for candidate in (
        WEBAPP_URL,
        os.getenv("WEBAPP_URL", ""),
        os.getenv("RENDER_EXTERNAL_URL", ""),
    ):
        c = _clean_url_str(candidate)
        if c.startswith("https://") and "localhost" not in c and "127.0.0.1" not in c:
            return c
    redir = _clean_url_str(REDIRECT_URI or os.getenv("REDIRECT_URI", ""))
    if redir.startswith("https://") and "/gmail/callback" in redir:
        return redir.split("/gmail/callback")[0].rstrip("/")
    if os.getenv("RENDER"):
        return "https://telegram-christina.onrender.com"
    port = os.getenv("PORT", "8000")
    return _clean_url_str(WEBAPP_URL or f"http://localhost:{port}")


def build_instaswipe_webapp_url(tg_user_id: int | str) -> str:
    base = get_public_base_url()
    tok = ig_crypto_db.create_signed_session_token(tg_user_id)
    return f"{base}/instaswipe?token={tok}"


def make_instaswipe_keyboard(tg_user_id: int | str, allow_webview: bool = True) -> list:
    app_url = build_instaswipe_webapp_url(tg_user_id)
    rows = []
    webview_cls = getattr(tl_types, "KeyboardButtonWebView", None)
    if allow_webview and webview_cls and app_url.startswith("https://"):
        rows.append([webview_cls("🚀 Открыть InstaSwipe Mini App", app_url)])
    elif app_url.startswith("https://") or app_url.startswith("http://"):
        rows.append([Button.url("🚀 Открыть InstaSwipe", app_url)])
    rows.append([
        Button.inline("📊 Мой Инста-Радар", data=b"igvault:stats"),
        Button.inline("🤖 Авто-отписка (IG)", data=b"igvault:connectig"),
    ])
    rows.append([
        Button.inline("📥 Скачать решения", data=b"igvault:export"),
        Button.inline("📖 Как выгрузить", data=b"igvault:howto"),
        Button.inline("🔥 Стереть", data=b"igvault:wipe_ask"),
    ])
    return rows


async def _send_or_edit_with_instaswipe_kb(
    client,
    chat_id: int,
    msg_html: str,
    uid: int | str,
    is_private: bool = True,
    edit_msg=None,
    reply_to: Optional[int] = None,
):
    """Отправляет или редактирует сообщение с 3-уровневым фоллбэком кнопки (WebView -> URL -> текстовая ссылка)."""
    global _last_ig_error
    app_url = build_instaswipe_webapp_url(uid)

    # Попытка 1: Нативный Telegram WebView (в личке по HTTPS)
    if is_private and app_url.startswith("https://") and getattr(tl_types, "KeyboardButtonWebView", None):
        kb1 = make_instaswipe_keyboard(uid, allow_webview=True)
        try:
            if edit_msg:
                await edit_msg.edit(msg_html, parse_mode="html", buttons=kb1)
            else:
                await client.send_message(chat_id, msg_html, parse_mode="html", buttons=kb1, reply_to=reply_to)
            return
        except Exception as ex1:
            # Telethon иногда не даёт заменить обычное сообщение на WebView через edit — пробуем новый send_message
            if edit_msg:
                try:
                    await client.send_message(chat_id, msg_html, parse_mode="html", buttons=kb1, reply_to=reply_to)
                    try:
                        await edit_msg.delete()
                    except Exception:
                        pass
                    return
                except Exception as ex1b:
                    _last_ig_error = f"WebView send fallback: {ex1b}"
            else:
                _last_ig_error = f"WebView fallback: {ex1}"

    # Попытка 2: Обычная URL-кнопка
    if app_url.startswith("https://"):
        kb2 = make_instaswipe_keyboard(uid, allow_webview=False)
        try:
            if edit_msg:
                await edit_msg.edit(msg_html, parse_mode="html", buttons=kb2)
            else:
                await client.send_message(chat_id, msg_html, parse_mode="html", buttons=kb2, reply_to=reply_to)
            return
        except Exception as ex2:
            _last_ig_error = f"URL button fallback: {ex2}"

    # Попытка 3: Ссылка в тексте + inline-кнопки
    common_rows = make_instaswipe_keyboard(uid, allow_webview=False)[1:]
    msg_with_link = msg_html + f"\n\n🔗 <a href=\"{html.escape(app_url, quote=True)}\">Открыть InstaSwipe</a>"
    if edit_msg:
        await edit_msg.edit(msg_with_link, parse_mode="html", buttons=common_rows, link_preview=False)
    else:
        await client.send_message(chat_id, msg_with_link, parse_mode="html", buttons=common_rows, reply_to=reply_to, link_preview=False)


# ============================================================================
# Работа с зашифрованным сейфом пользователя
# ============================================================================

def resolve_tg_user_id(
    x_telegram_init_data: Optional[str] = None,
    x_session_token: Optional[str] = None,
    query_token: Optional[str] = None,
) -> Optional[str]:
    if x_telegram_init_data:
        uid = ig_crypto_db.verify_telegram_webapp_init_data(x_telegram_init_data)
        if uid:
            return uid
    tok = x_session_token or query_token
    if tok:
        uid = ig_crypto_db.verify_signed_session_token(tok)
        if uid:
            return uid
    # В локальной разработке разрешаем OWNER_ID, а на публичном HTTPS требуем подписанный токен или Telegram initData
    if not get_public_base_url().startswith("https://"):
        return str(OWNER_ID)
    return None


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


def get_auto_ig_summary(tg_user_id: int | str, actions: Optional[Dict[str, Any]] = None, user_pin: str = "") -> Dict[str, Any]:
    """Возвращает статус подключения авто-отписки Instagram и счётчики очереди."""
    meta = ig_crypto_db.vault_storage.load_meta(tg_user_id, user_pin=user_pin)
    cookies = meta.get("ig_cookies") or {}
    connected = bool(cookies.get("sessionid"))
    if actions is None:
        _, actions, _ = ensure_user_vault_loaded(tg_user_id, user_pin=user_pin)
    pending_cnt = 0
    done_cnt = 0
    error_cnt = 0
    unqueued_cnt = 0
    for v in (actions or {}).values():
        if not isinstance(v, dict):
            continue
        act = v.get("action", "")
        if act not in EXECUTABLE_IG_ACTIONS:
            continue
        st = v.get("exec_status", "")
        if st == "pending":
            pending_cnt += 1
        elif st == "done":
            done_cnt += 1
        elif st == "error":
            error_cnt += 1
        else:
            unqueued_cnt += 1
    uid_str = str(tg_user_id)
    worker_t = _ig_worker_tasks.get(uid_str)
    worker_running = bool(worker_t and not worker_t.done())
    return {
        "connected": connected,
        "ig_username": meta.get("ig_username", ""),
        "ig_ds_user_id": meta.get("ig_ds_user_id", ""),
        "auto_execute": bool(meta.get("auto_execute", True)),
        "paused_reason": meta.get("paused_reason", ""),
        "pending_count": pending_cnt,
        "done_count": done_cnt,
        "error_count": error_cnt,
        "unqueued_count": unqueued_cnt,
        "worker_running": worker_running,
    }


async def _notify_user_tg(tg_user_id: int | str, html_text: str):
    if not _client_ref or not str(tg_user_id).lstrip("-").isdigit():
        return
    try:
        await _client_ref.send_message(int(tg_user_id), html_text, parse_mode="html", link_preview=False)
    except Exception:
        pass


async def _run_ig_auto_worker(uid_str: str):
    """
    Фоновый воркер облачной авто-отписки/удаления подписчиков:
    - Первое решение (если прошло >= 20 сек с предыдущего) выполняет МГНОВЕННО (за ~1 сек).
    - При серии решений подряд выдерживает безопасную анти-спам паузу 22–36 сек между запросами к Instagram.
    - Перед самым запросом перепроверяет БД: если пользователь нажал «Шаг назад» (Отмена), пропускает!
    """
    executed_batch: List[str] = []
    try:
        while True:
            def _fetch_next_candidate():
                meta = ig_crypto_db.vault_storage.load_meta(uid_str)
                cookies = meta.get("ig_cookies") or {}
                if not cookies.get("sessionid"):
                    return None, None, "no_cookie"
                if not meta.get("auto_execute", True) or meta.get("paused_reason"):
                    return None, None, "paused"
                raw_state, actions, pinfo = ensure_user_vault_loaded(uid_str)
                if pinfo.get("locked"):
                    return None, None, "locked"
                # Ищем первый профиль со статусом pending
                for uname, info in actions.items():
                    if isinstance(info, dict) and info.get("action") in EXECUTABLE_IG_ACTIONS and info.get("exec_status") == "pending":
                        f_obj = (raw_state.get("following") or {}).get(uname) or (raw_state.get("followers") or {}).get(uname) or {}
                        known_uid = str(f_obj.get("id") or "")
                        return (uname, info.get("action"), known_uid), cookies, "ok"
                return None, None, "empty"

            cand, cookies, status = await asyncio.to_thread(_fetch_next_candidate)
            if status != "ok" or not cand:
                break

            uname, act_init, known_uid = cand
            elapsed = time.time() - _ig_last_exec_ts.get(uid_str, 0.0)
            if elapsed < 20.0:
                wait_sec = random.uniform(22.0, 35.0) - max(0.0, elapsed)
                if wait_sec > 1.0:
                    await asyncio.sleep(wait_sec)

            # Перепроверяем после паузы: не отменил ли пользователь решение («Шаг назад»)
            def _verify_still_pending():
                meta = ig_crypto_db.vault_storage.load_meta(uid_str)
                cookies_now = meta.get("ig_cookies") or {}
                if not cookies_now.get("sessionid") or not meta.get("auto_execute", True) or meta.get("paused_reason"):
                    return None, None
                _, actions_now, pinfo_now = ensure_user_vault_loaded(uid_str)
                if pinfo_now.get("locked"):
                    return None, None
                cur = actions_now.get(uname)
                if not isinstance(cur, dict) or cur.get("action") not in EXECUTABLE_IG_ACTIONS or cur.get("exec_status") != "pending":
                    return "skipped", None
                return cur.get("action"), cookies_now

            act_now, cookies_now = await asyncio.to_thread(_verify_still_pending)
            if act_now == "skipped":
                continue
            if not act_now or not cookies_now:
                break

            res = await asyncio.to_thread(
                ig_cloud_actor.execute_ig_action_sync,
                cookies_now,
                uname,
                act_now,
                known_uid,
            )
            _ig_last_exec_ts[uid_str] = time.time()

            def _commit_result():
                raw_state, actions_cur, _ = ensure_user_vault_loaded(uid_str)
                cur_entry = actions_cur.get(uname)
                if not isinstance(cur_entry, dict):
                    return
                now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
                if res.get("ok"):
                    cur_entry["exec_status"] = "done"
                    cur_entry["exec_msg"] = res.get("message", "OK")
                    cur_entry["executed_at"] = now_iso
                elif res.get("status") in ("auth_error", "rate_limit") or res.get("auth_error") or res.get("rate_limit"):
                    cur_entry["exec_status"] = "pending"
                    cur_entry["exec_msg"] = res.get("message", "Пауза")
                    ig_crypto_db.vault_storage.save_meta(uid_str, {"paused_reason": res.get("message", "Пауза Instagram")})
                else:
                    cur_entry["exec_status"] = "error"
                    cur_entry["exec_msg"] = res.get("message", "Ошибка")
                actions_cur[uname] = cur_entry
                if not ig_crypto_db.vault_storage.update_actions_only(uid_str, actions_cur):
                    ig_crypto_db.vault_storage.save_vault(uid_str, raw_state, actions_cur)

            await asyncio.to_thread(_commit_result)

            if res.get("ok"):
                lbl = ACTION_RU_LABELS.get(act_now, act_now)
                note = f" <i>({html.escape(res.get('message', ''))})</i>" if res.get("status") == "not_found" else ""
                executed_batch.append(f"• <code>@{html.escape(uname)}</code> — {lbl}{note}")
                # Если в очереди больше никого нет — сразу уведомим пользователя
                def _has_more_pending():
                    _, acts, _ = ensure_user_vault_loaded(uid_str)
                    return any(
                        isinstance(v, dict) and v.get("action") in EXECUTABLE_IG_ACTIONS and v.get("exec_status") == "pending"
                        for v in acts.values()
                    )
                if not await asyncio.to_thread(_has_more_pending) or len(executed_batch) >= 5:
                    batch_txt = "\n".join(executed_batch)
                    executed_batch.clear()
                    await _notify_user_tg(
                        uid_str,
                        f"🤖 <b>Выполнено автоматически в Instagram:</b>\n{batch_txt}",
                    )
            elif res.get("status") in ("auth_error", "rate_limit") or res.get("auth_error") or res.get("rate_limit"):
                if executed_batch:
                    batch_txt = "\n".join(executed_batch)
                    executed_batch.clear()
                    await _notify_user_tg(uid_str, f"🤖 <b>Выполнено в Instagram:</b>\n{batch_txt}")
                await _notify_user_tg(
                    uid_str,
                    f"⚠️ <b>Авто-отписка Instagram поставлена на паузу:</b>\n"
                    f"<code>{html.escape(res.get('message', ''))}</code>\n\n"
                    f"Обнови куку через /connectig или нажми «Продолжить» в окне <b>🤖 IG Авто</b> внутри Mini App.",
                )
                break
    finally:
        if executed_batch:
            batch_txt = "\n".join(executed_batch)
            await _notify_user_tg(uid_str, f"🤖 <b>Выполнено автоматически в Instagram:</b>\n{batch_txt}")


def trigger_ig_auto_worker(tg_user_id: int | str):
    """Запускает асинхронный фоновый воркер авто-отписки для пользователя, если он ещё не запущен."""
    uid_str = str(tg_user_id)
    existing = _ig_worker_tasks.get(uid_str)
    if existing and not existing.done():
        return
    try:
        loop = asyncio.get_running_loop()
        _ig_worker_tasks[uid_str] = loop.create_task(_run_ig_auto_worker(uid_str))
    except RuntimeError:
        pass


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
    auto_info = get_auto_ig_summary(tg_user_id, actions=actions)
    if auto_info["connected"]:
        uname_str = f"@{html.escape(auto_info['ig_username'])}" if auto_info["ig_username"] else f"ID {html.escape(auto_info['ig_ds_user_id'])}"
        if auto_info["paused_reason"]:
            auto_line = f"• ⚠️ Авто-отписка (на паузе): <b>{uname_str}</b>"
        elif auto_info["auto_execute"]:
            auto_line = (
                f"• 🟢 Авто-отписка в облаке: <b>ВКЛ ({uname_str})</b> "
                f"[⏳ {auto_info['pending_count']} | ✅ {auto_info['done_count']}]"
            )
        else:
            auto_line = f"• ⏸ Авто-отписка: <b>на паузе ({uname_str})</b>"
    else:
        auto_line = "• ⚪ Авто-отписка в облаке: <b>не подключена</b> (нажми «🤖 Авто-отписка» или /connectig)"

    lines = [
        "📊 <b>Инста-Радар твоего сейфа (AES-256-GCM)</b>\n",
        f"• Подписки: <b>{s['total_following']}</b> | Подписчики: <b>{s['total_followers']}</b>",
        f"• Индекс взаимности: <b>{s['reciprocity_rate']}%</b> | Карма: <b>{s['follower_ratio']}x</b>",
        f"• ❌ Не подписаны в ответ: <b>{s['not_back_count']}</b>",
        f"• 👻 Призраки (0% контакта): <b>{s['ghosts_count']}</b>",
        f"• 🤝 Взаимные: <b>{s['mutual_count']}</b> | 🌟 Фанаты: <b>{s['fans_count']}</b>",
        f"• ⏳ Висящие заявки: <b>{s['pending_count']}</b> | ★ Близкие друзья: <b>{s['close_friends_count']}</b>",
        f"• 🛒 В корзине решений: <b>{chosen_cnt}</b>",
        auto_line,
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
    await _send_or_edit_with_instaswipe_kb(
        client,
        event.chat_id,
        reply_html,
        uid,
        is_private=bool(getattr(event, "is_private", True)),
        edit_msg=st_msg,
        reply_to=event.id,
    )
    return True


# ============================================================================
# Команды и inline-кнопки Telethon
# ============================================================================

CONNECT_IG_GUIDE_HTML = (
    "🤖 <b>Облачная авто-отписка в Instagram (Zero-Knowledge AES-256-GCM)</b>\n\n"
    "Чтобы Кристина <b>мгновенно сама отписывалась</b> (или удаляла из подписчиков) "
    "сразу после твоего свайпа в Mini App, подключи куку <code>sessionid</code>:\n\n"
    "1️⃣ Открой <b>instagram.com</b> в браузере на ПК/телефоне.\n"
    "2️⃣ Нажми <code>F12</code> → вкладка <b>Application</b> (или <b>Storage</b>) → <b>Cookies</b> → <code>https://www.instagram.com</code>.\n"
    "3️⃣ Найди строчку <code>sessionid</code>, скопируй её значение (Value) и <b>пришли мне следующим сообщением сюда в чат</b> (или вставь прямо внутри Mini App по кнопке «🤖 IG Авто»).\n\n"
    "🛡 <i>Я мгновенно удалю твоё сообщение с кукой из чата и зашифрую её AES-256-GCM в твоём сейфе! Для отмены напиши «отмена».</i>"
)


async def _connect_and_activate_cookie(uid: int | str, raw_cookie_text: str, user_pin: str = "") -> Dict[str, Any]:
    parsed = ig_cloud_actor.parse_ig_cookie_input(raw_cookie_text)
    if not parsed.get("sessionid"):
        return {
            "ok": False,
            "message": "Не удалось распознать sessionid. Скопируй значение куки sessionid с сайта instagram.com.",
        }
    ver = await asyncio.to_thread(ig_cloud_actor.verify_ig_session_sync, parsed)
    if not ver.get("ok"):
        return ver

    def _save_and_queue():
        ig_crypto_db.vault_storage.save_meta(
            uid,
            {
                "ig_cookies": ver["cookies"],
                "ig_username": ver.get("username", ""),
                "ig_ds_user_id": ver.get("ds_user_id", ""),
                "auto_execute": True,
                "paused_reason": "",
                "connected_at": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
            },
            user_pin=user_pin,
        )
        # Если в корзине уже были выбранные кандидаты без статуса выполнения — ставим их в очередь!
        raw_state, actions, pinfo = ensure_user_vault_loaded(uid, user_pin=user_pin)
        queued_now = 0
        if not pinfo.get("locked") and actions:
            for uname, entry in actions.items():
                if isinstance(entry, dict) and entry.get("action") in EXECUTABLE_IG_ACTIONS:
                    if entry.get("exec_status") not in ("done", "pending"):
                        entry["exec_status"] = "pending"
                        queued_now += 1
            if queued_now > 0:
                if not ig_crypto_db.vault_storage.update_actions_only(uid, actions, user_pin=user_pin):
                    ig_crypto_db.vault_storage.save_vault(uid, raw_state, actions, user_pin=user_pin)
        return queued_now

    queued_cnt = await asyncio.to_thread(_save_and_queue)
    trigger_ig_auto_worker(uid)
    ver["queued_count"] = queued_cnt
    return ver


async def handle_instaswipe_conversation_step(
    client, event, uid: int, state: Optional[str], user_states: dict
) -> bool:
    """Обрабатывает шаг ожидания куки Instagram (`waiting_for_ig_cookie`)."""
    if state != "waiting_for_ig_cookie":
        return False

    text = (event.raw_text or "").strip()
    if not text or text.startswith("/"):
        return False

    if text.lower() in ("отмена", "cancel", "стоп", "выход"):
        user_states.pop(uid, None)
        await event.reply("👌 Подключение авто-отписки отменено.")
        return True

    # Удаляем сообщение пользователя с кукой из чата для безопасности
    try:
        await event.delete()
    except Exception:
        pass

    wait_msg = await client.send_message(
        event.chat_id,
        "🔐 Проверяю сессию Instagram и шифрую куку <code>AES-256-GCM</code>...",
        parse_mode="html",
    )
    res = await _connect_and_activate_cookie(uid, text)
    if not res.get("ok"):
        await wait_msg.edit(
            f"❌ <b>Не удалось подключить сессию Instagram:</b>\n"
            f"<code>{html.escape(res.get('message', 'Ошибка'))}</code>\n\n"
            f"Попробуй прислать свежее значение <code>sessionid</code> ещё раз или напиши «отмена».",
            parse_mode="html",
        )
        return True

    user_states.pop(uid, None)
    uname_disp = f"@{html.escape(res['username'])}" if res.get("username") else f"ID {html.escape(res.get('ds_user_id', ''))}"
    q_cnt = res.get("queued_count", 0)
    q_line = (
        f"\n🚀 В корзине уже было <b>{q_cnt}</b> решений — я уже запустила их выполнение в фоне!"
        if q_cnt > 0
        else "\nТеперь любое твоё решение («Отписаться», «Удалить подписчика», «Разрыв») в Mini App будет автоматически выполняться в Instagram!"
    )
    await _send_or_edit_with_instaswipe_kb(
        client,
        event.chat_id,
        f"✅ <b>Авто-отписка Instagram подключена ({uname_disp})!</b>\n"
        f"🛡 Кука зашифрована AES-256-GCM, твоё сообщение с ней удалено из чата.{q_line}",
        uid,
        is_private=bool(getattr(event, "is_private", True)),
        edit_msg=wait_msg,
    )
    return True


async def send_instaswipe_hub(client, event, uid: int, user_states: Optional[dict] = None):
    global _last_ig_error
    try:
        msg_html = await asyncio.to_thread(format_radar_message_html, uid)
        await _send_or_edit_with_instaswipe_kb(
            client,
            event.chat_id,
            msg_html,
            uid,
            is_private=bool(getattr(event, "is_private", True)),
            reply_to=event.id,
        )
    except Exception as ex:
        _last_ig_error = f"Fatal send_instaswipe_hub: {ex}"
        await event.reply(f"❌ Ошибка открытия InstaSwipe: {ex}")


def register_instaswipe_handlers(client, user_states: dict):
    global _client_ref, _handlers_registered
    _client_ref = client
    if _handlers_registered:
        return
    _handlers_registered = True

    @client.on(events.NewMessage(pattern=r'(?i)^/(insta|instaswipe|instagram|ig|unfollow)(@\w+)?(\s|$)'))
    async def insta_cmd(e):
        await send_instaswipe_hub(client, e, e.sender_id, user_states)

    @client.on(events.NewMessage(pattern=r'(?i)^/igstats(@\w+)?(\s|$)'))
    async def igstats_cmd(e):
        await send_instaswipe_hub(client, e, e.sender_id, user_states)

    @client.on(events.NewMessage(pattern=r'(?i)^/(connectig|igcookie|iglogin)(@\w+)?(?:\s+(.*))?$'))
    async def connectig_cmd(e):
        arg = (e.pattern_match.group(3) or "").strip()
        if arg:
            try:
                await e.delete()
            except Exception:
                pass
            w = await client.send_message(e.chat_id, "🔐 Проверяю и шифрую куку Instagram...")
            res = await _connect_and_activate_cookie(e.sender_id, arg)
            if not res.get("ok"):
                await w.edit(f"❌ Ошибка подключения: <code>{html.escape(res.get('message', ''))}</code>", parse_mode="html")
                return
            uname_disp = f"@{html.escape(res['username'])}" if res.get("username") else f"ID {html.escape(res.get('ds_user_id', ''))}"
            await _send_or_edit_with_instaswipe_kb(
                client,
                e.chat_id,
                f"✅ <b>Авто-отписка Instagram подключена ({uname_disp})!</b>",
                e.sender_id,
                is_private=bool(getattr(e, "is_private", True)),
                edit_msg=w,
            )
            return
        user_states[e.sender_id] = "waiting_for_ig_cookie"
        await e.reply(CONNECT_IG_GUIDE_HTML, parse_mode="html")

    @client.on(events.NewMessage(pattern=r'(?i)^/igdisconnect(@\w+)?(\s|$)'))
    async def igdisconnect_cmd(e):
        await asyncio.to_thread(
            ig_crypto_db.vault_storage.save_meta,
            e.sender_id,
            {"ig_cookies": {}, "ig_username": "", "ig_ds_user_id": "", "auto_execute": False, "paused_reason": ""},
        )
        user_states.pop(e.sender_id, None)
        await e.reply("🔌 Сессия Instagram отключена и удалена из зашифрованного сейфа.")

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
            await _send_or_edit_with_instaswipe_kb(
                client,
                event.chat_id,
                msg_html,
                uid,
                is_private=bool(getattr(event, "is_private", True)),
                edit_msg=event,
            )
        elif act == "connectig":
            await event.answer()
            auto_info = await asyncio.to_thread(get_auto_ig_summary, uid)
            user_states[uid] = "waiting_for_ig_cookie"
            status_hdr = ""
            if auto_info["connected"]:
                u_lbl = f"@{html.escape(auto_info['ig_username'])}" if auto_info["ig_username"] else f"ID {html.escape(auto_info['ig_ds_user_id'])}"
                status_hdr = (
                    f"🟢 <b>Сейчас подключён аккаунт:</b> <code>{u_lbl}</code> "
                    f"(в очереди: <b>{auto_info['pending_count']}</b>, выполнено: <b>{auto_info['done_count']}</b>)\n"
                    f"Отключить сессию: /igdisconnect\n\n"
                )
            await event.respond(status_hdr + CONNECT_IG_GUIDE_HTML, parse_mode="html")
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


@instaswipe_router.get("/api/instaswipe/health")
async def instaswipe_health():
    connected = bool(_client_ref and _client_ref.is_connected())
    bot_uname = ""
    if connected:
        try:
            me = await _client_ref.get_me()
            bot_uname = getattr(me, "username", "") or ""
        except Exception:
            pass
    return {
        "ok": True,
        "handlers_registered": _handlers_registered,
        "bot_connected": connected,
        "bot_username": bot_uname,
        "public_base_url": get_public_base_url(),
        "backend": ig_crypto_db.vault_storage.backend_name,
        "last_error": _last_ig_error,
    }


@instaswipe_router.get("/api/instaswipe/data")
@instaswipe_router.get("/api/data")
async def api_ig_get_data(
    request: Request,
    x_telegram_init_data: Optional[str] = Header(default=None),
    x_session_token: Optional[str] = Header(default=None),
    x_vault_pin: Optional[str] = Header(default=""),
):
    uid = resolve_tg_user_id(x_telegram_init_data, x_session_token, request.query_params.get("token"))
    if not uid:
        return JSONResponse({
            "profiles": [],
            "summary": {},
            "auto_ig": {
                "connected": False,
                "ig_username": "",
                "auto_execute": True,
                "paused_reason": "",
                "pending_count": 0,
                "done_count": 0,
                "error_count": 0,
                "unqueued_count": 0,
                "worker_running": False,
            },
            "privacy": {
                "blind_uid": "Открой через кнопку в Telegram-боте (/insta)",
                "exists": False,
                "has_pin": False,
                "locked": False,
                "backend": ig_crypto_db.vault_storage.backend_name,
                "cipher_preview": "—",
                "ciphertext_bytes": 0,
            },
        })
    raw_state, actions, pinfo = await asyncio.to_thread(ensure_user_vault_loaded, uid, x_vault_pin or "")
    if pinfo.get("locked"):
        return JSONResponse({"profiles": [], "summary": {}, "privacy": pinfo, "auto_ig": {"connected": False}})
    compiled = await asyncio.to_thread(ig_parser.compile_dataset_from_raw, raw_state, actions)
    auto_ig = await asyncio.to_thread(get_auto_ig_summary, uid, actions, x_vault_pin or "")
    if auto_ig["connected"] and auto_ig["auto_execute"] and not auto_ig["paused_reason"] and auto_ig["pending_count"] > 0:
        trigger_ig_auto_worker(uid)
    return JSONResponse({
        "profiles": compiled["profiles"],
        "summary": compiled["summary"],
        "privacy": pinfo,
        "auto_ig": auto_ig,
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
    if not uid:
        return JSONResponse({"ok": False, "error": "Unauthorized: open from Telegram bot (/insta)"}, status_code=401)
    body = await request.json()
    username = str(body.get("username", "")).strip()
    action = str(body.get("action", "")).strip()
    tag = str(body.get("tag", "")).strip()

    def _save():
        raw_state, actions, _ = ensure_user_vault_loaded(uid, user_pin=x_vault_pin or "")
        now = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
        prev = actions.get(username) if isinstance(actions.get(username), dict) else {}
        if action == "reset" and not tag:
            actions.pop(username, None)
        else:
            # Если действие уже было выполнено в IG и само действие не поменялось (например, добавили тег), сохраняем done
            if prev.get("action") == action and prev.get("exec_status") == "done":
                exec_status = "done"
                exec_msg = prev.get("exec_msg", "")
            elif action in EXECUTABLE_IG_ACTIONS:
                exec_status = "pending"
                exec_msg = ""
            else:
                exec_status = ""
                exec_msg = ""
            actions[username] = {
                "action": action,
                "tag": tag,
                "updated_at": now,
                "exec_status": exec_status,
                "exec_msg": exec_msg,
            }
        if not ig_crypto_db.vault_storage.update_actions_only(uid, actions, user_pin=x_vault_pin or ""):
            ig_crypto_db.vault_storage.save_vault(uid, raw_state, actions, user_pin=x_vault_pin or "")
        return get_auto_ig_summary(uid, actions, x_vault_pin or "")

    auto_ig = await asyncio.to_thread(_save)
    if action in EXECUTABLE_IG_ACTIONS and auto_ig["connected"] and auto_ig["auto_execute"] and not auto_ig["paused_reason"]:
        trigger_ig_auto_worker(uid)
    return {"ok": True, "auto_ig": auto_ig}


@instaswipe_router.post("/api/instaswipe/batch_action")
@instaswipe_router.post("/api/batch_action")
async def api_ig_batch_action(
    request: Request,
    x_telegram_init_data: Optional[str] = Header(default=None),
    x_session_token: Optional[str] = Header(default=None),
    x_vault_pin: Optional[str] = Header(default=""),
):
    uid = resolve_tg_user_id(x_telegram_init_data, x_session_token, request.query_params.get("token"))
    if not uid:
        return JSONResponse({"ok": False, "error": "Unauthorized: open from Telegram bot (/insta)"}, status_code=401)
    body = await request.json()
    usernames = body.get("usernames", [])
    action = str(body.get("action", "unfollow_me")).strip()

    def _batch():
        raw_state, actions, _ = ensure_user_vault_loaded(uid, user_pin=x_vault_pin or "")
        now = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
        exec_st = "pending" if action in EXECUTABLE_IG_ACTIONS else ""
        for u in usernames:
            if u:
                u_str = str(u)
                prev = actions.get(u_str) if isinstance(actions.get(u_str), dict) else {}
                if prev.get("action") == action and prev.get("exec_status") == "done":
                    continue
                actions[u_str] = {
                    "action": action,
                    "tag": prev.get("tag", ""),
                    "updated_at": now,
                    "exec_status": exec_st,
                    "exec_msg": "",
                }
        if not ig_crypto_db.vault_storage.update_actions_only(uid, actions, user_pin=x_vault_pin or ""):
            ig_crypto_db.vault_storage.save_vault(uid, raw_state, actions, user_pin=x_vault_pin or "")
        return get_auto_ig_summary(uid, actions, x_vault_pin or "")

    auto_ig = await asyncio.to_thread(_batch)
    if action in EXECUTABLE_IG_ACTIONS and auto_ig["connected"] and auto_ig["auto_execute"] and not auto_ig["paused_reason"]:
        trigger_ig_auto_worker(uid)
    return {"ok": True, "count": len(usernames), "auto_ig": auto_ig}


@instaswipe_router.post("/api/instaswipe/connect_cookie")
async def api_ig_connect_cookie(
    request: Request,
    x_telegram_init_data: Optional[str] = Header(default=None),
    x_session_token: Optional[str] = Header(default=None),
    x_vault_pin: Optional[str] = Header(default=""),
):
    uid = resolve_tg_user_id(x_telegram_init_data, x_session_token, request.query_params.get("token"))
    if not uid:
        return JSONResponse({"ok": False, "error": "Unauthorized"}, status_code=401)
    body = await request.json()
    raw_cookie = str(body.get("cookie", "")).strip()
    res = await _connect_and_activate_cookie(uid, raw_cookie, user_pin=x_vault_pin or "")
    auto_ig = await asyncio.to_thread(get_auto_ig_summary, uid, None, x_vault_pin or "")
    return {"ok": bool(res.get("ok")), "result": res, "auto_ig": auto_ig}


@instaswipe_router.post("/api/instaswipe/toggle_auto")
async def api_ig_toggle_auto(
    request: Request,
    x_telegram_init_data: Optional[str] = Header(default=None),
    x_session_token: Optional[str] = Header(default=None),
    x_vault_pin: Optional[str] = Header(default=""),
):
    uid = resolve_tg_user_id(x_telegram_init_data, x_session_token, request.query_params.get("token"))
    if not uid:
        return JSONResponse({"ok": False, "error": "Unauthorized"}, status_code=401)
    body = await request.json()
    auto_execute = body.get("auto_execute")
    run_cart_now = bool(body.get("run_cart_now", False))

    def _update():
        updates: Dict[str, Any] = {"paused_reason": ""}
        if auto_execute is not None:
            updates["auto_execute"] = bool(auto_execute)
        if run_cart_now:
            updates["auto_execute"] = True
        ig_crypto_db.vault_storage.save_meta(uid, updates, user_pin=x_vault_pin or "")
        raw_state, actions, pinfo = ensure_user_vault_loaded(uid, user_pin=x_vault_pin or "")
        if run_cart_now and not pinfo.get("locked"):
            changed = 0
            for uname, entry in actions.items():
                if isinstance(entry, dict) and entry.get("action") in EXECUTABLE_IG_ACTIONS:
                    if entry.get("exec_status") != "done":
                        entry["exec_status"] = "pending"
                        entry["exec_msg"] = ""
                        changed += 1
            if changed > 0:
                if not ig_crypto_db.vault_storage.update_actions_only(uid, actions, user_pin=x_vault_pin or ""):
                    ig_crypto_db.vault_storage.save_vault(uid, raw_state, actions, user_pin=x_vault_pin or "")
        return get_auto_ig_summary(uid, actions, x_vault_pin or "")

    auto_ig = await asyncio.to_thread(_update)
    if auto_ig["connected"] and auto_ig["auto_execute"] and auto_ig["pending_count"] > 0:
        trigger_ig_auto_worker(uid)
    return {"ok": True, "auto_ig": auto_ig}


@instaswipe_router.post("/api/instaswipe/disconnect_cookie")
async def api_ig_disconnect_cookie(
    request: Request,
    x_telegram_init_data: Optional[str] = Header(default=None),
    x_session_token: Optional[str] = Header(default=None),
    x_vault_pin: Optional[str] = Header(default=""),
):
    uid = resolve_tg_user_id(x_telegram_init_data, x_session_token, request.query_params.get("token"))
    if not uid:
        return JSONResponse({"ok": False, "error": "Unauthorized"}, status_code=401)
    await asyncio.to_thread(
        ig_crypto_db.vault_storage.save_meta,
        uid,
        {"ig_cookies": {}, "ig_username": "", "ig_ds_user_id": "", "auto_execute": False, "paused_reason": ""},
        x_vault_pin or "",
    )
    auto_ig = await asyncio.to_thread(get_auto_ig_summary, uid, None, x_vault_pin or "")
    return {"ok": True, "auto_ig": auto_ig}


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
    if not uid:
        return JSONResponse({"ok": False, "error": "Unauthorized: open from Telegram bot (/insta)"}, status_code=401)
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
    if not uid:
        return JSONResponse({"ok": False, "error": "Unauthorized"}, status_code=401)
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
    if not uid:
        return JSONResponse({"ok": False, "error": "Unauthorized"}, status_code=401)
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
