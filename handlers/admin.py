import logging
from aiogram import Router, types, F
from aiogram.filters import Command
from sqlalchemy import select, func, delete
from aiogram.types import InlineKeyboardButton, BufferedInputFile, FSInputFile
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.filters.state import StateFilter
import pandas as pd
import os

from aiogram.exceptions import TelegramBadRequest
from datetime import datetime
from utils import safe, log_admin_action

import os
print("Текущая рабочая папка бота:", os.getcwd())
print("Файл базы должен быть здесь:", os.path.join(os.getcwd(), "mun_bot.db"))

from database import (
    AsyncSessionLocal,
    ConferenceCreationRequest,
    ConferenceEditRequest,
    Conference,
    Application,
    User,
    Role,
    get_or_create_user,
    DeletedConference,
    get_bot_status,
    set_bot_paused,
    SupportRequest,
    AdminActionLog
)
from sqlalchemy.orm import joinedload
from database import ConferenceRating

from keyboards import get_main_menu_keyboard, get_cancel_keyboard
from config import CHIEF_ADMIN_IDS, TECH_SPECIALIST_ID

from states import BanReasonState

router = Router()

# States для админских действий
class AdminStates(StatesGroup):
    delete_conf_reason = State()
    waiting_support_reply = State()

# Пагинация для обращений
edit_pagination = {}
support_pagination = {}
create_pagination = {}
all_conferences_pagination = {}

# Проверки ролей
async def is_admin_or_chief(user_id: int) -> bool:
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(User).where(User.telegram_id == user_id))
        user = result.scalar_one_or_none()
        if not user:
            return False
        return user.role in [Role.ADMIN.value, Role.CHIEF_ADMIN.value] if user else False

async def is_chief_admin(user_id: int) -> bool:
    return user_id in CHIEF_ADMIN_IDS

from config import TECH_SPECIALIST_ID

async def is_chief_tech(user_id: int) -> bool:
    return user_id == TECH_SPECIALIST_ID

async def can_delete_conference(user_id: int) -> bool:
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(User).where(User.telegram_id == user_id))
        user = result.scalar_one_or_none()
        if not user:
            return False
        return user.role in [Role.ADMIN.value, Role.CHIEF_ADMIN.value, Role.CHIEF_TECH.value] if user else False

async def can_pause_bot(user_id: int) -> bool:
    return user_id in CHIEF_ADMIN_IDS or await is_chief_tech(user_id)

async def can_view_conferences(user_id: int) -> bool:
    return await is_admin_or_chief(user_id) or await is_chief_tech(user_id)

# Универсальная функция обновления списка всех заявок
async def update_requests_message(event: types.Message | types.CallbackQuery):
    async with AsyncSessionLocal() as session:
        create_requests = (await session.execute(
            select(ConferenceCreationRequest).where(ConferenceCreationRequest.status == "pending")
        )).scalars().all()

        edit_requests = (await session.execute(
            select(ConferenceEditRequest).where(ConferenceEditRequest.status == "pending")
        )).scalars().all()

        appeal_requests = (await session.execute(
            select(ConferenceCreationRequest).where(
                ConferenceCreationRequest.status == "rejected",
                ConferenceCreationRequest.appeal == True
            )
        )).scalars().all()

        if not create_requests and not edit_requests and not appeal_requests:
            text = "Нет активных заявок."
            if isinstance(event, types.Message):
                await event.answer(text)
            else:
                await event.message.edit_text(text)
            return

        if create_requests:
            await event.bot.send_message(event.from_user.id, "<b>Заявки на создание конференций:</b>")
            for req in create_requests:
                user = await session.get(User, req.user_id)
                data = req.data

                text = f"ID: <code>{req.id}</code>\n"
                text += f"От: {safe(user.full_name) or safe(user.telegram_id)}\n"
                text += f"<b>Название:</b> {data.get('name', '—')}\n"
                if data.get('description'):
                    text += f"<b>Описание:</b>\n{data.get('description')}\n\n"
                text += f"<b>Город:</b> {data.get('city', 'Онлайн')}\n"
                text += f"<b>Дата проведения:</b> {data.get('date', '—')}\n"
                text += f"<b>Орг взнос:</b> {int(data.get('fee', 0))} сом\n"

                builder = InlineKeyboardBuilder()
                builder.row(
                    InlineKeyboardButton(text="Одобрить", callback_data=f"conf_create_approve_{req.id}"),
                    InlineKeyboardButton(text="Отклонить", callback_data=f"conf_create_reject_{req.id}")
                )

                if data.get('poster_path') and os.path.exists(data['poster_path']):
                    photo = FSInputFile(data['poster_path'])
                    await event.bot.send_photo(event.from_user.id, photo, caption=text, reply_markup=builder.as_markup())
                else:
                    await event.bot.send_message(event.from_user.id, text, reply_markup=builder.as_markup())

        if edit_requests:
            await event.bot.send_message(event.from_user.id, "<b>Заявки на редактирование:</b>")
            for req in edit_requests:
                conf = await session.get(Conference, req.conference_id)
                organizer = await session.get(User, req.organizer_id)
                data = req.data

                text = f"ID: <code>{req.id}</code>\n"
                text += f"Конференция: <b>{conf.name}</b>\n"
                text += f"От: {organizer.full_name or organizer.telegram_id}\n\n"
                text += f"<b>Текущие данные:</b>\n"
                text += f"Название: {conf.name}\n"
                if conf.description:
                    text += f"Описание: {conf.description}\n"
                text += f"Город: {conf.city or 'Онлайн'}\n"
                text += f"Дата проведения: {conf.date}\n"
                text += f"Орг взнос: {conf.fee} руб.\n\n"
                text += f"<b>Новые данные:</b>\n"
                text += f"Название: {data.get('name', conf.name)}\n"
                if data.get('description') is not None:
                    text += f"Описание: {data.get('description') or '(удалено)'}\n"
                text += f"Город: {data.get('city', conf.city)}\n"
                text += f"Дата проведения: {data.get('date', conf.date)}\n"
                text += f"Орг взнос: {data.get('fee', conf.fee)} руб.\n"

                builder = InlineKeyboardBuilder()
                builder.row(
                    InlineKeyboardButton(text="Одобрить", callback_data=f"conf_edit_approve_{req.id}"),
                    InlineKeyboardButton(text="Отклонить", callback_data=f"conf_edit_reject_{req.id}")
                )

                if data.get('poster_path') and os.path.exists(data['poster_path']):
                    photo = FSInputFile(data['poster_path'])
                    await event.bot.send_photo(event.from_user.id, photo, caption=text, reply_markup=builder.as_markup())
                else:
                    if conf.poster_path and os.path.exists(conf.poster_path):
                        photo = FSInputFile(conf.poster_path)
                        await event.bot.send_photo(event.from_user.id, photo, caption=text, reply_markup=builder.as_markup())
                    else:
                        await event.bot.send_message(event.from_user.id, text, reply_markup=builder.as_markup())

        if appeal_requests:
            await event.bot.send_message(event.from_user.id, "<b>Апелляции к Глав Админу:</b>")
            for req in appeal_requests:
                user = await session.get(User, req.user_id)
                data = req.data

                text = f"ID: <code>{req.id}</code> (апелляция)\n"
                text += f"От: {safe(user.full_name) or safe(user.telegram_id)}\n"
                text += f"Название: {safe(data.get('name', '—'))}\n"
                if data.get('description'):
                    text += f"Описание: {data.get('description')}\n"
                text += f"Город: {data.get('city')}\n"
                text += f"Дата проведения: {data.get('date')}\n"
                text += f"Орг взнос: {int(data.get('fee', 0))} сом\n"

                builder = InlineKeyboardBuilder()
                builder.row(
                    InlineKeyboardButton(text="Одобрить", callback_data=f"conf_appeal_approve_{req.id}"),
                    InlineKeyboardButton(text="Отклонить", callback_data=f"conf_appeal_reject_{req.id}")
                )

                if data.get('poster_path') and os.path.exists(data['poster_path']):
                    photo = FSInputFile(data['poster_path'])
                    await event.bot.send_photo(event.from_user.id, photo, caption=text, reply_markup=builder.as_markup())
                else:
                    await event.bot.send_message(event.from_user.id, text, reply_markup=builder.as_markup())

