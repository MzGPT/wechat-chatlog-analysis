from fastapi import APIRouter
from ..config import settings
from ..schemas import Health

router = APIRouter(prefix="/api", tags=["health"])


@router.get("/health", response_model=Health)
def health():
    return Health(status="ok", chatlog_http_base=settings.CHATLOG_HTTP_BASE, chatlog_dir=settings.CHATLOG_DIR)

