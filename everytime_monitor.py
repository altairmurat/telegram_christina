#!/usr/bin/env python3
"""Монитор и парсер Everytime DGIST (https://everytime.kr/393752) с дословным переводом через Gemini."""
import datetime
import html
import json
import os
import re
import requests
from bs4 import BeautifulSoup
from pydantic import BaseModel, Field
from google import genai
from google.genai import types

from database import SessionLocal
import models
from crypto_utils import encrypt as encrypt_str, decrypt as decrypt_str
from env import GEMINI_API_KEY

DGIST_FREE_BOARD_ID = "393752"
API_BASE = "https://api.everytime.kr"

DEFAULT_KEYWORDS = [
    "foreigner",
    "international",
    "외국인",
    "유학생",
    "201",
    "201동",
    "기숙사",
    "생활관",
    "dorm",
    "dormitory",
    "룸메",
    "roommate",
]

HEADERS_BASE = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
    "Origin": "https://everytime.kr",
    "Referer": "https://everytime.kr/393752",
    "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
}

gemini_client = genai.Client(api_key=GEMINI_API_KEY) if GEMINI_API_KEY else None


class EverytimeAuthError(Exception):
    pass


class TranslatedArticleItem(BaseModel):
    article_id: str = Field(description="ID поста")
    title_ru: str = Field(description="Дословный перевод заголовка на русский язык")
    text_ru: str = Field(
        description="Дословный перевод всего текста поста на русский язык (слово в слово, с пояснением корейского сленга в скобках при необходимости)"
    )
    comments_ru: list[str] = Field(
        default_factory=list,
        description="Дословный перевод комментариев на русский язык (если комментарии были переданы)",
    )


class BatchTranslationResult(BaseModel):
    items: list[TranslatedArticleItem]


def normalize_everytime_cookie(raw: str) -> str:
    if not raw:
        return ""
    s = raw.strip().strip('"').strip("'")
    if s.lower().startswith("cookie:"):
        s = s.split(":", 1)[1].strip()
    if "etsid=" in s:
        return s
    # Если пользователь скинул только само значение токена etsid
    return f"etsid={s}"


def save_everytime_cookie(uid: int, raw_cookie: str) -> str:
    cookie_str = normalize_everytime_cookie(raw_cookie)
    with SessionLocal() as db:
        st = db.query(models.EverytimeState).filter_by(telegram_user_id=uid).first()
        if not st:
            st = models.EverytimeState(
                telegram_user_id=uid,
                keywords_json=json.dumps(DEFAULT_KEYWORDS, ensure_ascii=False),
                seen_ids_json=json.dumps({"seen_ids": [], "cached_posts": {}}, ensure_ascii=False),
            )
            db.add(st)
        st.session_cookie = encrypt_str(cookie_str)
        st.updated_at = datetime.datetime.utcnow()
        db.commit()
    return cookie_str


def load_everytime_cookie(uid: int | None = None) -> str:
    with SessionLocal() as db:
        st = None
        if uid:
            st = db.query(models.EverytimeState).filter_by(telegram_user_id=uid).first()
        if not st or not st.session_cookie:
            st = db.query(models.EverytimeState).filter(models.EverytimeState.session_cookie.isnot(None)).first()
        if st and st.session_cookie:
            try:
                return decrypt_str(st.session_cookie)
            except Exception:
                return st.session_cookie
    env_ck = os.getenv("EVERYTIME_COOKIE") or os.getenv("ETSID") or ""
    return normalize_everytime_cookie(env_ck)


def list_everytime_user_ids() -> list[int]:
    with SessionLocal() as db:
        rows = db.query(models.EverytimeState).filter(models.EverytimeState.session_cookie.isnot(None)).all()
        return [r.telegram_user_id for r in rows if r.telegram_user_id]


def get_everytime_keywords(uid: int) -> list[str]:
    with SessionLocal() as db:
        st = db.query(models.EverytimeState).filter_by(telegram_user_id=uid).first()
        if st and st.keywords_json:
            try:
                kws = json.loads(st.keywords_json)
                if isinstance(kws, list) and kws:
                    return kws
            except Exception:
                pass
    return list(DEFAULT_KEYWORDS)