# Функция для заявок на редактирование
async def update_edit_requests_message(event: types.Message | types.CallbackQuery):
    async with AsyncSessionLocal() as session:
        edit_requests = (await session.execute(
            select(ConferenceEditRequest).where(ConferenceEditRequest.status == "pending")
        )).scalars().all()

        if not edit_requests:
            text = "Нет заявок на редактирование конференций."
            if isinstance(event, types.Message):
                await event.answer(text)
            else:
                await event.message.edit_text(text)
            return

        await event.bot.send_message(event.from_user.id, "<b>Заявки на редактирование конференций:</b>")
        for req in edit_requests:
            conf = await session.get(Conference, req.conference_id)
            organizer = await session.get(User, req.organizer_id)
            data = req.data

            text = f"ID: <code>{req.id}</code>\n"
            text += f"Конференция: <b>{conf.name}</b>\n"
            text += f"От: {organizer.full_name or organizer.telegram_id}\n\n"
            text += f"<b>Текущие данные:</b>\n"
            text += f"Название: {conf.name}\n"
            if conf.description:
                text += f"Описание: {conf.description}\n"
            text += f"Город: {conf.city or 'Онлайн'}\n"
            text += f"Дата проведения: {conf.date}\n"
            text += f"Орг взнос: {conf.fee} руб.\n\n"
            text += f"<b>Новые данные:</b>\n"
            text += f"Название: {data.get('name', conf.name)}\n"
            if data.get('description') is not None:
                text += f"Описание: {data.get('description') or '(удалено)'}\n"
            text += f"Город: {data.get('city', conf.city)}\n"
            text += f"Дата проведения: {data.get('date', conf.date)}\n"
            text += f"Орг взнос: {data.get('fee', conf.fee)} руб.\n"

            builder = InlineKeyboardBuilder()
            builder.row(
                InlineKeyboardButton(text="Одобрить", callback_data=f"conf_edit_approve_{req.id}"),
                InlineKeyboardButton(text="Отклонить", callback_data=f"conf_edit_reject_{req.id}")
            )

            if data.get('poster_path') and os.path.exists(data['poster_path']):
                photo = FSInputFile(data['poster_path'])
                await event.bot.send_photo(event.from_user.id, photo, caption=text, reply_markup=builder.as_markup())
            else:
                if conf.poster_path and os.path.exists(conf.poster_path):
                    photo = FSInputFile(conf.poster_path)
                    await event.bot.send_photo(event.from_user.id, photo, caption=text, reply_markup=builder.as_markup())
                else:
                    await event.bot.send_message(event.from_user.id, text, reply_markup=builder.as_markup())

# Команда просмотра всех заявок
@router.message(F.text == "📩 Просмотр заявок на конференции")
async def admin_conference_requests(message: types.Message):
    if not await is_admin_or_chief(message.from_user.id):
        await message.answer("Доступ запрещён.")
        return

    async with AsyncSessionLocal() as session:
        create_requests = (await session.execute(
            select(ConferenceCreationRequest).where(ConferenceCreationRequest.status == "pending")
        )).scalars().all()

        if not create_requests:
            await message.answer("Нет заявок на создание.")
            return

        create_pagination[message.from_user.id] = {"requests": create_requests, "index": 0}
        await show_create_request(message, create_requests, 0)

# Кнопка "Посмотреть апелляции"
@router.message(F.text == "📥 Посмотреть апелляции")
async def view_appeals(message: types.Message):
    if not await is_chief_admin(message.from_user.id):
        await message.answer("Доступ только Глав Админу.")
        return

    async with AsyncSessionLocal() as session:
        appeal_requests = (await session.execute(
            select(ConferenceCreationRequest).where(
                ConferenceCreationRequest.status == "rejected",
                ConferenceCreationRequest.appeal == True
            )
        )).scalars().all()

        if not appeal_requests:
            await message.answer("Нет активных апелляций.")
            return

        await message.answer("<b>Активные апелляции:</b>")
        for req in appeal_requests:
            user = await session.get(User, req.user_id)
            data = req.data

            text = f"ID: <code>{req.id}</code> (апелляция)\n"
            text += f"От: {safe(user.full_name) or safe(user.telegram_id)}\n"
            text += f"Название: {safe(data.get('name', '—'))}\n"
            if data.get('description'):
                text += f"Описание: {data.get('description')}\n"
            text += f"Город: {data.get('city')}\n"
            text += f"Дата проведения: {data.get('date')}\n"
            text += f"Орг взнос: {int(data.get('fee', 0))} сом\n"

            builder = InlineKeyboardBuilder()
            builder.row(
                InlineKeyboardButton(text="Одобрить", callback_data=f"conf_appeal_approve_{req.id}"),
                InlineKeyboardButton(text="Отклонить", callback_data=f"conf_appeal_reject_{req.id}")
            )

            if data.get('poster_path') and os.path.exists(data['poster_path']):
                photo = FSInputFile(data['poster_path'])
                await message.answer_photo(photo, caption=text, reply_markup=builder.as_markup())
            else:
                await message.answer(text, reply_markup=builder.as_markup())

# Просмотр всех конференций
@router.message(F.text == "🗂 Все конференции")
async def view_all_conferences(message: types.Message):
    if not await can_view_conferences(message.from_user.id):
        await message.answer("Доступ запрещён.")
        return

    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(Conference)
            .options(joinedload(Conference.organizer))
            .order_by(Conference.id.desc())
        )
        conferences = result.unique().scalars().all()

    if not conferences:
        await message.answer("Нет конференций.")
        return

    user_id = message.from_user.id
    all_conferences_pagination[user_id] = {"conferences": conferences, "index": 0}
    await show_conference_page(message, conferences, 0)

# ====================== ПАГИНАЦИЯ ВСЕХ КОНФЕРЕНЦИЙ ======================
async def show_conference_page(target: types.Message | types.CallbackQuery, conferences: list, index: int):
    conf = conferences[index]
    organizer = conf.organizer
    organizer_name = organizer.full_name or f"ID {organizer.telegram_id}" if organizer else "—"

    text = f"<b>Конференция {index + 1} из {len(conferences)}</b>\n\n"
    text += f"<b>{conf.name}</b> (ID: <code>{conf.id}</code>)\n"
    text += f"Организатор: {organizer_name}\n"
    text += f"Город: {conf.city or 'Онлайн'}\n"
    text += f"Дата: {conf.date}\n"
    text += f"Оргвзнос: {int(conf.fee)} сом\n"
    if conf.description:
        text += f"\nОписание: {conf.description[:300]}..." if len(conf.description) > 300 else f"\nОписание: {conf.description}"

    builder = InlineKeyboardBuilder()

    if await can_delete_conference(target.from_user.id):
        builder.row(InlineKeyboardButton(text="🗑 Удалить конференцию", callback_data=f"admin_delete_conf_{conf.id}"))

    nav = []
    if index > 0:
        nav.append(InlineKeyboardButton(text="◀ Назад", callback_data=f"nav_all_conf_{index - 1}"))
    if index < len(conferences) - 1:
        nav.append(InlineKeyboardButton(text="Вперёд ▶", callback_data=f"nav_all_conf_{index + 1}"))
    if nav:
        builder.row(*nav)

    builder.row(InlineKeyboardButton(text="🔙 Главное меню", callback_data="back_to_main"))

    if conf.poster_path and os.path.exists(conf.poster_path):
        photo = FSInputFile(conf.poster_path)
        if isinstance(target, types.Message):
            await target.answer_photo(photo, caption=text, reply_markup=builder.as_markup())
        else:
            await target.message.edit_media(
                media=types.InputMediaPhoto(media=photo, caption=text),
                reply_markup=builder.as_markup()
            )
    else:
        if isinstance(target, types.Message):
            await target.answer(text, reply_markup=builder.as_markup())
        else:
            await target.message.edit_text(text, reply_markup=builder.as_markup())


