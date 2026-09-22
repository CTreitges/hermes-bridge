"""Die Uebergabe an Hermes. `subprocess.run` ist durchgehend ersetzt — im Test wird NIE
ein echtes Kommando ausgefuehrt, weder `hermes send` noch ein Cron-Job."""

import ast
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest

from app import config, hermes


class FalscherLauf:
    """Nimmt Kommandos entgegen, statt sie auszufuehren."""

    def __init__(self, stdout="", returncode=0, stderr=""):
        self.kommandos = []
        self.eingaben = []
        self.stdout = stdout
        self.returncode = returncode
        self.stderr = stderr

    def __call__(self, cmd, **kw):
        self.kommandos.append(cmd)
        self.eingaben.append(kw.get("input"))
        return subprocess.CompletedProcess(cmd, self.returncode, self.stdout, self.stderr)

    def mit(self, teil):
        """Alle Kommandos, die [teil] enthalten."""
        return [k for k in self.kommandos if any(teil in str(a) for a in k)]


@pytest.fixture
def lauf(monkeypatch):
    f = FalscherLauf(stdout=json.dumps({"id": "job-7"}))
    monkeypatch.setattr(subprocess, "run", f)
    return f


# --- Chat-Id --------------------------------------------------------------

def test_konfigurierte_chat_id_fragt_hermes_gar_nicht(lauf):
    assert hermes.resolve_chat_id("999") == "999"
    assert lauf.kommandos == []


def test_ohne_konfiguration_wird_hermes_gefragt(monkeypatch):
    f = FalscherLauf(stdout="telegram:\n  telegram:Christof Treitges  [123456789]\n")
    monkeypatch.setattr(subprocess, "run", f)
    assert hermes.resolve_chat_id("") == "123456789"
    assert f.mit("--list")


def test_ohne_telegram_ziel_ein_klarer_fehler(monkeypatch):
    monkeypatch.setattr(subprocess, "run", FalscherLauf(stdout="telegram:\n  (keine)\n"))
    with pytest.raises(hermes.HermesError, match="kein Telegram-Ziel"):
        hermes.resolve_chat_id("")


# --- Dokument -------------------------------------------------------------

def test_dokument_traegt_kopf_dauer_und_text(tmp_path):
    pfad = hermes.dokument_schreiben("Ruf den Klempner an", "2026-09-21T20:15:00", 65_000, tmp_path)
    text = pfad.read_text(encoding="utf-8")
    assert pfad.name.startswith("sprachauftrag-") and pfad.suffix == ".md"
    assert text.startswith("# Sprachauftrag ")
    assert "1:05 min" in text
    assert "Ruf den Klempner an" in text


def test_zustellung_nutzt_as_document(lauf, tmp_path):
    pfad = tmp_path / "x.md"
    pfad.write_text("hallo", encoding="utf-8")
    hermes.dokument_zustellen(pfad, "4242")
    gesendet = lauf.mit("send")
    assert len(gesendet) == 1
    argumente = " ".join(gesendet[0])
    # Ohne [[as_document]] kaeme es als normale Nachricht an.
    assert "[[as_document]]" in argumente
    assert f"MEDIA:{pfad}" in argumente
    assert "telegram:4242" in argumente


def test_fehlgeschlagene_zustellung_wirft(monkeypatch, tmp_path):
    monkeypatch.setattr(subprocess, "run", FalscherLauf(returncode=1, stderr="kein Bot-Token"))
    pfad = tmp_path / "x.md"
    pfad.write_text("hallo", encoding="utf-8")
    with pytest.raises(hermes.HermesError, match="nicht zugestellt"):
        hermes.dokument_zustellen(pfad, "4242")


# --- Auftrag --------------------------------------------------------------

def test_der_agent_wird_direkt_gefragt(lauf):
    assert hermes.agent_fragen("Bestell Kaffee") == '{"id": "job-7"}'
    assert len(lauf.kommandos) == 1
    kommando = lauf.kommandos[0]
    assert "chat" in kommando and "-q" in kommando
    # -Q: nur die Antwort auf stdout, die Sitzungs-Id geht nach stderr.
    assert "-Q" in kommando


def test_der_prompt_bekommt_seinen_rahmen(lauf):
    hermes.agent_fragen("Bestell Kaffee")
    prompt = lauf.kommandos[0][lauf.kommandos[0].index("-q") + 1]
    assert "Bestell Kaffee" in prompt
    assert prompt != "Bestell Kaffee", "der rohe Text darf nicht unveraendert als Prompt rausgehen"
    assert "nachfragen statt raten" in prompt
    assert "IMMER" in prompt, "sonst schweigt der Agent, wenn er nichts Neues zu melden glaubt"


def test_das_transkript_ist_ein_einziges_argument(lauf):
    """Keine Shell dazwischen: das Transkript kann kein Kommando werden."""
    boesartig = "Test\"; rm -rf /; echo \""
    hermes.agent_fragen(boesartig)
    kommando = lauf.kommandos[0]
    treffer = [a for a in kommando if boesartig in a]
    assert len(treffer) == 1, "das Transkript muss genau EIN argv-Element sein"


def test_leere_antwort_ist_ein_fehler(monkeypatch):
    """Sonst kaeme beim Auftraggeber eine leere Nachricht an."""
    monkeypatch.setattr(subprocess, "run", FalscherLauf(stdout="   \n"))
    with pytest.raises(hermes.HermesError, match="nichts geantwortet"):
        hermes.agent_fragen("x")


