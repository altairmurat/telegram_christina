import asyncio, os, sys, subprocess, traceback
from telethon import events
import dgist_monitor
from dgist_accounts import save_dgist_account, load_dgist_account, delete_dgist_account, list_dgist_user_ids

_active_portal_checks = set()
AUTO_CHECK_INTERVAL_SEC = 5 * 60 * 60
_auto_monitor_task = None

def register_dgist_handlers(client, user_states: dict):
    global _auto_monitor_task

    @client.on(events.NewMessage(pattern='/connectmyportal'))
    async def connect_portal(e):
        user_states[e.sender_id] = "waiting_for_dgist_username"
        await e.respond("🎓 **Подключение DGIST портала** (1/4)\n\nВведи свой логин от портала:")

    @client.on(events.NewMessage(pattern='/disconnectportal'))
    async def disconnect_portal(e):
        await asyncio.to_thread(delete_dgist_account, e.sender_id)
        user_states.pop(e.sender_id, None)
        await e.respond("🗑 Данные портала удалены.")

    @client.on(events.NewMessage(pattern='/cancel'))
    async def cancel_flow(e):
        user_states.pop(e.sender_id, None)
        await e.respond("Ок, отменил текущий диалог.")

    @client.on(events.NewMessage(pattern='/checkportal'))
    async def check_portal(e):
        uid = e.sender_id
        acc = await asyncio.to_thread(load_dgist_account, uid)
        if not acc:
            return await e.respond("Портал ещё не подключён. Сначала: /connectmyportal")
        if uid in _active_portal_checks:
            return await e.respond("Уже идёт проверка портала, подожди немного...")

        _active_portal_checks.add(uid)
        status_msg = await e.respond("🔄 Проверяю DGIST...")
        try:
            res = await _check_user_portal(uid, acc)
            if res.get("error"):
                await status_msg.edit(f"❌ Ошибка проверки:\n{res['error']}")
            else:
                msgs = _format_new_posts_messages(res)
                for idx, m in enumerate(msgs):
                    if idx == 0: await status_msg.edit(m)
                    else: await e.respond(m)
        except Exception as ex:
            await status_msg.edit(f"❌ Не удалось проверить портал: {ex}")
        finally:
            _active_portal_checks.discard(uid)

    if _auto_monitor_task is None or _auto_monitor_task.done():
        _auto_monitor_task = asyncio.create_task(_auto_monitor_loop(client))

async def _check_user_portal(uid: int, account: dict) -> dict:
    paths = dgist_monitor.build_user_paths(uid)
    cookie = ""
    if os.path.exists(paths["cookie_file"]):
        with open(paths["cookie_file"], "r", encoding="utf-8") as f: cookie = f.read().strip()
    if cookie:
        res = await asyncio.to_thread(dgist_monitor.run_once_for_user, uid, cookie)
        if res and not _result_has_session_error(res): return res
        print(f"[AUTO] Cookie пользователя {uid} истекла, перелогиниваюсь...")

    await asyncio.to_thread(_run_login_subprocess, uid, account)
    with open(paths["cookie_file"], "r", encoding="utf-8") as f: cookie = f.read().strip()
    return await asyncio.to_thread(dgist_monitor.run_once_for_user, uid, cookie)

def _result_has_session_error(res: dict) -> bool:
    if not isinstance(res, dict): return True
    err = str(res.get("error", "")).lower()
    if not err or any(m in err for m in ("timed out", "timeout", "connectionerror", "connection refused")):
        return False
    return any(m in err for m in ("сессия", "session", "cookie", "login.html", "login.do", "isign.dgist.ac.kr"))

def _format_new_posts_messages(res: dict) -> list[str]:
    if not res: return ["❌ Монитор вернул пустой результат."]
    if res.get("error"): return [f"❌ {res['error']}"]
    posts = list(res.get("new_important", [])) + list(res.get("new_minor", []))
    if not posts: return ["✅ Новых объявлений DGIST нет."]

    msgs, cur, cur_len = [], ["🔔 **Новые объявления DGIST:**\n"], 0
    for p in posts:
        em = "🔴" if p.get("important") else "📢"
        block = f"{em} [{p.get('category','DGIST')}]\n**{p.get('title','Без названия')}**"
        if p.get("date"): block += f"\n📅 {p['date']}"
        if p.get("summary"): block += f"\n{p['summary']}"
        block += "\n"
        if cur_len + len(block) > 3800:
            msgs.append("\n".join(cur).strip())
            cur, cur_len = [block], len(block)
        else:
            cur.append(block); cur_len += len(block)
    if cur: msgs.append("\n".join(cur).strip())
    return msgs

