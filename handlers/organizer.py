from aiogram import Router, types, F
from aiogram.filters import Command
from aiogram.types import FSInputFile, InlineKeyboardButton, BufferedInputFile, InlineKeyboardMarkup, InputMediaPhoto
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.fsm.context import FSMContext
from sqlalchemy import select, func, delete
from sqlalchemy.orm import joinedload
from datetime import datetime, timedelta
import os
import pandas as pd
import logging

from utils import safe, log_admin_action   # ← измени эту строку
from database import AsyncSessionLocal, Conference, Application, User, Role, ConferenceEditRequest, ConferenceRating
from keyboards import get_main_menu_keyboard, get_cancel_keyboard
from states import RejectReason, EditConference, Broadcast
from config import CHIEF_ADMIN_IDS, TECH_SPECIALIST_ID

router = Router()

PAYMENTS_DIR = "payments"
os.makedirs(PAYMENTS_DIR, exist_ok=True)
os.makedirs("qr_codes", exist_ok=True)
os.makedirs("posters", exist_ok=True)

pagination = {}
last_my_conferences_msg = {}

logger = logging.getLogger(__name__)


# ====================== ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ======================
async def is_active_organizer(user_id: int) -> bool:
    if user_id == TECH_SPECIALIST_ID:
        return True

    async with AsyncSessionLocal() as session:
        result = await session.execute(select(User).where(User.telegram_id == user_id))
        user = result.scalar_one_or_none()
        if not user:
            return False
        return user.role == Role.ORGANIZER.value and not user.is_banned


async def get_applications(user_id: int, mode: str):
    if not await is_active_organizer(user_id):
        return []

    async with AsyncSessionLocal() as session:
        organizer_result = await session.execute(select(User).where(User.telegram_id == user_id))
        organizer = organizer_result.scalar_one_or_none()
        if not organizer:
            return []

        conf_result = await session.execute(select(Conference).where(Conference.organizer_id == organizer.id))
        conf_ids = [c.id for c in conf_result.scalars().all()]
        if not conf_ids:
            return []

        query = select(Application).options(
            joinedload(Application.user),
            joinedload(Application.conference)
        ).where(Application.conference_id.in_(conf_ids))

        if mode == "current":
            query = query.where(Application.status.in_(["pending", "payment_pending", "payment_sent", "confirmed"]))
        else:
            query = query.where(Application.status.in_(["approved", "rejected", "link_sent"]))

        result = await session.execute(query.order_by(Application.id))
        return result.unique().scalars().all()


def build_keyboard(app_id: int, index: int, total: int, mode: str):
    builder = InlineKeyboardBuilder()

    if mode == "current":
        builder.row(
            InlineKeyboardButton(text="✅ Принять", callback_data=f"approve_{app_id}"),
            InlineKeyboardButton(text="❌ Отклонить", callback_data=f"reject_{app_id}")
        )

    nav = []
    if index > 0:
        nav.append(InlineKeyboardButton(text="◀ Назад", callback_data=f"nav_org_{mode}_{index - 1}"))
    if index < total - 1:
        nav.append(InlineKeyboardButton(text="▶ Вперёд", callback_data=f"nav_org_{mode}_{index + 1}"))
    if nav:
        builder.row(*nav)

    export_text = "📊 Экспорт текущих" if mode == "current" else "📊 Экспорт архива"
    builder.row(InlineKeyboardButton(text=export_text, callback_data=f"export_{mode}"))
    builder.row(InlineKeyboardButton(text="🔙 Главное меню", callback_data="back_to_menu_org"))
    return builder.as_markup()


async def show_application(target, apps: list, index: int, mode: str):
    if not apps:
        text = "Нет текущих заявок." if mode == "current" else "Архив пуст."
        if isinstance(target, types.Message):
            await target.answer(text, reply_markup=get_main_menu_keyboard("Организатор"))
        else:
            await target.message.edit_text(text, reply_markup=get_main_menu_keyboard("Организатор"))
        return

    app = apps[index]
    conf = app.conference
    participant = app.user

    text = f"<b>Заявка {index + 1} из {len(apps)}</b>\n\n"
    text += f"<b>🎯 Конференция:</b> {conf.name}\n"
    text += f"<b>ID заявки:</b> <code>{app.id}</code>\n\n"
    text += f"<b>👤 Анкета участника:</b>\n"
    text += f"• ФИО: {participant.full_name or 'Не указано'}\n"
    text += f"• Возраст: {participant.age or '—'}\n"
    text += f"• Email: {participant.email or '—'}\n"
    text += f"• Учебное заведение: {participant.institution or '—'}\n"
    text += f"• Опыт в MUN: {participant.experience or 'Нет'}\n"
    text += f"• Комитет: {app.committee or '—'}\n\n"
    text += f"<b>📊 Статус:</b> {app.status}"
    if app.reject_reason:
        text += f"\n<b>❌ Причина отклонения:</b> {app.reject_reason}"

    keyboard = build_keyboard(app.id, index, len(apps), mode)

    if isinstance(target, types.Message):
        await target.answer(text, reply_markup=keyboard)
    else:
        await target.message.edit_text(text, reply_markup=keyboard)


# ====================== ОСНОВНЫЕ ОБРАБОТЧИКИ ======================