def set_everytime_keywords(uid: int, keywords: list[str]) -> list[str]:
    clean = []
    for k in keywords:
        w = k.strip()
        if w and w not in clean:
            clean.append(w)
    if not clean:
        clean = list(DEFAULT_KEYWORDS)
    with SessionLocal() as db:
        st = db.query(models.EverytimeState).filter_by(telegram_user_id=uid).first()
        if not st:
            st = models.EverytimeState(telegram_user_id=uid)
            db.add(st)
        st.keywords_json = json.dumps(clean, ensure_ascii=False)
        st.updated_at = datetime.datetime.utcnow()
        db.commit()
    return clean


def load_everytime_seen_data(uid: int) -> dict:
    with SessionLocal() as db:
        st = db.query(models.EverytimeState).filter_by(telegram_user_id=uid).first()
        if not st:
            st = db.query(models.EverytimeState).first()
        if st and st.seen_ids_json:
            try:
                data = json.loads(st.seen_ids_json)
                if isinstance(data, dict):
                    return data
            except Exception:
                pass
    return {"seen_ids": [], "cached_posts": {}, "last_magazine_posts": []}


def save_everytime_seen_data(uid: int, data: dict):
    with SessionLocal() as db:
        st = db.query(models.EverytimeState).filter_by(telegram_user_id=uid).first()
        if not st:
            st = models.EverytimeState(telegram_user_id=uid)
            db.add(st)
        st.seen_ids_json = json.dumps(data, ensure_ascii=False)
        st.updated_at = datetime.datetime.utcnow()
        db.commit()


def _clean_et_html(raw_str: str) -> str:
    if not raw_str:
        return ""
    s = re.sub(r"<br\s*/?>", "\n", raw_str, flags=re.IGNORECASE)
    s = re.sub(r"<[^>]+>", "", s)
    return html.unescape(s).strip()


def fetch_board_articles(
    cookie: str,
    board_id: str = DGIST_FREE_BOARD_ID,
    limit: int = 20,
    start_num: int = 0,
    search_type: int = 0,
    keyword: str = "",
) -> list[dict]:
    if not cookie:
        raise EverytimeAuthError("Кука Everytime не подключена. Нажми /connecteverytime")

    headers = dict(HEADERS_BASE)
    headers["Cookie"] = normalize_everytime_cookie(cookie)
    payload = {
        "id": str(board_id),
        "limit_num": str(limit),
        "start_num": str(start_num),
        "moiminfo": "true",
    }
    if search_type > 0 and keyword:
        payload["search_type"] = str(search_type)
        payload["keyword"] = keyword

    r = requests.post(f"{API_BASE}/find/board/article/list", headers=headers, data=payload, timeout=20)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    resp_tag = soup.find("response")
    if resp_tag and not resp_tag.find("article"):
        code_txt = resp_tag.get_text(strip=True)
        if code_txt in ("0", "-100", "-300", "-400"):
            raise EverytimeAuthError(
                "Сессия Everytime истекла или неверная кука `etsid`. Обнови её через /connecteverytime"
            )

    articles = []
    for art in soup.find_all("article"):
        aid = art.get("id")
        if not aid:
            continue
        title_ko = _clean_et_html(art.get("title") or "")
        text_ko = _clean_et_html(art.get("text") or "")
        created_at = art.get("created_at") or ""
        posvote = int(art.get("posvote") or 0)
        comment_cnt = int(art.get("comment") or 0)
        nick = _clean_et_html(art.get("user_nickname") or "익명")
        articles.append(
            {
                "id": str(aid),
                "board_id": str(board_id),
                "url": f"https://everytime.kr/{board_id}/v/{aid}",
                "title_ko": title_ko,
                "text_ko": text_ko,
                "created_at": created_at,
                "likes": posvote,
                "comments_count": comment_cnt,
                "author": nick,
            }
        )
    return articles