@router.callback_query(F.data.startswith("nav_all_conf_"))
async def navigate_all_conferences(callback: types.CallbackQuery):
    index = int(callback.data.split("_")[-1])
    user_id = callback.from_user.id

    if user_id not in all_conferences_pagination:
        await callback.answer("Сессия истекла. Нажми кнопку заново.", show_alert=True)
        return

    data = all_conferences_pagination[user_id]
    if index < 0 or index >= len(data["conferences"]):
        await callback.answer("Конец списка.", show_alert=True)
        return

    data["index"] = index
    await show_conference_page(callback, data["conferences"], index)
    await callback.answer(f"{index + 1}/{len(data['conferences'])}")

# Статистика
@router.message(F.text == "📊 Статистика")
async def stats(message: types.Message):
    if not (await is_admin_or_chief(message.from_user.id) or await is_chief_tech(message.from_user.id)):
        await message.answer("Доступ запрещён.")
        return

    async with AsyncSessionLocal() as session:
        total_users = await session.scalar(select(func.count(User.id)))
        banned_users = await session.scalar(select(func.count(User.id)).where(User.is_banned == True))
        active_users = total_users - banned_users

        active_confs = await session.scalar(
            select(func.count(Conference.id)).where(Conference.is_active == True)
        )
        completed_confs = await session.scalar(
            select(func.count(Conference.id)).where(Conference.is_completed == True)
        )
        total_confs = active_confs + completed_confs

        total_applications = await session.scalar(select(func.count(Application.id)))

        avg_rating = await session.scalar(select(func.avg(ConferenceRating.rating)))
        avg_rating = round(float(avg_rating), 2) if avg_rating else 0.0

        rated_confs = await session.scalar(
            select(func.count(func.distinct(ConferenceRating.conference_id)))
        )

        popular_conf = await session.execute(
            select(Conference.name, func.count(Application.id).label("app_count"))
            .join(Application, Application.conference_id == Conference.id)
            .group_by(Conference.id)
            .order_by(func.count(Application.id).desc())
            .limit(1)
        )
        popular = popular_conf.first()
        popular_text = f"{popular[0]} ({popular[1]} заявок)" if popular else "—"

    text = (
        "📊 <b>Расширенная статистика MUN-Бота</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"👥 Всего пользователей: <b>{total_users}</b>\n"
        f"✅ Активных пользователей: <b>{active_users}</b>\n"
        f"🚫 Забанено: <b>{banned_users}</b>\n\n"
        f"🏛 Всего конференций: <b>{total_confs}</b>\n"
        f"🔴 Активных конференций: <b>{active_confs}</b>\n"
        f"✅ Завершённых конференций: <b>{completed_confs}</b>\n\n"
        f"📄 Всего заявок на участие: <b>{total_applications}</b>\n"
        f"⭐ Средний рейтинг конференций: <b>{avg_rating}</b> ({rated_confs} оценено)\n"
        f"🔥 Самая популярная конференция:\n<b>{popular_text}</b>\n\n"
        f"⏰ Активность за последние 3 часа: пользователи писали в бот\n"
    )

    await message.answer(text, parse_mode="HTML")


# Удаление через кнопку
@router.callback_query(F.data.startswith("admin_delete_conf_"))
async def admin_delete_start(callback: types.CallbackQuery, state: FSMContext):
    if not await can_delete_conference(callback.from_user.id):
        await callback.answer("Доступ запрещён.", show_alert=True)
        return

    conf_id = int(callback.data.split("_")[-1])
    await state.update_data(conf_id=conf_id)
    await state.set_state(AdminStates.delete_conf_reason)

    try:
        await callback.message.delete()
    except Exception:
        pass

    await callback.message.answer(
        f"Введите причину удаления конференции (ID {conf_id}):\n\n"
        "Напишите текст и отправьте.",
        reply_markup=get_cancel_keyboard()
    )
    await callback.answer("Готов к вводу причины")

@router.message(Command("delete_conf"))
async def delete_conference_command(message: types.Message):
    if not await can_delete_conference(message.from_user.id):
        await message.answer("Доступ запрещён.")
        return

    try:
        _, conf_id_str, *reason_parts = message.text.split(maxsplit=2)
        conf_id = int(conf_id_str)
        reason = " ".join(reason_parts).strip()
        if not reason:
            await message.answer("Укажите причину: /delete_conf ID_конференции причина")
            return
    except:
        await message.answer("Формат: /delete_conf ID_конференции причина")
        return

    await perform_conference_deletion(message, conf_id, reason)

@router.message(StateFilter(AdminStates.delete_conf_reason))
async def delete_reason_handler(message: types.Message, state: FSMContext):
    reason = message.text.strip()
    data = await state.get_data()
    conf_id = data["conf_id"]

    await perform_conference_deletion(message, conf_id, reason)
    await state.clear()

async def perform_conference_deletion(target, conf_id: int, reason: str):
    async with AsyncSessionLocal() as session:
        conf = await session.get(Conference, conf_id)
        if not conf:
            await target.answer("Конференция не найдена.")
            return

        organizer = await session.get(User, conf.organizer_id)

        deleted_log = DeletedConference(
            conference_name=conf.name,
            organizer_telegram_id=organizer.telegram_id,
            deleted_by_telegram_id=target.from_user.id,
            reason=reason,
            deleted_at=datetime.now().strftime("%Y-%m-%d %H:%M")
        )
        session.add(deleted_log)

        await session.execute(delete(Application).where(Application.conference_id == conf_id))
        await session.execute(delete(ConferenceEditRequest).where(ConferenceEditRequest.conference_id == conf_id))

        await session.delete(conf)
        await session.commit()

        await log_admin_action(
            admin_id=target.from_user.id,
            admin_username=target.from_user.username,
            action="delete_conference",
            target=f"Конференция ID {conf_id} — {conf.name}",
            details=f"Причина: {reason}"
        )

    await target.answer(f"✅ Конференция <b>{conf.name}</b> удалена по причине: {reason}", parse_mode="HTML")

    try:
        await target.bot.send_message(
            organizer.telegram_id,
            f"❌ Ваша конференция <b>{conf.name}</b> удалена администратором.\nПричина: {reason}"
        )
    except:
        pass

