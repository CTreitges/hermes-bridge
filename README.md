# hermes-bridge

Nimmt gesprochene Aufträge aus der Android-App [WhisperLoom](https://github.com/CTreitges/whisperloom-android)
entgegen und gibt sie an deinen eigenen [Hermes](https://github.com/NousResearch/hermes)-Agenten weiter.
Die Antwort kommt dort an, wo du dem Agenten sonst schreibst.

> **In English:** a small self-hosted FastAPI service. WhisperLoom's home screen widget records
> your voice, transcribes it on the phone's chosen engine and POSTs the text here; this service
> hands it to your Hermes agent and delivers the answer to your messenger. One endpoint, one
> bearer token, no accounts anywhere. Code and comments are in German.

Warum das existiert: Telegram transkribiert Sprachnachrichten selbst, und zwar schlechter als
WhisperLoom. Also spricht man ins Widget, und WhisperLoom erkennt den Text mit dem Zugang, den
du ohnehin eingerichtet hast.

## Was du brauchst

- Einen Rechner, der dauerhaft läuft und aus dem Internet erreichbar ist (VPS, Heimserver mit
  Portfreigabe oder Tailscale). **Python 3.11+** (entwickelt und getestet mit 3.12).
- Einen eingerichteten Hermes-Agenten auf demselben Rechner, mit laufendem Gateway und einem
  Messenger-Ziel (getestet mit Telegram). `hermes send --list telegram` muss ein Ziel zeigen.
- TLS davor. Ohne HTTPS geht das Token im Klartext über die Leitung. Ein Reverse-Proxy genügt,
  Beispiele unten.
- WhisperLoom **3.4.0 oder neuer** auf dem Telefon.

Der Dienst bindet ausschließlich auf `127.0.0.1` — nach außen führt einzig der Reverse-Proxy.

## Ablauf eines Auftrags

1. `POST /v1/task` mit Bearer-Token. Antwort **202**, sofort.
2. Das Transkript wird als `sprachauftrag-<datum>.md` geschrieben und per
   `hermes send --to telegram:<chat> "[[as_document]] MEDIA:<pfad>"` in den Chat gelegt.
   Das passiert noch während der Anfrage: scheitert es, bekommt die App **503** und kann es
   erneut versuchen. Ohne `[[as_document]]` käme die Datei als normale Nachricht an.
3. Danach, im Hintergrund: `hermes chat -q "<Auftrag>" -Q` lässt den Agenten arbeiten, und die
   Antwort geht per `hermes send` in denselben Chat.

Gemessen liegt ein einfacher Auftrag bei rund 16 Sekunden vom Abschicken bis zur Antwort.

<details>
<summary>Warum nicht über einen Cron-Job (wie in v1)?</summary>

Die erste Fassung legte einen Cron-Einmaljob an. Das brachte `attach_to_session` mit — die
Antwort landete auch in der Chat-Sitzung, der Agent erinnerte sich dort daran. Es kostete aber
60 bis 120 Sekunden: Hermes' kleinste Zeiteinheit ist die Minute, und der eingebaute Ticker
läuft im 60-Sekunden-Takt. Der Agentenlauf selbst dauert rund sieben Sekunden. Der Direktaufruf
gibt `attach_to_session` auf und ist dafür fünfmal schneller.

Dabei fiel auch ein stiller Fehler auf: Hermes stellt jedem Cron-Prompt den Hinweis voran, bei
„nichts Neues zu berichten" mit `[SILENT]` zu antworten. Für einen Überwachungsjob ist das
richtig, für einen Sprachauftrag genau verkehrt — der Agent schwieg, und im Protokoll stand
„completed successfully". Siehe PR #2.
</details>

## Schnittstelle

```
POST /v1/task
Authorization: Bearer <token>
Content-Type: application/json

{"transcript": "…", "request_id": "<uuid>", "recorded_at": "<ISO-8601>",
 "duration_ms": 12345, "source": "widget"}
```

| Code | Wann |
|---|---|
| 202 | angenommen (`duplicate: true`, wenn die `request_id` schon da war) |
| 400 | Transkript leer |
| 401 | Token fehlt oder stimmt nicht |
| 422 | Transkript länger als 20 000 Zeichen |
| 429 | mehr als 30 Aufträge pro Minute |
| 503 | Hermes nicht erreichbar — die `request_id` wird wieder freigegeben |

`GET /health` verrät nichts Geheimes: `{"status": "ok", "configured": true, "pending": 0}`.

Die `request_id` bleibt über alle Wiederholungen der App gleich; dieselbe Id wird nur einmal
ausgeführt (eine Stunde lang gemerkt). Scheitert die Übergabe, wird sie wieder freigegeben —
sonst wäre der Auftrag eine Stunde gesperrt, obwohl nichts passiert ist.

Ein leeres Transkript beantwortet der Dienst mit 400, **nachdem** er das Token geprüft hat.
Genau darauf baut „Verbindung prüfen" in der App: 400 heißt „erreichbar und Token stimmt",
401 heißt „Token falsch" — und es entsteht weder ein Auftrag noch Rate-Limit-Verbrauch.

## Einrichten

```bash
git clone https://github.com/CTreitges/hermes-bridge.git ~/hermes-bridge
cd ~/hermes-bridge
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt

cp .env.example .env && chmod 600 .env
python3 -c "import secrets; print(secrets.token_urlsafe(32))"   # Token erzeugen, in .env eintragen
```

Die `.env` gehört **nicht** ins Repo (steht in `.gitignore`). Die Chat-ID kann leer bleiben —
dann holt der Dienst sie beim ersten Auftrag über `hermes send --list telegram`, und sie steht
in keiner Datei.

Dienst als systemd-User-Unit (läuft ohne root, überlebt den Neustart mit `loginctl enable-linger`):

```bash
mkdir -p ~/.config/systemd/user
sed "s|/home/chris|$HOME|g" deploy/hermes-bridge.service > ~/.config/systemd/user/hermes-bridge.service
systemctl --user daemon-reload && systemctl --user enable --now hermes-bridge
curl -s localhost:8093/health
```

Läuft der Dienst schon, bevor das Token da ist, ist das in Ordnung: ohne `BRIDGE_TOKEN`
beantwortet er jeden Auftrag mit 401 und meldet `"configured": false`.

### TLS davor

Caddy (kümmert sich selbst um das Zertifikat):

```caddyfile
hermes-bridge.example.de {
	encode zstd gzip
	header X-Robots-Tag "noindex, nofollow, noarchive"
	reverse_proxy 127.0.0.1:8093
}
```

nginx, falls schon vorhanden:

```nginx
location / {
    proxy_pass http://127.0.0.1:8093;
    proxy_set_header Host $host;
}
```

### In der App eintragen

Einstellungen → **Erweiterte Optionen**: Schalter an, **Server-Adresse**
(`https://hermes-bridge.example.de`, ohne Pfad — `/v1/task` hängt die App selbst an), **Token**,
dann **Verbindung prüfen**. Es muss „Verbunden · x,x s" erscheinen. Danach das Widget
„Sprachauftrag" auf den Startbildschirm legen.

## Konfiguration

Alles Geheime steht in `.env`, nichts davon im Repo:

| Schlüssel | Pflicht | Bedeutung |
|---|---|---|
| `BRIDGE_TOKEN` | ja | Was die App im `Authorization`-Header mitschickt. Lang und zufällig. |
| `HERMES_CHAT_ID` | nein | Ziel-Chat. Leer = zur Laufzeit von Hermes erfragen (steht dann in keiner Datei). |

Pfade und Grenzwerte stehen in `app/config.py`: Hermes-Verzeichnis, Zeitlimits
(`AGENT_TIMEOUT_S` = 600 s für den Agentenlauf, 30 s für die kurzen Aufrufe),
Idempotenz-Dauer, Rate-Limit, Höchstlänge des Transkripts.

## Wenn etwas nicht klappt

```bash
systemctl --user status hermes-bridge
journalctl --user -u hermes-bridge -n 50 --no-pager
```

Der Dienst protokolliert jeden Schritt sichtbar:

```
INFO hermes-bridge: Auftrag abc-123 angenommen, Agent laeuft
INFO hermes-bridge: Agent fertig (412 Zeichen), stelle zu
INFO hermes-bridge: Antwort zugestellt an telegram:…
INFO hermes-bridge: Auftrag abc-123 beantwortet
```

- **„Verbindung prüfen" meldet 401** — Token in `.env` und App stimmen nicht überein. Nach jeder
  Änderung an `.env`: `systemctl --user restart hermes-bridge`.
- **503 „Hermes nicht erreichbar"** — läuft das Gateway (`systemctl --user status hermes-gateway`)?
  Zeigt `hermes send --list telegram` ein Ziel?
- **Dokument kommt an, Antwort nicht** — dann scheiterte der Agentenlauf. Der Auftraggeber bekommt
  in dem Fall eine Meldung im Chat; Einzelheiten stehen in `~/.hermes/logs/agent.log`.
- **Nichts kommt an, das Widget bleibt auf „Wird gesendet …"** — meist fehlt dem Telefon das Netz.
  Ein Tipp auf die Fläche sieht nach, ob der Auftrag noch eingeplant ist, und reiht ihn nötigenfalls
  neu ein.

## Was dieser Dienst nie tut

`hermes gateway run`, `cron tick`, `systemctl restart hermes-gateway`, und Schreiben an
`~/.hermes/config.yaml` (lebende Datei, nur über `hermes config set`). Ein Test hält das fest.

## Tests

```bash
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest tests/ -q
```

Hermes ist in allen Tests ersetzt — es wird nie echt zugestellt und nie ein echter Agent gestartet.

## Lizenz

MIT.
