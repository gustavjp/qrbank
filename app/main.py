from __future__ import annotations

from fastapi import FastAPI, HTTPException, status

from app.database import database_is_ready


app = FastAPI(
    title="QRBank",
    version="0.1.0",
    docs_url=None,
    redoc_url=None,
)


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
