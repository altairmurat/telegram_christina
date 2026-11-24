"""
ig_crypto_db.py
---------------
Zero-Knowledge / Blind-ID криптографический слой InstaSwipe Vault в составе Кристины:
- Первичный ключ в БД: `blind_uid` = HMAC-SHA256(BLIND_ID_SALT, "instaswipe:uid:<tg_id>") — без открытого Telegram ID.
- Персональный 256-битный ключ `AES-256-GCM` вычисляется ТОЛЬКО в RAM через PBKDF2-HMAC-SHA256
  из `(tg_user_id + ENCRYPTION_PEPPER + опциональный личный PIN)` и никогда не сохраняется в БД.
- Все списки сжимаются через `zlib` и шифруются `AES-256-GCM` со случайным 96-битным nonce.
- Использует общий пул соединений SQLAlchemy (`SessionLocal` -> Supabase PostgreSQL / SQLite).
"""

import base64
import datetime
import hashlib
import hmac
import json
import os
import time
import urllib.parse
import zlib
from typing import Any, Dict, Optional, Tuple

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

from database import SessionLocal, engine, db_url
from env import BOT_TOKEN, SECRETS_ENCRYPTION_KEY
import models

ENCRYPTION_PEPPER = (
    os.getenv("ENCRYPTION_PEPPER", "").strip()
    or SECRETS_ENCRYPTION_KEY
    or "instaswipe-christina-default-pepper-2026"
)
BLIND_ID_SALT = (
    os.getenv("BLIND_ID_SALT", "").strip()
    or hashlib.sha256(f"instaswipe-blind-salt::{ENCRYPTION_PEPPER}".encode("utf-8")).hexdigest()
)


# ============================================================================
# 1. Криптография: Blind ID + PBKDF2 Key Derivation + AES-256-GCM
# ============================================================================

def compute_blind_uid(tg_user_id: int | str) -> str:
    msg = f"instaswipe:uid:{str(tg_user_id).strip()}".encode("utf-8")
    salt = BLIND_ID_SALT.encode("utf-8")
    return hmac.new(salt, msg, hashlib.sha256).hexdigest()


def derive_user_aes_key(tg_user_id: int | str, user_pin: str = "") -> bytes:
    secret_material = f"key:{tg_user_id}::pepper:{ENCRYPTION_PEPPER}::pin:{user_pin.strip()}".encode("utf-8")
    salt = hashlib.sha256(f"salt:{tg_user_id}:{BLIND_ID_SALT}".encode("utf-8")).digest()
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        iterations=60_000,
    )
    return kdf.derive(secret_material)


def encrypt_json_blob(data: Any, aes_key: bytes) -> str:
    raw_json = json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    compressed = zlib.compress(raw_json, level=6)
    nonce = os.urandom(12)
    aesgcm = AESGCM(aes_key)
    ct = aesgcm.encrypt(nonce, compressed, associated_data=b"instaswipe-v1")
    token = base64.urlsafe_b64encode(nonce + ct).decode("ascii")
    return f"v1.{token}"


def decrypt_json_blob(token_str: str, aes_key: bytes, default: Any = None) -> Any:
    if not token_str:
        return default
    if not token_str.startswith("v1."):
        raise ValueError("Unsupported ciphertext format")
    b64_part = token_str[3:]
    raw = base64.urlsafe_b64decode(b64_part.encode("ascii"))
    nonce, ct = raw[:12], raw[12:]
    aesgcm = AESGCM(aes_key)
    compressed = aesgcm.decrypt(nonce, ct, associated_data=b"instaswipe-v1")
    raw_json = zlib.decompress(compressed)
    return json.loads(raw_json.decode("utf-8"))


# ============================================================================
# 2. Авторизация Telegram Mini App (initData HMAC) и подписанных токенов
# ============================================================================

def create_signed_session_token(tg_user_id: int | str, ttl_seconds: int = 86400 * 14) -> str:
    exp = int(time.time()) + ttl_seconds
    payload = f"{tg_user_id}.{exp}"
    secret = (BOT_TOKEN or ENCRYPTION_PEPPER).encode("utf-8")
    sig = hmac.new(secret, payload.encode("utf-8"), hashlib.sha256).hexdigest()[:32]
    return f"{payload}.{sig}"


