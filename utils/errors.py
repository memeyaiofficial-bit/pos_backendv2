import logging
from fastapi import HTTPException

logger = logging.getLogger(__name__)

def safe_error(
    e: Exception,
    user_message: str,
    status_code: int = 400,
    *,
    log_level: str = "error",
) -> HTTPException:
    """
    Log the real exception internally and return a safe HTTPException for the client.
    Never leaks internal error details to the response body.
    """
    getattr(logger, log_level)("Internal error: %s", e, exc_info=True)
    return HTTPException(status_code=status_code, detail=user_message)