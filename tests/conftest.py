import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Muss vor dem Import von app.main stehen: die App liest das Token beim Aufruf, aber
# eine echte .env darf in Tests nie mitreden.
os.environ["BRIDGE_TOKEN"] = "test-token-geheim"
os.environ["HERMES_CHAT_ID"] = "4242"
