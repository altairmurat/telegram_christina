from sqlalchemy import Column, Integer, Float, String, ForeignKey, Text, BigInteger, DateTime, Boolean
import datetime
from database import Base

class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True)
    telegram_user_id = Column(BigInteger, unique=True)
    username = Column(String, nullable=True)

class Contact(Base):
    __tablename__ = "contacts"
    
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"))
    display_name = Column(String)
    email = Column(String)
    source = Column(String)
    confidence = Column(Float, default=1.0)
    
class GoogleAccount(Base):
    __tablename__ = "google_accounts"
    id = Column(Integer, primary_key=True)
    telegram_user_id = Column(BigInteger, unique=True, index=True)
    email = Column(String, nullable=True)          # для показа "подключено как X@gmail.com"
    token_json = Column(Text)                      # весь Credentials.to_json() одной строкой
    
class Communication(Base):
    __tablename__ = "communication"
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    username = Column(String)
    usermessage = Column(String)
    
class DgistAccount(Base):
    __tablename__ = "dgist_accounts"

    id = Column(Integer, primary_key=True)
    telegram_user_id = Column(BigInteger, unique=True, index=True)

    dgist_username = Column(String)              # логин от портала — не секрет, не шифруем
    dgist_password_enc = Column(Text)             # ЗАШИФРОВАНО через crypto_utils.encrypt()

    email_address = Column(String)                # почта, куда приходит код — не секрет
    email_app_password_enc = Column(Text)          # ЗАШИФРОВАНО
    email_imap_host = Column(String, default="imap.gmail.com")

class GroupTopic(Base):
    __tablename__ = "group_topics"

    id = Column(Integer, primary_key=True, autoincrement=True)
    chat_id = Column(BigInteger, index=True)
    topic_id = Column(Integer, index=True)
    topic_title = Column(String)
    updated_at = Column(DateTime, default=datetime.datetime.utcnow)

class GroupMessage(Base):
    __tablename__ = "group_messages"

    id = Column(Integer, primary_key=True, autoincrement=True)
    message_id = Column(BigInteger, index=True)
    chat_id = Column(BigInteger, index=True)
    topic_id = Column(Integer, index=True, nullable=True)
    topic_title = Column(String, nullable=True)
    sender_id = Column(BigInteger, nullable=True)
    sender_name = Column(String, nullable=True)
    text = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.datetime.utcnow, index=True)

class CalendarReminder(Base):
    __tablename__ = "calendar_reminders"

    id = Column(Integer, primary_key=True, autoincrement=True)
    telegram_user_id = Column(BigInteger, index=True)
    event_id = Column(String, nullable=True)
    title = Column(String)
    event_time_str = Column(String, nullable=True)
    remind_at = Column(DateTime, index=True)
    is_important = Column(Boolean, default=False)
    is_sent = Column(Boolean, default=False, index=True)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)

class VideoAdmin(Base):
    __tablename__ = "video_admins"

    id = Column(Integer, primary_key=True, autoincrement=True)
    username = Column(String, unique=True, index=True, nullable=True)  # в нижнем регистре без @
    telegram_user_id = Column(BigInteger, unique=True, index=True, nullable=True)
    added_at = Column(DateTime, default=datetime.datetime.utcnow)

class VideoGenerationLog(Base):
    __tablename__ = "video_generation_logs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    telegram_user_id = Column(BigInteger, index=True)
    username = Column(String, index=True, nullable=True)
    prompt = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.datetime.utcnow, index=True)
