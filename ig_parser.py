"""
ig_parser.py
------------
Универсальный парсер выгрузок Instagram (ZIP-архивы, целые папки, отдельные JSON-файлы, CSV).

Особенности:
- Автоматически чинит битую кодировку Instagram (Mojibake UTF-8: "\\u00d0\\u0090..." -> кириллица/корейский/эмодзи).
- Понимает любую структуру папок (старый и новый формат Meta/Instagram, плюс живые слепки live_following.json).
- Поддерживает инкрементальную дозагрузку: пользователь может скинуть боту сначала `following.json`,
  затем `followers_1.json`, `pending_follow_requests.json`, `close_friends.json`, `liked_posts.json` —
  всё аккуратно склеится внутри его зашифрованного сейфа.
- Считает аналитические метрики:
  * 4 вкладки: Не взаимно (`not_back`), Взаимные (`mutual`), Подписаны (`fans`), Висящие заявки (`pending`).
  * Кто подписался первым (`who_first`: `you`, `them`, `same_day`) и разница в днях.
  * Индекс близости / Ghost-метр (`tie_score` 0..100 и категория: Призрак / Слабый / Тёплый / Близкий круг).
  * Сводка для «Инста-Радара» (индекс взаимности, карма подписок, топ «мамонтов», самые долгие заявки).
"""

import csv
import io
import json
import time
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple


def fix_mojibake(text: str) -> str:
    """Восстанавливает нормальный UTF-8 текст из битой Latin-1 кодировки экспортов Instagram."""
    if not text or not isinstance(text, str):
        return text or ""
    try:
        if any("\u00c0" <= ch <= "\u00ff" for ch in text):
            return text.encode("latin-1").decode("utf-8")
    except Exception:
        pass
    return text


def clean_username(u: Any) -> str:
    if not u or not isinstance(u, str):
        return ""
    u = fix_mojibake(u).strip().lstrip("@")
    if "/" in u:
        u = u.rstrip("/").split("/")[-1]
    return u.strip()


def extract_entries_from_ig_list(items: Any) -> List[Tuple[str, int]]:
    """Вытаскивает пары (username, timestamp) из стандартного списка Instagram."""
    out: List[Tuple[str, int]] = []
    if not isinstance(items, list):
        return out
    for item in items:
        if not isinstance(item, dict):
            continue
        s_list = item.get("string_list_data")
        u = ""
        ts = 0
        if isinstance(s_list, list) and s_list:
            first = s_list[0] if isinstance(s_list[0], dict) else {}
            u = first.get("value") or item.get("title") or first.get("href", "")
            ts = int(first.get("timestamp") or 0)
        else:
            u = item.get("title") or item.get("username") or item.get("value") or ""
            ts = int(item.get("timestamp") or 0)
        u = clean_username(u)
        if u:
            out.append((u, ts))
    return out


def empty_raw_state() -> Dict[str, Any]:
    return {
        "following": {},
        "followers": {},
        "pending_requests": {},
        "recent_requests": {},
        "close_friends": [],
        "blocked": {},
        "unfollowed": [],
        "interactions": {},
        "files_ingested": [],
    }


def looks_like_ig_filename(filename: str) -> bool:
    """Быстрая проверка, похоже ли имя файла на выгрузку Instagram."""
    low = (filename or "").lower()
    if low.endswith(".zip"):
        return any(k in low for k in ("instagram", "connections", "followers", "following", "export", "meta"))
    if low.endswith(".json") or low.endswith(".csv"):
        return any(k in low for k in (
            "following", "follower", "close_friend", "pending", "unfollow",
            "blocked", "liked_", "likes", "comment", "story", "message", "instagram",
        ))
    return False


