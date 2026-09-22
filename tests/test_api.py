"""Die HTTP-Schicht. Hermes wird durchgehend ersetzt — im Test wird NIE echt zugestellt."""

import pytest
from fastapi.testclient import TestClient

from app import hermes, main

TOKEN = "test-token-geheim"
KOPF = {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture
def aufrufe(monkeypatch):
    """Zaehlt Dokument-Zustellung und Agentenlauf, statt sie auszufuehren.

    `TestClient` fuehrt Hintergrund-Aufgaben nach der Antwort aus — der Agentenlauf
    landet also mit im Protokoll, obwohl er in der echten Anwendung erst nach dem 202
    laeuft.
    """
    protokoll = []

    def falsches_dokument(transcript, recorded_at, duration_ms, chat_id):
        protokoll.append({"schritt": "dokument", "transcript": transcript, "chat_id": chat_id, "duration_ms": duration_ms})

    def falsche_antwort(transcript, chat_id):
        protokoll.append({"schritt": "agent", "transcript": transcript, "chat_id": chat_id})
        return "erledigt"

    monkeypatch.setattr(hermes, "dokument_uebergeben", falsches_dokument)
    monkeypatch.setattr(hermes, "beantworten", falsche_antwort)
    monkeypatch.setattr(hermes, "resolve_chat_id", lambda configured="": "4242")
    return protokoll


def dokumente(protokoll):
    return [e for e in protokoll if e["schritt"] == "dokument"]


def agentenlaeufe(protokoll):
    return [e for e in protokoll if e["schritt"] == "agent"]


@pytest.fixture
def client():
    main.store._seen.clear()
    main.limiter._hits.clear()
    return TestClient(main.app)


def auftrag(request_id="r1", transcript="Erinnere mich an den Zahnarzt"):
    return {
        "transcript": transcript,
        "request_id": request_id,
        "recorded_at": "2026-09-21T20:15:00+02:00",
        "duration_ms": 4200,
        "source": "widget",
    }


# --- Zugang ---------------------------------------------------------------

def test_ohne_token_401(client, aufrufe):
    antwort = client.post("/v1/task", json=auftrag())
    assert antwort.status_code == 401
    assert antwort.json()["detail"]
    assert aufrufe == []


def test_falsches_token_401(client, aufrufe):
    antwort = client.post("/v1/task", json=auftrag(), headers={"Authorization": "Bearer falsch"})
    assert antwort.status_code == 401
    assert aufrufe == []


def test_token_ohne_bearer_schema_401(client, aufrufe):
    antwort = client.post("/v1/task", json=auftrag(), headers={"Authorization": TOKEN})
    assert antwort.status_code == 401
    assert aufrufe == []


# --- Eingabe --------------------------------------------------------------

def test_leeres_transkript_400(client, aufrufe):
    antwort = client.post("/v1/task", json=auftrag(transcript="   "), headers=KOPF)
    assert antwort.status_code == 400
    assert aufrufe == []


def test_zu_langes_transkript_422(client, aufrufe):
    antwort = client.post("/v1/task", json=auftrag(transcript="x" * 20_001), headers=KOPF)
    assert antwort.status_code == 422
    assert aufrufe == []


# --- Der gute Fall --------------------------------------------------------

def test_gueltiger_auftrag_202_und_genau_eine_uebergabe(client, aufrufe):
    antwort = client.post("/v1/task", json=auftrag(), headers=KOPF)
    assert antwort.status_code == 202
    daten = antwort.json()
    assert daten["status"] == "accepted"
    assert daten["request_id"] == "r1"
    assert len(dokumente(aufrufe)) == 1
    assert len(agentenlaeufe(aufrufe)) == 1
    assert dokumente(aufrufe)[0]["transcript"] == "Erinnere mich an den Zahnarzt"
    assert dokumente(aufrufe)[0]["chat_id"] == "4242"
    # Das Dokument geht VOR dem Agentenlauf raus.
    assert aufrufe[0]["schritt"] == "dokument"


# --- Idempotenz -----------------------------------------------------------

def test_gleiche_request_id_fuehrt_nur_einmal_aus(client, aufrufe):
    erste = client.post("/v1/task", json=auftrag(request_id="gleich"), headers=KOPF)
    zweite = client.post("/v1/task", json=auftrag(request_id="gleich"), headers=KOPF)
    assert erste.status_code == 202
    assert zweite.status_code == 202
    assert zweite.json()["duplicate"] is True
    assert len(dokumente(aufrufe)) == 1, "Der Retry darf nichts erneut ausloesen"
    assert len(agentenlaeufe(aufrufe)) == 1, "und schon gar nicht den Agenten zweimal"


def test_verschiedene_request_ids_laufen_beide(client, aufrufe):
    client.post("/v1/task", json=auftrag(request_id="a"), headers=KOPF)
    client.post("/v1/task", json=auftrag(request_id="b"), headers=KOPF)
    assert len(dokumente(aufrufe)) == 2
    assert len(agentenlaeufe(aufrufe)) == 2


# --- Hermes faellt aus ----------------------------------------------------

def test_hermes_fehler_503_und_keine_halbe_zustellung(client, monkeypatch):
    def kaputt(**_):
        raise hermes.HermesError("Gateway antwortet nicht")

    monkeypatch.setattr(hermes, "resolve_chat_id", lambda configured="": "4242")
    monkeypatch.setattr(hermes, "dokument_uebergeben", kaputt)

    antwort = client.post("/v1/task", json=auftrag(request_id="kaputt"), headers=KOPF)
    assert antwort.status_code == 503
    assert "Gateway antwortet nicht" in antwort.json()["detail"]


def test_nach_einem_fehler_darf_die_app_es_erneut_versuchen(client, monkeypatch, aufrufe):
    def kaputt(**_):
        raise hermes.HermesError("kurz weg")

    monkeypatch.setattr(hermes, "dokument_uebergeben", kaputt)
    assert client.post("/v1/task", json=auftrag(request_id="retry"), headers=KOPF).status_code == 503

    # Jetzt geht es wieder — dieselbe Id muss durchkommen, sonst waere der Auftrag
    # eine Stunde lang gesperrt, obwohl nichts passiert ist.
    monkeypatch.setattr(hermes, "dokument_uebergeben",
                        lambda **kw: aufrufe.append({"schritt": "dokument", **kw}))
    zweite = client.post("/v1/task", json=auftrag(request_id="retry"), headers=KOPF)
    assert zweite.status_code == 202
    assert len(dokumente(aufrufe)) == 1


# --- Rate-Limit und Health ------------------------------------------------

def test_rate_limit_greift(client, aufrufe):
    for i in range(30):
        assert client.post("/v1/task", json=auftrag(request_id=f"n{i}"), headers=KOPF).status_code == 202
    zuviel = client.post("/v1/task", json=auftrag(request_id="n30"), headers=KOPF)
    assert zuviel.status_code == 429
    assert len(dokumente(aufrufe)) == 30


def test_health_verraet_nichts_geheimes(client):
    antwort = client.get("/health")
    assert antwort.status_code == 200
    text = antwort.text
    assert antwort.json()["status"] == "ok"
    assert TOKEN not in text and "4242" not in text


def test_die_bridge_protokolliert_sichtbar(caplog, client, aufrufe):
    """Ohne eigene Konfiguration verschluckt uvicorn den Logger — beim Fehlersuchen
    sah man dann weder, dass ein Auftrag ankam, noch was der Hintergrundlauf tat."""
    with caplog.at_level("INFO", logger="hermes-bridge"):
        client.post("/v1/task", json=auftrag(request_id="sichtbar"), headers=KOPF)
    zeilen = [r.message for r in caplog.records if r.name == "hermes-bridge"]
    assert any("angenommen" in z for z in zeilen), zeilen
    assert any("beantwortet" in z for z in zeilen), zeilen

