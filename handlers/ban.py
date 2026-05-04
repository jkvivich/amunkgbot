# handlers/ban.py
from aiogram import Router, types, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import BufferedInputFile
from sqlalchemy import select
import pandas as pd
import os
from utils import safe, log_admin_action   # ← измени эту строку
from database import AsyncSessionLocal, User, Role
from config import TECH_SPECIALIST_ID, CHIEF_ADMIN_IDS
from states import BanReasonState

router = Router()


async def can_ban_unban(user_id: int) -> bool:
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(User).where(User.telegram_id == user_id)
        )
        user = result.scalar_one_or_none()
        return user and user.role in [Role.ADMIN.value, Role.CHIEF_ADMIN.value, Role.CHIEF_TECH.value]


@router.message(Command("ban"))
async def start_ban(message: types.Message, state: FSMContext):
    if not await can_ban_unban(message.from_user.id):
        await message.answer("Доступ запрещён.")
        return

    try:
        _, target = message.text.split(maxsplit=1)
        target = target.lstrip("@").strip()
    except ValueError:
        await message.answer("Использование: /ban @username или /ban ID")
        return

    await state.update_data(target=target, action="ban")
    if message.from_user.id == TECH_SPECIALIST_ID:
        await do_ban_unban(message, state, reason="Без причины (Глав Тех Специалист)")
    else:
        await state.set_state(BanReasonState.reason)
        await message.answer("Введите причину бана:")
        


@router.message(Command("unban"))
async def start_unban(message: types.Message, state: FSMContext):
    if not await can_ban_unban(message.from_user.id):
        await message.answer("Доступ запрещён.")
        return

    try:
        _, target = message.text.split(maxsplit=1)
        target = target.lstrip("@").strip()
    except ValueError:
        await message.answer("Использование: /unban @username или /unban ID")
        return

    await state.update_data(target=target, action="unban")
    if message.from_user.id == TECH_SPECIALIST_ID:
        await do_ban_unban(message, state, reason="Без причины (Глав Тех Специалист)")
    else:
        await state.set_state(BanReasonState.reason)
        await message.answer("Введите причину разбана:")


@router.message(BanReasonState.reason)
async def process_reason(message: types.Message, state: FSMContext):
    await state.update_data(reason=message.text)
    await do_ban_unban(message, state, reason=message.text)


# =========================
# ⚙️ ВЫПОЛНЕНИЕ БАНА / РАЗБАНА
# =========================
async def do_ban_unban(message: types.Message, state: FSMContext, reason: str):
    data = await state.get_data()
    target = data["target"]
    action = data["action"]

    # === ЗАЩИТА: Организаторы вообще не могут банить ===
    if message.from_user.id != TECH_SPECIALIST_ID and message.from_user.id not in CHIEF_ADMIN_IDS:
        # Проверяем роль пользователя
        async with AsyncSessionLocal() as session:
            result = await session.execute(
                select(User.role).where(User.telegram_id == message.from_user.id)
            )
            user_role = result.scalar_one_or_none()

        if user_role == Role.ORGANIZER.value:
            await message.answer("🚫 У вас нет прав на блокировку пользователей.")
            await state.clear()
            return

    async with AsyncSessionLocal() as session:
        # Получаем целевого пользователя
        if str(target).isdigit():
            result = await session.execute(
                select(User).where(User.telegram_id == int(target))
            )
        else:
            result = await session.execute(
                select(User).where(User.full_name.ilike(f"%{target}%"))
            )

        user = result.scalar_one_or_none()

        if not user:
            await message.answer("Пользователь не найден.")
            await state.clear()
            return

        # === ЗАЩИТА ВЫСОКИХ РОЛЕЙ ===
        is_target_chief_tech = user.telegram_id == TECH_SPECIALIST_ID
        is_target_chief_admin = user.telegram_id in CHIEF_ADMIN_IDS

        current_user_id = message.from_user.id
        is_current_chief_tech = current_user_id == TECH_SPECIALIST_ID

        if action == "ban":
            # Никто не может забанить Глав Тех Специалиста
            if is_target_chief_tech:
                await message.answer("🚫 Невозможно заблокировать Главного Технического Специалиста.")
                await state.clear()
                return

            # Только Глав Тех Спец может забанить Главного Админа
            if is_target_chief_admin and not is_current_chief_tech:
                await message.answer("🚫 Только Главный Технический Специалист может заблокировать Главного Админа.")
                await state.clear()
                return

            if user.is_banned:
                await message.answer(f"Пользователь {user.full_name or user.telegram_id} уже забанен.")
                await state.clear()
                return

            user.is_banned = True
            user.ban_reason = reason
            await session.commit()

            await log_admin_action(
                admin_id=message.from_user.id,
                admin_username=message.from_user.username,
                action="ban",
                target=f"ID {user.telegram_id} ({user.full_name or '—'})",
                details=reason
            )

            action_text = "заблокирован"
            user_text = f"🚫 Вы заблокированы в боте MUN.\nПричина: {reason}"

        else:  # unban
            if not user.is_banned:
                await message.answer(f"Пользователь {user.full_name or user.telegram_id} не забанен.")
                await state.clear()
                return

            old_reason = user.ban_reason

            user.is_banned = False
            user.ban_reason = None
            user.role = Role.PARTICIPANT.value
            await session.commit()

            await log_admin_action(
                admin_id=message.from_user.id,
                admin_username=message.from_user.username,
                action="unban",
                target=f"ID {user.telegram_id} ({user.full_name or '—'})",
                details=f"Ранее причина: {old_reason or 'не указана'}"
            )

            action_text = "разблокирован"
            user_text = "✅ Вы разблокированы в боте MUN."

    await message.answer(
        f"✅ Пользователь {user.full_name or user.telegram_id} **{action_text}**.",
        parse_mode="HTML"
    )

    try:
        await message.bot.send_message(user.telegram_id, user_text)
    except:
        pass

    await state.clear()