def ingest_single_json(raw_state: Dict[str, Any], filename: str, data: Any) -> Optional[str]:
    """
    Распознаёт тип JSON-файла Instagram (даже если его переименовали) по ключам и имени
    и добавляет данные в `raw_state`. Возвращает название распознанной категории или None.
    """
    if data is None:
        return None

    fname = Path(filename).name.lower()
    parent = str(Path(filename).parent).lower()

    following = raw_state.setdefault("following", {})
    followers = raw_state.setdefault("followers", {})
    pending = raw_state.setdefault("pending_requests", {})
    recent_req = raw_state.setdefault("recent_requests", {})
    close_set = set(raw_state.get("close_friends", []))
    blocked = raw_state.setdefault("blocked", {})
    unfollowed_set = set(raw_state.get("unfollowed", []))
    interactions = raw_state.setdefault("interactions", {})

    def bump_inter(user: Any, key: str, count: int = 1):
        u = clean_username(user).lower()
        if not u:
            return
        rec = interactions.setdefault(u, {"likes": 0, "comments": 0, "messages": 0, "stories": 0})
        rec[key] = rec.get(key, 0) + count

    recognized = None

    # 1. Проверка по характерным корневым ключам словаря Instagram
    if isinstance(data, dict):
        if "relationships_following" in data:
            for u, ts in extract_entries_from_ig_list(data["relationships_following"]):
                prev = following.get(u, {})
                following[u] = {**prev, "ts": ts or prev.get("ts", 0)}
            recognized = "following"

        elif "relationships_followers" in data:
            for u, ts in extract_entries_from_ig_list(data["relationships_followers"]):
                prev = followers.get(u, {})
                followers[u] = {**prev, "ts": ts or prev.get("ts", 0)}
            recognized = "followers"

        elif "relationships_close_friends" in data:
            for u, _ in extract_entries_from_ig_list(data["relationships_close_friends"]):
                close_set.add(u.lower())
            recognized = "close_friends"

        elif "relationships_follow_requests_sent" in data:
            for u, ts in extract_entries_from_ig_list(data["relationships_follow_requests_sent"]):
                pending[u] = {"ts": ts}
            recognized = "pending_requests"

        elif "relationships_permanent_follow_requests" in data:
            for u, ts in extract_entries_from_ig_list(data["relationships_permanent_follow_requests"]):
                recent_req[u] = {"ts": ts}
            recognized = "recent_requests"

        elif "relationships_blocked_users" in data:
            for u, ts in extract_entries_from_ig_list(data["relationships_blocked_users"]):
                blocked[u] = {"ts": ts}
            recognized = "blocked"

        elif "relationships_unfollowed_users" in data:
            hist_unf = set(raw_state.get("history_unfollowed", []))
            for u, _ in extract_entries_from_ig_list(data["relationships_unfollowed_users"]):
                hist_unf.add(u.lower())
            raw_state["history_unfollowed"] = sorted(hist_unf)
            recognized = "recently_unfollowed"

        elif "likes_media_likes" in data or "likes_comment_likes" in data:
            items = data.get("likes_media_likes") or data.get("likes_comment_likes") or []
            if isinstance(items, list):
                for it in items:
                    if isinstance(it, dict):
                        bump_inter(it.get("title"), "likes", max(1, len(it.get("string_list_data", []))))
            recognized = "likes"

        elif "comments_media_comments" in data:
            for it in data.get("comments_media_comments", []):
                if isinstance(it, dict):
                    smap = it.get("string_map_data", {})
                    owner = (smap.get("Media Owner") or {}).get("value") or it.get("title")
                    bump_inter(owner, "comments", 1)
            recognized = "comments"

        elif "participants" in data and "messages" in data and isinstance(data.get("messages"), list):
            title = fix_mojibake(data.get("title", ""))
            msgs = data.get("messages", [])
            if title and msgs:
                bump_inter(title, "messages", len(msgs))
            recognized = "messages"

        elif any(k.startswith("story_activities_") for k in data.keys()):
            for _, v in data.items():
                if isinstance(v, list):
                    for it in v:
                        if isinstance(it, dict):
                            bump_inter(it.get("title"), "stories", max(1, len(it.get("string_list_data", []))))
            recognized = "stories"

        elif fname in ("live_following.json", "live_followers.json") or (
            data and all(isinstance(v, dict) and ("is_private" in v or "full_name" in v or "id" in v) for v in list(data.values())[:5])
        ):
            target = followers if "follower" in fname else following
            for raw_u, info in data.items():
                u = clean_username(raw_u)
                if not u or not isinstance(info, dict):
                    continue
                prev = target.get(u, {})
                target[u] = {
                    "ts": prev.get("ts", 0),
                    "full_name": fix_mojibake(info.get("full_name", "")) or prev.get("full_name", ""),
                    "is_private": bool(info.get("is_private", prev.get("is_private", False))),
                    "is_verified": bool(info.get("is_verified", prev.get("is_verified", False))),
                    "id": str(info.get("id", prev.get("id", ""))),
                }
            recognized = "live_followers" if "follower" in fname else "live_following"

    # 2. Если корневой элемент — список (как в followers_1.json, post_comments_1.json или unfollow_log.json)
    elif isinstance(data, list) and data:
        first = data[0] if isinstance(data[0], dict) else {}
        if "username" in first and "status" in first:
            for entry in data:
                if isinstance(entry, dict) and entry.get("status") in ("ok", "skipped") and entry.get("username"):
                    unfollowed_set.add(clean_username(entry["username"]).lower())
            recognized = "unfollow_log"
        elif "string_map_data" in first and "Media Owner" in first.get("string_map_data", {}):
            for it in data:
                if isinstance(it, dict):
                    owner = (it.get("string_map_data", {}).get("Media Owner") or {}).get("value")
                    bump_inter(owner, "comments", 1)
            recognized = "comments"
        elif "string_list_data" in first:
            entries = extract_entries_from_ig_list(data)
            if "following" in fname and "follower" not in fname:
                for u, ts in entries:
                    prev = following.get(u, {})
                    following[u] = {**prev, "ts": ts or prev.get("ts", 0)}
                recognized = "following"
            elif "close_friend" in fname:
                for u, _ in entries:
                    close_set.add(u.lower())
                recognized = "close_friends"
            elif "pending" in fname:
                for u, ts in entries:
                    pending[u] = {"ts": ts}
                recognized = "pending_requests"
            elif "like" in fname or "likes" in parent:
                for it in data:
                    if isinstance(it, dict):
                        bump_inter(it.get("title"), "likes", max(1, len(it.get("string_list_data", []))))
                recognized = "likes"
            else:
                for u, ts in entries:
                    prev = followers.get(u, {})
                    followers[u] = {**prev, "ts": ts or prev.get("ts", 0)}
                recognized = "followers"

    raw_state["close_friends"] = sorted(close_set)
    raw_state["unfollowed"] = sorted(unfollowed_set)
    if recognized:
        ingested = raw_state.setdefault("files_ingested", [])
        if fname not in ingested:
            ingested.append(fname)
    return recognized