# Обработка создания
@router.callback_query(F.data.startswith("conf_create_approve_") | F.data.startswith("conf_create_reject_"))
async def process_create_request(callback: types.CallbackQuery):
    action = "approve" if "approve" in callback.data else "reject"
    req_id = int(callback.data.split("_")[-1])

    async with AsyncSessionLocal() as session:
        req = await session.get(ConferenceCreationRequest, req_id)
        if not req:
            await callback.answer("Заявка не найдена.")
            return

        user = await session.get(User, req.user_id)
        req_data = req.data

        if action == "approve":
            req.status = "approved"
            user.role = Role.ORGANIZER.value

            conference = Conference(
                name=req_data["name"],
                description=req_data.get("description"),
                city=req_data.get("city"),
                date=req_data.get("date"),
                fee=float(req_data.get("fee", 0)),
                qr_code_path=req_data.get("qr_code_path"),
                poster_path=req_data.get("poster_path"),
                organizer_id=user.id,
                is_active=True
            )
            session.add(conference)
            await session.commit()

            await log_admin_action(
                admin_id=callback.from_user.id,
                admin_username=callback.from_user.username,
                action="approve_conference_creation",
                target=f"Заявка {req_id} — {req_data.get('name', '—')}",
                details="Пользователь стал Организатором"
            )

            await callback.bot.send_message(
                user.telegram_id,
                f"🎉 Ваша заявка на создание конференции <b>{req_data['name']}</b> одобрена!\n\n"
                "Теперь вы — Организатор.\n"
                "Перезапустите бота командой /main_menu."
            )
        else:
            req.status = "rejected"
            await session.commit()

            await log_admin_action(
                admin_id=callback.from_user.id,
                admin_username=callback.from_user.username,
                action="reject_conference_creation",
                target=f"Заявка {req_id} — {req_data.get('name', '—')}",
                details="Заявка отклонена"
            )

            builder = InlineKeyboardBuilder()
            builder.row(
                InlineKeyboardButton(text="Подать апелляцию", callback_data=f"appeal_submit_{req.id}"),
                InlineKeyboardButton(text="Главное меню", callback_data="back_to_main")
            )

            await callback.bot.send_message(
                user.telegram_id,
                f"❌ Ваша заявка на создание конференции <b>{req_data['name']}</b> отклонена.",
                reply_markup=builder.as_markup()
            )

        await callback.answer(f"Заявка {'одобрена' if action == 'approve' else 'отклонена'}")

    try:
        await callback.message.delete()
    except:
        pass

    await update_requests_message(callback)

# Обработка редактирования
@router.callback_query(F.data.startswith("conf_edit_approve_") | F.data.startswith("conf_edit_reject_"))
async def process_edit_request(callback: types.CallbackQuery):
    action = "approve" if "approve" in callback.data else "reject"
    req_id = int(callback.data.split("_")[-1])

    async with AsyncSessionLocal() as session:
        req = await session.get(ConferenceEditRequest, req_id)
        if not req:
            await callback.answer("Заявка не найдена.")
            return

        conf = await session.get(Conference, req.conference_id)
        organizer = await session.get(User, req.organizer_id)
        edit_data = req.data

        if action == "approve":
            conf.name = edit_data.get("name", conf.name)
            conf.description = edit_data.get("description", conf.description)
            conf.city = edit_data.get("city", conf.city)
            conf.date = edit_data.get("date", conf.date)
            conf.fee = edit_data.get("fee", conf.fee)
            if edit_data.get("qr_code_path"):
                conf.qr_code_path = edit_data["qr_code_path"]
            if edit_data.get("poster_path"):
                conf.poster_path = edit_data["poster_path"]

            req.status = "approved"
            await session.commit()

            await log_admin_action(
                admin_id=callback.from_user.id,
                admin_username=callback.from_user.username,
                action="approve_conference_edit",
                target=f"Редактирование конференции {conf.name} (ID {conf.id})",
                details="Изменения применены"
            )

            await callback.bot.send_message(
                organizer.telegram_id,
                f"✅ Ваши изменения в конференции <b>{conf.name}</b> одобрены!"
            )
        else:
            req.status = "rejected"
            await session.commit()

            await log_admin_action(
                admin_id=callback.from_user.id,
                admin_username=callback.from_user.username,
                action="reject_conference_edit",
                target=f"Редактирование конференции {conf.name} (ID {conf.id})",
                details="Изменения отклонены"
            )

            await callback.bot.send_message(
                organizer.telegram_id,
                f"❌ Ваши изменения в конференции <b>{conf.name}</b> отклонены."
            )

        await callback.answer(f"Редактирование {'одобрено' if action == 'approve' else 'отклонено'}")

    try:
        await callback.message.delete()
    except:
        pass

    await update_edit_requests_message(callback)

# Подача апелляции
@router.callback_query(F.data.startswith("appeal_submit_"))
async def appeal_submit(callback: types.CallbackQuery):
    req_id = int(callback.data.split("_")[-1])

    async with AsyncSessionLocal() as session:
        req = await session.get(ConferenceCreationRequest, req_id)
        if not req:
            await callback.answer("Заявка не найдена.")
            return

        req.appeal = True
        await session.commit()

    await callback.message.edit_text("Ваша апелляция отправлена Глав Админу.\nОжидайте решения.")

    for admin_id in CHIEF_ADMIN_IDS:
        try:
            await callback.bot.send_message(admin_id, f"🆕 Новая апелляция! ID: <code>{req_id}</code>")
        except:
            pass

    await callback.answer()

# Возврат в главное меню
@router.callback_query(F.data == "back_to_main")
async def back_to_main(callback: types.CallbackQuery):
    db_user = await get_or_create_user(callback.from_user.id)

    try:
        await callback.message.delete()
    except:
        pass

    await callback.message.answer(
        "Главное меню",
        reply_markup=get_main_menu_keyboard(db_user.role)
    )

    await callback.answer()


@router.callback_query(F.data == "back_to_menu")
async def back_to_menu(callback: types.CallbackQuery):
    db_user = await get_or_create_user(callback.from_user.id)

    try:
        await callback.message.delete()
    except:
        pass

    await callback.message.answer(
        "Главное меню",
        reply_markup=get_main_menu_keyboard(db_user.role)
    )

    await callback.answer()

# Обработка апелляции
@router.callback_query(F.data.startswith("conf_appeal_approve_") | F.data.startswith("conf_appeal_reject_"))
async def process_appeal(callback: types.CallbackQuery):
    if not await is_chief_admin(callback.from_user.id):
        await callback.answer("Доступ только Глав Админу.")
        return

    action = "approve" if "approve" in callback.data else "reject"
    req_id = int(callback.data.split("_")[-1])

    async with AsyncSessionLocal() as session:
        req = await session.get(ConferenceCreationRequest, req_id)
        if not req:
            await callback.answer("Заявка не найдена.")
            return

        user = await session.get(User, req.user_id)
        req_data = req.data

        if action == "approve":
            req.status = "approved"
            user.role = Role.ORGANIZER.value

            conference = Conference(
                name=req_data["name"],
                description=req_data.get("description"),
                city=req_data.get("city"),
                date=req_data.get("date"),
                fee=float(req_data.get("fee", 0)),
                qr_code_path=req_data.get("qr_code_path"),
                poster_path=req_data.get("poster_path"),
                organizer_id=user.id,
                is_active=True
            )
            session.add(conference)
            await session.commit()

            await log_admin_action(
                admin_id=callback.from_user.id,
                admin_username=callback.from_user.username,
                action="approve_appeal",
                target=f"Апелляция {req_id} — {req_data.get('name', '—')}",
                details="Пользователь стал Организатором"
            )

            await callback.bot.send_message(
                user.telegram_id,
                "✅ Ваша апелляция одобрена! Вы стали Организатором."
            )
        else:
            req.appeal = False
            await session.commit()

            await log_admin_action(
                admin_id=callback.from_user.id,
                admin_username=callback.from_user.username,
                action="reject_appeal",
                target=f"Апелляция {req_id} — {req_data.get('name', '—')}",
                details="Апелляция отклонена"
            )

            await callback.bot.send_message(
                user.telegram_id,
                "❌ Ваша апелляция отклонена."
            )

        await callback.answer("Апелляция обработана")

    try:
        await callback.message.delete()
    except:
        pass

    await update_requests_message(callback)

