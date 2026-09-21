"""Uebergabe eines Sprachauftrags an den Hermes-Agenten.

Zwei Schritte, in dieser Reihenfolge:

1. Das Transkript als Markdown-Dokument in den Telegram-Chat legen — damit ist
   nachvollziehbar, was WhisperLoom verstanden hat, auch wenn der Auftrag daneben geht.
2. Einen Cron-Einmaljob anlegen. Der Ticker im laufenden Gateway fuehrt ihn binnen 60 s aus
   — mit vollem Werkzeugkasten inklusive MCP — und stellt die Antwort ueber Telegram zu.
   `attach_to_session` spiegelt sie in die Telegram-Sitzung, damit Hermes sich im Chat
   daran erinnert.

Verboten und hier bewusst nirgends aufgerufen: `hermes gateway run`, `cron tick`,
`systemctl restart hermes-gateway`, und jedes Schreiben an `~/.hermes/config.yaml`
(lebende Datei, nur ueber `hermes config set`).
"""

from __future__ import annotations

import json
import re
import subprocess
import tempfile
from datetime import datetime
from pathlib import Path

from . import config


class HermesError(RuntimeError):
    """Hermes war nicht erreichbar oder hat den Auftrag abgelehnt."""


#: Rahmen um das Transkript. An EINER Stelle, nicht im Code verstreut.
#:
#: Der rohe Text geht nicht unveraendert als Prompt raus: Hermes soll wissen, woher er
#: kommt, und dass Erkennungsfehler moeglich sind — sonst raet er bei einem verhoerten
#: Wort, statt nachzufragen.
PROMPT_RAHMEN = (
    "Sprachauftrag von Christof, per WhisperLoom transkribiert. "
    "Erkennungsfehler sind moeglich — im Zweifel nachfragen statt raten.\n\n"
    "Auftrag:\n{transcript}"
)

DOKUMENT_KOPF = "# Sprachauftrag {stamp}\n\nAufgenommen: {recorded_at}\nDauer: {dauer}\n\n---\n\n{transcript}\n"

_CHAT_ID_MUSTER = re.compile(r"\[(\d+)\]")


def _run(cmd: list[str], *, stdin: str | None = None) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            cmd,
            input=stdin,
            capture_output=True,
            text=True,
            timeout=config.HERMES_TIMEOUT_S,
            check=False,
        )
    except FileNotFoundError as e:  # Hermes nicht installiert
        raise HermesError(f"Hermes nicht gefunden: {e}") from e
    except subprocess.TimeoutExpired as e:
        raise HermesError("Hermes hat nicht rechtzeitig geantwortet") from e


def resolve_chat_id(configured: str = "") -> str:
    """Telegram-Chat-Id — aus der Konfiguration, sonst von Hermes selbst.

    Zur Laufzeit zu fragen ist der Vorzugsweg: dann steht die Id in keiner Datei.
    """
    if configured:
        return configured
    ergebnis = _run([str(config.HERMES_CLI), "send", "--list", "telegram"])
    if ergebnis.returncode != 0:
        raise HermesError(f"Chat-Id nicht ermittelbar (Exit {ergebnis.returncode})")
    treffer = _CHAT_ID_MUSTER.search(ergebnis.stdout)
    if not treffer:
        raise HermesError("Hermes meldet kein Telegram-Ziel")
    return treffer.group(1)


def dauer_text(duration_ms: int) -> str:
    sekunden = max(0, duration_ms) // 1000
    return f"{sekunden // 60}:{sekunden % 60:02d} min"


def dokument_schreiben(transcript: str, recorded_at: str, duration_ms: int, ordner: Path | None = None) -> Path:
    """Schreibt das Transkript als `.md` mit sprechendem Namen."""
    stamp = datetime.now().strftime("%Y-%m-%d-%H%M")
    ziel = Path(ordner or tempfile.gettempdir()) / f"sprachauftrag-{stamp}.md"
    ziel.write_text(
        DOKUMENT_KOPF.format(
            stamp=stamp,
            recorded_at=recorded_at,
            dauer=dauer_text(duration_ms),
            transcript=transcript,
        ),
        encoding="utf-8",
    )
    return ziel


def dokument_zustellen(pfad: Path, chat_id: str) -> None:
    """Legt die Datei als Dokument in den Chat.

    Ohne `[[as_document]]` kaeme sie als normale Nachricht an; ueber den Antwortpfad eines
    Webhooks funktioniert die Dateizustellung gar nicht — deshalb dieser Weg.
    """
    ergebnis = _run([
        str(config.HERMES_CLI), "send",
        "--to", f"telegram:{chat_id}",
        f"[[as_document]] MEDIA:{pfad}",
    ])
    if ergebnis.returncode != 0:
        raise HermesError(f"Dokument nicht zugestellt (Exit {ergebnis.returncode}): {ergebnis.stderr.strip()[:200]}")


def auftrag_anlegen(transcript: str, chat_id: str) -> str | None:
    """Legt den Einmaljob an und liefert dessen Id.

    Der Job laeuft ueber den Interpreter des Agenten — nur der kennt das `cron`-Modul.
    """
    args = {
        "_agent_path": str(config.HERMES_AGENT),
        "prompt": PROMPT_RAHMEN.format(transcript=transcript),
        "schedule": "1m",
        "repeat": 1,
        "name": "WhisperLoom-Sprachauftrag",
        "deliver": f"telegram:{chat_id}",
        "origin": {"platform": "telegram", "chat_id": chat_id},
        "attach_to_session": True,
    }
    ergebnis = _run(
        [str(config.HERMES_PYTHON), str(Path(__file__).with_name("hermes_job.py"))],
        stdin=json.dumps(args),
    )
    if ergebnis.returncode != 0:
        raise HermesError(f"Auftrag nicht angelegt (Exit {ergebnis.returncode}): {ergebnis.stderr.strip()[:200]}")
    try:
        return json.loads(ergebnis.stdout).get("id")
    except json.JSONDecodeError as e:
        raise HermesError("Hermes hat keine Job-Id geliefert") from e


def uebergeben(transcript: str, recorded_at: str, duration_ms: int, chat_id: str) -> str | None:
    """Beide Schritte. Scheitert der erste, wird der zweite nicht versucht — ein Auftrag
    ohne sichtbares Transkript waere schlechter nachvollziehbar als gar keiner."""
    pfad = dokument_schreiben(transcript, recorded_at, duration_ms)
    try:
        dokument_zustellen(pfad, chat_id)
        return auftrag_anlegen(transcript, chat_id)
    finally:
        pfad.unlink(missing_ok=True)
