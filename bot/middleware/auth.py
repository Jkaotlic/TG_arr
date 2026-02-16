"""Authentication middleware for Telegram bot."""

import time
from collections import defaultdict
from typing import Any, Awaitable, Callable, Dict

import structlog
from aiogram import BaseMiddleware
from aiogram.types import Message, CallbackQuery, TelegramObject

from bot.config import get_settings
from bot.db import Database

logger = structlog.get_logger()

# Simple in-memory rate limiting
_user_requests: Dict[int, list] = defaultdict(list)
MAX_REQUESTS_PER_MINUTE = 30  # Max requests per user per minute


class AuthMiddleware(BaseMiddleware):
    """Middleware to check if user is allowed to use the bot."""

    def __init__(self, db: Database):
        self.settings = get_settings()
        self.db = db
        super().__init__()

    async def __call__(
        self,
        handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: Dict[str, Any],
    ) -> Any:
        """Check user authorization before processing."""
        user_id = None
        user = None

        # Extract user from different event types
        if isinstance(event, Message):
            user = event.from_user
        elif isinstance(event, CallbackQuery):
            user = event.from_user

        if user:
            user_id = user.id

        if user_id is None:
            logger.warning("Could not extract user ID from event")
            return None

        # Check if user is allowed
        if not self.settings.is_user_allowed(user_id):
            logger.warning(
                "Unauthorized access attempt",
                user_id=user_id,
                username=user.username if user else None,
            )

            # Send rejection message
            if isinstance(event, Message):
                await event.answer(
                    "⛔ Доступ запрещён. Вы не авторизованы для использования бота.\n"
                    "Свяжитесь с администратором для получения доступа."
                )
            elif isinstance(event, CallbackQuery):
                await event.answer("Доступ запрещён", show_alert=True)

            return None

        # Ensure user exists in database
        db_user = await self.db.get_user(user_id)
        if db_user is None:
            # Create user
            from bot.models import User, UserRole

            is_admin = self.settings.is_admin(user_id)
            db_user = User(
                tg_id=user_id,
                username=user.username,
                first_name=user.first_name,
                role=UserRole.ADMIN if is_admin else UserRole.USER,
            )
            await self.db.create_user(db_user)
            logger.info("Created new user", user_id=user_id, role=db_user.role.value)

        # Add user info and database to handler data
        data["db_user"] = db_user
        data["is_admin"] = self.settings.is_admin(user_id)
        data["db"] = self.db

        return await handler(event, data)


class LoggingMiddleware(BaseMiddleware):
    """Middleware for logging all incoming events."""

    async def __call__(
        self,
        handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: Dict[str, Any],
    ) -> Any:
        """Log incoming events."""
        log = logger.bind()

        if isinstance(event, Message):
            log = log.bind(
                event_type="message",
                user_id=event.from_user.id if event.from_user else None,
                chat_id=event.chat.id,
                text=event.text[:50] if event.text else None,
            )
        elif isinstance(event, CallbackQuery):
            log = log.bind(
                event_type="callback",
                user_id=event.from_user.id if event.from_user else None,
                data=event.data,
            )

        log.debug("Incoming event")

        try:
            result = await handler(event, data)
            log.debug("Event processed successfully")
            return result
        except Exception as e:
            log.error("Error processing event", error=str(e), exc_info=True)
            raise


class RateLimitMiddleware(BaseMiddleware):
    """Simple in-memory rate limiting middleware."""

    def __init__(self, max_requests: int = MAX_REQUESTS_PER_MINUTE, window_seconds: int = 60):
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        super().__init__()

    async def __call__(
        self,
        handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: Dict[str, Any],
    ) -> Any:
        """Check rate limit before processing."""
        user_id = None

        if isinstance(event, Message):
            user_id = event.from_user.id if event.from_user else None
        elif isinstance(event, CallbackQuery):
            user_id = event.from_user.id if event.from_user else None

        if user_id is None:
            return await handler(event, data)

        now = time.time()
        window_start = now - self.window_seconds

        # Clean old requests
        _user_requests[user_id] = [t for t in _user_requests[user_id] if t > window_start]

        # Remove empty entries to prevent unbounded dict growth
        if not _user_requests[user_id]:
            del _user_requests[user_id]

        # Check rate limit
        if len(_user_requests[user_id]) >= self.max_requests:
            logger.warning("Rate limit exceeded", user_id=user_id)
            if isinstance(event, Message):
                await event.answer("⏳ Слишком много запросов. Подождите минуту.")
            elif isinstance(event, CallbackQuery):
                await event.answer("Слишком много запросов", show_alert=True)
            return None

        # Record request
        _user_requests[user_id].append(now)

        return await handler(event, data)