# ====================== ЭКСПОРТ ДАННЫХ БОТА ======================
@router.message(F.text == "📤 Экспорт данных бота")
async def export_bot_data(message: types.Message):
    if message.from_user.id != TECH_SPECIALIST_ID:
        await message.answer("❌ Эта функция доступна только Главному Тех Специалисту.")
        return

    await message.answer("🔄 Подготавливаю полный экспорт данных...\nЭто может занять 5–10 секунд.")

    async with AsyncSessionLocal() as session:
        users = (await session.execute(select(User))).scalars().all()
        users_data = []
        for u in users:
            users_data.append({
                "Telegram ID": u.telegram_id,
                "Username": u.username or "—",
                "Имя": u.full_name or "—",
                "Роль": u.role,
                "Забанен": "Да" if u.is_banned else "Нет",
                "Причина бана": u.ban_reason or "—",
                "Последняя активность": u.last_activity.strftime("%d.%m.%Y %H:%M") if u.last_activity else "—",
                "Возраст": u.age or "—",
                "Email": u.email or "—",
                "Учебное заведение": u.institution or "—"
            })

        conferences = (await session.execute(select(Conference))).scalars().all()
        confs_data = []
        for c in conferences:
            organizer = await session.get(User, c.organizer_id)
            confs_data.append({
                "ID": c.id,
                "Название": c.name,
                "Организатор ID": c.organizer_id,
                "Организатор": organizer.full_name if organizer else "—",
                "Город": c.city or "Онлайн",
                "Дата": c.date,
                "Оргвзнос (сом)": float(c.fee),
                "Активна": "Да" if c.is_active else "Нет",
                "Завершена": "Да" if c.is_completed else "Нет"
            })

        apps_result = await session.execute(
            select(Application)
            .options(joinedload(Application.user), joinedload(Application.conference))
        )
        applications = apps_result.scalars().all()

        apps_data = []
        for a in applications:
            apps_data.append({
                "ID заявки": a.id,
                "Пользователь ID": a.user_id,
                "Пользователь": a.user.full_name if a.user else "—",
                "Конференция": a.conference.name if a.conference else "—",
                "Комитет": a.committee or "—",
                "Статус": a.status,
                "Причина отклонения": a.reject_reason or "—"
            })

    filename = f"mun_bot_full_export_{datetime.now().strftime('%Y%m%d_%H%M')}.xlsx"

    with pd.ExcelWriter(filename, engine="openpyxl") as writer:
        pd.DataFrame(users_data).to_excel(writer, sheet_name="Users", index=False)
        pd.DataFrame(confs_data).to_excel(writer, sheet_name="Conferences", index=False)
        pd.DataFrame(apps_data).to_excel(writer, sheet_name="Applications", index=False)

    with open(filename, "rb") as f:
        await message.answer_document(
            BufferedInputFile(f.read(), filename=filename),
            caption=f"📤 <b>Полный экспорт данных бота</b>\n\n"
                    f"Дата экспорта: {datetime.now().strftime('%d.%m.%Y %H:%M')}\n"
                    f"Пользователей: {len(users_data)}\n"
                    f"Конференций: {len(confs_data)}\n"
                    f"Заявок: {len(apps_data)}"
        )

    try:
        os.remove(filename)
    except:
        pass

# ====================== НАЗНАЧЕНИЕ РОЛИ ======================
@router.message(Command("set_role"))
async def set_user_role(message: types.Message):
    if message.from_user.id != TECH_SPECIALIST_ID:
        await message.answer("🚫 Эта команда доступна только Главному Техническому Специалисту.")
        return

    try:
        _, target, new_role = message.text.split(maxsplit=2)
        new_role = new_role.strip()
    except ValueError:
        await message.answer(
            "❌ Неверный формат команды!\n\n"
            "Используйте:\n"
            "<code>/set_role @username Роль</code>\n"
            "или\n"
            "<code>/set_role ID Роль</code>\n\n"
            "Пример: <code>/set_role @timur Организатор</code>",
            parse_mode="HTML"
        )
        return

    valid_roles = ["Участник", "Организатор", "Админ", "Главный Админ"]
    if new_role not in valid_roles:
        await message.answer(
            f"❌ Неизвестная роль.\n\n"
            f"Доступные роли:\n{', '.join(valid_roles)}",
            parse_mode="HTML"
        )
        return

    async with AsyncSessionLocal() as session:
        if target.startswith("@"):
            username = target[1:].strip()
            result = await session.execute(
                select(User).where(User.username.ilike(username))
            )
        else:
            try:
                tg_id = int(target)
                result = await session.execute(
                    select(User).where(User.telegram_id == tg_id)
                )
            except ValueError:
                await message.answer("❌ ID пользователя должен быть числом.")
                return

        target_user = result.scalar_one_or_none()

        if not target_user:
            await message.answer("❌ Пользователь не найден.")
            return

        if target_user.telegram_id == message.from_user.id:
            await message.answer("🚫 Вы не можете изменить свою собственную роль.")
            return

        old_role = target_user.role
        target_user.role = new_role
        await session.commit()

        await log_admin_action(
            admin_id=message.from_user.id,
            admin_username=message.from_user.username,
            action="set_role",
            target=f"ID {target_user.telegram_id} ({target_user.full_name or '—'})",
            details=f"{old_role} → {new_role}"
        )

        try:
            await message.bot.send_message(
                target_user.telegram_id,
                f"🔑 <b>Ваша роль в боте была изменена</b>\n\n"
                f"Новая роль: <b>{new_role}</b>\n\n"
                f"Нажмите кнопку «🔄 Обновить» в главном меню, чтобы увидеть изменения.",
                parse_mode="HTML"
            )
        except Exception as e:
            logging.error(f"Не удалось уведомить пользователя {target_user.telegram_id}: {e}")

    await message.answer(
        f"✅ Роль успешно изменена!\n\n"
        f"Пользователь: <code>{target_user.telegram_id}</code>\n"
        f"Имя: {target_user.full_name or '—'}\n"
        f"Старая роль: {old_role}\n"
        f"Новая роль: <b>{new_role}</b>",
        parse_mode="HTML"
    )

# ====================== Обращения пользователей (для Глав Тех Специалиста) ======================

@router.message(F.text == "📩 Обращения пользователей")
async def view_support_requests(message: types.Message):
    if not await is_chief_tech(message.from_user.id):
        await message.answer("Доступ запрещён.")
        return

    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(SupportRequest)
            .options(joinedload(SupportRequest.user))
            .order_by(SupportRequest.id.desc())
        )
        requests = result.scalars().all()

    if not requests:
        await message.answer("✅ На данный момент обращений нет.")
        return

    support_pagination[message.from_user.id] = {
        "requests": requests,
        "index": 0
    }

    await show_support_request(message, requests, 0)


async def show_support_request(target: types.Message | types.CallbackQuery, requests: list, index: int):
    req = requests[index]
    user = req.user

    user_name = user.full_name or f"ID {user.telegram_id}" if user else f"ID {req.user_id}"

    text = f"<b>Обращение {index + 1} из {len(requests)}</b>\n\n"
    text += f"<b>ID:</b> <code>{req.id}</code>\n"
    text += f"<b>От:</b> {safe(user_name)}\n"
    text += f"<b>Дата:</b> {req.created_at.strftime('%d.%m.%Y %H:%M') if hasattr(req, 'created_at') and req.created_at else '—'}\n\n"
    text += f"<b>Текст обращения:</b>\n{req.message}\n\n"
    text += f"<b>Статус:</b> {req.status}\n"
    if req.response:
        text += f"\n<b>Ответ:</b>\n{req.response}"

    builder = InlineKeyboardBuilder()

    nav = []
    if index > 0:
        nav.append(InlineKeyboardButton(text="◀ Назад", callback_data=f"nav_support_{index - 1}"))
    if index < len(requests) - 1:
        nav.append(InlineKeyboardButton(text="Вперёд ▶", callback_data=f"nav_support_{index + 1}"))
    if nav:
        builder.row(*nav)

    builder.row(InlineKeyboardButton(text="📩 Ответить", callback_data=f"reply_support_{req.id}"))
    builder.row(InlineKeyboardButton(text="🔙 Главное меню", callback_data="back_to_main"))

    has_photo = bool(req.screenshot_path and os.path.exists(req.screenshot_path))

    if isinstance(target, types.Message):
        if has_photo:
            photo = FSInputFile(req.screenshot_path)
            await target.answer_photo(photo, caption=text, reply_markup=builder.as_markup())
        else:
            await target.answer(text, reply_markup=builder.as_markup())
        return

    message_obj = target.message
    try:
        if has_photo:
            photo = FSInputFile(req.screenshot_path)
            await message_obj.edit_media(
                media=types.InputMediaPhoto(media=photo, caption=text),
                reply_markup=builder.as_markup()
            )
        else:
            await message_obj.edit_text(text, reply_markup=builder.as_markup())
    except TelegramBadRequest as e:
        if "message is not modified" not in str(e).lower():
            try:
                await message_obj.delete()
                if has_photo:
                    photo = FSInputFile(req.screenshot_path)
                    await target.bot.send_photo(
                        message_obj.chat.id,
                        photo,
                        caption=text,
                        reply_markup=builder.as_markup()
                    )
                else:
                    await target.bot.send_message(
                        message_obj.chat.id,
                        text,
                        reply_markup=builder.as_markup()
                    )
            except:
                await target.answer("Не удалось обновить сообщение", show_alert=True)
    except Exception:
        await target.answer("Ошибка при обновлении сообщения", show_alert=True)


