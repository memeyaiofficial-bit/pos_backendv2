"""
main.py
────────
FastAPI application factory.

WHAT HAPPENS ON STARTUP:
  1. All SQLAlchemy models are created (CREATE TABLE IF NOT EXISTS).
  2. A default admin account is seeded if the users table is empty.
  3. An initial expiry alert scan runs.

RUN WITH:
    uvicorn main:app --reload --port 8000
"""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import JSONResponse
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException

from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded

from routers.payments import router as payments_router

from config import get_settings
from database import Base, engine, SessionLocal
from models.orm import User, UserRole
from routers.auth import router as auth_router
from routers.users import router as users_router
from routers.medicines import router as medicines_router
from routers.all_routers import (
    inventory_router, sales_router, customers_router,
    suppliers_router, po_router, alerts_router, reports_router,
)
from services.alert_service import scan_expiry_alerts
from utils.security import hash_password, require_admin

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
)
logger = logging.getLogger(__name__)
settings = get_settings()



# ── Startup / shutdown lifecycle ──────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Runs once at startup before accepting requests, and once at shutdown."""

    # 1. Create all tables
    logger.info("Creating database tables...")
    Base.metadata.create_all(bind=engine)
    logger.info("Database ready.")

    # 2. Seed first admin
    db = SessionLocal()
    try:
        admin_exists = db.query(User).filter(User.role == UserRole.ADMIN).first()
        if not admin_exists:
            logger.info("No admin found – creating default admin account.")
            admin = User(
                full_name=settings.FIRST_ADMIN_NAME,
                email=settings.FIRST_ADMIN_EMAIL.lower(),
                hashed_password=hash_password(
                    settings.FIRST_ADMIN_PASSWORD.get_secret_value()
                ),
                role=UserRole.ADMIN,
                is_active=True,
            )
            db.add(admin)
            db.commit()
            logger.info("Default admin created: %s", settings.FIRST_ADMIN_EMAIL)
            logger.warning(
                "IMPORTANT: Change the default admin password immediately after first login!"
            )

        # 3. Initial expiry scan
        scan_expiry_alerts(db)

    finally:
        db.close()

    logger.info("Flex Pharmacy POS API is ready.")
    yield
    logger.info("Shutting down.")

Limiterimiter = Limiter(key_func=get_remote_address)
# ── App factory ───────────────────────────────────────────────────────────────

app = FastAPI(
    title=settings.APP_NAME,
    version=settings.APP_VERSION,
    description=(
        "Backend API for Flex POS Pharmacy System. "
        "Medicine data seeded from WHO Essential Medicines List (24th edition, 2025) "
        "and enriched via OpenFDA."
    ),
    lifespan=lifespan,
    # Disable /docs and /redoc in production
    docs_url="/docs" if settings.DEBUG else None,
    redoc_url="/redoc" if settings.DEBUG else None,
    openapi_url="/openapi.json" if settings.DEBUG else None,
)

app.state.limiter = Limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

@app.exception_handler(StarletteHTTPException)
async def http_exception_handler(request, exc):
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "error": True,
            "status_code": exc.status_code,
            "message": exc.detail,
        }
    )
# ERROR handler response
@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request, exc):
    errors = []
    for error in exc.errors():
        field = " -> ".join(str(x) for x in error["loc"])
        errors.append({
            "field": field,
            "message": error["msg"],
        })
    return JSONResponse(
        status_code=422,
        content={
            "error": True,
            "status_code": 422,
            "message": "Validation failed. Check your request data.",
            "details": errors,
        }
    )

@app.exception_handler(Exception)
async def general_exception_handler(request, exc):
    logger.error("Unexpected error: %s", exc, exc_info=True)
    return JSONResponse(
        status_code=500,
        content={
            "error": True,
            "status_code": 500,
            "message": "An unexpected error occurred. Please try again or contact support.",
        }
    )
# Payment
app.include_router(payments_router)

# ── Security middleware ────────────────────────────────────────────────────────

# CORS – tighten allowed_origins for production
ALLOWED_ORIGINS = [
    "http://localhost:3000",           # Local dev frontend
    "http://localhost:5173",         # Vite dev server
    "http://localhost:58506",
     "http://localhost:63342",
    "https://pos.kaziflex.com",        # Production frontend
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS if not settings.DEBUG else ["*"],
    allow_credentials=True,            # Now safe because origins are explicit
    allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type"],
)
# Reject requests with unexpected Host headers (helps prevent host-header injection)
if not settings.DEBUG:
    app.add_middleware(
        TrustedHostMiddleware,
        allowed_hosts=["pos.kaziflex.com", "api.kaziflex.com", "localhost"],
    )


# ── Routers ───────────────────────────────────────────────────────────────────

app.include_router(auth_router)
app.include_router(users_router)
app.include_router(medicines_router)
app.include_router(inventory_router)
app.include_router(sales_router)
app.include_router(customers_router)
app.include_router(suppliers_router)
app.include_router(po_router)
app.include_router(alerts_router)
app.include_router(reports_router)


# ── Health check ─────────────────────────────────────────────────────────────

@app.get("/health", tags=["System"])
def health():
    """Simple liveness probe for load balancers / monitoring."""
    return {"status": "ok"}

# Aauthenticated version endpoint for internal tooling
@app.get("/health/detail", tags=["System"])
def health_detail(_: User = Depends(require_admin)):
    return {
        "status": "ok",
        "version": settings.APP_VERSION,
        "app": settings.APP_NAME,
        "debug": settings.DEBUG,
    }


# ── Dev entry point ───────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=8000,
        reload=True,          # Hot-reload on file save (dev only)
        reload_dirs=["."],
    )