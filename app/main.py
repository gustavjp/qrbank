from __future__ import annotations

from fastapi import FastAPI, HTTPException, status

from app.database import database_is_ready
from app.routes.actions import router as action_router
from app.routes.auth import router as auth_router
from app.routes.behavior import router as behavior_router
from app.routes.classrooms import router as classroom_router
from app.routes.students import router as student_router
from app.routes.scanner import router as scanner_router
from app.routes.transactions import router as transaction_router


app = FastAPI(
    title="QRBank",
    version="0.1.0",
    docs_url=None,
    redoc_url=None,
)

app.include_router(action_router)
app.include_router(auth_router)
app.include_router(behavior_router)
app.include_router(classroom_router)
app.include_router(student_router)
app.include_router(scanner_router)
app.include_router(transaction_router)


@app.get("/health", tags=["system"])
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/ready", tags=["system"])
def ready() -> dict[str, str]:
    if not database_is_ready():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="database unavailable",
        )
    return {"status": "ready"}
