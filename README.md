# hermes-bridge

Nimmt Sprachaufträge von WhisperLoom entgegen und übergibt sie an den Hermes-Agenten.

Telegram transkribiert Sprachnachrichten selbst, und zwar schlechter als WhisperLoom.
Deshalb spricht man ins WhisperLoom-Widget, und dieser Dienst legt das Transkript als
Dokument in den Telegram-Chat und gibt den Auftrag an Hermes weiter. Die Antwort kommt
über Telegram.

## Ablauf eines Auftrags

1. `POST /v1/task` mit Bearer-Token.
2. Das Transkript wird als `sprachauftrag-<datum>.md` geschrieben und per
   `hermes send --to telegram:<chat> "[[as_document]] MEDIA:<pfad>"` zugestellt.
   Ohne `[[as_document]]` käme es als normale Nachricht an.
3. Ein Cron-**Einmaljob** wird angelegt (`schedule="1m"`, `repeat=1`,
   `deliver="telegram:<chat>"`, `origin={platform, chat_id}`, `attach_to_session=True`).
   Der Ticker im laufenden Gateway führt ihn binnen 60 s aus — mit vollem Werkzeugkasten
   inklusive MCP — und spiegelt die Antwort in die Telegram-Sitzung, damit Hermes sich im
   Chat daran erinnert.
4. Antwort `202`. Auf Hermes wird nicht gewartet.

Bis zu 60 s Anlaufzeit sind bewusst in Kauf genommen: dafür bekommt der Auftrag den
vollen Agenten statt der vier Werkzeuge eines Webhooks.

## Schnittstelle

```
POST /v1/task
Authorization: Bearer <token>

{"transcript": "…", "request_id": "<uuid>", "recorded_at": "<ISO-8601>",
 "duration_ms": 12345, "source": "widget"}
```

| Code | Wann |
|---|---|
| 202 | angenommen (`duplicate: true`, wenn die `request_id` schon da war) |
| 400 | Transkript leer |
| 401 | Token fehlt oder stimmt nicht |
| 422 | Transkript zu lang (> 20 000 Zeichen) |
| 429 | mehr als 30 Aufträge pro Minute |
| 503 | Hermes nicht erreichbar |

Fehler kommen als JSON mit brauchbarer Meldung — die App zeigt sie an, und „Internal
Server Error" hilft am Homescreen niemandem.

Eine `request_id` gilt eine Stunde lang als bearbeitet; ein Retry bei wackeligem Netz
löst also nichts doppelt aus. Schlägt die Übergabe fehl, wird die Reservierung wieder
freigegeben — sonst wäre der Auftrag eine Stunde gesperrt, obwohl nichts passiert ist.

## Einrichten

```bash
cd /home/chris/hermes-bridge
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env && chmod 600 .env
python3 -c "import secrets; print(secrets.token_urlsafe(32))"   # Token erzeugen, in .env eintragen
```

Die `.env` gehört **nicht** ins Repo (`.gitignore`). Die Telegram-Chat-ID kann leer
bleiben — dann holt der Dienst sie beim ersten Auftrag über `hermes send --list telegram`,
und sie steht in keiner Datei.

Dienst:

```bash
cp deploy/hermes-bridge.service ~/.config/systemd/user/
systemctl --user daemon-reload && systemctl --user enable --now hermes-bridge
curl -s localhost:8093/health
```

Gebunden wird ausschließlich auf `127.0.0.1:8093`; nach außen führt Caddy mit TLS.

## Was dieser Dienst nie tut

`hermes gateway run`, `cron tick`, `systemctl restart hermes-gateway`, und Schreiben an
`~/.hermes/config.yaml` (lebende Datei, nur über `hermes config set`). Ein Test hält das
fest.

## Tests

```bash
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest tests/ -q
```

Hermes ist in allen Tests ersetzt — es wird nie echt zugestellt und nie ein echter
Cron-Job angelegt.
