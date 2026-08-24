from __future__ import annotations

import uvicorn

from .app import create_worker_app
from .settings import WorkerSettings


def main() -> None:
    settings = WorkerSettings()
    uvicorn.run(
        create_worker_app(settings),
        host=settings.host,
        port=settings.port,
        log_level="info",
    )


if __name__ == "__main__":
    main()
