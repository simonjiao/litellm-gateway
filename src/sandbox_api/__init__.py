"""Shared HTTP primitives for Sandbox control and data APIs."""

from .auth import install_bearer_auth

__all__ = ["install_bearer_auth"]
