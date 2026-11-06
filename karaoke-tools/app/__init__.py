"""KaraokeGayak Mini App server (FastAPI). It reuses the karaoke-tools modules
(catalog, suggest, feedback, youtube, cache) so the chat and the app agree."""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
