import asyncio
import sqlite3
import sqlalchemy as sa
import os
import logging
from enum import StrEnum
from datetime import datetime

from sqlalchemy import (
    String, Integer, BigInteger, Float, Text, ForeignKey, JSON, select, func, DateTime
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship, sessionmaker
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.pool import StaticPool

from config import TECH_SPECIALIST_ID, CHIEF_ADMIN_IDS


# ====================== DATABASE ENGINE ======================
# ====================== DATABASE ENGINE ======================
# ====================== DATABASE ENGINE ======================
DATABASE_URL = os.getenv("DATABASE_URL")

if DATABASE_URL:
    # Принудительно используем asyncpg
    if DATABASE_URL.startswith("postgresql://"):
        DATABASE_URL = DATABASE_URL.replace("postgresql://", "postgresql+asyncpg://", 1)
    
    engine = create_async_engine(
        DATABASE_URL,
        echo=False,
        pool_pre_ping=True,
        pool_size=20,
        max_overflow=10,
        pool_timeout=30,
    )
    print("✅ PostgreSQL + asyncpg engine подключён")
else:
    # SQLite для локальной разработки
    DB_PATH = "mun_bot.db"
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{DB_PATH}",
        connect_args={
            "timeout": 30.0,
            "check_same_thread": False,
        },
        echo=False,
        pool_pre_ping=True,
        future=True,
        poolclass=StaticPool,
    )
    print("✅ SQLite engine подключён (локально)")

AsyncSessionLocal = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


# Убрали enable_wal, т.к. он только для SQLite
# async def enable_wal(): ...


class Base(DeclarativeBase):
    pass


class Role(StrEnum):
    PARTICIPANT = "Участник"
    ORGANIZER = "Организатор"
    CHIEF_TECH = "Глав Тех Специалист"
    ADMIN = "Админ"
    CHIEF_ADMIN = "Главный Админ"


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)
    username: Mapped[str | None] = mapped_column(String(100), nullable=True)
    role: Mapped[str] = mapped_column(String(50), default=Role.PARTICIPANT.value)
    is_banned: Mapped[bool] = mapped_column(default=False)
    ban_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    full_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    age: Mapped[int | None] = mapped_column(Integer, nullable=True)
    email: Mapped[str | None] = mapped_column(String(100), nullable=True)
    institution: Mapped[str | None] = mapped_column(String(300), nullable=True)
    experience: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Поля для активности
    last_activity: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    messages_last_3h: Mapped[int] = mapped_column(Integer, default=0)

    applications: Mapped[list["Application"]] = relationship(back_populates="user")
    conferences: Mapped[list["Conference"]] = relationship(back_populates="organizer")
    support_requests: Mapped[list["SupportRequest"]] = relationship(back_populates="user")
    ratings: Mapped[list["ConferenceRating"]] = relationship(back_populates="user")
    edit_requests: Mapped[list["ConferenceEditRequest"]] = relationship(
        back_populates="organizer"
    )


class Conference(Base):
    __tablename__ = "conferences"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    city: Mapped[str | None] = mapped_column(String(100), nullable=True)
    date: Mapped[str] = mapped_column(String(50))
    is_active: Mapped[bool] = mapped_column(default=True)
    is_completed: Mapped[bool] = mapped_column(default=False)

    fee: Mapped[float] = mapped_column(Float, default=0.0)
    qr_code_path: Mapped[str | None] = mapped_column(String(500), nullable=True)
    poster_path: Mapped[str | None] = mapped_column(String(500), nullable=True)
    committee_chats: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    organizer_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    organizer: Mapped["User"] = relationship(back_populates="conferences")
    applications: Mapped[list["Application"]] = relationship(
        back_populates="conference",
        cascade="all, delete, delete-orphan"
    )
    ratings: Mapped[list["ConferenceRating"]] = relationship(
        back_populates="conference",
        cascade="all, delete-orphan"
    )
    edit_requests: Mapped[list["ConferenceEditRequest"]] = relationship(
        back_populates="conference",
        cascade="all, delete-orphan"
    )

    def get_average_rating(self) -> float | None:
        if not self.ratings:
            return None
        return round(sum(r.rating for r in self.ratings) / len(self.ratings), 2)


class Application(Base):
    __tablename__ = "applications"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    conference_id: Mapped[int] = mapped_column(ForeignKey("conferences.id"))

    committee: Mapped[str | None] = mapped_column(String(100), nullable=True)
    status: Mapped[str] = mapped_column(String(50), default="pending")
    payment_screenshot: Mapped[str | None] = mapped_column(String(500), nullable=True)
    reject_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    user: Mapped["User"] = relationship(back_populates="applications")
    conference: Mapped["Conference"] = relationship(back_populates="applications")


