"""HTTP-Schnittstelle der Bridge.

Ein Endpunkt: die App schickt ein Transkript, der Dienst legt es als Dokument in den
Telegram-Chat und uebergibt den Auftrag an Hermes. Die Antwort kommt spaeter ueber
Telegram — die App wartet nicht darauf, sie bekommt nur die Annahme bestaetigt.

Gebunden wird ausschliesslich auf 127.0.0.1; nach aussen fuehrt Caddy mit TLS.
"""

from __future__ import annotations

import hmac
import logging
from typing import Annotated

from fastapi import Depends, FastAPI, Header, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from . import config, hermes
from .idempotency import IdempotencyStore, RateLimiter

log = logging.getLogger("hermes-bridge")

config.load_env()

app = FastAPI(title="WhisperLoom → Hermes Bridge", docs_url=None, redoc_url=None)

store = IdempotencyStore(config.IDEMPOTENCY_TTL_S)
limiter = RateLimiter(config.RATE_LIMIT_PER_MIN)


class Task(BaseModel):
    transcript: str = Field(max_length=config.MAX_TRANSCRIPT_CHARS)
    request_id: str = Field(min_length=1, max_length=200)
    recorded_at: str = Field(default="", max_length=64)
    duration_ms: int = Field(default=0, ge=0)
    source: str = Field(default="widget", max_length=32)


def fehler(status: int, meldung: str) -> JSONResponse:
    """Fehler als brauchbares JSON — die App zeigt diese Meldung an, und
    "Internal Server Error" hilft am Homescreen niemandem."""
    return JSONResponse(status_code=status, content={"status": "error", "detail": meldung})


def pruefe_token(authorization: Annotated[str, Header()] = "") -> None:
    """Bearer-Token in fester Zeit vergleichen."""
    erwartet = config.settings().token
    if not erwartet:
        raise PermissionError("Der Dienst hat kein Token konfiguriert")
    schema, _, mitgegeben = authorization.partition(" ")
    if schema.lower() != "bearer" or not hmac.compare_digest(mitgegeben.strip(), erwartet):
        raise PermissionError("Token fehlt oder stimmt nicht")


@app.exception_handler(PermissionError)
async def _auth_fehler(_: Request, exc: PermissionError) -> JSONResponse:
    return fehler(401, str(exc))


@app.get("/health")
def health() -> dict[str, object]:
    """Fuer den Health-Check nach dem Deploy. Verraet nichts Geheimes."""
    return {"status": "ok", "configured": config.settings().configured, "pending": len(store)}


@app.post("/v1/task", status_code=202)
def task(auftrag: Task, _: Annotated[None, Depends(pruefe_token)] = None) -> JSONResponse:
    if not auftrag.transcript.strip():
        return fehler(400, "Das Transkript ist leer")
    if not limiter.allow():
        return fehler(429, "Zu viele Auftraege — kurz warten")

    # Reservieren VOR der Bearbeitung: zwei gleichzeitige Retries derselben Id duerfen
    # nicht beide durchlaufen.
    if not store.claim(auftrag.request_id):
        log.info("Auftrag %s war schon da — nichts wiederholt", auftrag.request_id)
        return JSONResponse(
            status_code=202,
            content={"status": "accepted", "request_id": auftrag.request_id, "duplicate": True},
        )

    try:
        chat_id = hermes.resolve_chat_id(config.settings().chat_id)
        job_id = hermes.uebergeben(
            transcript=auftrag.transcript.strip(),
            recorded_at=auftrag.recorded_at,
            duration_ms=auftrag.duration_ms,
            chat_id=chat_id,
        )
    except hermes.HermesError as e:
        # Freigeben, damit die App es erneut versuchen kann — sonst waere der Auftrag
        # fuer eine Stunde gesperrt, obwohl nichts passiert ist.
        store.release(auftrag.request_id)
        log.warning("Auftrag %s nicht uebergeben: %s", auftrag.request_id, e)
        return fehler(503, f"Hermes nicht erreichbar: {e}")

    log.info("Auftrag %s uebergeben (Job %s)", auftrag.request_id, job_id)
    return JSONResponse(
        status_code=202,
        content={"status": "accepted", "request_id": auftrag.request_id, "job_id": job_id},
    )
