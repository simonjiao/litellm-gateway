from __future__ import annotations

import uvicorn

from .app import create_app
from .settings import ArtifactSettings


def main() -> None:
    settings = ArtifactSettings()  # pyright: ignore[reportCallIssue]
    uvicorn.run(create_app(settings), host=settings.host, port=settings.port)


if __name__ == "__main__":
    main()
