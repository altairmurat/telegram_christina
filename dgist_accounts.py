"""
dgist_accounts.py
------------------
DB-хелперы для DGIST-кредов и сессий (куки, SSO storage_state, seen_ids, group_target).
Все данные хранятся в БД (PostgreSQL / SQLite), поэтому сессия и список просмотренных
постов никогда не сбрасываются при редеплое на Render и не требуют тяжелого профиля на диске.
"""

import json
import datetime
from database import SessionLocal, engine
import models
import crypto_utils


def _ensure_tables():
    try:
        models.Base.metadata.create_all(bind=engine)
    except Exception:
        pass


def save_dgist_account(
    telegram_user_id: int,
    dgist_username: str,
    dgist_password: str,
    email_address: str,
    email_app_password: str,
    email_imap_host: str = "imap.gmail.com",
) -> None:
    _ensure_tables()
    db = SessionLocal()
    try:
        acc = db.query(models.DgistAccount).filter_by(telegram_user_id=telegram_user_id).first()
        if not acc:
            acc = models.DgistAccount(telegram_user_id=telegram_user_id)
            db.add(acc)

        acc.dgist_username = dgist_username
        acc.dgist_password_enc = crypto_utils.encrypt(dgist_password)
        acc.email_address = email_address
        acc.email_app_password_enc = crypto_utils.encrypt(email_app_password)
        acc.email_imap_host = email_imap_host or "imap.gmail.com"
        db.commit()
    except Exception as e:
        db.rollback()
        print(f"save_dgist_account error for {telegram_user_id}: {e}")
        raise
    finally:
        db.close()


def load_dgist_account(telegram_user_id: int) -> dict | None:
    """Возвращает dict с РАСШИФРОВАННЫМИ полями, готовый к передаче в auto_login,
    либо None, если юзер ещё не подключал портал."""
    _ensure_tables()
    db = SessionLocal()
    try:
        acc = db.query(models.DgistAccount).filter_by(telegram_user_id=telegram_user_id).first()
        if not acc:
            return None
        return {
            "dgist_username": acc.dgist_username,
            "dgist_password": crypto_utils.decrypt(acc.dgist_password_enc),
            "email_address": acc.email_address,
            "email_app_password": crypto_utils.decrypt(acc.email_app_password_enc),
            "email_imap_host": acc.email_imap_host or "imap.gmail.com",
        }
    except Exception as e:
        print(f"load_dgist_account error for {telegram_user_id}: {e}")
        return None
    finally:
        db.close()


def delete_dgist_account(telegram_user_id: int) -> None:
    _ensure_tables()
    db = SessionLocal()
    try:
        acc = db.query(models.DgistAccount).filter_by(telegram_user_id=telegram_user_id).first()
        if acc:
            db.delete(acc)
        st = db.query(models.DgistSessionState).filter_by(telegram_user_id=telegram_user_id).first()
        if st:
            db.delete(st)
        db.commit()
    except Exception as e:
        db.rollback()
        print(f"delete_dgist_account error for {telegram_user_id}: {e}")
    finally:
        db.close()


def list_dgist_user_ids() -> list[int]:
    """Возвращает Telegram ID всех пользователей с подключённым DGIST."""
    _ensure_tables()
    db = SessionLocal()
    try:
        rows = db.query(models.DgistAccount.telegram_user_id).all()
        return [int(row[0]) for row in rows if row[0] is not None]
    except Exception as e:
        print(f"list_dgist_user_ids error: {e}")
        return []
    finally:
        db.close()


def save_dgist_session(
    telegram_user_id: int,
    session_cookie: str | None = None,
    storage_state: dict | str | None = None,
) -> None:
    """Сохраняет session_cookie и SSO storage_state браузера напрямую в базу данных."""
    if not telegram_user_id:
        return
    _ensure_tables()
    db = SessionLocal()
    try:
        st = db.query(models.DgistSessionState).filter_by(telegram_user_id=int(telegram_user_id)).first()
        if not st:
            st = models.DgistSessionState(telegram_user_id=int(telegram_user_id))
            db.add(st)
        if session_cookie is not None:
            st.session_cookie = session_cookie
        if storage_state is not None:
            st.storage_state_json = (
                storage_state if isinstance(storage_state, str)
                else json.dumps(storage_state, ensure_ascii=False)
            )
        st.updated_at = datetime.datetime.utcnow()
        db.commit()
    except Exception as e:
        db.rollback()
        print(f"save_dgist_session error for {telegram_user_id}: {e}")
    finally:
        db.close()


