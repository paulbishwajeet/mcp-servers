"""Run the app server: `venv/bin/python -m app` from the karaoke-tools folder.
Binds 127.0.0.1 only; Tailscale Funnel is the public https front."""

import os

from .config import load_env_files


def main() -> None:
    applied = load_env_files()
    import uvicorn

    from common import log
    log(f"[app] env from files: {', '.join(sorted(applied)) or 'none'}")
    uvicorn.run("app.main:app", host="127.0.0.1", port=int(os.environ.get("APP_PORT", "8765")),
                proxy_headers=False, access_log=False, log_level="info")


if __name__ == "__main__":
    main()