def ingest_files_bundle(
    files: Iterable[Tuple[str, bytes]],
    existing_raw_state: Optional[Dict[str, Any]] = None,
) -> Tuple[Dict[str, Any], List[str]]:
    """
    Принимает коллекцию файлов `(filename, raw_bytes)` (.zip, .json, .csv).
    Распаковывает любые ZIP-архивы в RAM (без записи на диск) и сливает всё в `raw_state`.
    """
    state = existing_raw_state if existing_raw_state is not None else empty_raw_state()
    recognized_files: List[str] = []

    for filename, raw_bytes in files:
        if not raw_bytes:
            continue
        lower_name = filename.lower()

        if lower_name.endswith(".zip") or (len(raw_bytes) > 4 and raw_bytes[:4] == b"PK\x03\x04"):
            try:
                with zipfile.ZipFile(io.BytesIO(raw_bytes)) as zf:
                    for info in zf.infolist():
                        if info.is_dir():
                            continue
                        zname = info.filename
                        if not zname.lower().endswith(".json"):
                            continue
                        if info.file_size > 25 * 1024 * 1024:
                            continue
                        try:
                            content = zf.read(info.filename)
                            parsed = json.loads(content.decode("utf-8", errors="replace"))
                            cat = ingest_single_json(state, zname, parsed)
                            if cat:
                                recognized_files.append(f"{Path(zname).name} ({cat})")
                        except Exception:
                            continue
            except Exception:
                continue

        elif lower_name.endswith(".json"):
            try:
                parsed = json.loads(raw_bytes.decode("utf-8", errors="replace"))
                cat = ingest_single_json(state, filename, parsed)
                if cat:
                    recognized_files.append(f"{Path(filename).name} ({cat})")
            except Exception:
                continue

        elif lower_name.endswith(".csv"):
            try:
                text = raw_bytes.decode("utf-8-sig", errors="replace")
                reader = csv.DictReader(io.StringIO(text))
                fnames = [c.lower() for c in (reader.fieldnames or [])]
                if "username" not in fnames:
                    continue
                following = state.setdefault("following", {})
                added_any = False
                for row in reader:
                    un = clean_username(row.get("username", ""))
                    if un:
                        prev = following.get(un, {})
                        following[un] = {
                            "ts": prev.get("ts", 0),
                            "full_name": fix_mojibake(row.get("full_name", "")) or prev.get("full_name", ""),
                            "is_private": str(row.get("is_private", "")).lower() == "true",
                            "is_verified": str(row.get("is_verified", "")).lower() == "true",
                            "id": row.get("user_id", "") or prev.get("id", ""),
                        }
                        added_any = True
                if added_any:
                    recognized_files.append(f"{Path(filename).name} (csv)")
            except Exception:
                continue

    return state, recognized_files


