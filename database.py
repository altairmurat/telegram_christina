from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, declarative_base
from env import DATABASE_URL

db_url = (DATABASE_URL or "").strip()
if db_url.startswith("postgres://"):
    db_url = db_url.replace("postgres://", "postgresql://", 1)

connect_args = {}
if ("postgresql" in db_url or "postgres" in db_url) and "sslmode" not in db_url.lower():
    connect_args = {"sslmode": "require"}
elif "sqlite" in db_url:
    connect_args = {"check_same_thread": False}

engine = create_engine(
    db_url or "sqlite:///./test.db",
    pool_pre_ping=True,
    connect_args=connect_args
)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine, expire_on_commit=False)
Base = declarative_base()