import asyncio
import math
import re
import secrets
import traceback
from telethon import events, Button

import everytime_monitor as etm
from llm import store_everytime_context

ET_POSTS_PER_PAGE = 2
everytime_magazines: dict[str, dict] = {}
_active_et_checks = set()
_et_auto_task = None
ET_AUTO_INTERVAL_SEC = 2 * 3600  # Автопроверка новых постов по ключевым словам каждые 2 часа


def create_et_magazine(uid: int, posts: list[dict], header: str, mode: str = "keywords") -> str:
    mag_id = secrets.token_hex(4)
    everytime_magazines[mag_id] = {
        "mag_id": mag_id,
        "uid": uid,
        "posts": posts,
        "header": header,
        "mode": mode,
    }
    if len(everytime_magazines) > 100:
        oldest = next(iter(everytime_magazines))
        everytime_magazines.pop(oldest, None)
    return mag_id


def format_et_magazine_page(mag: dict, page: int) -> tuple[str, list]:
    posts = mag.get("posts") or []
    header = mag.get("header") or "🇰🇷 **Журнал DGIST Everytime (에브리타임):**"
    mag_id = mag.get("mag_id", "default")
    mode = mag.get("mode", "keywords")

    mode_row = [
        Button.inline(
            f"{'✅ ' if mode == 'keywords' else ''}🎯 По ключевым словам",
            data=f"etmag:mode:{mag_id}:keywords".encode("utf-8"),
        ),
        Button.inline(
            f"{'✅ ' if mode == 'all' else ''}🔥 Вся свежая лента",
            data=f"etmag:mode:{mag_id}:all".encode("utf-8"),
        ),
    ]

    if not posts:
        return (
            f"{header}\n\n📭 Постов по заданным фильтрам пока не найдено.\n"
            "Нажми **«🔥 Вся свежая лента»** ниже, чтобы посмотреть и перевести последние посты подряд!",
            [mode_row],
        )

    total_pages = max(1, math.ceil(len(posts) / ET_POSTS_PER_PAGE))
    page = max(0, min(page, total_pages - 1))
    start_idx = page * ET_POSTS_PER_PAGE
    slice_posts = posts[start_idx : start_idx + ET_POSTS_PER_PAGE]

    lines = [f"{header}\n📖 **Страница {page + 1} из {total_pages}** (найдено постов: {len(posts)})\n"]

    for offset, p in enumerate(slice_posts):
        num = start_idx + offset + 1
        new_badge = "🆕 " if p.get("is_new") else ""
        kws = p.get("matched_keywords") or []
        kw_str = f" 🏷 `[{', '.join(kws)}]`" if kws else ""

        title_ru = (p.get("title_ru") or p.get("title_ko") or "Без заголовка").strip()
        text_ru = (p.get("text_ru") or p.get("text_ko") or "").strip()
        title_ko = (p.get("title_ko") or "").strip()
        text_ko = (p.get("text_ko") or "").strip()

        if len(text_ru) > 850:
            text_ru = text_ru[:850] + "..."
        if len(text_ko) > 300:
            text_ko = text_ko[:300] + "..."

        block = f"━━━━━━━━━━━━━━━━━━\n**#{num}** {new_badge}**{title_ru}**{kw_str}\n"
        if p.get("created_at"):
            block += f"🕒 {p['created_at']}  |  👍 {p.get('likes', 0)}  |  💬 {p.get('comments_count', 0)}\n"
        block += f"🔗 [Открыть пост на Everytime]({p.get('url', 'https://everytime.kr/393752')})\n\n"
        block += f"🇷🇺 **Перевод слово в слово:**\n{text_ru}\n"

        if title_ko or text_ko:
            orig_preview = f"{title_ko}: {text_ko}" if title_ko and title_ko != text_ko else (title_ko or text_ko)
            block += f"\n🇰🇷 _Оригинал:_ `{orig_preview[:240]}`\n"

        comments_ru = p.get("comments_ru") or []
        if comments_ru:
            c_preview = "\n".join(f"  • {c}" for c in comments_ru[:4])
            if len(comments_ru) > 4:
                c_preview += f"\n  _...и ещё {len(comments_ru) - 4} комм. (спроси меня про пост #{num}, чтобы прочитать все)_"
            block += f"\n💬 **Комментарии (перевод):**\n{c_preview}\n"

        lines.append(block)

    lines.append(
        "💡 _Можешь спросить меня про любой пост (например: «о чём подробнее пост #1 в Everytime?» или скинуть прямую ссылку на пост)_"
    )
    text = "\n".join(lines)
    if len(text) > 4000:
        text = text[:3990] + "..."

    buttons = []
    if total_pages > 1:
        prev_p = (page - 1) % total_pages
        next_p = (page + 1) % total_pages
        buttons.append(
            [
                Button.inline("⬅️ Назад", data=f"etmag:page:{mag_id}:{prev_p}".encode("utf-8")),
                Button.inline(f"📄 {page + 1}/{total_pages}", data=b"etmag:noop"),
                Button.inline("Вперёд ➡️", data=f"etmag:page:{mag_id}:{next_p}".encode("utf-8")),
            ]
        )
    buttons.append(mode_row)
    return text, buttons


