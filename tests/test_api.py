"""Die HTTP-Schicht. Hermes wird durchgehend ersetzt — im Test wird NIE echt zugestellt."""

import pytest
from fastapi.testclient import TestClient

from app import hermes, main

TOKEN = "test-token-geheim"
KOPF = {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture
def aufrufe(monkeypatch):
    """Zaehlt die Uebergaben an Hermes, statt sie auszufuehren."""
    protokoll = []

    def falsche_uebergabe(transcript, recorded_at, duration_ms, chat_id):
        protokoll.append({"transcript": transcript, "chat_id": chat_id, "duration_ms": duration_ms})
        return "job-1"

    monkeypatch.setattr(hermes, "uebergeben", falsche_uebergabe)
    monkeypatch.setattr(hermes, "resolve_chat_id", lambda configured="": "4242")
    return protokoll


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
    assert daten["job_id"] == "job-1"
    assert len(aufrufe) == 1
    assert aufrufe[0]["transcript"] == "Erinnere mich an den Zahnarzt"
    assert aufrufe[0]["chat_id"] == "4242"


# --- Idempotenz -----------------------------------------------------------

def test_gleiche_request_id_fuehrt_nur_einmal_aus(client, aufrufe):
    erste = client.post("/v1/task", json=auftrag(request_id="gleich"), headers=KOPF)
    zweite = client.post("/v1/task", json=auftrag(request_id="gleich"), headers=KOPF)
    assert erste.status_code == 202
    assert zweite.status_code == 202
    assert zweite.json()["duplicate"] is True
    assert len(aufrufe) == 1, "Der Retry darf nichts erneut ausloesen"


def test_verschiedene_request_ids_laufen_beide(client, aufrufe):
    client.post("/v1/task", json=auftrag(request_id="a"), headers=KOPF)
    client.post("/v1/task", json=auftrag(request_id="b"), headers=KOPF)
    assert len(aufrufe) == 2


# --- Hermes faellt aus ----------------------------------------------------

def test_hermes_fehler_503_und_keine_halbe_zustellung(client, monkeypatch):
    def kaputt(**_):
        raise hermes.HermesError("Gateway antwortet nicht")

    monkeypatch.setattr(hermes, "resolve_chat_id", lambda configured="": "4242")
    monkeypatch.setattr(hermes, "uebergeben", kaputt)

    antwort = client.post("/v1/task", json=auftrag(request_id="kaputt"), headers=KOPF)
    assert antwort.status_code == 503
    assert "Gateway antwortet nicht" in antwort.json()["detail"]


def test_nach_einem_fehler_darf_die_app_es_erneut_versuchen(client, monkeypatch, aufrufe):
    def kaputt(**_):
        raise hermes.HermesError("kurz weg")

    monkeypatch.setattr(hermes, "uebergeben", kaputt)
    assert client.post("/v1/task", json=auftrag(request_id="retry"), headers=KOPF).status_code == 503

    # Jetzt geht es wieder — dieselbe Id muss durchkommen, sonst waere der Auftrag
    # eine Stunde lang gesperrt, obwohl nichts passiert ist.
    monkeypatch.setattr(hermes, "uebergeben", lambda **kw: aufrufe.append(kw) or "job-2")
    zweite = client.post("/v1/task", json=auftrag(request_id="retry"), headers=KOPF)
    assert zweite.status_code == 202
    assert len(aufrufe) == 1


# --- Rate-Limit und Health ------------------------------------------------

def test_rate_limit_greift(client, aufrufe):
    for i in range(30):
        assert client.post("/v1/task", json=auftrag(request_id=f"n{i}"), headers=KOPF).status_code == 202
    zuviel = client.post("/v1/task", json=auftrag(request_id="n30"), headers=KOPF)
    assert zuviel.status_code == 429
    assert len(aufrufe) == 30


def test_health_verraet_nichts_geheimes(client):
    antwort = client.get("/health")
    assert antwort.status_code == 200
    text = antwort.text
    assert antwort.json()["status"] == "ok"
    assert TOKEN not in text and "4242" not in text