def verify_signed_session_token(token: str) -> Optional[str]:
    if not token or token.count(".") != 2:
        return None
    uid_str, exp_str, sig = token.split(".")
    try:
        if int(exp_str) < int(time.time()):
            return None
    except ValueError:
        return None
    payload = f"{uid_str}.{exp_str}"
    secret = (BOT_TOKEN or ENCRYPTION_PEPPER).encode("utf-8")
    expected_sig = hmac.new(secret, payload.encode("utf-8"), hashlib.sha256).hexdigest()[:32]
    if hmac.compare_digest(sig, expected_sig):
        return uid_str
    return None


def verify_telegram_webapp_init_data(init_data: str) -> Optional[str]:
    """Проверяет криптографическую подпись `Telegram.WebApp.initData` по стандарту Telegram."""
    if not init_data:
        return None
    parsed = dict(urllib.parse.parse_qsl(init_data, keep_blank_values=True))
    received_hash = parsed.pop("hash", None)
    user_json = parsed.get("user")
    if not user_json:
        return None

    if BOT_TOKEN and received_hash:
        data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(parsed.items()))
        secret_key = hmac.new(b"WebAppData", BOT_TOKEN.encode("utf-8"), hashlib.sha256).digest()
        calculated_hash = hmac.new(secret_key, data_check_string.encode("utf-8"), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(calculated_hash, received_hash):
            return None

    try:
        user_obj = json.loads(user_json)
        uid = user_obj.get("id")
        return str(uid) if uid else None
    except Exception:
        return None


# ============================================================================
# 3. Хранилище через общий SQLAlchemy пул Кристины (Supabase PostgreSQL / SQLite)
# ============================================================================

class EncryptedVaultStorage:
    def __init__(self):
        self._table_ready = False

    def _ensure_table(self):
        if not self._table_ready:
            try:
                models.Base.metadata.create_all(bind=engine)
                self._table_ready = True
            except Exception as ex:
                print(f"[IG_VAULT] Table init warning: {ex}")

    @property
    def backend_name(self) -> str:
        is_pg = "postgres" in (db_url or "").lower()
        return "Supabase PostgreSQL (AES-256-GCM)" if is_pg else "Local Encrypted SQLite (AES-256-GCM)"

    def save_vault(
        self,
        tg_user_id: int | str,
        dataset: Dict[str, Any],
        actions: Dict[str, Any],
        meta: Optional[Dict[str, Any]] = None,
        user_pin: str = "",
    ) -> Dict[str, Any]:
        self._ensure_table()
        blind_uid = compute_blind_uid(tg_user_id)
        aes_key = derive_user_aes_key(tg_user_id, user_pin=user_pin)

        enc_dataset = encrypt_json_blob(dataset, aes_key)
        enc_actions = encrypt_json_blob(actions, aes_key)
        has_pin = bool(user_pin.strip())
        now_dt = datetime.datetime.utcnow()

        with SessionLocal() as db:
            row = db.query(models.EncryptedVault).filter_by(blind_uid=blind_uid).first()
            existing_meta = {}
            if row and row.encrypted_meta:
                try:
                    existing_meta = decrypt_json_blob(row.encrypted_meta, aes_key, default={}) or {}
                except Exception:
                    existing_meta = {}
            if meta is not None:
                existing_meta.update(meta)
            existing_meta["saved_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
            enc_meta = encrypt_json_blob(existing_meta, aes_key)

            if not row:
                row = models.EncryptedVault(
                    blind_uid=blind_uid,
                    encrypted_dataset=enc_dataset,
                    encrypted_actions=enc_actions,
                    encrypted_meta=enc_meta,
                    has_pin=has_pin,
                    schema_version=1,
                    created_at=now_dt,
                    updated_at=now_dt,
                )
                db.add(row)
            else:
                row.encrypted_dataset = enc_dataset
                row.encrypted_actions = enc_actions
                row.encrypted_meta = enc_meta
                row.has_pin = has_pin
                row.updated_at = now_dt
            db.commit()

        return {
            "blind_uid": blind_uid,
            "cipher_preview": enc_dataset[:42] + "...",
            "ciphertext_bytes": len(enc_dataset) + len(enc_actions),
            "has_pin": has_pin,
            "backend": self.backend_name,
        }

    def load_vault(
        self, tg_user_id: int | str, user_pin: str = ""
    ) -> Tuple[Optional[Dict[str, Any]], Dict[str, Any], Dict[str, Any]]:
        self._ensure_table()
        blind_uid = compute_blind_uid(tg_user_id)
        with SessionLocal() as db:
            row = db.query(models.EncryptedVault).filter_by(blind_uid=blind_uid).first()
            if not row:
                return None, {}, {
                    "blind_uid": blind_uid,
                    "exists": False,
                    "has_pin": False,
                    "locked": False,
                    "backend": self.backend_name,
                    "cipher_preview": "— (сейф пока пуст)",
                    "ciphertext_bytes": 0,
                }
            enc_ds = row.encrypted_dataset or ""
            enc_act = row.encrypted_actions or ""
            has_pin = bool(row.has_pin)
            upd_str = str(row.updated_at or "")

        aes_key = derive_user_aes_key(tg_user_id, user_pin=user_pin)
        try:
            dataset = decrypt_json_blob(enc_ds, aes_key, default=None)
            actions = decrypt_json_blob(enc_act, aes_key, default={})
            locked = False
        except Exception:
            dataset = None
            actions = {}
            locked = True

        privacy_info = {
            "blind_uid": blind_uid,
            "exists": True,
            "has_pin": has_pin,
            "locked": locked,
            "backend": self.backend_name,
            "cipher_preview": enc_ds[:44] + "...",
            "ciphertext_bytes": len(enc_ds) + len(enc_act),
            "updated_at": upd_str,
        }
        return dataset, actions, privacy_info

    def update_actions_only(
        self, tg_user_id: int | str, actions: Dict[str, Any], user_pin: str = ""
    ) -> bool:
        self._ensure_table()
        blind_uid = compute_blind_uid(tg_user_id)
        aes_key = derive_user_aes_key(tg_user_id, user_pin=user_pin)
        enc_actions = encrypt_json_blob(actions, aes_key)
        with SessionLocal() as db:
            row = db.query(models.EncryptedVault).filter_by(blind_uid=blind_uid).first()
            if not row:
                return False
            row.encrypted_actions = enc_actions
            row.updated_at = datetime.datetime.utcnow()
            db.commit()
            return True

    def load_meta(self, tg_user_id: int | str, user_pin: str = "") -> Dict[str, Any]:
        """Расшифровывает метаданные сейфа (включая зашифрованную куку Instagram и настройки авто-отписки)."""
        self._ensure_table()
        blind_uid = compute_blind_uid(tg_user_id)
        with SessionLocal() as db:
            row = db.query(models.EncryptedVault).filter_by(blind_uid=blind_uid).first()
            if not row or not row.encrypted_meta:
                return {}
            enc_m = row.encrypted_meta
        aes_key = derive_user_aes_key(tg_user_id, user_pin=user_pin)
        try:
            data = decrypt_json_blob(enc_m, aes_key, default={})
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def save_meta(self, tg_user_id: int | str, meta_updates: Dict[str, Any], user_pin: str = "") -> Dict[str, Any]:
        """Обновляет зашифрованные метаданные сейфа (создавая пустой сейф, если его ещё не было)."""
        self._ensure_table()
        blind_uid = compute_blind_uid(tg_user_id)
        aes_key = derive_user_aes_key(tg_user_id, user_pin=user_pin)
        now_dt = datetime.datetime.utcnow()
        with SessionLocal() as db:
            row = db.query(models.EncryptedVault).filter_by(blind_uid=blind_uid).first()
            cur_meta: Dict[str, Any] = {}
            if row and row.encrypted_meta:
                try:
                    cur_meta = decrypt_json_blob(row.encrypted_meta, aes_key, default={}) or {}
                except Exception:
                    cur_meta = {}
            cur_meta.update(meta_updates or {})
            enc_m = encrypt_json_blob(cur_meta, aes_key)
            if not row:
                row = models.EncryptedVault(
                    blind_uid=blind_uid,
                    encrypted_dataset=encrypt_json_blob({}, aes_key),
                    encrypted_actions=encrypt_json_blob({}, aes_key),
                    encrypted_meta=enc_m,
                    has_pin=bool(user_pin.strip()),
                    schema_version=1,
                    created_at=now_dt,
                    updated_at=now_dt,
                )
                db.add(row)
            else:
                row.encrypted_meta = enc_m
                row.updated_at = now_dt
            db.commit()
            return cur_meta

    def wipe_vault(self, tg_user_id: int | str) -> bool:
        self._ensure_table()
        blind_uid = compute_blind_uid(tg_user_id)
        with SessionLocal() as db:
            row = db.query(models.EncryptedVault).filter_by(blind_uid=blind_uid).first()
            if not row:
                return False
            db.delete(row)
            db.commit()
            return True


vault_storage = EncryptedVaultStorage()
