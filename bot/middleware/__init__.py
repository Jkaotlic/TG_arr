"""Middleware for Telegram bot."""

from bot.middleware.auth import AuthMiddleware

__all__ = ["AuthMiddleware"]
