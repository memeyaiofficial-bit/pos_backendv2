"""

Sets up the SQLAlchemy async-compatible engine and session factory for SQLite.

DESIGN DECISIONS:
  • SQLite with check_same_thread=False  → FastAPI runs handlers in a thread pool;
    the flag is safe because SQLAlchemy manages its own connection-per-session.
  • connect_args["timeout"] = 30          → Prevents indefinite lock waits; raises
    OperationalError instead of hanging.
  • echo=DEBUG                            → SQL logging only in debug mode to avoid
    leaking sensitive data in production logs.

RISKS MITIGATED:
  • SQLite WAL mode enabled via event listener → dramatically reduces "database is
    locked" errors under concurrent reads + writes.
  • Foreign key enforcement ON            → SQLite disables FK checks by default;
    this re-enables them per connection.
"""

import logging
from pathlib import Path

from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import DeclarativeBase, sessionmaker, Session
from typing import Generator

from config import get_settings

settings = get_settings()


# ── Engine ────────────────────────────────────────────────────────────────────
# connect_args with check_same_thread is SQLite-only.
# For PostgreSQL (Render) we pass no connect_args.
logger = logging.getLogger(__name__)

connect_args = {}
database_url = settings.DATABASE_URL
if database_url.startswith("sqlite"):
    connect_args = {"check_same_thread": False, "timeout": 30}
    # Resolve relative SQLite paths against the repository folder, not cwd.
    if database_url.startswith("sqlite:///"):
        relative_path = database_url[len("sqlite:///"):]
        if relative_path and not Path(relative_path).is_absolute():
            root_dir = Path(__file__).resolve().parent
            absolute_path = root_dir / relative_path
            database_url = f"sqlite:///{absolute_path.as_posix()}"
            logger.info("Resolved SQLite database path to %s", absolute_path)

engine = create_engine(
    database_url,
    connect_args=connect_args,
    echo=settings.DEBUG,
    pool_pre_ping=True,
    pool_size=10,
    max_overflow=20,
    pool_timeout=30,
)
@event.listens_for(engine, "connect")
def _configure_sqlite(dbapi_conn, _connection_record):
    """
    Called once per new raw DBAPI connection.
    PRAGMA commands are SQLite-only — skip entirely for PostgreSQL.
    """
    if settings.DATABASE_URL.startswith("sqlite"):
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA journal_mode=WAL;")
        cursor.execute("PRAGMA foreign_keys=ON;")
        cursor.execute("PRAGMA synchronous=NORMAL;")
        cursor.close()

# ── Session factory ───────────────────────────────────────────────────────────

SessionLocal = sessionmaker(
    bind=engine,
    autocommit=False,   # Explicit transaction management
    autoflush=False,    # Flush only when we call .commit() or explicitly .flush()
    expire_on_commit=False,  # Keep objects usable after commit (important for FastAPI responses)
)


# ── Base declarative class ────────────────────────────────────────────────────

class Base(DeclarativeBase):
    """All ORM models inherit from this class."""
    pass


# ── Dependency ────────────────────────────────────────────────────────────────

def get_db() -> Generator[Session, None, None]:
    """
    FastAPI dependency that yields a database session and guarantees cleanup.

    Usage in a router:
        db: Session = Depends(get_db)

    The try/finally ensures the session is always closed even if an exception
    is raised mid-request, preventing connection leaks.
    """
    db = SessionLocal()
    try:
        yield db
    except Exception:
        db.rollback()  # Roll back any uncommitted transaction on error
        raise  # Re-raise so FastAPI can handle it normally
    finally:
        db.close()  # Always close to return connection to pool