@router.message(F.text == "📋 Мои конференции")
@router.callback_query(F.data == "back_to_my_conf")
async def my_conferences(event: types.Message | types.CallbackQuery):
    user_id = event.from_user.id if isinstance(event, types.Message) else event.from_user.id
    target = event if isinstance(event, types.Message) else event.message

    async with AsyncSessionLocal() as session:
        organizer = await session.execute(select(User).where(User.telegram_id == user_id))
        organizer = organizer.scalar_one_or_none()

        if not organizer or organizer.role != Role.ORGANIZER.value:
            text = "У вас нет роли Организатора."
            if isinstance(event, types.CallbackQuery):
                await target.edit_text(text)
            else:
                await target.answer(text)
            return

        conf_result = await session.execute(select(Conference).where(Conference.organizer_id == organizer.id))
        conf = conf_result.scalar_one_or_none()

        if not conf:
            text = "У вас пока нет созданных конференций."
            if isinstance(event, types.CallbackQuery):
                await target.edit_text(text)
            else:
                await target.answer(text)
            return

        text = f"<b>Кабинет Организатора</b>\n\n"
        text += f"Конференция: <b>{conf.name}</b>\n"
        text += f"Город: {conf.city or 'Онлайн'}\n"
        text += f"Дата: {conf.date}\n"
        text += f"Орг взнос: {int(conf.fee)} сом\n"
        if conf.is_completed:
            text += "\n<i>Конференция завершена</i>\n"
        elif not conf.is_active:
            text += "\n<i>Конференция неактивна</i>\n"

        builder = InlineKeyboardBuilder()
        builder.row(InlineKeyboardButton(text="ℹ️ Информация о конференции", callback_data=f"org_conf_info_{conf.id}"))
        builder.row(InlineKeyboardButton(text="👥 Список участников", callback_data=f"org_participants_{conf.id}"))
        builder.row(InlineKeyboardButton(text="⭐ Рейтинг конференции", callback_data=f"org_rating_{conf.id}"))
        builder.row(InlineKeyboardButton(text="✏️ Редактировать конференцию", callback_data=f"org_edit_request_{conf.id}"))
        builder.row(InlineKeyboardButton(text="🗑 Удалить конференцию", callback_data=f"org_delete_{conf.id}"))
        builder.row(InlineKeyboardButton(text="🔙 Главное меню", callback_data="back_to_menu_org"))

        if isinstance(event, types.CallbackQuery):
            await target.edit_text(text, reply_markup=builder.as_markup())
        else:
            await target.answer(text, reply_markup=builder.as_markup())


@router.callback_query(F.data.startswith("nav_org_"))
async def navigate(callback: types.CallbackQuery):
    if not await is_active_organizer(callback.from_user.id):
        await callback.answer("🚫 Доступ запрещён: вы заблокированы.", show_alert=True)
        return

    parts = callback.data.split("_")
    mode = parts[2]
    index = int(parts[3])

    user_id = callback.from_user.id
    pagination[user_id] = {"mode": mode, "index": index}

    apps = await get_applications(user_id, mode)
    await show_application(callback, apps, index, mode)
    await callback.answer()


@router.message(F.text == "📩 Заявки участников")
async def current_applications(message: types.Message):
    if not await is_active_organizer(message.from_user.id):
        await message.answer("🚫 Доступ запрещён: вы заблокированы или не являетесь Организатором.")
        return

    apps = await get_applications(message.from_user.id, "current")
    pagination[message.from_user.id] = {"mode": "current", "index": 0}
    await show_application(message, apps, 0, "current")


