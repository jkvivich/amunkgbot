from html import escape
from datetime import datetime
from sqlalchemy import select
from database import AsyncSessionLocal, AdminActionLog

def safe(text: str | None) -> str:
    """Защита от HTML-инъекций"""
    if text is None:
        return ""
    return escape(str(text))


async def log_admin_action(admin_id: int, admin_username: str | None, action: str, target: str, details: str | None = None):
    """Логирует действие админа (только для Глав Тех Специалиста)"""
    async with AsyncSessionLocal() as session:
        log = AdminActionLog(
            admin_id=admin_id,
            admin_username=admin_username,
            action=action,
            target=target,
            details=details
        )
        session.add(log)
        await session.commit()