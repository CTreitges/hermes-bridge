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

from fastapi import BackgroundTasks, Depends, FastAPI, Header, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from . import config, hermes
from .idempotency import IdempotencyStore, RateLimiter

# Ohne eigene Konfiguration verschluckt uvicorn alles, was dieser Logger sagt: der
# Wurzel-Logger hat dort keinen Handler. Unter systemd landet stdout im Journal —
# genau dort will man beim Fehlersuchen nachsehen. (Genau das hat einmal eine halbe
# Stunde gekostet: der Hintergrundlauf lief, war aber unsichtbar.)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
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
def task(
    auftrag: Task,
    hintergrund: BackgroundTasks,
    _: Annotated[None, Depends(pruefe_token)] = None,
) -> JSONResponse:
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

    text = auftrag.transcript.strip()
    try:
        chat_id = hermes.resolve_chat_id(config.settings().chat_id)
        # Schritt 1 noch hier: scheitert die Zustellung, soll die App es erfahren und
        # erneut versuchen koennen.
        hermes.dokument_uebergeben(
            transcript=text,
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

    # Schritt 2 im Hintergrund: der Agent darf Minuten brauchen, das Telefon soll nicht
    # so lange auf die Bestaetigung warten. Scheitert er, erfaehrt es der Auftraggeber
    # im Chat (hermes.beantworten), nicht die App — sie ist da laengst fertig.
    hintergrund.add_task(_bearbeiten, auftrag.request_id, text, chat_id)

    log.info("Auftrag %s angenommen, Agent laeuft", auftrag.request_id)
    return JSONResponse(
        status_code=202,
        content={"status": "accepted", "request_id": auftrag.request_id},
    )


def _bearbeiten(request_id: str, transcript: str, chat_id: str) -> None:
    """Der Agentenlauf, nach der HTTP-Antwort."""
    try:
        hermes.beantworten(transcript, chat_id)
        log.info("Auftrag %s beantwortet", request_id)
    except hermes.HermesError:
        # beantworten() hat den Auftraggeber schon im Chat informiert und geloggt.
        # Die Id bleibt belegt: ein Wiederholungsversuch der App wuerde denselben
        # Auftrag ein zweites Mal durch den Agenten schicken.
        pass
