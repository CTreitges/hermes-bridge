"""Legt einen Cron-Einmaljob in Hermes an.

Laeuft NICHT im venv der Bridge, sondern wird mit dem Interpreter des Hermes-Agenten
aufgerufen (`app/hermes.py`) — nur der kennt das `cron`-Modul und dessen Abhaengigkeiten.

Eingabe: ein JSON-Objekt auf stdin. Ausgabe: ein JSON-Objekt mit der Job-Id auf stdout.
Argumente gehen bewusst ueber stdin und nicht ueber die Kommandozeile: ein Transkript ist
Nutzereingabe und hat in einer Argumentliste nichts verloren.
"""

from __future__ import annotations

import json
import sys


def main() -> int:
    args = json.load(sys.stdin)
    sys.path.insert(0, args.pop("_agent_path"))

    from cron.jobs import create_job  # noqa: PLC0415 — erst nach sys.path importierbar

    job = create_job(**args)
    job_id = job.get("id") if isinstance(job, dict) else None
    json.dump({"id": job_id}, sys.stdout)
    return 0


if __name__ == "__main__":
    sys.exit(main())
