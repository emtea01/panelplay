"""python -m portal  (PANELPLAY_HOST, PANELPLAY_PORT and PANELPLAY_DATA override the defaults)."""
import os

import uvicorn

if __name__ == "__main__":
    uvicorn.run("portal.app:app", host=os.environ.get("PANELPLAY_HOST", "127.0.0.1"),
                port=int(os.environ.get("PANELPLAY_PORT", "8077")), log_level="info")