@router.message(F.text == "🗃 Архив заявок")
async def archive_applications(message: types.Message):
    user_id = message.from_user.id
    async with AsyncSessionLocal() as session:
        confs = await session.execute(
            select(Conference.id).where(Conference.organizer_id == (await session.execute(
                select(User.id).where(User.telegram_id == user_id)
            )).scalar_one())
        )
        conf_ids = [row[0] for row in confs.all()]

        if not conf_ids:
            await message.answer("У вас нет конференций.")
            return

        apps = await session.execute(
            select(Application)
            .options(joinedload(Application.user))
            .where(Application.conference_id.in_(conf_ids))
            .where(Application.status.in_(["approved", "rejected", "link_sent", "confirmed"]))
            .order_by(Application.status.desc(), Application.id.desc())
        )
        applications = apps.scalars().all()

        if not applications:
            await message.answer("Архив заявок пуст.")
            return

        text = "<b>Архив заявок</b>\n\n"
        for app in applications:
            u = app.user
            emoji = {"approved": "✅", "rejected": "❌", "link_sent": "🔗", "confirmed": "🎟"}.get(app.status, "❓")
            text += f"{emoji} {u.full_name or u.telegram_id}\n"
            text += f"   Статус: {app.status}\n"
            text += f"   Комитет: {app.committee or '—'}\n\n"

        builder = InlineKeyboardBuilder()
        builder.row(InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_menu_org"))

        await message.answer(text, reply_markup=builder.as_markup())


@router.callback_query(F.data.startswith("approve_"))
async def approve_application(callback: types.CallbackQuery):
    if not await is_active_organizer(callback.from_user.id):
        await callback.answer("🚫 Доступ запрещён: вы заблокированы.", show_alert=True)
        return

    app_id = int(callback.data.split("_")[1])

    async with AsyncSessionLocal() as session:
        app = await session.get(Application, app_id)
        if not app:
            await callback.answer("Заявка не найдена.")
            return

        app.status = "approved"
        await session.commit()

        conf = await session.get(Conference, app.conference_id)
        participant = await session.get(User, app.user_id)

        # Логирование
        await log_admin_action(
            admin_id=callback.from_user.id,
            admin_username=callback.from_user.username,
            action="approve_application",
            target=f"Заявка {app_id} на конференцию {conf.name}",
            details=f"Участник: {participant.full_name or participant.telegram_id}"
        )

        # Красивое уведомление участнику
        notify_text = (
            f"🎉 <b>Поздравляем! Заявка одобрена</b>\n\n"
            f"🏛 <b>{safe(conf.name)}</b>\n"
            f"📍 {conf.city or 'Онлайн'} • 📅 {conf.date}\n\n"
        )

        if conf.fee > 0:
            notify_text += f"💰 Оргвзнос: <b>{int(conf.fee)} сом</b>\n"
            notify_text += "Оплатите по QR-коду и пришлите скриншот боту."
        else:
            notify_text += "✅ Участие бесплатное."

        builder = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="✅ Подтвердить участие", callback_data=f"confirm_part_{app.id}")
        ]])

        try:
            await callback.bot.send_message(
                participant.telegram_id,
                notify_text,
                reply_markup=builder,
                parse_mode="HTML"
            )
        except Exception as e:
            logging.error(f"Не удалось отправить уведомление участнику {participant.telegram_id}: {e}")

        await callback.answer("✅ Заявка одобрена и участник уведомлён.")

        # Обновляем список заявок
        user_id = callback.from_user.id
        state = pagination.get(user_id, {"mode": "current", "index": 0})
        apps = await get_applications(user_id, state["mode"])
        if apps and state["index"] < len(apps):
            await show_application(callback, apps, state["index"], state["mode"])


@router.callback_query(F.data.startswith("reject_"))
async def start_reject(callback: types.CallbackQuery, state: FSMContext):
    if not await is_active_organizer(callback.from_user.id):
        await callback.answer("🚫 Доступ запрещён: вы заблокированы.", show_alert=True)
        return

    app_id = int(callback.data.split("_")[1])
    await state.update_data(app_id=app_id)
    await state.set_state(RejectReason.waiting)
    await callback.message.answer("📝 Введите причину отклонения:", reply_markup=get_cancel_keyboard())
    await callback.answer()


@router.message(RejectReason.waiting)
async def save_reject_reason(message: types.Message, state: FSMContext):
    if not await is_active_organizer(message.from_user.id):
        await message.answer("🚫 Доступ запрещён: вы заблокированы.")
        await state.clear()
        return

    data = await state.get_data()
    app_id = data["app_id"]

    async with AsyncSessionLocal() as session:
        app = await session.get(Application, app_id)
        if not app:
            await message.answer("Заявка не найдена.")
            await state.clear()
            return

        app.status = "rejected"
        app.reject_reason = message.text.strip()
        await session.commit()

        conf = await session.get(Conference, app.conference_id)
        participant = await session.get(User, app.user_id)

        # Логирование
        await log_admin_action(
            admin_id=message.from_user.id,
            admin_username=message.from_user.username,
            action="reject_application",
            target=f"Заявка {app_id} на конференцию {conf.name}",
            details=f"Причина: {message.text.strip()}"
        )

        # === УЛУЧШЕННОЕ УВЕДОМЛЕНИЕ ПРИ ОТКЛОНЕНИИ ===
        notify_text = (
            f"❌ <b>Ваша заявка отклонена</b>\n\n"
            f"🏛 Конференция: <b>{safe(conf.name)}</b>\n"
            f"📅 Дата: {conf.date}\n\n"
            f"<b>Причина отклонения:</b>\n"
            f"{safe(message.text.strip())}\n\n"
            f"Если у вас есть вопросы — напишите организатору конференции "
            f"или обратитесь в техподдержку бота."
        )

        try:
            await message.bot.send_message(
                participant.telegram_id,
                notify_text,
                parse_mode="HTML"
            )
        except:
            pass

    await message.answer(
        "✅ Заявка отклонена и участник уведомлён.",
        reply_markup=get_main_menu_keyboard("Организатор")
    )
    await state.clear()