@router.callback_query(F.data.startswith("nav_support_"))
async def navigate_support(callback: types.CallbackQuery):
    try:
        index = int(callback.data.split("_")[-1])
    except:
        await callback.answer("Ошибка", show_alert=True)
        return

    user_id = callback.from_user.id
    data = support_pagination.get(user_id)

    if not data or index < 0 or index >= len(data["requests"]):
        await callback.answer("Сессия истекла или конец списка.\nОткройте обращения заново.", show_alert=True)
        return

    data["index"] = index
    await show_support_request(callback, data["requests"], index)
    await callback.answer(f"{index + 1} / {len(data['requests'])}")

# Начало ответа
@router.callback_query(F.data.startswith("reply_support_"))
async def start_reply_support(callback: types.CallbackQuery, state: FSMContext):
    if not await is_chief_tech(callback.from_user.id):
        await callback.answer("Доступ запрещён.", show_alert=True)
        return

    req_id = int(callback.data.split("_")[-1])
    await state.update_data(support_id=req_id)
    await state.set_state(AdminStates.waiting_support_reply)
    await callback.message.answer(
        f"Введите ответ на обращение ID <code>{req_id}</code>:",
        reply_markup=get_cancel_keyboard()
    )
    await callback.answer()

# Обработка ответа на обращение
@router.message(StateFilter(AdminStates.waiting_support_reply))
async def process_support_reply(message: types.Message, state: FSMContext):
    if not await is_chief_tech(message.from_user.id):
        await message.answer("Доступ запрещён.")
        await state.clear()
        return

    data = await state.get_data()
    support_id = data.get("support_id")
    if not support_id:
        await message.answer("Ошибка: ID обращения не найден.")
        await state.clear()
        return

    response_text = message.text.strip()
    if not response_text:
        await message.answer("Ответ не может быть пустым.")
        return

    async with AsyncSessionLocal() as session:
        req_result = await session.execute(select(SupportRequest).where(SupportRequest.id == support_id))
        req = req_result.scalar_one_or_none()
        if not req:
            await message.answer("Обращение не найдено.")
            await state.clear()
            return

        req.response = response_text
        req.status = "answered"
        await session.commit()

        user_result = await session.execute(select(User).where(User.id == req.user_id))
        user = user_result.scalar_one_or_none()

        if user and user.telegram_id:
            try:
                await message.bot.send_message(
                    user.telegram_id,
                    f"📩 <b>Ответ от техподдержки:</b>\n\n{response_text}"
                )
            except Exception as e:
                await message.answer(f"Ответ сохранён, но не удалось отправить пользователю: {e}")
        else:
            await message.answer("Ответ сохранён, но пользователь не найден или заблокировал бота.")

    await message.answer(
        "✅ Ответ успешно отправлен и сохранён.",
        reply_markup=get_main_menu_keyboard("Глав Тех Специалист")
    )
    await state.clear()

# Команда /reply_support ID текст
@router.message(Command("reply_support"))
async def cmd_reply_support(message: types.Message):
    if not await is_chief_tech(message.from_user.id):
        await message.answer("Доступ запрещён.")
        return

    try:
        parts = message.text.split(maxsplit=2)
        if len(parts) < 3:
            raise ValueError
        _, support_id_str, response_text = parts
        support_id = int(support_id_str)
    except:
        await message.answer("Формат: /reply_support ID_обращения текст_ответа")
        return

    if not response_text.strip():
        await message.answer("Текст ответа не может быть пустым.")
        return

    async with AsyncSessionLocal() as session:
        req_result = await session.execute(select(SupportRequest).where(SupportRequest.id == support_id))
        req = req_result.scalar_one_or_none()
        if not req:
            await message.answer("Обращение не найдено.")
            return

        req.response = response_text
        req.status = "answered"
        await session.commit()

        user_result = await session.execute(select(User).where(User.id == req.user_id))
        user = user_result.scalar_one_or_none()

        if user and user.telegram_id:
            try:
                await message.bot.send_message(
                    user.telegram_id,
                    f"📩 <b>Ответ от техподдержки:</b>\n\n{response_text}"
                )
            except Exception as e:
                await message.answer(f"Ответ сохранён, но не удалось отправить: {e}")
        else:
            await message.answer("Ответ сохранён, но пользователь не найден.")

    await message.answer("Ответ отправлен пользователю.")

# Экспорт обращений
@router.message(F.text == "📤 Экспорт обращений")
async def export_support_requests(message: types.Message):
    if not await is_chief_tech(message.from_user.id):
        await message.answer("Доступ запрещён.")
        return

    async with AsyncSessionLocal() as session:
        requests = (await session.execute(select(SupportRequest))).scalars().all()

        if not requests:
            await message.answer("Нет обращений для экспорта.")
            return

        data = []
        for req in requests:
            user = await session.get(User, req.user_id)
            data.append({
                "ID": req.id,
                "ФИО": user.full_name or "—",
                "Telegram ID": user.telegram_id,
                "Текст обращения": req.message,
                "Скриншот (путь)": req.screenshot_path or "—",
                "Статус": req.status,
                "Ответ": req.response or "—"
            })

        df = pd.DataFrame(data)
        filename = "support_requests_export.xlsx"
        df.to_excel(filename, index=False)

        with open(filename, "rb") as f:
            await message.answer_document(
                BufferedInputFile(f.read(), filename=filename),
                caption="📤 Экспорт всех обращений в техподдержку"
            )

        os.remove(filename)

@router.message(Command("backup_db"))
async def backup_db(message: types.Message):
    if message.from_user.id != TECH_SPECIALIST_ID:
        await message.answer("🚫 Доступ запрещён.")
        return

    try:
        with open("mun_bot.db", "rb") as db_file:
            await message.answer_document(
                BufferedInputFile(db_file.read(), filename=f"mun_bot_backup_{datetime.now().strftime('%Y%m%d_%H%M')}.db"),
                caption="✅ Бэкап базы данных mun_bot.db"
            )
        await message.answer("✅ Бэкап успешно отправлен!")
    except FileNotFoundError:
        await message.answer("❌ Ошибка: файл базы не найден (mun_bot.db).")
    except Exception as e:
        await message.answer(f"❌ Ошибка при отправке бэкапа: {e}")

@router.message(F.text == "✏️ Заявки на редактирование")
async def admin_edit_requests(message: types.Message):
    if not await is_admin_or_chief(message.from_user.id):
        await message.answer("Доступ запрещён.")
        return

    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(ConferenceEditRequest)
            .options(
                joinedload(ConferenceEditRequest.conference),
                joinedload(ConferenceEditRequest.organizer)
            )
            .where(ConferenceEditRequest.status == "pending")
            .order_by(ConferenceEditRequest.id.desc())
        )
        requests = result.unique().scalars().all()

        if not requests:
            await message.answer("Нет активных заявок на редактирование.")
            return

        user_id = message.from_user.id
        edit_pagination[user_id] = {"requests": requests, "index": 0}
        await show_edit_request(message, requests, 0)