class ConferenceCreationRequest(Base):
    __tablename__ = "conference_creation_requests"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    data: Mapped[dict] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(50), default="pending")
    appeal: Mapped[bool] = mapped_column(default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


class ConferenceEditRequest(Base):
    __tablename__ = "conference_edit_requests"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    conference_id: Mapped[int] = mapped_column(ForeignKey("conferences.id"))
    organizer_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    data: Mapped[dict] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(50), default="pending")

    conference: Mapped["Conference"] = relationship("Conference", back_populates="edit_requests")
    organizer: Mapped["User"] = relationship("User", back_populates="edit_requests")


class SupportRequest(Base):
    __tablename__ = "support_requests"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    message: Mapped[str] = mapped_column(Text)
    screenshot_path: Mapped[str | None] = mapped_column(String(500), nullable=True)
    status: Mapped[str] = mapped_column(String(50), default="pending")
    response: Mapped[str | None] = mapped_column(Text, nullable=True)

    user: Mapped["User"] = relationship(back_populates="support_requests")


class ConferenceRating(Base):
    __tablename__ = "conference_ratings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    conference_id: Mapped[int] = mapped_column(ForeignKey("conferences.id"), index=True)
    rating: Mapped[int] = mapped_column(Integer)
    review: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)

    user: Mapped["User"] = relationship("User", back_populates="ratings")
    conference: Mapped["Conference"] = relationship("Conference", back_populates="ratings")

    __table_args__ = (
        sa.UniqueConstraint("user_id", "conference_id", name="uq_user_conference_rating"),
    )


class DeletedConference(Base):
    __tablename__ = "deleted_conferences"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    conference_name: Mapped[str] = mapped_column(String(200))
    organizer_telegram_id: Mapped[int] = mapped_column(BigInteger)
    deleted_by_telegram_id: Mapped[int] = mapped_column(BigInteger)
    reason: Mapped[str] = mapped_column(Text)
    deleted_at: Mapped[str] = mapped_column(String(50))


class BotStatus(Base):
    __tablename__ = "bot_status"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    is_paused: Mapped[bool] = mapped_column(default=False)
    pause_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    paused_by: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    paused_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    resumed_by: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    resumed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class AdminActionLog(Base):
    __tablename__ = "admin_action_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    admin_id: Mapped[int] = mapped_column(BigInteger)
    admin_username: Mapped[str | None] = mapped_column(String(100), nullable=True)
    action: Mapped[str] = mapped_column(String(100))
    target: Mapped[str] = mapped_column(String(200))
    details: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


async def get_bot_status() -> BotStatus:
    async with AsyncSessionLocal() as session:
        status = await session.get(BotStatus, 1)
        if not status:
            status = BotStatus(id=1, is_paused=False)
            session.add(status)
            await session.commit()
        return status


async def set_bot_paused(paused: bool, reason: str | None, user_id: int):
    async with AsyncSessionLocal() as session:
        status = await session.get(BotStatus, 1)
        if not status:
            status = BotStatus(id=1)
            session.add(status)

        status.is_paused = paused
        if paused:
            status.pause_reason = reason
            status.paused_by = user_id
            status.paused_at = datetime.now()
            status.resumed_by = None
            status.resumed_at = None
        else:
            status.pause_reason = None
            status.resumed_by = user_id
            status.resumed_at = datetime.now()
            status.paused_by = None
            status.paused_at = None

        await session.commit()


async def init_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async with AsyncSessionLocal() as session:
        status = await session.get(BotStatus, 1)
        if not status:
            status = BotStatus(id=1, is_paused=False)
            session.add(status)
            await session.commit()


async def get_or_create_user(telegram_id: int, full_name: str | None = None, username: str | None = None) -> User:
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(User).where(User.telegram_id == telegram_id))
        user = result.scalar_one_or_none()

        if user is None:
            user = User(
                telegram_id=telegram_id,
                username=username,
                full_name=full_name or "Не указано",
                role=Role.PARTICIPANT.value,
                is_banned=False
            )
            session.add(user)
            await session.commit()
            await session.refresh(user)
            logging.info(f"Создан новый пользователь: {telegram_id} ({full_name})")
        else:
            updated = False
            if full_name and user.full_name != full_name:
                user.full_name = full_name
                updated = True
            if username and user.username != username:
                user.username = username
                updated = True
            if updated:
                await session.commit()
                await session.refresh(user)
                logging.info(f"Обновлён пользователь: {telegram_id}")

        # Назначение специальных ролей
        if telegram_id in CHIEF_ADMIN_IDS and user.role != Role.CHIEF_ADMIN.value:
            user.role = Role.CHIEF_ADMIN.value
            await session.commit()
            logging.info(f"Назначена роль Главный Админ для {telegram_id}")

        if telegram_id == TECH_SPECIALIST_ID and user.role != Role.CHIEF_TECH.value:
            user.role = Role.CHIEF_TECH.value
            await session.commit()
            logging.info(f"Назначена роль Главный Тех Специалист для {telegram_id}")

        return user


async def update_user_activity(telegram_id: int):
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(User).where(User.telegram_id == telegram_id)
        )
        user = result.scalar_one_or_none()
        if user:
            user.last_activity = datetime.now()
            user.messages_last_3h += 1
            await session.commit()


class ApplicationState:
    pass