@router.callback_query(F.data.startswith("confirm_part_"))
async def confirm_participation(callback: types.CallbackQuery):
    app_id = int(callback.data.split("_")[-1])

    async with AsyncSessionLocal() as session:
        app = await session.get(Application, app_id)
        if not app:
            await callback.answer("Заявка не найдена.")
            return

        conf = await session.get(Conference, app.conference_id)
        participant = await session.get(User, app.user_id)
        organizer = await session.get(User, conf.organizer_id)

        if conf.fee > 0:
            app.status = "payment_pending"
            await session.commit()

            # === УЛУЧШЕННОЕ УВЕДОМЛЕНИЕ ДЛЯ ПЛАТНЫХ КОНФЕРЕНЦИЙ ===
            text = (
                f"💳 <b>Конференция платная — подтвердите оплату</b>\n\n"
                f"🎉 Поздравляем! Вы прошли отбор на конференцию:\n"
                f"<b>{safe(conf.name)}</b>\n"
                f"📍 {conf.city or 'Онлайн'} | 📅 {conf.date}\n\n"
                f"Оргвзнос: <b>{int(conf.fee)} сом</b>\n\n"
                f"Оплатите по QR-коду (если он был отправлен организатором) "
                f"и отправьте скриншот чека этому боту."
            )

            if conf.qr_code_path and os.path.exists(conf.qr_code_path):
                photo = FSInputFile(conf.qr_code_path)
                await callback.bot.send_photo(participant.telegram_id, photo, caption=text)
            else:
                await callback.bot.send_message(participant.telegram_id, text)

            await callback.bot.send_message(
                participant.telegram_id,
                "📸 Отправьте скриншот оплаты:"
            )

        else:
            app.status = "confirmed"
            await session.commit()

            # === УЛУЧШЕННОЕ УВЕДОМЛЕНИЕ ДЛЯ БЕСПЛАТНЫХ КОНФЕРЕНЦИЙ ===
            await callback.bot.send_message(
                participant.telegram_id,
                f"✅ <b>Участие успешно подтверждено!</b>\n\n"
                f"🏛 Конференция: <b>{safe(conf.name)}</b>\n"
                f"📍 {conf.city or 'Онлайн'} | 📅 {conf.date}\n\n"
                f"🔗 Ожидайте ссылку на чат комитета от организатора в ближайшее время.\n\n"
                f"Удачи на конференции! 🚀",
                reply_markup=get_main_menu_keyboard("Участник")
            )

            # Уведомление организатору
            organizer_text = (
                f"✅ <b>Участник подтвердил участие</b>\n\n"
                f"👤 {safe(participant.full_name or f'ID {participant.telegram_id}')}\n"
                f"📋 ID заявки: <code>{app.id}</code>\n\n"
                f"Отправьте ссылку на чат командой:\n"
                f"<code>/verify {app.id} [ссылка]</code>"
            )
            try:
                await callback.bot.send_message(organizer.telegram_id, organizer_text)
            except:
                pass

    await callback.answer("✅ Участие подтверждено!")


@router.message(Command("verify"))
async def verify_payment(message: types.Message):
    if not await is_active_organizer(message.from_user.id):
        await message.answer("🚫 Доступ запрещён: вы заблокированы или не Организатор.")
        return

    try:
        _, app_id_str, *link_parts = message.text.split(maxsplit=2)
        app_id = int(app_id_str)
        link = " ".join(link_parts).strip()
        if not link:
            raise ValueError("Не указана ссылка")
    except:
        await message.answer(
            "📋 <b>Формат:</b> <code>/verify ID_заявки ссылка_на_чат</code>\n\n"
            "Пример: <code>/verify 123 https://t.me/chat123</code>"
        )
        return

    async with AsyncSessionLocal() as session:
        app = await session.get(Application, app_id)
        if not app:
            await message.answer("❌ Заявка не найдена.")
            return

        participant = await session.get(User, app.user_id)

        app.status = "link_sent"
        await session.commit()

        await message.bot.send_message(
            participant.telegram_id,
            f"✅ <b>Участие полностью подтверждено!</b>\n\n"
            f"🔗 <b>Ссылка на чат комитета:</b>\n<code>{link}</code>\n\n"
            "Удачи на конференции! 🚀"
        )

    await message.answer(f"✅ Ссылка отправлена участнику заявки <code>{app_id}</code>")


# ====================== ЭКСПОРТЫ ======================
@router.callback_query(F.data.startswith("export_conf_"))
async def export_conference_participants(callback: types.CallbackQuery):
    if not await is_active_organizer(callback.from_user.id):
        await callback.answer("🚫 Доступ запрещён: вы заблокированы.", show_alert=True)
        return

    conf_id = int(callback.data.split("_")[-1])
    async with AsyncSessionLocal() as session:
        conf = await session.get(Conference, conf_id)
        if not conf:
            await callback.answer("Конференция не найдена.")
            return

        result = await session.execute(
            select(Application).options(joinedload(Application.user)).where(Application.conference_id == conf_id)
        )
        apps = result.scalars().all()

        if not apps:
            await callback.answer("Нет участников для экспорта", show_alert=True)
            return

        data = []
        for app in apps:
            participant = app.user
            data.append({
                "ФИО": participant.full_name or "—",
                "Возраст": participant.age or "—",
                "Email": participant.email or "—",
                "Учебное заведение": participant.institution or "—",
                "Опыт MUN": participant.experience or "—",
                "Комитет": app.committee or "—",
                "Статус": app.status,
                "Причина отклонения": app.reject_reason or "—",
                "Скриншот оплаты": app.payment_screenshot or "—"
            })

        df = pd.DataFrame(data)
        filename = f"participants_{conf.name.replace(' ', '_')[:30]}_{conf.id}.xlsx"
        df.to_excel(filename, index=False)

        with open(filename, "rb") as f:
            file = BufferedInputFile(f.read(), filename=filename)

        await callback.message.answer_document(
            file,
            caption=f"📊 <b>Экспорт участников:</b> {conf.name}\nВсего: {len(apps)} заявок"
        )
        await callback.answer("✅ Файл отправлен!")
        os.remove(filename)