def fetch_article_detail_and_comments(
    cookie: str, article_id: str, board_id: str = DGIST_FREE_BOARD_ID
) -> dict:
    if not cookie:
        raise EverytimeAuthError("Кука Everytime не подключена. Нажми /connecteverytime")

    headers = dict(HEADERS_BASE)
    headers["Cookie"] = normalize_everytime_cookie(cookie)
    payload = {
        "id": str(article_id),
        "limit_num": "-1",
        "articleInfo": "true",
        "moiminfo": "true",
    }
    r = requests.post(f"{API_BASE}/find/board/comment/list", headers=headers, data=payload, timeout=20)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    resp_tag = soup.find("response")
    if resp_tag and not resp_tag.find("article") and not resp_tag.find("comment"):
        code_txt = resp_tag.get_text(strip=True)
        if code_txt in ("0", "-100", "-300", "-400"):
            raise EverytimeAuthError(
                "Сессия Everytime истекла или неверная кука `etsid`. Обнови её через /connecteverytime"
            )

    art = soup.find("article")
    article_data = {
        "id": str(article_id),
        "board_id": str(board_id),
        "url": f"https://everytime.kr/{board_id}/v/{article_id}",
        "title_ko": _clean_et_html(art.get("title") or "") if art else "",
        "text_ko": _clean_et_html(art.get("text") or "") if art else "",
        "created_at": (art.get("created_at") or "") if art else "",
        "likes": int(art.get("posvote") or 0) if art else 0,
        "comments_count": int(art.get("comment") or 0) if art else 0,
        "author": _clean_et_html(art.get("user_nickname") or "익명") if art else "익명",
        "comments_ko": [],
    }
    comments = []
    for c in soup.find_all("comment"):
        cid = c.get("id")
        ctext = _clean_et_html(c.get("text") or "")
        if not ctext or ctext == "삭제된 댓글입니다.":
            continue
        cnick = _clean_et_html(c.get("user_nickname") or "익명")
        clikes = int(c.get("posvote") or 0)
        is_reply = c.get("parent_id") and c.get("parent_id") != "0"
        prefix = "↳ " if is_reply else ""
        like_str = f" (👍{clikes})" if clikes > 0 else ""
        comments.append(f"{prefix}{cnick}{like_str}: {ctext}")
    article_data["comments_ko"] = comments[:25]
    return article_data


def match_keywords_in_article(article: dict, keywords: list[str]) -> list[str]:
    haystack = f"{article.get('title_ko', '')}\n{article.get('text_ko', '')}".lower()
    matched = []
    for kw in keywords:
        k_low = kw.lower().strip()
        if k_low and k_low in haystack:
            matched.append(kw)
    return matched