def format_single_et_post(art: dict) -> str:
    title_ru = (art.get("title_ru") or art.get("title_ko") or "Без заголовка").strip()
    text_ru = (art.get("text_ru") or art.get("text_ko") or "").strip()
    title_ko = (art.get("title_ko") or "").strip()
    text_ko = (art.get("text_ko") or "").strip()
    comments_ru = art.get("comments_ru") or []

    lines = [
        f"🇰🇷 **Пост с DGIST Everytime**",
        f"🔗 {art.get('url', '')}",
        f"🕒 {art.get('created_at', '')}  |  👍 {art.get('likes', 0)}  |  💬 {art.get('comments_count', 0)}\n",
        f"🇷🇺 **Заголовок (перевод):** **{title_ru}**",
        f"🇷🇺 **Текст (перевод слово в слово):**\n{text_ru}\n",
        f"🇰🇷 **Оригинал:**\n`{title_ko}`\n{text_ko[:700]}",
    ]
    if comments_ru:
        lines.append("\n💬 **Комментарии (дословный перевод):**")
        for c in comments_ru[:15]:
            lines.append(f"• {c}")
    out = "\n".join(lines)
    return out[:4000]


async def run_everytime_command(event, mode: str = "keywords", custom_query: str = ""):
    uid = event.sender_id
    cookie = await asyncio.to_thread(etm.load_everytime_cookie, uid)
    if not cookie:
        await event.reply(
            "🔑 **Everytime ещё не подключён!**\n\n"
            "Нажми /connecteverytime — подключение занимает 10 секунд через твой открытый браузер (без паролей и reCAPTCHA)."
        )
        return

    mode_label = (
        f"по запросу «{custom_query}»"
        if mode == "custom" and custom_query
        else ("всю свежую ленту" if mode == "all" else "по ключевым словам (foreigner / 외국인 / 201 / 기숙사 / dormitory и др.)")
    )
    status_msg = await event.reply(f"🔄 Паршу DGIST Everytime ({mode_label}) и перевожу слово в слово через Gemini...")

    # Если фоновый поток прямо сейчас уже обновляет ленту, дождёмся его завершения (до 25 сек)
    wait_ticks = 0
    while uid in _active_et_checks and wait_ticks < 25:
        await asyncio.sleep(1)
        wait_ticks += 1

    _active_et_checks.add(uid)
    try:
        res = await asyncio.to_thread(etm.run_everytime_check, uid, mode, custom_query)
        posts = res.get("posts") or []
        kws = res.get("keywords") or []
        store_everytime_context(uid, posts)

        if mode == "custom" and custom_query:
            header = f"🔎 **DGIST Everytime — поиск «{custom_query}»:**"
        elif mode == "all":
            header = "🔥 **DGIST Everytime — вся свежая лента (перевод слово в слово):**"
        else:
            header = (
                f"🎯 **DGIST Everytime — фильтр по ключевым словам:**\n"
                f"_Ключи: {', '.join(kws[:8])}{'...' if len(kws) > 8 else ''}_"
            )

        mag_id = create_et_magazine(uid, posts, header, mode=mode)
        text, buttons = format_et_magazine_page(everytime_magazines[mag_id], 0)
        await status_msg.edit(text, buttons=buttons, link_preview=False)
    except etm.EverytimeAuthError as auth_err:
        await status_msg.edit(f"🔑 {auth_err}")
    except Exception as ex:
        await status_msg.edit(f"❌ Ошибка парсинга Everytime: {ex}")
    finally:
        _active_et_checks.discard(uid)


async def handle_everytime_url_in_message(event, text: str) -> bool:
    m = re.search(r"everytime\.kr/(\d+|hotarticle|bestarticle)/v/(\d+)", text)
    if not m:
        return False
    board_id, article_id = m.group(1), m.group(2)
    uid = event.sender_id
    cookie = await asyncio.to_thread(etm.load_everytime_cookie, uid)
    if not cookie:
        await event.reply("🔑 Чтобы я могла открывать и переводить ссылки Everytime, подключи куку через /connecteverytime")
        return True

    st = await event.reply(f"🔄 Загружаю пост `#{article_id}` и комментарии с Everytime, перевожу слово в слово...")
    try:
        art = await asyncio.to_thread(etm.fetch_and_translate_single_post, uid, article_id, board_id)
        store_everytime_context(uid, [art])
        await st.edit(format_single_et_post(art), link_preview=False)
    except Exception as ex:
        await st.edit(f"❌ Не удалось загрузить пост Everytime: {ex}")
    return True