@router.callback_query(F.data.in_(["export_current", "export_archive"]))
async def export_applications(callback: types.CallbackQuery):
    if not await is_active_organizer(callback.from_user.id):
        await callback.answer("🚫 Доступ запрещён: вы заблокированы.", show_alert=True)
        return

    mode = "current" if callback.data == "export_current" else "archive"
    user_id = callback.from_user.id

    apps = await get_applications(user_id, mode)
    if not apps:
        await callback.answer(f"Нет заявок для экспорта ({mode})", show_alert=True)
        return

    data = []
    for app in apps:
        participant = app.user
        data.append({
            "ID": app.id,
            "ФИО": participant.full_name or "—",
            "Возраст": participant.age or "—",
            "Email": participant.email or "—",
            "УЗ": participant.institution or "—",
            "Опыт": participant.experience or "—",
            "Комитет": app.committee or "—",
            "Статус": app.status,
            "Причина": app.reject_reason or "—"
        })

    df = pd.DataFrame(data)
    filename = f"applications_{mode}_{datetime.now().strftime('%Y%m%d')}.xlsx"
    df.to_excel(filename, index=False)

    with open(filename, "rb") as f:
        file = BufferedInputFile(f.read(), filename=filename)

    await callback.message.answer_document(
        file,
        caption=f"📊 Экспорт {mode}: {len(apps)} заявок"
    )
    await callback.answer("✅ Готово!")
    os.remove(filename)


@router.callback_query(F.data.startswith("delete_conf_"))
async def confirm_delete(callback: types.CallbackQuery):
    if not await is_active_organizer(callback.from_user.id):
        await callback.answer("🚫 Доступ запрещён: вы заблокированы.", show_alert=True)
        return

    conf_id = int(callback.data.split("_")[-1])
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="🔴 ДА, УДАЛИТЬ", callback_data=f"confirm_delete_{conf_id}"),
        InlineKeyboardButton(text="❌ Отмена", callback_data="back_to_menu_org")
    )
    await callback.message.edit_text(
        "⚠️ <b>ВЫ УВЕРЕНЫ?</b>\n\n"
        "Будет удалена конференция + ВСЕ заявки и заявки на редактирование навсегда!\n"
        "Действие <b>необратимо</b>.\n\n"
        "Нажмите «ДА, УДАЛИТЬ» для подтверждения.",
        reply_markup=builder.as_markup()
    )


@router.callback_query(F.data.startswith("confirm_delete_"))
async def do_delete(callback: types.CallbackQuery):
    if not await is_active_organizer(callback.from_user.id):
        await callback.answer("🚫 Доступ запрещён: вы заблокированы.", show_alert=True)
        return

    conf_id = int(callback.data.split("_")[-1])
    user_id = callback.from_user.id

    async with AsyncSessionLocal() as session:
        conf = await session.get(Conference, conf_id)
        if not conf:
            await callback.answer("Конференция не найдена.")
            return

        organizer = await session.get(User, conf.organizer_id)

        notify_text = f"🗑 <b>Организатор удалил конференцию:</b>\n{conf.name}\n👤 @{organizer.telegram_id}"
        for admin_id in CHIEF_ADMIN_IDS:
            try:
                await callback.bot.send_message(admin_id, notify_text)
            except:
                pass

        await session.execute(delete(Application).where(Application.conference_id == conf_id))
        await session.execute(delete(ConferenceEditRequest).where(ConferenceEditRequest.conference_id == conf_id))
        await session.delete(conf)
        await session.commit()

        remaining_confs = await session.scalar(
            select(func.count(Conference.id)).where(Conference.organizer_id == organizer.id)
        )
        if remaining_confs == 0:
            organizer.role = Role.PARTICIPANT.value
            await session.commit()
            await callback.bot.send_message(
                organizer.telegram_id,
                "📢 <b>У вас больше нет конференций!</b>\n\n"
                "🔄 Роль изменена на <b>Участник</b>.\n"
                "/main_menu — для обновления меню."
            )

    if user_id in last_my_conferences_msg:
        try:
            await callback.bot.delete_message(callback.message.chat.id, last_my_conferences_msg[user_id])
            del last_my_conferences_msg[user_id]
        except:
            pass

    await callback.message.edit_text(
        f"✅ <b>Конференция удалена:</b> {conf.name}\n"
        f"🗑 Все заявки тоже."
    )
    await callback.answer("🗑 Удалено!")

    if remaining_confs > 0:
        await my_conferences(callback.message)


@router.callback_query(F.data.startswith("broadcast_"))
async def start_broadcast(callback: types.CallbackQuery, state: FSMContext):
    if not await is_active_organizer(callback.from_user.id):
        await callback.answer("🚫 Доступ запрещён: вы заблокированы.", show_alert=True)
        return

    conf_id = int(callback.data.split("_")[-1])
    await state.update_data(conference_id=conf_id)
    await state.set_state(Broadcast.message_text)

    async with AsyncSessionLocal() as session:
        conf = await session.get(Conference, conf_id)
        if not conf:
            await callback.answer("Конференция не найдена.")
            return

        await callback.message.edit_text(
            f"📢 <b>Рассылка по конференции:</b> {conf.name}\n\n"
            "💬 Введите текст сообщения:",
            reply_markup=get_cancel_keyboard()
        )
    await callback.answer()


