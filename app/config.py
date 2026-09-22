"""Konfiguration der Bridge.

Alles Geheime kommt aus `.env` (chmod 600, nie im Repo): das Token, mit dem sich die App
ausweist, und optional die Telegram-Chat-ID. Fehlt die ID, holt [hermes.resolve_chat_id]
sie zur Laufzeit von Hermes — dann steht sie nirgends in einer Datei.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

# Pfade des Hermes-Agenten auf diesem Rechner.
HERMES_HOME = Path("/home/chris/.hermes")
HERMES_AGENT = HERMES_HOME / "hermes-agent"
HERMES_PYTHON = HERMES_AGENT / "venv" / "bin" / "python"
HERMES_CLI = Path("/home/chris/.local/bin/hermes")

#: Wie lange eine `request_id` als "schon bearbeitet" gilt. Deckt den App-Retry bei
#: wackeligem Netz ab, ohne den Speicher unbegrenzt wachsen zu lassen.
IDEMPOTENCY_TTL_S = 3600

#: Grosszuegig, aber vorhanden — ein durchgedrehtes Widget soll Hermes nicht fluten.
RATE_LIMIT_PER_MIN = 30

#: Hoechstlaenge eines Transkripts. Ein Diktat ist kein Buch; alles darueber ist ein Fehler.
MAX_TRANSCRIPT_CHARS = 20_000

#: Zeitlimit fuer die kurzen Hermes-Aufrufe (Chat-Id ermitteln, Nachricht zustellen).
HERMES_TIMEOUT_S = 30

#: Zeitlimit fuer den Agentenlauf selbst. Deutlich groesser: ein Auftrag darf recherchieren,
#: rechnen und Werkzeuge benutzen. Gemessen liegt ein einfacher Auftrag bei rund 11 s.
AGENT_TIMEOUT_S = 600


@dataclass(frozen=True)
class Settings:
    token: str
    chat_id: str

    @property
    def configured(self) -> bool:
        return bool(self.token)


def load_env(path: Path | None = None) -> None:
    """Liest `.env` in die Umgebung — schlicht, ohne Fremdbibliothek.

    Bereits gesetzte Variablen gewinnen, damit ein Test oder die systemd-Unit
    ueberschreiben kann.
    """
    env = path or BASE_DIR / ".env"
    if not env.is_file():
        return
    for line in env.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip())


def settings() -> Settings:
    return Settings(
        token=os.environ.get("BRIDGE_TOKEN", "").strip(),
        chat_id=os.environ.get("HERMES_CHAT_ID", "").strip(),
    )