async def handle_everytime_conversation_step(event, uid: int, state, user_states: dict) -> bool:
    if state != "waiting_for_everytime_cookie":
        return False
    txt = (event.text or "").strip()
    if not txt:
        await event.reply("Пришли значение куки `etsid` (или /cancel):")
        return True
    try:
        await event.delete()
    except Exception:
        pass

    st = await event.reply("🔄 Проверяю доступ к доске DGIST (`everytime.kr/393752`)...")
    try:
        test_articles = await asyncio.to_thread(etm.fetch_board_articles, txt, etm.DGIST_FREE_BOARD_ID, 5, 0)
        await asyncio.to_thread(etm.save_everytime_cookie, uid, txt)
        user_states.pop(uid, None)
        await st.edit(
            f"✅ **Everytime DGIST успешно подключён!** (Найдено {len(test_articles)} свежих постов на доске 393752, сессия сохранена в БД).\n\n"
            "📌 **Как пользоваться:**\n"
            "• Нажми /everytime (или напиши *«чекни еверитайм»*) — я найду все посты по ключевым словам (`foreigner`, `외국인`, `201`, `기숙사`, `dormitory` и др.) и выдам журнал с **переводом слово в слово**.\n"
            "• Нажми /etkeywords — чтобы посмотреть или добавить свои ключевые слова.\n"
            "• Или просто скинь любую ссылку вида `https://everytime.kr/393752/v/...`!"
        )
    except Exception as ex:
        await st.edit(
            f"❌ **Не удалось войти по этой куке:** {ex}\n\n"
            "Убедись, что скопировал значение **`etsid`** из браузера, где открыт `everytime.kr`, и отправь ещё раз (или /cancel):"
        )
    return True