@router.message(Broadcast.message_text)
async def send_broadcast(message: types.Message, state: FSMContext):
    if not await is_active_organizer(message.from_user.id):
        await message.answer("🚫 Доступ запрещён: вы заблокированы.")
        await state.clear()
        return

    data = await state.get_data()
    conf_id = data["conference_id"]
    text = message.text.strip()

    if not text:
        await message.answer("❌ Текст не может быть пустым!")
        return

    async with AsyncSessionLocal() as session:
        conf = await session.get(Conference, conf_id)
        if not conf:
            await message.answer("Конференция не найдена.")
            await state.clear()
            return

        result = await session.execute(
            select(Application).options(joinedload(Application.user)).where(
                Application.conference_id == conf_id,
                Application.status.in_(["approved", "payment_pending", "payment_sent", "confirmed", "link_sent"])
            )
        )
        applications = result.scalars().all()

        sent_count = 0
        failed_count = 0
        for app in applications:
            try:
                await message.bot.send_message(
                    app.user.telegram_id,
                    f"📢 <b>Сообщение от организатора {safe(conf.name)}</b>\n\n{safe(text)}"
                )
                sent_count += 1
            except Exception as e:
                logger.error(f"Ошибка рассылки {app.user.telegram_id}: {e}")
                failed_count += 1

    await message.answer(
        f"✅ <b>Рассылка завершена!</b>\n\n"
        f"📨 Отправлено: <b>{sent_count}</b>\n"
        f"❌ Ошибок: <b>{failed_count}</b>",
        reply_markup=get_main_menu_keyboard("Организатор")
    )
    await state.clear()


@router.callback_query(F.data == "back_to_menu_org")
async def back_to_menu(callback: types.CallbackQuery):
    user_id = callback.from_user.id

    if user_id in last_my_conferences_msg:
        try:
            await callback.bot.delete_message(callback.message.chat.id, last_my_conferences_msg[user_id])
            del last_my_conferences_msg[user_id]
        except:
            pass

    await callback.message.edit_text(
        "🔙 <b>Главное меню Организатора</b>",
        reply_markup=get_main_menu_keyboard("Организатор")
    )
    await callback.answer()


@router.callback_query(F.data.startswith("org_conf_info_"))
async def org_conf_info(callback: types.CallbackQuery):
    conf_id = int(callback.data.split("_")[-1])
    async with AsyncSessionLocal() as session:
        conf = await session.get(Conference, conf_id)
        if not conf:
            await callback.answer("Конференция не найдена.", show_alert=True)
            return

        text = f"<b>Информация о конференции</b>\n\n"
        text += f"Название: <b>{conf.name}</b>\n"
        text += f"Описание: {conf.description or '—'}\n"
        text += f"Город: {conf.city or 'Онлайн'}\n"
        text += f"Дата: {conf.date}\n"
        text += f"Орг взнос: {int(conf.fee)} сом\n"
        text += f"QR-код: {'есть' if conf.qr_code_path else 'нет'}\n"
        text += f"Постер: {'есть' if conf.poster_path else 'нет'}\n"
        text += f"Статус: {'Активна' if conf.is_active else 'Неактивна'}"
        if conf.is_completed:
            text += " (завершена)"

        builder = InlineKeyboardBuilder()
        builder.row(InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_my_conf"))

        await callback.message.edit_text(text, reply_markup=builder.as_markup())
        await callback.answer()


@router.callback_query(F.data.startswith("org_participants_"))
async def org_participants(callback: types.CallbackQuery):
    conf_id = int(callback.data.split("_")[-1])
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(Conference)
            .options(joinedload(Conference.applications).joinedload(Application.user))
            .where(Conference.id == conf_id)
        )
        conf = result.unique().scalar_one_or_none()

        if not conf:
            await callback.answer("Конференция не найдена.", show_alert=True)
            return

        text = f"<b>Участники «{conf.name}»</b>\n\n"
        if not conf.applications:
            text += "Пока нет заявок."
        else:
            for app in conf.applications:
                u = app.user
                emoji = {"pending": "⏳", "approved": "✅", "rejected": "❌", "payment_pending": "💳", "confirmed": "🎟", "link_sent": "🔗"}.get(app.status, "❓")
                text += f"{emoji} {u.full_name or u.telegram_id} ({u.age or '—'} лет)\n"
                text += f"   Email: {u.email or '—'}\n"
                text += f"   Вуз: {u.institution or '—'}\n"
                text += f"   Комитет: {app.committee or '—'}\n"
                text += f"   Статус: {app.status}\n"
                if app.payment_screenshot:
                    text += "   Оплата загружена\n"
                text += "\n"

        builder = InlineKeyboardBuilder()
        builder.row(InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_my_conf"))

        await callback.message.edit_text(text, reply_markup=builder.as_markup())
        await callback.answer()


@router.callback_query(F.data.startswith("org_rating_"))
async def org_rating(callback: types.CallbackQuery):
    conf_id = int(callback.data.split("_")[-1])
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(Conference)
            .options(joinedload(Conference.ratings).joinedload(ConferenceRating.user))
            .where(Conference.id == conf_id)
        )
        conf = result.unique().scalar_one_or_none()

        if not conf:
            await callback.answer("Конференция не найдена.", show_alert=True)
            return

        text = f"<b>Рейтинг «{conf.name}»</b>\n\n"
        if not conf.ratings:
            text += "Пока нет оценок."
        else:
            avg = conf.get_average_rating()
            text += f"Средняя оценка: <b>{avg} ⭐</b> ({len(conf.ratings)} оценок)\n\n"
            text += "<b>Отзывы:</b>\n"
            for r in conf.ratings:
                u = r.user
                text += f"• {r.rating} ⭐ от {u.full_name or u.telegram_id}\n"
                if r.review:
                    text += f"  «{r.review}»\n"
                text += "\n"

        builder = InlineKeyboardBuilder()
        builder.row(InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_my_conf"))

        await callback.message.edit_text(text, reply_markup=builder.as_markup())
        await callback.answer()


@router.callback_query(F.data.startswith("org_delete_"))
async def org_delete_confirm(callback: types.CallbackQuery):
    conf_id = int(callback.data.split("_")[-1])
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="🔴 ДА, УДАЛИТЬ НАВСЕГДА", callback_data=f"confirm_delete_{conf_id}"),
        InlineKeyboardButton(text="❌ Отмена", callback_data="back_to_my_conf")
    )
    await callback.message.edit_text(
        "⚠️ <b>ВЫ УВЕРЕНЫ?</b>\n\n"
        "Будет удалена конференция + ВСЕ заявки + заявки на редактирование.\n"
        "Действие <b>необратимо</b>.\n\n"
        "Нажмите кнопку ниже для подтверждения.",
        reply_markup=builder.as_markup()
    )
    await callback.answer()


