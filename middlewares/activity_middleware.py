# middlewares/activity_middleware.py
import logging
from aiogram import BaseMiddleware
from aiogram.types import Message, CallbackQuery

from database import get_or_create_user, update_user_activity


class ActivityMiddleware(BaseMiddleware):
    async def __call__(self, handler, event, data):
        telegram_id = None

        if isinstance(event, Message):
            telegram_id = event.from_user.id
        elif isinstance(event, CallbackQuery):
            telegram_id = event.from_user.id

        if telegram_id:
            try:
                await get_or_create_user(telegram_id)
                await update_user_activity(telegram_id)
            except Exception as e:
                logging.error(f"Ошибка в ActivityMiddleware для пользователя {telegram_id}: {e}")

        return await handler(event, data)