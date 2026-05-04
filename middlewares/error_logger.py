# middlewares/error_logger.py
import traceback
import logging
from aiogram import BaseMiddleware
from aiogram.types import TelegramObject, Message, CallbackQuery

from config import TECH_SPECIALIST_ID


class ErrorLoggerMiddleware(BaseMiddleware):
    async def __call__(self, handler, event: TelegramObject, data):
        try:
            return await handler(event, data)
        except Exception as e:
            # Безопасное получение user_id
            user_id = "unknown"
            if isinstance(event, (Message, CallbackQuery)):
                if event.from_user:
                    user_id = event.from_user.id
            elif hasattr(event, "from_user") and event.from_user:
                user_id = event.from_user.id

            # Формируем сообщение об ошибке
            error_text = (
                f"🚨 <b>КРИТИЧЕСКАЯ ОШИБКА В БОТЕ</b>\n\n"
                f"Пользователь: <code>{user_id}</code>\n"
                f"Тип ошибки: <b>{type(e).__name__}</b>\n"
                f"Описание: {e}\n\n"
                f"<pre>{traceback.format_exc()[:3000]}</pre>"
            )

            try:
                await event.bot.send_message(
                    TECH_SPECIALIST_ID,
                    error_text,
                    parse_mode="HTML"
                )
            except:
                pass  # если не смогли отправить — не падаем

            logging.exception("Глобальная ошибка в боте")
            raise  # пробрасываем ошибку дальше