async def _auto_monitor_loop(client):
    await asyncio.sleep(5)
    while True:
        try:
            for uid in await asyncio.to_thread(list_dgist_user_ids):
                if uid in _active_portal_checks: continue
                acc = await asyncio.to_thread(load_dgist_account, uid)
                if not acc: continue
                _active_portal_checks.add(uid)
                try:
                    p = dgist_monitor.build_user_paths(uid)
                    is_first = not os.path.exists(p["state_file"])
                    res = await _check_user_portal(uid, acc)
                    if not is_first and not res.get("error"):
                        posts = list(res.get("new_important", [])) + list(res.get("new_minor", []))
                        if posts:
                            for m in _format_new_posts_messages(res):
                                await client.send_message(uid, m, link_preview=False)
                except Exception as ex:
                    print(f"[AUTO] Ошибка user={uid}: {ex}")
                finally:
                    _active_portal_checks.discard(uid)
        except Exception:
            traceback.print_exc()
        await asyncio.sleep(AUTO_CHECK_INTERVAL_SEC)

def _run_login_subprocess(uid: int, account: dict):
    if not account or not account.get("dgist_password") or not account.get("email_app_password"):
        raise RuntimeError("Учётные данные не найдены. Подключи портал заново через /connectmyportal")
    script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "auto_login_cli.py")
    env = os.environ.copy()
    env.update({
        "CLI_DGIST_USERNAME": str(account.get("dgist_username") or ""),
        "CLI_DGIST_PASSWORD": str(account.get("dgist_password") or ""),
        "CLI_EMAIL_ADDRESS": str(account.get("email_address") or ""),
        "CLI_EMAIL_APP_PASSWORD": str(account.get("email_app_password") or ""),
        "CLI_EMAIL_IMAP_HOST": str(account.get("email_imap_host") or "imap.gmail.com"),
    })
    proc = subprocess.run([sys.executable, script, str(uid)], env=env, capture_output=True, text=True, timeout=180)
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "").strip()
        raise RuntimeError(f"Логин не удался: {tail[-400:]}")

async def handle_dgist_conversation_step(event, uid: int, state, user_states: dict) -> bool:
    txt = (event.text or "").strip()
    if state == "waiting_for_dgist_username":
        if not txt: return await event.reply("Логин не может быть пустым.")
        user_states[uid] = ("waiting_for_dgist_password", txt)
        await event.respond("🎓 **Подключение DGIST** (2/4)\nВведи пароль от портала (сообщение удалю):")
        return True

    if isinstance(state, tuple) and state[0] == "waiting_for_dgist_password":
        try: await event.delete()
        except Exception: pass
        if not txt: return await event.reply("Пароль не может быть пустым.")
        user_states[uid] = ("waiting_for_dgist_email", state[1], txt)
        await event.respond("🎓 **Подключение DGIST** (3/4)\nВведи email, куда приходит 2FA код:")
        return True

    if isinstance(state, tuple) and state[0] == "waiting_for_dgist_email":
        if "@" not in txt: return await event.reply("Это не похоже на email. Попробуй ещё раз:")
        user_states[uid] = ("waiting_for_dgist_email_app_password", state[1], state[2], txt)
        await event.respond("🎓 **Подключение DGIST** (4/4)\nВведи App Password от почты (сообщение удалю):")
        return True

    if isinstance(state, tuple) and state[0] == "waiting_for_dgist_email_app_password":
        _, u, p, em = state
        try: await event.delete()
        except Exception: pass
        if not txt: return await event.reply("App password не может быть пустым.")
        try:
            await asyncio.to_thread(save_dgist_account, uid, u, p, em, txt)
            user_states.pop(uid, None)
            await event.respond("✅ **Портал успешно подключён!** Пароли зашифрованы.\n\nКоманда /checkportal проверяет объявления.")
        except Exception as ex:
            user_states.pop(uid, None)
            await event.respond(f"❌ Ошибка сохранения: {ex}\nПопробуй запустить /connectmyportal снова.")
        return True

    return False