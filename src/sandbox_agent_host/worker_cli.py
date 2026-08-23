from __future__ import annotations

import uvicorn

from .settings import WorkerSettings
from .worker import create_worker_app


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