@router.callback_query(F.data.startswith("confirm_delete_"))
async def org_delete_execute(callback: types.CallbackQuery):
    conf_id = int(callback.data.split("_")[-1])
    user_id = callback.from_user.id

    async with AsyncSessionLocal() as session:
        conf = await session.get(Conference, conf_id)
        if not conf:
            await callback.answer("Конференция не найдена.")
            return

        organizer_id = conf.organizer_id
        await session.execute(delete(Application).where(Application.conference_id == conf_id))
        await session.execute(delete(ConferenceEditRequest).where(ConferenceEditRequest.conference_id == conf_id))
        await session.delete(conf)
        await session.commit()

        remaining = await session.scalar(
            select(func.count(Conference.id)).where(Conference.organizer_id == organizer_id)
        )
        if remaining == 0:
            organizer = await session.get(User, organizer_id)
            organizer.role = Role.PARTICIPANT.value
            await session.commit()
            try:
                await callback.bot.send_message(
                    organizer.telegram_id,
                    "Ваша конференция удалена.\nРоль изменена на Участник."
                )
            except:
                pass

    await callback.message.edit_text("✅ Конференция успешно удалена.")
    await callback.answer()

    if remaining > 0:
        await my_conferences(callback)


# ====================== РЕДАКТИРОВАНИЕ КОНФЕРЕНЦИИ ======================