def register_everytime_handlers(client, user_states: dict):
    global _et_auto_task

    @client.on(events.NewMessage(pattern=r"(?i)^/connecteverytime(@\w+)?(\s|$)"))
    async def connect_et_cmd(e):
        user_states[e.sender_id] = "waiting_for_everytime_cookie"
        await e.reply(
            "🇰🇷 **Подключение DGIST Everytime (за 10 секунд)**\n\n"
            "Так как на сайте Everytime стоит защита Google reCAPTCHA Enterprise, самый быстрый и вечный способ без сброса сессии — передать твою активную куку **`etsid`**:\n\n"
            "1️⃣ Открой на компе вкладку `https://everytime.kr/393752` (где ты уже залогинен).\n"
            "2️⃣ Нажми клавишу **F12** (откроется панель разработчика) → сверху выбери вкладку **Application** (или **Приложение** / **Хранилище**).\n"
            "3️⃣ Слева в меню открой **Cookies** → `https://everytime.kr`.\n"
            "4️⃣ Найди строчку с именем **`etsid`**, скопируй её **Value (Значение)** и отправь мне сюда в ответ!\n\n"
            "🔒 _Я сразу удалю твоё сообщение из чата и сохраню сессию в БД (или нажми /cancel)._"
        )

    @client.on(events.NewMessage(pattern=r"(?i)^/(everytime|et)(@\w+)?(?:\s+([\s\S]*))?$"))
    async def check_et_cmd(e):
        arg = (e.pattern_match.group(3) or "").strip()
        if arg.lower() in ("all", "все", "всё", "лента"):
            await run_everytime_command(e, mode="all")
        elif arg:
            await run_everytime_command(e, mode="custom", custom_query=arg)
        else:
            await run_everytime_command(e, mode="keywords")

    @client.on(events.NewMessage(pattern=r"(?i)^/etkeywords(@\w+)?(?:\s+([\s\S]*))?$"))
    async def et_keywords_cmd(e):
        uid = e.sender_id
        arg = (e.pattern_match.group(2) or "").strip()
        cur_kws = await asyncio.to_thread(etm.get_everytime_keywords, uid)

        if not arg:
            await e.reply(
                "🏷 **Отслеживаемые ключевые слова Everytime:**\n"
                + ", ".join(f"`{k}`" for k in cur_kws)
                + "\n\n💡 **Как изменить:**\n"
                "• Добавить слово: `/etkeywords +слово` (например `/etkeywords +학식`)\n"
                "• Удалить слово: `/etkeywords -слово`\n"
                "• Сбросить по умолчанию: `/etkeywords reset`"
            )
            return

        if arg.lower() == "reset":
            new_kws = await asyncio.to_thread(etm.set_everytime_keywords, uid, list(etm.DEFAULT_KEYWORDS))
            await e.reply("✅ Ключевые слова сброшены к стандартным:\n" + ", ".join(f"`{k}`" for k in new_kws))
            return

        tokens = [t.strip() for t in re.split(r"[,;\s]+", arg) if t.strip()]
        updated = list(cur_kws)
        for t in tokens:
            if t.startswith("+") and len(t) > 1:
                w = t[1:].strip()
                if w and w not in updated:
                    updated.append(w)
            elif t.startswith("-") and len(t) > 1:
                w = t[1:].strip().lower()
                updated = [x for x in updated if x.lower() != w]
            else:
                if t not in updated:
                    updated.append(t)

        saved = await asyncio.to_thread(etm.set_everytime_keywords, uid, updated)
        await e.reply("✅ **Обновлённый список ключевых слов Everytime:**\n" + ", ".join(f"`{k}`" for k in saved))

    @client.on(events.CallbackQuery(pattern=rb"^etmag:"))
    async def handle_et_magazine_callback(event):
        data = event.data.decode("utf-8", errors="ignore")
        if data == "etmag:noop":
            await event.answer("Листай страницы кнопками ⬅️ Назад / Вперёд ➡️")
            return

        parts = data.split(":")
        if len(parts) < 4:
            return
        _, subaction, mag_id, val = parts[0], parts[1], parts[2], parts[3]

        mag = everytime_magazines.get(mag_id)
        if not mag:
            uid = event.sender_id
            seen_data = await asyncio.to_thread(etm.load_everytime_seen_data, uid)
            last_posts = seen_data.get("last_magazine_posts") or []
            if not last_posts:
                await event.answer("Журнал устарел. Напиши /everytime для обновления!", alert=True)
                return
            mag = {
                "mag_id": mag_id,
                "uid": uid,
                "posts": last_posts,
                "header": "🇰🇷 **Журнал DGIST Everytime:**",
                "mode": "keywords",
            }
            everytime_magazines[mag_id] = mag

        if subaction == "page":
            try:
                page = int(val)
            except ValueError:
                return
            store_everytime_context(event.sender_id, mag["posts"])
            text, buttons = format_et_magazine_page(mag, page)
            try:
                await event.edit(text, buttons=buttons, link_preview=False)
            except Exception:
                pass
            await event.answer()
            return

        if subaction == "mode":
            target_mode = val if val in ("keywords", "all") else "keywords"
            await event.answer("Загружаю и перевожу посты...")
            uid = mag.get("uid") or event.sender_id
            try:
                await event.edit("🔄 Переключаю режим и перевожу свежие посты с Everytime...", buttons=None)
                res = await asyncio.to_thread(etm.run_everytime_check, uid, target_mode, "")
                posts = res.get("posts") or []
                kws = res.get("keywords") or []
                store_everytime_context(event.sender_id, posts)
                if target_mode == "all":
                    header = "🔥 **DGIST Everytime — вся свежая лента (перевод слово в слово):**"
                else:
                    header = (
                        f"🎯 **DGIST Everytime — фильтр по ключевым словам:**\n"
                        f"_Ключи: {', '.join(kws[:8])}{'...' if len(kws) > 8 else ''}_"
                    )
                mag["posts"] = posts
                mag["header"] = header
                mag["mode"] = target_mode
                text, buttons = format_et_magazine_page(mag, 0)
                await event.edit(text, buttons=buttons, link_preview=False)
            except Exception as ex:
                await event.edit(f"❌ Ошибка при загрузке Everytime: {ex}")
            return

    if _et_auto_task is None or _et_auto_task.done():
        _et_auto_task = asyncio.create_task(_auto_everytime_loop(client))


async def _auto_everytime_loop(client):
    # Ждём 15 минут после старта, чтобы не блокировать ручные команды /everytime сразу после деплоя
    await asyncio.sleep(900)
    while True:
        try:
            uids = await asyncio.to_thread(etm.list_everytime_user_ids)
            for uid in uids:
                if uid in _active_et_checks:
                    continue
                _active_et_checks.add(uid)
                try:
                    seen_before = await asyncio.to_thread(etm.load_everytime_seen_data, uid)
                    is_first = not bool(seen_before.get("seen_ids"))
                    res = await asyncio.to_thread(etm.run_everytime_check, uid, "keywords", "")
                    new_posts = res.get("new_posts") or []
                    if not is_first and new_posts:
                        store_everytime_context(uid, new_posts)
                        header = f"🔔 **Новые посты на DGIST Everytime по твоим ключам ({len(new_posts)} шт.):**"
                        mag_id = create_et_magazine(uid, new_posts, header, mode="keywords")
                        text, buttons = format_et_magazine_page(everytime_magazines[mag_id], 0)
                        await client.send_message(uid, text, buttons=buttons, link_preview=False)
                except Exception as ex:
                    print(f"[EVERYTIME AUTO] user={uid}: {ex}")
                finally:
                    _active_et_checks.discard(uid)
        except Exception:
            traceback.print_exc()
        await asyncio.sleep(ET_AUTO_INTERVAL_SEC)