def load_dgist_session(telegram_user_id: int) -> dict:
    """Возвращает {'session_cookie': str, 'storage_state': dict | None} из БД."""
    if not telegram_user_id:
        return {"session_cookie": "", "storage_state": None}
    _ensure_tables()
    db = SessionLocal()
    try:
        st = db.query(models.DgistSessionState).filter_by(telegram_user_id=int(telegram_user_id)).first()
        if not st:
            return {"session_cookie": "", "storage_state": None}
        storage_dict = None
        if st.storage_state_json:
            try:
                storage_dict = json.loads(st.storage_state_json)
            except Exception:
                storage_dict = None
        return {
            "session_cookie": (st.session_cookie or "").strip(),
            "storage_state": storage_dict,
        }
    except Exception as e:
        print(f"load_dgist_session error for {telegram_user_id}: {e}")
        return {"session_cookie": "", "storage_state": None}
    finally:
        db.close()


def save_dgist_seen_ids(telegram_user_id: int, seen: dict) -> None:
    """Сохраняет словарь просмотренных объявлений (seen_ids) в БД."""
    if not telegram_user_id:
        return
    _ensure_tables()
    db = SessionLocal()
    try:
        st = db.query(models.DgistSessionState).filter_by(telegram_user_id=int(telegram_user_id)).first()
        if not st:
            st = models.DgistSessionState(telegram_user_id=int(telegram_user_id))
            db.add(st)
        st.seen_ids_json = json.dumps(seen or {}, ensure_ascii=False)
        st.updated_at = datetime.datetime.utcnow()
        db.commit()
    except Exception as e:
        db.rollback()
        print(f"save_dgist_seen_ids error for {telegram_user_id}: {e}")
    finally:
        db.close()


def load_dgist_seen_ids(telegram_user_id: int) -> dict:
    """Загружает словарь просмотренных объявлений (seen_ids) из БД."""
    if not telegram_user_id:
        return {}
    _ensure_tables()
    db = SessionLocal()
    try:
        st = db.query(models.DgistSessionState).filter_by(telegram_user_id=int(telegram_user_id)).first()
        if not st or not st.seen_ids_json:
            return {}
        return json.loads(st.seen_ids_json)
    except Exception as e:
        print(f"load_dgist_seen_ids error for {telegram_user_id}: {e}")
        return {}
    finally:
        db.close()


def save_dgist_group_target(target: dict) -> None:
    """Сохраняет целевой топик группы для авто-уведомлений DGIST в БД."""
    _ensure_tables()
    db = SessionLocal()
    try:
        st = db.query(models.DgistSessionState).order_by(models.DgistSessionState.id.asc()).first()
        if not st:
            st = models.DgistSessionState(telegram_user_id=0)
            db.add(st)
        st.group_target_json = json.dumps(target or {}, ensure_ascii=False)
        db.commit()
    except Exception as e:
        db.rollback()
        print(f"save_dgist_group_target error: {e}")
    finally:
        db.close()


def load_dgist_group_target() -> dict | None:
    """Загружает целевой топик группы из БД."""
    _ensure_tables()
    db = SessionLocal()
    try:
        rows = db.query(models.DgistSessionState).filter(models.DgistSessionState.group_target_json.isnot(None)).all()
        for r in rows:
            if r.group_target_json:
                try:
                    return json.loads(r.group_target_json)
                except Exception:
                    continue
        return None
    except Exception:
        return None
    finally:
        db.close()


def get_dgist_last_check_age_sec(telegram_user_id: int) -> float | None:
    """Возвращает сколько секунд прошло с момента последнего обновления seen_ids/сессии в БД."""
    if not telegram_user_id:
        return None
    _ensure_tables()
    db = SessionLocal()
    try:
        st = db.query(models.DgistSessionState).filter_by(telegram_user_id=int(telegram_user_id)).first()
        if not st or not st.seen_ids_json or not st.updated_at:
            return None
        return max(0.0, (datetime.datetime.utcnow() - st.updated_at).total_seconds())
    except Exception:
        return None
    finally:
        db.close()

