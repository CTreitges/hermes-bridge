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

import logging
import re
import subprocess
import tempfile
from datetime import datetime
from pathlib import Path

from . import config

log = logging.getLogger("hermes-bridge")


class HermesError(RuntimeError):
    """Hermes war nicht erreichbar oder hat den Auftrag abgelehnt."""


#: Rahmen um das Transkript. An EINER Stelle, nicht im Code verstreut.
#:
#: Der rohe Text geht nicht unveraendert als Prompt raus: Hermes soll wissen, woher er
#: kommt, und dass Erkennungsfehler moeglich sind — sonst raet er bei einem verhoerten
#: Wort, statt nachzufragen.
PROMPT_RAHMEN = (
    "Sprachauftrag, per WhisperLoom transkribiert. "
    "Erkennungsfehler sind moeglich — im Zweifel nachfragen statt raten.\n\n"
    "Deine Antwort geht unveraendert als Nachricht an den Auftraggeber, der gerade darauf "
    "wartet. Antworte deshalb IMMER mit einem Ergebnis — knapp, in ganzen Saetzen, ohne "
    "Ueberschriften. Auch wenn du nur nachfragen kannst, auch wenn etwas schiefging.\n\n"
    "Auftrag:\n{transcript}"
)

#: Was im Chat steht, wenn der Agent den Auftrag nicht bearbeiten konnte.
FEHLER_NACHRICHT = "Dein Sprachauftrag konnte nicht bearbeitet werden: {grund}"

DOKUMENT_KOPF = "# Sprachauftrag {stamp}\n\nAufgenommen: {recorded_at}\nDauer: {dauer}\n\n---\n\n{transcript}\n"

_CHAT_ID_MUSTER = re.compile(r"\[(\d+)\]")


def _run(cmd: list[str], *, stdin: str | None = None, timeout: int | None = None) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            cmd,
            input=stdin,
            capture_output=True,
            text=True,
            timeout=timeout or config.HERMES_TIMEOUT_S,
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


def agent_fragen(transcript: str) -> str:
    """Laesst den Agenten den Auftrag SOFORT bearbeiten und liefert seine Antwort.

    Frueher lief das ueber einen Cron-Einmaljob. Der brachte `attach_to_session` mit (die
    Antwort landete auch in der Chat-Sitzung), kostete aber 60 bis 120 s: Hermes' kleinste
    Zeiteinheit ist die Minute, und der Ticker laeuft im 60-s-Takt. Der Direktaufruf
    braucht gemessen rund 11 s.

    `-Q` laesst nur die Antwort auf stdout; die Sitzungs-Id geht nach stderr.
    Das Transkript steht als EIN argv-Element in der Liste — es wird keine Shell
    dazwischengeschaltet, also kann daraus kein Kommando werden.
    """
    ergebnis = _run(
        [str(config.HERMES_CLI), "chat", "-q", PROMPT_RAHMEN.format(transcript=transcript), "-Q"],
        timeout=config.AGENT_TIMEOUT_S,
    )
    if ergebnis.returncode != 0:
        raise HermesError(f"Agent abgebrochen (Exit {ergebnis.returncode}): {ergebnis.stderr.strip()[:200]}")
    antwort = ergebnis.stdout.strip()
    if not antwort:
        raise HermesError("Agent hat nichts geantwortet")
    return antwort


def nachricht_zustellen(text: str, chat_id: str) -> None:
    """Die Antwort als normale Nachricht in den Chat (kein Dokument)."""
    ergebnis = _run([str(config.HERMES_CLI), "send", "--to", f"telegram:{chat_id}", text])
    if ergebnis.returncode != 0:
        raise HermesError(f"Antwort nicht zugestellt (Exit {ergebnis.returncode}): {ergebnis.stderr.strip()[:200]}")


def dokument_uebergeben(transcript: str, recorded_at: str, duration_ms: int, chat_id: str) -> None:
    """Schritt 1: das Transkript als Dokument in den Chat.

    Passiert noch waehrend der HTTP-Anfrage — scheitert es, erfaehrt die App davon (503)
    und kann es erneut versuchen. Ein Auftrag ohne sichtbares Transkript waere schlechter
    nachvollziehbar als gar keiner.
    """
    pfad = dokument_schreiben(transcript, recorded_at, duration_ms)
    try:
        dokument_zustellen(pfad, chat_id)
    finally:
        pfad.unlink(missing_ok=True)


def beantworten(transcript: str, chat_id: str) -> str:
    """Schritt 2: Agent laufen lassen und die Antwort zustellen.

    Laeuft NACH der HTTP-Antwort im Hintergrund — der Agent darf Minuten brauchen, das
    Telefon soll nicht so lange auf ein 202 warten. Geht dabei etwas schief, erfaehrt es
    der Auftraggeber im Chat: er hat gesprochen und wartet, Schweigen waere die
    schlechteste Antwort.
    """
    try:
        antwort = agent_fragen(transcript)
    except HermesError as e:
        log.warning("Auftrag nicht bearbeitet: %s", e)
        try:
            nachricht_zustellen(FEHLER_NACHRICHT.format(grund=e), chat_id)
        except HermesError as zweiter:
            log.error("Auch die Fehlermeldung kam nicht durch: %s", zweiter)
        raise
    log.info("Agent fertig (%d Zeichen), stelle zu", len(antwort))
    nachricht_zustellen(antwort, chat_id)
    log.info("Antwort zugestellt an telegram:%s", chat_id)
    return antwort
