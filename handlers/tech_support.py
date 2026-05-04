from aiogram import Router, types, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.types import InlineKeyboardButton, BufferedInputFile, FSInputFile
from sqlalchemy import select
import os
import logging
from utils import safe

from database import AsyncSessionLocal, SupportRequest, User, Role
from keyboards import get_main_menu_keyboard, get_cancel_keyboard
from states import SupportResponse

router = Router()

logger = logging.getLogger(__name__)

# Пагинация обращений
support_pagination = {}


async def is_tech_specialist(user_id: int) -> bool:
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(User).where(User.telegram_id == user_id))
        user = result.scalar_one_or_none()
        return user and user.role == Role.CHIEF_TECH.value


# ====================== СПИСОК ОБРАЩЕНИЙ ======================
@router.message(F.text == "📩 Обращения пользователей")
@router.message(Command("support_requests"))
async def list_support_requests(message: types.Message):
    if not await is_tech_specialist(message.from_user.id):
        await message.answer("Доступ запрещён. Только для Главного Тех Специалиста.")
        return

    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(SupportRequest)
            .options(joinedload(SupportRequest.user))  # нужно добавить joinedload в импорт
            .order_by(SupportRequest.id.desc())
        )
        requests = result.scalars().all()

    if not requests:
        await message.answer(
            "✅ Очередь обращений пуста.",
            reply_markup=get_main_menu_keyboard("Глав Тех Специалист")
        )
        return

    # Сохраняем пагинацию
    support_pagination[message.from_user.id] = {"requests": requests, "index": 0}
    await show_support_request(message, requests, 0)


async def show_support_request(target: types.Message | types.CallbackQuery, requests: list, index: int):
    req = requests[index]
    user = req.user

    text = f"<b>Обращение {index + 1} из {len(requests)}</b>\n\n"
    text += f"<b>ID:</b> <code>{req.id}</code>\n"
    text += f"<b>От:</b> {safe(user.full_name or f'ID {user.telegram_id}')}\n"
    text += f"<b>Дата:</b> {req.created_at.strftime('%d.%m.%Y %H:%M') if hasattr(req, 'created_at') else '—'}\n\n"
    text += f"<b>Сообщение:</b>\n{req.message}\n\n"
    text += f"<b>Статус:</b> {req.status}\n"
    if req.response:
        text += f"\n<b>Ответ:</b>\n{req.response}"

    builder = InlineKeyboardBuilder()

    # Навигация
    nav = []
    if index > 0:
        nav.append(InlineKeyboardButton(text="◀ Назад", callback_data=f"nav_support_{index-1}"))
    if index < len(requests) - 1:
        nav.append(InlineKeyboardButton(text="▶ Вперёд", callback_data=f"nav_support_{index+1}"))
    if nav:
        builder.row(*nav)

    builder.row(InlineKeyboardButton(text="📩 Ответить", callback_data=f"reply_support_{req.id}"))
    builder.row(InlineKeyboardButton(text="🔙 Главное меню", callback_data="back_to_menu"))

    # Если есть скриншот — отправляем как фото
    if req.screenshot_path and os.path.exists(req.screenshot_path):
        photo = FSInputFile(req.screenshot_path)
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


# Навигация
@router.callback_query(F.data.startswith("nav_support_"))
async def navigate_support(callback: types.CallbackQuery):
    if not await is_tech_specialist(callback.from_user.id):
        await callback.answer("Доступ запрещён.", show_alert=True)
        return

    index = int(callback.data.split("_")[-1])
    user_id = callback.from_user.id

    data = support_pagination.get(user_id)
    if not data or index < 0 or index >= len(data["requests"]):
        await callback.answer("Сессия истекла. Откройте обращения заново.", show_alert=True)
        return

    data["index"] = index
    await show_support_request(callback, data["requests"], index)
    await callback.answer(f"{index + 1}/{len(data['requests'])}")


# Ответ на обращение
@router.callback_query(F.data.startswith("reply_support_"))
async def start_support_response(callback: types.CallbackQuery, state: FSMContext):
    if not await is_tech_specialist(callback.from_user.id):
        await callback.answer("Доступ запрещён.", show_alert=True)
        return

    req_id = int(callback.data.split("_")[-1])
    await state.update_data(request_id=req_id)
    await state.set_state(SupportResponse.response_text)

    await callback.message.edit_text(
        f"Напишите ответ на обращение <b>ID {req_id}</b>:",
        reply_markup=get_cancel_keyboard()
    )
    await callback.answer()


@router.message(SupportResponse.response_text)
async def send_support_response(message: types.Message, state: FSMContext):
    if not await is_tech_specialist(message.from_user.id):
        await state.clear()
        return

    data = await state.get_data()
    req_id = data["request_id"]
    response_text = message.text.strip()

    async with AsyncSessionLocal() as session:
        req = await session.get(SupportRequest, req_id)
        if not req:
            await message.answer("Обращение не найдено.")
            await state.clear()
            return

        req.status = "resolved"
        req.response = response_text
        await session.commit()

        user = await session.get(User, req.user_id)
        try:
            await message.bot.send_message(
                user.telegram_id,
                f"📩 <b>Ответ от техподдержки</b>\n\n"
                f"По вашему обращению:\n{safe(req.message)}\n\n"
                f"<b>Ответ:</b>\n{safe(response_text)}"
            )
        except:
            pass

    await message.answer(
        f"✅ Ответ на обращение #{req_id} отправлен.",
        reply_markup=get_main_menu_keyboard("Глав Тех Специалист")
    )
    await state.clear()