@router.callback_query(F.data.startswith("org_edit_request_"))
async def start_edit_conference(callback: types.CallbackQuery, state: FSMContext):
    conf_id = int(callback.data.split("_")[-1])

    async with AsyncSessionLocal() as session:
        conf = await session.get(Conference, conf_id)
        if not conf:
            await callback.answer("Конференция не найдена.", show_alert=True)
            return

        organizer = await session.execute(
            select(User).where(User.telegram_id == callback.from_user.id)
        )
        organizer = organizer.scalar_one_or_none()
        if not organizer or conf.organizer_id != organizer.id:
            await callback.answer("Это не ваша конференция!", show_alert=True)
            return

        await state.update_data(conf_id=conf_id)

        text = f"<b>Редактирование конференции «{conf.name}»</b>\n\nВыберите поле:"

        builder = InlineKeyboardBuilder()
        builder.row(InlineKeyboardButton(text="Название", callback_data="edit_field_name"))
        builder.row(InlineKeyboardButton(text="Описание", callback_data="edit_field_description"))
        builder.row(InlineKeyboardButton(text="Город", callback_data="edit_field_city"))
        builder.row(InlineKeyboardButton(text="Дата (ГГГГ-ММ-ДД)", callback_data="edit_field_date"))
        builder.row(InlineKeyboardButton(text="Оргвзнос", callback_data="edit_field_fee"))
        builder.row(InlineKeyboardButton(text="QR-код (фото или 'нет')", callback_data="edit_field_qr"))
        builder.row(InlineKeyboardButton(text="Постер (фото или 'нет')", callback_data="edit_field_poster"))
        builder.row(InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_edit"))

        await callback.message.edit_text(text, reply_markup=builder.as_markup())
        await callback.answer()


@router.callback_query(F.data == "cancel_edit")
async def cancel_edit(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.edit_text("Редактирование отменено.")
    await callback.answer()
    await my_conferences(callback)


@router.callback_query(F.data.startswith("edit_field_"))
async def process_edit_field(callback: types.CallbackQuery, state: FSMContext):
    field = callback.data.split("_")[-1]
    await state.update_data(field=field)

    field_names = {
        "name": "новое название конференции",
        "description": "новое описание",
        "city": "новый город (или 'Онлайн')",
        "date": "новую дату (ГГГГ-ММ-ДД)",
        "fee": "новый оргвзнос (число)",
        "qr": "новое фото QR-кода или напишите 'нет'",
        "poster": "новое фото постера или напишите 'нет'"
    }

    text = f"Введите {field_names.get(field, 'значение')}:\n\n"
    if field in ["qr", "poster"]:
        text += "(можно отправить фото или написать 'нет')"

    await state.set_state(EditConference.new_value)
    await callback.message.edit_text(text, reply_markup=get_cancel_keyboard())
    await callback.answer()


@router.message(EditConference.new_value)
async def save_edit_value(message: types.Message, state: FSMContext):
    data = await state.get_data()
    conf_id = data.get("conf_id")
    field = data.get("field")

    if not conf_id or not field:
        await message.answer("Сессия устарела. Начните заново.")
        await state.clear()
        return

    new_value = None
    file_path = None

    if message.text:
        new_value = message.text.strip()
        if new_value.lower() in ["нет", "удалить", "нету"] and field in ["qr", "poster"]:
            new_value = None
    elif message.photo and field in ["qr", "poster"]:
        file_info = await message.bot.get_file(message.photo[-1].file_id)
        dir_path = "qr_codes" if field == "qr" else "posters"
        file_path = f"{dir_path}/{field}_{conf_id}_{message.from_user.id}_{message.message_id}.jpg"
        await message.bot.download_file(file_info.file_path, file_path)
        new_value = file_path
    else:
        await message.answer("Отправьте текст или фото.")
        return

    async with AsyncSessionLocal() as session:
        conf = await session.get(Conference, conf_id)
        if not conf:
            await message.answer("Конференция не найдена.")
            await state.clear()
            return

        edit_request = ConferenceEditRequest(
            conference_id=conf_id,
            organizer_id=conf.organizer_id,
            data={
                "original": {
                    "name": conf.name,
                    "description": conf.description,
                    "city": conf.city,
                    "date": conf.date,
                    "fee": conf.fee,
                    "qr_code_path": conf.qr_code_path,
                    "poster_path": conf.poster_path
                },
                "changes": {field: new_value}
            },
            status="pending"
        )
        session.add(edit_request)
        await session.commit()

        notify_text = (
            f"✏️ <b>НОВАЯ ЗАЯВКА НА РЕДАКТИРОВАНИЕ!</b>\n\n"
            f"Конференция: <b>{conf.name}</b>\n"
            f"Организатор: {message.from_user.full_name or message.from_user.id}\n"
            f"Поле: <b>{field}</b>\n"
            f"Новое значение: <b>{new_value or 'удалить файл'}</b>\n"
            f"ID заявки: <code>{edit_request.id}</code>"
        )

        admins_result = await session.execute(
            select(User.telegram_id).where(User.role.in_(["Админ", "Главный Админ"]))
        )
        admin_ids = [row[0] for row in admins_result.all()]

        sent = 0
        for admin_id in admin_ids:
            try:
                await message.bot.send_message(admin_id, notify_text, parse_mode="HTML")
                sent += 1
            except:
                pass

    reply = f"✅ Заявка отправлена! Уведомлено администраторов: {sent}"
    if file_path:
        reply += "\nФото успешно добавлено."

    await message.answer(reply)
    await state.clear()


# ====================== ГЛОБАЛЬНЫЙ ОБРАБОТЧИК ОПЛАТЫ ======================
@router.message(F.photo)
async def receive_payment_screenshot(message: types.Message):
    async with AsyncSessionLocal() as session:
        user_apps = await session.execute(
            select(Application)
            .join(User)
            .where(User.telegram_id == message.from_user.id)
            .where(Application.status == "payment_pending")
        )
        apps = user_apps.scalars().all()

        if not apps:
            return

        app = apps[0]
        conf = await session.get(Conference, app.conference_id)
        organizer = await session.get(User, conf.organizer_id)
        participant = await session.get(User, app.user_id)

        participant_name = participant.full_name or f"ID {participant.telegram_id}"

        file_info = await message.bot.get_file(message.photo[-1].file_id)
        file_path = f"{PAYMENTS_DIR}/payment_{app.id}_{message.message_id}.jpg"
        await message.bot.download_file(file_info.file_path, file_path)

        app.payment_screenshot = file_path
        app.status = "payment_sent"
        await session.commit()

        caption = (
            f"💳 <b>Новый скриншот оплаты!</b>\n\n"
            f"👤 Участник: {safe(participant_name)}\n"
            f"📋 ID заявки: <code>{app.id}</code>\n"
            f"🎯 Конференция: {safe(conf.name)}\n\n"
            f"✅ Проверьте оплату и подтвердите:\n"
            f"<code>/verify {app.id} [ссылка_на_чат]</code>"
        )
        await message.bot.send_photo(organizer.telegram_id, message.photo[-1].file_id, caption=caption)

    await message.answer(
        "✅ Скриншот отправлен организатору!\n"
        "Ожидайте подтверждения оплаты и ссылку на чат."
    )