from __future__ import annotations

import uvicorn

from dexter.config import Settings

if __name__ == "__main__":
    settings = Settings.from_env()
    uvicorn.run("dexter.api.app:app", host=settings.host, port=settings.port, log_config=None, access_log=False)