def translate_articles_word_for_word(articles: list[dict], cached_posts: dict | None = None) -> list[dict]:
    if not articles:
        return []
    cached_posts = cached_posts if isinstance(cached_posts, dict) else {}
    to_translate = []
    for art in articles:
        aid = str(art["id"])
        cached = cached_posts.get(aid)
        if (
            cached
            and cached.get("text_ru")
            and len(cached.get("comments_ko", [])) == len(art.get("comments_ko", []))
        ):
            art["title_ru"] = cached.get("title_ru", "")
            art["text_ru"] = cached.get("text_ru", "")
            art["comments_ru"] = cached.get("comments_ru", [])
        else:
            to_translate.append(art)

    if not to_translate or not gemini_client:
        return articles

    # Переводим пачками по 8 постов для высокой скорости и точности
    batch_size = 8
    env_model = os.getenv("GEMINI_MODEL", "gemini-3.5-flash-lite")
    models_to_try = []
    for m in ("gemini-3.8-flash", env_model, "gemini-3.5-flash-lite"):
        if m and m not in models_to_try:
            models_to_try.append(m)

    sys_inst = (
        "Ты — профессиональный переводчик студенческого форума Everytime университета DGIST (Корея).\n"
        "Твоя задача — перевести каждый пост и его комментарии с корейского/английского на русский язык ДОСЛОВНО (слово в слово, максимально точно передавая весь смысл, эмоции и детали, без сокращений!).\n"
        "Если встречаются корейские студенческие сокращения или сленг (например: ㄹㅇ = реально, 에타 = Everytime, 201동 = 201-й корпус общаги, 기숙사 = общежитие, 학식 = студ. столовая, 계단 = лестница), переводи их точно и понятно."
    )

    for i in range(0, len(to_translate), batch_size):
        chunk = to_translate[i : i + batch_size]
        payload_for_llm = []
        for a in chunk:
            payload_for_llm.append(
                {
                    "article_id": str(a["id"]),
                    "title_ko": a.get("title_ko", ""),
                    "text_ko": a.get("text_ko", ""),
                    "comments_ko": a.get("comments_ko", []),
                }
            )
        prompt = "Переведи дословно (слово в слово) следующие посты с DGIST Everytime:\n" + json.dumps(
            payload_for_llm, ensure_ascii=False, indent=2
        )

        translated_map: dict[str, TranslatedArticleItem] = {}
        for model_name in models_to_try:
            try:
                resp = gemini_client.models.generate_content(
                    model=model_name,
                    contents=prompt,
                    config=types.GenerateContentConfig(
                        system_instruction=sys_inst,
                        response_mime_type="application/json",
                        response_schema=BatchTranslationResult,
                        temperature=0.1,
                    ),
                )
                parsed = BatchTranslationResult.model_validate_json(resp.text)
                for item in parsed.items:
                    translated_map[str(item.article_id)] = item
                break
            except Exception as e:
                print(f"[EVERYTIME] Ошибка перевода моделью {model_name}: {e}")

        for a in chunk:
            aid = str(a["id"])
            tr = translated_map.get(aid)
            if tr:
                a["title_ru"] = tr.title_ru
                a["text_ru"] = tr.text_ru
                a["comments_ru"] = tr.comments_ru
                cached_posts[aid] = {
                    "title_ru": tr.title_ru,
                    "text_ru": tr.text_ru,
                    "comments_ko": a.get("comments_ko", []),
                    "comments_ru": tr.comments_ru,
                }
            else:
                a["title_ru"] = a.get("title_ko", "")
                a["text_ru"] = a.get("text_ko", "")
                a["comments_ru"] = a.get("comments_ko", [])

    return articles