def test_abgebrochener_agent_wirft(monkeypatch):
    monkeypatch.setattr(subprocess, "run", FalscherLauf(returncode=1, stderr="kein Modell erreichbar"))
    with pytest.raises(hermes.HermesError, match="abgebrochen"):
        hermes.agent_fragen("x")


def test_der_agent_bekommt_mehr_zeit_als_die_kurzen_aufrufe(monkeypatch):
    """Ein Auftrag darf recherchieren; 30 s waeren zu knapp."""
    gesehen = {}

    def merken(cmd, **kw):
        gesehen["timeout"] = kw.get("timeout")
        return subprocess.CompletedProcess(cmd, 0, "Antwort", "")

    monkeypatch.setattr(subprocess, "run", merken)
    hermes.agent_fragen("x")
    assert gesehen["timeout"] == config.AGENT_TIMEOUT_S
    assert config.AGENT_TIMEOUT_S > config.HERMES_TIMEOUT_S


def test_die_antwort_geht_als_nachricht_nicht_als_dokument(lauf):
    hermes.nachricht_zustellen("Der Kaffee ist bestellt.", "4242")
    argumente = " ".join(lauf.kommandos[0])
    assert "telegram:4242" in argumente
    assert "[[as_document]]" not in argumente, "die Antwort ist eine Nachricht, kein Dokument"
    assert "Der Kaffee ist bestellt." in argumente


# --- Beide Schritte zusammen ---------------------------------------------

def test_dokument_geht_vor_der_antwort_raus(lauf):
    hermes.dokument_uebergeben("Mach das Licht an", "2026-09-21T20:00:00", 3000, "4242")
    assert len(lauf.kommandos) == 1
    assert "[[as_document]]" in " ".join(lauf.kommandos[0])


def test_beantworten_fragt_und_stellt_zu(lauf):
    assert hermes.beantworten("Mach das Licht an", "4242") == '{"id": "job-7"}'
    assert len(lauf.kommandos) == 2
    assert "chat" in lauf.kommandos[0]
    assert "send" in lauf.kommandos[1]


def test_scheitert_der_agent_erfaehrt_es_der_auftraggeber(monkeypatch):
    """Er hat gesprochen und wartet — Schweigen waere die schlechteste Antwort."""
    aufrufe = []

    def wechselhaft(cmd, **kw):
        aufrufe.append(cmd)
        if "chat" in cmd:
            return subprocess.CompletedProcess(cmd, 1, "", "kein Modell erreichbar")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(subprocess, "run", wechselhaft)
    with pytest.raises(hermes.HermesError):
        hermes.beantworten("Mach das Licht an", "4242")

    assert len(aufrufe) == 2, "nach dem Fehlschlag muss eine Meldung rausgehen"
    meldung = " ".join(aufrufe[1])
    assert "telegram:4242" in meldung
    assert "konnte nicht bearbeitet werden" in meldung


def test_die_datei_bleibt_nicht_liegen(lauf, monkeypatch, tmp_path):
    gemerkt = {}
    original = hermes.dokument_schreiben

    def merken(transcript, recorded_at, duration_ms, ordner=None):
        pfad = original(transcript, recorded_at, duration_ms, tmp_path)
        gemerkt["pfad"] = pfad
        return pfad

    monkeypatch.setattr(hermes, "dokument_schreiben", merken)
    hermes.dokument_uebergeben("Text", "2026-09-21T20:00:00", 1000, "4242")
    assert not gemerkt["pfad"].exists(), "das Transkript darf nicht im /tmp liegen bleiben"


# --- Verbote --------------------------------------------------------------

def _ausfuehrbare_texte(pfad: Path) -> list[str]:
    """Alle String-Literale einer Datei OHNE Docstrings.

    Ein naiver Textvergleich schlaegt hier fehl: die Verbote stehen als Merksatz in den
    Docstrings, also faende er sich selbst.
    """
    baum = ast.parse(pfad.read_text(encoding="utf-8"))
    docs = set()
    for knoten in ast.walk(baum):
        if isinstance(knoten, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            d = ast.get_docstring(knoten, clean=False)
            if d:
                docs.add(d)
    return [
        k.value for k in ast.walk(baum)
        if isinstance(k, ast.Constant) and isinstance(k.value, str) and k.value not in docs
    ]


def test_die_bridge_fasst_das_gateway_nie_an():
    """Kein Neustart, kein `cron tick`, kein Editor auf config.yaml (Auftrag §A3)."""
    ordner = Path(hermes.__file__).parent
    texte = []
    for name in ("hermes.py", "main.py", "config.py"):
        texte += _ausfuehrbare_texte(ordner / name)
    zusammen = " ".join(texte)
    for verboten in ("systemctl", "gateway run", "cron tick", "config.yaml"):
        assert verboten not in zusammen, f"verbotener Zugriff im Code: {verboten}"


def test_rahmen_nennt_keinen_namen():
    """Der Dienst soll fuer jeden brauchbar sein — kein fest verdrahteter Vorname."""
    text = hermes.PROMPT_RAHMEN.format(transcript="x")
    assert "Christof" not in text


def test_rahmen_traegt_den_auftrag():
    text = hermes.PROMPT_RAHMEN.format(transcript="Kauf Milch")
    assert text.rstrip().endswith("Kauf Milch")
    assert "Erkennungsfehler" in text


