from datetime import datetime, timezone
from time import perf_counter

from fastapi import APIRouter
from sqlalchemy import text

from ..config import settings
from ..db import SessionLocal
from ..schemas import Health, ReadyOut, HealthCheckItem
from ..services.llm_client import load_ai_config

router = APIRouter(prefix="/api", tags=["health"])


@router.get("/health", response_model=Health)
def health():
    return Health(status="ok", chatlog_http_base=settings.CHATLOG_HTTP_BASE, chatlog_dir=settings.CHATLOG_DIR)


@router.get("/ready", response_model=ReadyOut)
def ready():
    checks: list[HealthCheckItem] = []

    # 1) DB readiness
    t0 = perf_counter()
    try:
        db = SessionLocal()
        try:
            db.execute(text("SELECT 1"))
        finally:
            db.close()
        checks.append(
            HealthCheckItem(
                name="database",
                status="ok",
                latency_ms=int((perf_counter() - t0) * 1000),
            )
        )
    except Exception as e:
        checks.append(
            HealthCheckItem(
                name="database",
                status="fail",
                error_code="DB-UNAVAILABLE-001",
                message=str(e),
                latency_ms=int((perf_counter() - t0) * 1000),
            )
        )

    # 2) AI router readiness
    t1 = perf_counter()
    try:
        conf = load_ai_config()
        router = conf.get("model_router") if isinstance(conf.get("model_router"), dict) else {}
        if not router or not bool(router.get("enabled", True)):
            raise RuntimeError("model_router disabled")
        lanes = ("main_channels", "mid_channels", "tool_channels")
        enabled_count = 0
        for lane in lanes:
            channels = router.get(lane) if isinstance(router.get(lane), list) else []
            enabled_count += sum(1 for c in channels if isinstance(c, dict) and bool(c.get("enabled", True)))
        if enabled_count <= 0:
            raise RuntimeError("no enabled route channels")
        checks.append(
            HealthCheckItem(
                name="model_router",
                status="ok",
                message=f"enabled_channels={enabled_count}",
                latency_ms=int((perf_counter() - t1) * 1000),
            )
        )
    except Exception as e:
        checks.append(
            HealthCheckItem(
                name="model_router",
                status="fail",
                error_code="RTR-STATE-001",
                message=str(e),
                latency_ms=int((perf_counter() - t1) * 1000),
            )
        )

    # 3) config readiness
    t2 = perf_counter()
    try:
        if not settings.CHATLOG_HTTP_BASE:
            raise RuntimeError("CHATLOG_HTTP_BASE empty")
        checks.append(
            HealthCheckItem(
                name="config",
                status="ok",
                latency_ms=int((perf_counter() - t2) * 1000),
            )
        )
    except Exception as e:
        checks.append(
            HealthCheckItem(
                name="config",
                status="fail",
                error_code="CFG-STATE-002",
                message=str(e),
                latency_ms=int((perf_counter() - t2) * 1000),
            )
        )

    failed = [c for c in checks if c.status != "ok"]
    error_code = failed[0].error_code if failed else None
    return ReadyOut(
        status="ok" if not failed else "degraded",
        healthy=not failed,
        error_code=error_code,
        checks=checks,
        timestamp=datetime.now(timezone.utc).isoformat(),
    )