def run_everytime_check(
    uid: int,
    mode: str = "keywords",
    custom_query: str = "",
) -> dict:
    """
    mode:
      - 'keywords': ищет посты по ключевым словам (foreigner, 외국인, 201, 기숙사, dormitory и др.) в свежей ленте + через поиск Everytime
      - 'all': берёт последние 15 постов подряд с доски 393752 (자유게시판) и переводит слово в слово
      - 'custom': ищет по конкретному запросу custom_query
    """
    cookie = load_everytime_cookie(uid)
    if not cookie:
        raise EverytimeAuthError("Everytime ещё не подключён. Нажми /connecteverytime")

    seen_data = load_everytime_seen_data(uid)
    cached_posts = seen_data.get("cached_posts") or {}
    seen_ids = set(str(x) for x in (seen_data.get("seen_ids") or []))
    keywords = get_everytime_keywords(uid)

    collected_by_id: dict[str, dict] = {}

    if mode == "custom" and custom_query.strip():
        q = custom_query.strip()
        found = fetch_board_articles(cookie, DGIST_FREE_BOARD_ID, limit=20, start_num=0, search_type=4, keyword=q)
        for art in found:
            art["matched_keywords"] = [q]
            collected_by_id[art["id"]] = art
    elif mode == "all":
        recent = fetch_board_articles(cookie, DGIST_FREE_BOARD_ID, limit=15, start_num=0)
        for art in recent:
            art["matched_keywords"] = match_keywords_in_article(art, keywords)
            collected_by_id[art["id"]] = art
    else:
        # 1. Проверяем 40 самых свежих постов на главной доске DGIST (393752)
        for start in (0, 20):
            batch = fetch_board_articles(cookie, DGIST_FREE_BOARD_ID, limit=20, start_num=start)
            for art in batch:
                matched = match_keywords_in_article(art, keywords)
                if matched:
                    art["matched_keywords"] = matched
                    collected_by_id[art["id"]] = art

        # 2. Дополнительно опрашиваем встроенный поиск Everytime по главным ключевым словам,
        # чтобы не пропустить важные посты, даже если они ушли ниже 2-й страницы
        search_probes = ["외국인", "foreigner", "201", "기숙사", "dormitory"]
        for extra_kw in keywords:
            if extra_kw not in search_probes and len(search_probes) < 8:
                search_probes.append(extra_kw)

        for probe in search_probes:
            try:
                res_list = fetch_board_articles(
                    cookie, DGIST_FREE_BOARD_ID, limit=10, start_num=0, search_type=4, keyword=probe
                )
                for art in res_list:
                    aid = art["id"]
                    if aid in collected_by_id:
                        if probe not in collected_by_id[aid]["matched_keywords"]:
                            collected_by_id[aid]["matched_keywords"].append(probe)
                    else:
                        matched = match_keywords_in_article(art, keywords) or [probe]
                        art["matched_keywords"] = matched
                        collected_by_id[aid] = art
            except EverytimeAuthError:
                raise
            except Exception as ex:
                print(f"[EVERYTIME] Ошибка поиска по '{probe}': {ex}")

    # Сортируем от самых новых к старым по числовому ID поста
    sorted_articles = sorted(
        collected_by_id.values(),
        key=lambda x: int(x["id"]) if str(x["id"]).isdigit() else 0,
        reverse=True,
    )[:18]

    # Для постов, у которых есть комментарии, подгружаем комментарии (до 12 постов)
    for idx, art in enumerate(sorted_articles):
        if idx < 12 and art.get("comments_count", 0) > 0:
            try:
                det = fetch_article_detail_and_comments(cookie, art["id"], art.get("board_id", DGIST_FREE_BOARD_ID))
                if det.get("text_ko"):
                    art["text_ko"] = det["text_ko"]
                if det.get("title_ko"):
                    art["title_ko"] = det["title_ko"]
                art["comments_ko"] = det.get("comments_ko", [])
            except Exception:
                art["comments_ko"] = []
        else:
            art["comments_ko"] = []

    translate_articles_word_for_word(sorted_articles, cached_posts=cached_posts)

    new_articles = []
    for art in sorted_articles:
        aid = str(art["id"])
        art["is_new"] = aid not in seen_ids
        if art["is_new"]:
            new_articles.append(art)
        seen_ids.add(aid)

    # Ограничиваем размер кэша в БД
    if len(cached_posts) > 80:
        keep_keys = list(cached_posts.keys())[-60:]
        cached_posts = {k: cached_posts[k] for k in keep_keys}

    seen_data["seen_ids"] = list(seen_ids)[-300:]
    seen_data["cached_posts"] = cached_posts
    seen_data["last_magazine_posts"] = sorted_articles[:18]
    save_everytime_seen_data(uid, seen_data)

    return {
        "posts": sorted_articles,
        "new_posts": new_articles,
        "keywords": keywords,
        "mode": mode,
    }


def fetch_and_translate_single_post(uid: int, article_id: str, board_id: str = DGIST_FREE_BOARD_ID) -> dict:
    cookie = load_everytime_cookie(uid)
    if not cookie:
        raise EverytimeAuthError("Everytime ещё не подключён. Нажми /connecteverytime")
    seen_data = load_everytime_seen_data(uid)
    cached_posts = seen_data.get("cached_posts") or {}

    art = fetch_article_detail_and_comments(cookie, article_id, board_id)
    translate_articles_word_for_word([art], cached_posts=cached_posts)

    seen_data["cached_posts"] = cached_posts
    save_everytime_seen_data(uid, seen_data)
    return art
