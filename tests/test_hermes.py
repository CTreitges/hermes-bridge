"""Die Uebergabe an Hermes. `subprocess.run` ist durchgehend ersetzt — im Test wird NIE
ein echtes Kommando ausgefuehrt, weder `hermes send` noch ein Cron-Job."""

import ast
import json
import subprocess
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

def test_auftrag_wird_als_einmaljob_angelegt(lauf):
    assert hermes.auftrag_anlegen("Bestell Kaffee", "4242") == "job-7"
    assert len(lauf.kommandos) == 1
    # Der Interpreter des Agenten, nicht der eigene.
    assert str(config.HERMES_PYTHON) in lauf.kommandos[0][0]

    args = json.loads(lauf.eingaben[0])
    assert args["schedule"] == "1m"
    assert args["repeat"] == 1, "Ein Sprachauftrag ist einmalig"
    assert args["deliver"] == "telegram:4242"
    assert args["origin"] == {"platform": "telegram", "chat_id": "4242"}
    assert args["attach_to_session"] is True, "sonst erinnert sich Hermes im Chat nicht daran"


def test_der_prompt_bekommt_seinen_rahmen(lauf):
    hermes.auftrag_anlegen("Bestell Kaffee", "4242")
    prompt = json.loads(lauf.eingaben[0])["prompt"]
    assert "Bestell Kaffee" in prompt
    assert prompt != "Bestell Kaffee", "der rohe Text darf nicht unveraendert als Prompt rausgehen"
    assert "nachfragen statt raten" in prompt


def test_transkript_geht_ueber_stdin_nicht_ueber_die_kommandozeile(lauf):
    boesartig = "Test\"; rm -rf /; echo \""
    hermes.auftrag_anlegen(boesartig, "4242")
    assert boesartig not in " ".join(lauf.kommandos[0])
    assert boesartig in json.loads(lauf.eingaben[0])["prompt"]


def test_fehlgeschlagener_auftrag_wirft(monkeypatch):
    monkeypatch.setattr(subprocess, "run", FalscherLauf(returncode=2, stderr="jobs.json gesperrt"))
    with pytest.raises(hermes.HermesError, match="nicht angelegt"):
        hermes.auftrag_anlegen("x", "4242")


# --- Beide Schritte zusammen ---------------------------------------------

def test_uebergabe_stellt_erst_zu_und_legt_dann_an(lauf):
    hermes.uebergeben("Mach das Licht an", "2026-09-21T20:00:00", 3000, "4242")
    assert len(lauf.kommandos) == 2
    assert "send" in lauf.kommandos[0]
    assert str(config.HERMES_PYTHON) in lauf.kommandos[1][0]


def test_ohne_zustellung_kein_auftrag(monkeypatch):
    """Ein Auftrag ohne sichtbares Transkript waere schlechter nachvollziehbar als keiner."""
    f = FalscherLauf(returncode=1, stderr="Telegram weg")
    monkeypatch.setattr(subprocess, "run", f)
    with pytest.raises(hermes.HermesError):
        hermes.uebergeben("Mach das Licht an", "2026-09-21T20:00:00", 3000, "4242")
    assert len(f.kommandos) == 1, "nach dem Fehlschlag darf kein Job mehr angelegt werden"


def test_die_datei_bleibt_nicht_liegen(lauf, monkeypatch, tmp_path):
    gemerkt = {}

    original = hermes.dokument_schreiben

    def merken(transcript, recorded_at, duration_ms, ordner=None):
        pfad = original(transcript, recorded_at, duration_ms, tmp_path)
        gemerkt["pfad"] = pfad
        return pfad

    monkeypatch.setattr(hermes, "dokument_schreiben", merken)
    hermes.uebergeben("Text", "2026-09-21T20:00:00", 1000, "4242")
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
    for name in ("hermes.py", "hermes_job.py", "main.py", "config.py"):
        texte += _ausfuehrbare_texte(ordner / name)
    zusammen = " ".join(texte)
    for verboten in ("systemctl", "gateway run", "cron tick", "config.yaml"):
        assert verboten not in zusammen, f"verbotener Zugriff im Code: {verboten}"


def test_rahmen_verbietet_schweigen():
    """Der Cron-Rahmen von Hermes sagt dem Agenten, er solle bei "nichts Neues" mit
    [SILENT] antworten — eine Konvention fuer Ueberwachungsjobs. Ein Sprachauftrag ist
    das Gegenteil: jemand wartet auf eine Antwort. Ohne diesen Satz lief der Auftrag
    durch, und der Scheduler protokollierte "agent returned [SILENT] — skipping
    delivery"; beim Nutzer kam nichts an.
    """
    text = hermes.PROMPT_RAHMEN.format(transcript="Wie spaet ist es?")
    assert "[SILENT]" in text and "verboten" in text
    assert "IMMER" in text


def test_rahmen_nennt_keinen_namen():
    """Der Dienst soll fuer jeden brauchbar sein — kein fest verdrahteter Vorname."""
    text = hermes.PROMPT_RAHMEN.format(transcript="x")
    assert "Christof" not in text


def test_rahmen_traegt_den_auftrag():
    text = hermes.PROMPT_RAHMEN.format(transcript="Kauf Milch")
    assert text.rstrip().endswith("Kauf Milch")
    assert "Erkennungsfehler" in text