@router.callback_query(F.data.startswith("edit_approve_"))
async def approve_edit(callback: types.CallbackQuery):
    req_id = int(callback.data.split("_")[-1])
    user_id = callback.from_user.id

    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(ConferenceEditRequest)
            .options(joinedload(ConferenceEditRequest.conference),
                     joinedload(ConferenceEditRequest.organizer))
            .where(ConferenceEditRequest.id == req_id)
        )
        req = result.unique().scalar_one_or_none()
        if not req:
            await callback.answer("Заявка не найдена.", show_alert=True)
            return

        conf = req.conference
        organizer = req.organizer
        changes = req.data.get("changes", {})

        for field, value in changes.items():
            if field in ["qr", "poster"]:
                field_name = "qr_code_path" if field == "qr" else "poster_path"
                setattr(conf, field_name, value)
            else:
                setattr(conf, field, value)

        req.status = "approved"
        await session.commit()

        await log_admin_action(
            admin_id=callback.from_user.id,
            admin_username=callback.from_user.username,
            action="approve_conference_edit",
            target=f"Редактирование конференции {conf.name} (ID {conf.id})",
            details="Изменения применены"
        )

        try:
            await callback.bot.send_message(
                organizer.telegram_id,
                f"✅ Изменения в конференции <b>{conf.name}</b> одобрены!"
            )
        except:
            pass

    if user_id in edit_pagination:
        data = edit_pagination[user_id]
        data["requests"] = [r for r in data["requests"] if r.id != req_id]

        if not data["requests"]:
            await callback.message.delete()
            await callback.message.answer(
                "✅ Все заявки на редактирование обработаны!",
                reply_markup=get_main_menu_keyboard("Админ")
            )
            del edit_pagination[user_id]
            await callback.answer("✅ Одобрено!")
            return

        if data["index"] >= len(data["requests"]):
            data["index"] = max(0, len(data["requests"]) - 1)

        await show_edit_request(callback, data["requests"], data["index"])
    else:
        await callback.message.edit_text("✅ Заявка одобрена.")

    await callback.answer("✅ Одобрено!")


@router.callback_query(F.data.startswith("edit_reject_"))
async def reject_edit(callback: types.CallbackQuery):
    req_id = int(callback.data.split("_")[-1])
    user_id = callback.from_user.id

    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(ConferenceEditRequest)
            .options(joinedload(ConferenceEditRequest.organizer))
            .where(ConferenceEditRequest.id == req_id)
        )
        req = result.unique().scalar_one_or_none()
        if not req:
            await callback.answer("Заявка не найдена.", show_alert=True)
            return

        organizer = req.organizer
        req.status = "rejected"
        await session.commit()

        await log_admin_action(
            admin_id=callback.from_user.id,
            admin_username=callback.from_user.username,
            action="reject_conference_edit",
            target=f"Редактирование конференции {req.conference.name} (ID {req.conference_id})",
            details="Изменения отклонены"
        )

        try:
            await callback.bot.send_message(
                organizer.telegram_id,
                f"❌ Заявка на редактирование конференции <b>{req.conference.name}</b> отклонена."
            )
        except:
            pass

    if user_id in edit_pagination:
        data = edit_pagination[user_id]
        data["requests"] = [r for r in data["requests"] if r.id != req_id]

        if not data["requests"]:
            await callback.message.delete()
            await callback.message.answer(
                "✅ Все заявки обработаны!",
                reply_markup=get_main_menu_keyboard("Админ")
            )
            del edit_pagination[user_id]
            await callback.answer("✅ Отклонено!")
            return

        if data["index"] >= len(data["requests"]):
            data["index"] = max(0, len(data["requests"]) - 1)

        await show_edit_request(callback, data["requests"], data["index"])
    else:
        await callback.message.edit_text("✅ Заявка отклонена.")

    await callback.answer("✅ Отклонено!")

async def show_edit_request(target, requests: list, index: int):
    req = requests[index]
    conf = req.conference
    org = req.organizer
    changes = req.data.get("changes", {})

    text = f"<b>Заявка на редактирование {index + 1} из {len(requests)}</b>\n\n"
    text += f"ID: <code>{req.id}</code>\nКонференция: <b>{conf.name}</b>\nОрганизатор: {org.full_name or org.telegram_id}\n\n"
    text += "<b>Изменения:</b>\n"
    for field, value in changes.items():
        original = req.data.get("original", {}).get(field, "—")
        text += f"• {field.capitalize()}: {original} → <b>{value or 'удалить'}</b>\n"

    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="✅ Одобрить", callback_data=f"edit_approve_{req.id}"),
        InlineKeyboardButton(text="❌ Отклонить", callback_data=f"edit_reject_{req.id}")
    )
    nav = []
    if index > 0:
        nav.append(InlineKeyboardButton(text="◀ Назад", callback_data=f"nav_edit_{index-1}"))
    if index < len(requests) - 1:
        nav.append(InlineKeyboardButton(text="Вперёд ▶", callback_data=f"nav_edit_{index+1}"))
    if nav:
        builder.row(*nav)
    builder.row(InlineKeyboardButton(text="🔙 В меню", callback_data="back_to_menu"))

    photo_path = None
    if "poster" in changes and changes["poster"] and os.path.exists(changes["poster"]):
        photo_path = changes["poster"]
    elif conf.poster_path and os.path.exists(conf.poster_path):
        photo_path = conf.poster_path

    if photo_path:
        photo = FSInputFile(photo_path)
        if isinstance(target, types.Message):
            await target.answer_photo(photo, caption=text, reply_markup=builder.as_markup())
        else:
            await target.message.edit_media(
                media=types.InputMediaPhoto(media=photo, caption=text),
                reply_markup=builder.as_markup()
            )
    else:
        if isinstance(target, types.Message):
            await target.answer(text, reply_markup=builder.as_markup())
        else:
            await target.message.edit_text(text, reply_markup=builder.as_markup())


@router.callback_query(F.data.startswith("nav_edit_"))
async def navigate_edit(callback: types.CallbackQuery):
    index = int(callback.data.split("_")[-1])
    user_id = callback.from_user.id

    if user_id not in edit_pagination:
        await callback.answer("Сессия истекла. Нажмите кнопку заново.", show_alert=True)
        return

    data = edit_pagination[user_id]
    if index < 0 or index >= len(data["requests"]):
        await callback.answer("Конец списка.", show_alert=True)
        return

    data["index"] = index
    await show_edit_request(callback, data["requests"], index)
    await callback.answer()

async def show_create_request(target, requests: list, index: int):
    req = requests[index]

    async with AsyncSessionLocal() as session:
        user = await session.get(User, req.user_id)

    data = req.data

    text = f"<b>Заявка на создание {index + 1} из {len(requests)}</b>\n\n"
    text += f"ID: <code>{req.id}</code>\nОт: {user.full_name or user.telegram_id}\n\n"
    text += f"<b>Название:</b> {data.get('name')}\n"
    text += f"<b>Описание:</b>\n{data.get('description', '—')}\n\n"
    text += f"<b>Город:</b> {data.get('city', 'Онлайн')}\n"
    text += f"<b>Дата:</b> {data.get('date')}\n"
    text += f"<b>Орг взнос:</b> {int(data.get('fee', 0))} сом"

    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="✅ Одобрить", callback_data=f"conf_create_approve_{req.id}"),
        InlineKeyboardButton(text="❌ Отклонить", callback_data=f"conf_create_reject_{req.id}")
    )
    nav = []
    if index > 0:
        nav.append(InlineKeyboardButton(text="◀ Назад", callback_data=f"nav_create_{index-1}"))
    if index < len(requests) - 1:
        nav.append(InlineKeyboardButton(text="Вперёд ▶", callback_data=f"nav_create_{index+1}"))
    if nav:
        builder.row(*nav)
    builder.row(InlineKeyboardButton(text="🔙 В меню", callback_data="back_to_menu"))

    if data.get('poster_path') and os.path.exists(data['poster_path']):
        photo = FSInputFile(data['poster_path'])
        if isinstance(target, types.Message):
            await target.answer_photo(photo, caption=text, reply_markup=builder.as_markup())
        else:
            await target.message.edit_media(
                media=types.InputMediaPhoto(media=photo, caption=text),
                reply_markup=builder.as_markup()
            )
    else:
        if isinstance(target, types.Message):
            await target.answer(text, reply_markup=builder.as_markup())
        else:
            await target.message.edit_text(text, reply_markup=builder.as_markup())

