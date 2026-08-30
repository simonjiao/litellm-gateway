"""Private immutable Artifact HTTP service."""

from .app import create_app
from .settings import ArtifactSettings

__all__ = ["ArtifactSettings", "create_app"]