def compile_dataset_from_raw(
    raw_state: Dict[str, Any],
    actions_map: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Превращает сырой `raw_state` в аналитический датасет для Tinder Mini App и ИИ-ответов Кристины."""
    actions_map = actions_map or {}
    following: Dict[str, Dict[str, Any]] = raw_state.get("following", {})
    followers: Dict[str, Dict[str, Any]] = raw_state.get("followers", {})
    pending: Dict[str, Dict[str, Any]] = raw_state.get("pending_requests", {})
    close_friends = set(raw_state.get("close_friends", []))
    blocked = raw_state.get("blocked", {})
    already_unfollowed = set(raw_state.get("unfollowed", []))
    history_unfollowed = set(raw_state.get("history_unfollowed", []))
    interactions: Dict[str, Dict[str, int]] = raw_state.get("interactions", {})

    now_ts = int(time.time())
    all_usernames = sorted(set(following.keys()) | set(followers.keys()) | set(pending.keys()))

    profiles: List[Dict[str, Any]] = []
    ghosts_count = 0

    for u in all_usernames:
        ul = u.lower()
        is_following = (u in following) and (ul not in already_unfollowed)
        is_follower = u in followers
        is_pending = (u in pending) and not is_following

        if is_following and is_follower:
            tab = "mutual"
        elif is_following and not is_follower:
            tab = "not_back"
        elif is_follower and not is_following:
            tab = "fans"
        elif is_pending:
            tab = "pending"
        else:
            continue

        f_info = following.get(u, {})
        fl_info = followers.get(u, {})
        p_info = pending.get(u, {})

        f_ts = int(f_info.get("ts") or p_info.get("ts") or 0)
        fl_ts = int(fl_info.get("ts") or 0)
        primary_ts = f_ts or fl_ts

        who_first = ""
        diff_days = 0
        if tab == "mutual" and f_ts > 0 and fl_ts > 0:
            delta_sec = f_ts - fl_ts
            diff_days = int(round(abs(delta_sec) / 86400))
            if abs(delta_sec) < 86400:
                who_first = "same_day"
            elif f_ts < fl_ts:
                who_first = "you"
            else:
                who_first = "them"

        days_ago = int(max(0, (now_ts - primary_ts) // 86400)) if primary_ts > 0 else 0

        inter = interactions.get(ul, {"likes": 0, "comments": 0, "messages": 0, "stories": 0})
        raw_inter_points = (
            inter.get("messages", 0) * 5
            + inter.get("comments", 0) * 3
            + inter.get("stories", 0) * 2
            + inter.get("likes", 0) * 1
        )
        is_cf = ul in close_friends
        act_obj = actions_map.get(u, {})
        act = act_obj.get("action", "") if isinstance(act_obj, dict) else ""
        tag = act_obj.get("tag", "") if isinstance(act_obj, dict) else ""
        exec_status = act_obj.get("exec_status", "") if isinstance(act_obj, dict) else ""
        exec_msg = act_obj.get("exec_msg", "") if isinstance(act_obj, dict) else ""
        is_keep = (act == "keep") or bool(tag)

        tie_score = min(100, raw_inter_points * 2 + (60 if is_cf else 0) + (25 if is_keep else 0) + (15 if tab == "mutual" else 0))
        if who_first == "them":
            tie_score = min(100, tie_score + 5)

        if tie_score >= 60 or is_cf:
            tie_tier = "inner_circle"
        elif tie_score >= 25:
            tie_tier = "warm"
        elif tie_score > 0:
            tie_tier = "low"
        else:
            tie_tier = "ghost"

        if tab == "not_back" and tie_tier == "ghost" and not is_cf and not is_keep:
            ghosts_count += 1

        full_name = f_info.get("full_name") or fl_info.get("full_name") or ""
        is_private = bool(f_info.get("is_private") or fl_info.get("is_private") or (tab == "pending"))
        is_verified = bool(f_info.get("is_verified") or fl_info.get("is_verified"))
        user_id = str(f_info.get("id") or fl_info.get("id") or "")

        profiles.append({
            "username": u,
            "full_name": full_name,
            "is_private": is_private,
            "is_verified": is_verified,
            "user_id": user_id,
            "tab": tab,
            "followed_at": f_ts,
            "followed_date": datetime.fromtimestamp(f_ts).strftime("%Y-%m-%d") if f_ts > 0 else "—",
            "follower_since": fl_ts,
            "follower_date": datetime.fromtimestamp(fl_ts).strftime("%Y-%m-%d") if fl_ts > 0 else "—",
            "primary_ts": primary_ts,
            "days_ago": days_ago,
            "who_first": who_first,
            "diff_days": diff_days,
            "is_close_friend": is_cf,
            "is_keep": is_keep,
            "was_unfollowed_before": ul in history_unfollowed,
            "interactions": inter,
            "interaction_score": tie_score,
            "raw_inter_points": raw_inter_points,
            "tie_tier": tie_tier,
            "action": act,
            "tag": tag,
            "exec_status": exec_status,
            "exec_msg": exec_msg,
            "link": f"https://www.instagram.com/{u}/",
        })

    mutual_cnt = sum(1 for p in profiles if p["tab"] == "mutual")
    not_back_cnt = sum(1 for p in profiles if p["tab"] == "not_back")
    fans_cnt = sum(1 for p in profiles if p["tab"] == "fans")
    pending_cnt = sum(1 for p in profiles if p["tab"] == "pending")
    total_following = mutual_cnt + not_back_cnt
    total_followers = mutual_cnt + fans_cnt

    reciprocity_rate = round((mutual_cnt / total_following) * 100, 1) if total_following > 0 else 0.0
    follower_ratio = round(total_followers / total_following, 2) if total_following > 0 else 0.0

    oldest_not_back = sorted(
        [p for p in profiles if p["tab"] == "not_back" and p["primary_ts"] > 0],
        key=lambda x: x["primary_ts"],
    )[:5]

    oldest_pending = sorted(
        [p for p in profiles if p["tab"] == "pending" and p["primary_ts"] > 0],
        key=lambda x: x["primary_ts"],
    )[:5]

    return {
        "profiles": profiles,
        "raw_state": raw_state,
        "summary": {
            "total_following": total_following,
            "total_followers": total_followers,
            "mutual_count": mutual_cnt,
            "not_back_count": not_back_cnt,
            "fans_count": fans_cnt,
            "pending_count": pending_cnt,
            "close_friends_count": len(close_friends),
            "blocked_count": len(blocked),
            "ghosts_count": ghosts_count,
            "reciprocity_rate": reciprocity_rate,
            "follower_ratio": follower_ratio,
            "files_ingested": raw_state.get("files_ingested", []),
            "oldest_not_back": [
                {"username": p["username"], "date": p["followed_date"], "days": p["days_ago"]}
                for p in oldest_not_back
            ],
            "oldest_pending": [
                {"username": p["username"], "date": p["followed_date"], "days": p["days_ago"]}
                for p in oldest_pending
            ],
        },
    }