@router.callback_query(F.data.startswith("nav_create_"))
async def navigate_create(callback: types.CallbackQuery):
    index = int(callback.data.split("_")[-1])
    user_id = callback.from_user.id
    if user_id not in create_pagination:
        await callback.answer("Сессия истекла.")
        return
    data = create_pagination[user_id]
    data["index"] = index
    await show_create_request(callback, data["requests"], index)
    await callback.answer()

@router.message(F.text == "👥 Все пользователи")
async def all_users_list(message: types.Message):
    user_id = message.from_user.id

    if user_id not in CHIEF_ADMIN_IDS and user_id != TECH_SPECIALIST_ID:
        async with AsyncSessionLocal() as session:
            result = await session.execute(
                select(User.role).where(User.telegram_id == user_id)
            )
            role = result.scalar_one_or_none()

        if role != Role.ADMIN.value:
            await message.answer("🚫 У вас нет доступа к этому разделу.")
            return

    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(User)
            .order_by(User.last_activity.desc().nulls_last(), User.telegram_id)
        )
        users = result.scalars().all()

    await show_all_users(message, users, 0)


async def show_all_users(message: types.Message, users: list, page: int = 0):
    if not users:
        await message.answer("👥 Пользователей пока нет.")
        return

    ITEMS_PER_PAGE = 5
    total_pages = (len(users) - 1) // ITEMS_PER_PAGE + 1
    start = page * ITEMS_PER_PAGE
    end = start + ITEMS_PER_PAGE

    text = "👥 <b>Все пользователи</b>\n\n"
    shown_count = 0

    builder = InlineKeyboardBuilder()

    for i, user in enumerate(users[start:end]):
        if (user.telegram_id == TECH_SPECIALIST_ID or user.telegram_id in CHIEF_ADMIN_IDS):
            if message.from_user.id != TECH_SPECIALIST_ID and message.from_user.id not in CHIEF_ADMIN_IDS:
                continue

        status = "🚫 Забанен" if user.is_banned else "✅ Активен"
        role_name = {
            Role.PARTICIPANT.value: "Участник",
            Role.ORGANIZER.value: "Организатор",
            Role.ADMIN.value: "Админ",
            Role.CHIEF_ADMIN.value: "Главный Админ",
            Role.CHIEF_TECH.value: "Глав Тех Специалист"
        }.get(user.role, user.role)

        text += (
            f"🆔 <b>ID:</b> <code>{user.telegram_id}</code>\n"
            f"👤 <b>Имя:</b> {safe(user.full_name or '—')}\n"
            f"📛 <b>Username:</b> @{safe(user.username or '—')}\n"
            f"🔰 <b>Роль:</b> {role_name}\n"
            f"📊 <b>Статус:</b> {status}\n"
            f"⏰ <b>Последняя активность:</b> {user.last_activity.strftime('%d.%m.%Y %H:%M') if user.last_activity else '—'}\n"
            f"💬 <b>Сообщений за 3 часа:</b> {user.messages_last_3h}\n"
            f"{'─' * 30}\n\n"
        )

        if user.is_banned:
            builder.button(text=f"✅ Разбанить {user.telegram_id}", callback_data=f"unban_user_{user.telegram_id}")
        else:
            builder.button(text=f"🚫 Забанить {user.telegram_id}", callback_data=f"ban_user_{user.telegram_id}")

        shown_count += 1

    if shown_count == 0:
        text += "В этом разделе пока ничего нет.\n"

    text += f"\nСтраница <b>{page + 1}</b> из <b>{total_pages}</b>"

    if page > 0:
        builder.button(text="⬅️ Назад", callback_data=f"all_users_page_{page-1}")
    if page < total_pages - 1:
        builder.button(text="Вперёд ➡️", callback_data=f"all_users_page_{page+1}")

    builder.button(text="🏠 Главное меню", callback_data="back_to_main")
    builder.adjust(1)

    await message.answer(text, reply_markup=builder.as_markup(), parse_mode="HTML")


@router.callback_query(F.data.startswith("all_users_page_"))
async def navigate_all_users(callback: types.CallbackQuery):
    page = int(callback.data.split("_")[-1])

    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(User)
            .order_by(User.last_activity.desc().nulls_last(), User.telegram_id)
        )
        users = result.scalars().all()

    await show_all_users(callback.message, users, page)
    await callback.answer()


# ====================== ОБРАБОТКА КНОПОК БАН/РАЗБАН ======================

@router.callback_query(F.data.startswith("ban_user_"))
async def ban_from_users_list(callback: types.CallbackQuery, state: FSMContext):
    if not (await is_admin_or_chief(callback.from_user.id) or await is_chief_tech(callback.from_user.id)):
        await callback.answer("Доступ запрещён.", show_alert=True)
        return

    target_id = int(callback.data.split("_")[-1])

    if target_id == callback.from_user.id:
        await callback.answer("❌ Вы не можете забанить самого себя!", show_alert=True)
        return

    await state.update_data(target=target_id, action="ban")
    await state.set_state(BanReasonState.reason)

    await callback.message.answer(
        f"Введите причину бана пользователя <code>{target_id}</code>:",
        reply_markup=get_cancel_keyboard(),
        parse_mode="HTML"
    )
    await callback.answer()


@router.callback_query(F.data.startswith("unban_user_"))
async def unban_from_users_list(callback: types.CallbackQuery, state: FSMContext):
    if not (await is_admin_or_chief(callback.from_user.id) or await is_chief_tech(callback.from_user.id)):
        await callback.answer("Доступ запрещён.", show_alert=True)
        return

    target_id = int(callback.data.split("_")[-1])

    if target_id == callback.from_user.id:
        await callback.answer("❌ Вы не можете разбанить самого себя!", show_alert=True)
        return

    await state.update_data(target=target_id, action="unban")
    await state.set_state(BanReasonState.reason)

    await callback.message.answer(
        f"Введите причину разбана пользователя <code>{target_id}</code>:",
        reply_markup=get_cancel_keyboard(),
        parse_mode="HTML"
    )
    await callback.answer()

# ====================== ЛОГИ ДЕЙСТВИЙ АДМИНОВ ======================
@router.message(F.text == "📜 Логи действий")
async def show_admin_logs(message: types.Message):
    if message.from_user.id != TECH_SPECIALIST_ID:
        await message.answer("❌ Доступно только Главному Тех Специалисту.")
        return

    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(AdminActionLog)
            .order_by(AdminActionLog.created_at.desc())
            .limit(50)
        )
        logs = result.scalars().all()

    if not logs:
        await message.answer("📭 Логов пока нет.")
        return

    text = "<b>📜 Последние 50 действий админов</b>\n\n"
    for log in logs:
        time_str = log.created_at.strftime("%d.%m %H:%M")
        text += f"<b>{time_str}</b> | <code>{log.admin_username or log.admin_id}</code>\n"
        text += f"   {log.action} → {log.target}\n"
        if log.details:
            text += f"   Причина: {log.details}\n"
        text += "\n"

    await message.answer(text)
