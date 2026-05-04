from aiogram import Router, types, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.types import InlineKeyboardButton, BufferedInputFile, InlineKeyboardMarkup
from sqlalchemy import select
import pandas as pd
import os
import logging
from utils import safe

from database import AsyncSessionLocal, SupportRequest, User, Role
from keyboards import get_main_menu_keyboard, get_cancel_keyboard
from states import SupportResponse  # если ещё не импортировано
from aiogram.fsm.state import State, StatesGroup

# Новые состояния для рассылки
class TechBroadcast(StatesGroup):
    waiting_text = State()      # Ввод текста (опционально)
    waiting_media = State()     # Фото/видео (опционально)
    confirm = State()           # Подтверждение

# Кнопка подтверждения
def get_broadcast_confirm_keyboard():
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="✅ Отправить всем", callback_data="broadcast_send"))
    builder.row(InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_form"))
    return builder.as_markup()

router = Router()

# Настройка логирования
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

_broadcast_mode = set()

# Проверка роли "Глав Тех Специалист"
async def is_tech_specialist(user_id: int) -> bool:
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(User).where(User.telegram_id == user_id))
        user = result.scalar_one_or_none()
        return user.role == Role.CHIEF_TECH.value if user else False


# ======================
# Просмотр очереди обращений
# ======================
@router.message(Command("support_requests"))
async def list_support_requests(message: types.Message):
    if not await is_tech_specialist(message.from_user.id):
        await message.answer("Доступ запрещён. Только для Главного Тех Специалиста.")
        return

    async with AsyncSessionLocal() as session:
        result = await session.execute(select(SupportRequest).order_by(SupportRequest.id))
        requests = result.scalars().all()

        if not requests:
            await message.answer(
                "Очередь обращений в техподдержку пуста.",
                reply_markup=get_main_menu_keyboard("Глав Тех Специалист")
            )
            return

        builder = InlineKeyboardBuilder()
        text = "<b>Очередь обращений в техподдержку:</b>\n\n"
        for req in requests:
            user = await session.get(User, req.user_id)
            status_emoji = "✅" if req.status == "resolved" else "⏳"
            status_text = "Обработано" if req.status == "resolved" else "Ожидает ответа"
            text += f"{status_emoji} <b>ID обращения: {req.id}</b> ({status_text})\n"
            text += f"От: {user.full_name or 'Без имени'} (@{user.telegram_id})\n"
            text += f"Сообщение:\n{req.message}\n"
            if req.response:
                text += f"\nОтвет:\n{req.response}\n"
            text += "\n"

            if req.status == "pending":
                builder.row(
                    InlineKeyboardButton(text=f"Ответить на обращение {req.id}", callback_data=f"support_answer_{req.id}")
                )

        builder.row(InlineKeyboardButton(text="📊 Экспорт обращений в CSV", callback_data="export_support_csv"))
        builder.row(InlineKeyboardButton(text="🔙 Главное меню", callback_data="back_to_menu"))

        await message.answer(text, reply_markup=builder.as_markup())


# ======================
# Экспорт обращений в CSV
# ======================
@router.callback_query(F.data == "export_support_csv")
async def export_support_csv(callback: types.CallbackQuery):
    if not await is_tech_specialist(callback.from_user.id):
        await callback.answer("Доступ запрещён.", show_alert=True)
        return

    async with AsyncSessionLocal() as session:
        result = await session.execute(select(SupportRequest).order_by(SupportRequest.id))
        requests = result.scalars().all()

        data = []
        for req in requests:
            user = await session.get(User, req.user_id)
            data.append({
                "ID обращения": req.id,
                "Telegram ID": user.telegram_id,
                "ФИО": user.full_name or "—",
                "Сообщение": req.message,
                "Статус": req.status,
                "Ответ": req.response or "—"
            })

    if not data:
        await callback.answer("Нет данных для экспорта", show_alert=True)
        return

    df = pd.DataFrame(data)
    filename = "support_requests_export.csv"
    df.to_csv(filename, index=False, encoding="utf-8-sig")

    with open(filename, "rb") as f:
        file = BufferedInputFile(f.read(), filename=filename)

    await callback.message.answer_document(file, caption="📊 Экспорт всех обращений в техподдержку")
    await callback.answer("Файл отправлен!")
    os.remove(filename)


# ======================
# Ответ на обращение
# ======================
@router.callback_query(F.data.startswith("support_answer_"))
async def start_support_response(callback: types.CallbackQuery, state: FSMContext):
    if not await is_tech_specialist(callback.from_user.id):
        await callback.answer("Доступ запрещён.", show_alert=True)
        return

    req_id = int(callback.data.split("_")[-1])
    await state.update_data(request_id=req_id)
    await state.set_state(SupportResponse.response_text)

    await callback.message.edit_text(
        f"Ответ на обращение <b>ID {req_id}</b>\n\n"
        "Введите текст ответа участнику:",
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
    response_text = message.text

    async with AsyncSessionLocal() as session:
        req = await session.get(SupportRequest, req_id)
        if not req or req.status == "resolved":
            await message.answer("Обращение не найдено или уже обработано.")
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
                f"По вашему обращению:\n\"{safe(req.message)}\"\n\n"
                f"Ответ:\n{safe(response_text)}"
            )
        except Exception as e:
            logger.error(f"Ошибка отправки ответа пользователю {user.telegram_id}: {e}")
            await message.answer("Не удалось отправить ответ (пользователь заблокировал бота или удалён).")

    await message.answer(
        f"Ответ на обращение ID {req_id} отправлен.",
        reply_markup=get_main_menu_keyboard("Глав Тех Специалист")
    )
    await state.clear()


# ======================
# ПРОСТАЯ РАССЫЛКА ВСЕМ ПОЛЬЗОВАТЕЛЯМ
# ПРОСТАЯ РАССЫЛКА ВСЕМ ПОЛЬЗОВАТЕЛЯМ (ФИНАЛЬНАЯ ВЕРСИЯ)
# ======================
@router.message(F.text == "📢 Рассылка всем пользователям")
async def broadcast_button_help(message: types.Message):
    if not await is_tech_specialist(message.from_user.id):
        await message.answer("🚫 Доступ запрещён.")
        return

    await message.answer(
        "📢 <b>Рассылка всем пользователям</b>\n\n"
        "Чтобы разослать сообщение:\n\n"
        "• Напишите <code>/broadcast ваш текст</code>\n\n"
        "• Или ответьте <code>/broadcast</code> на фото/видео — оно будет разослано всем\n\n"
        "Заголовок добавляется автоматически.",
        parse_mode="HTML",
        reply_markup=get_main_menu_keyboard("Глав Тех Специалист")
    )

# 2. Команда /broadcast — основная рассылка (фото, видео, текст)
@router.message(Command("broadcast"))
async def cmd_broadcast(message: types.Message):
    if not await is_tech_specialist(message.from_user.id):
        await message.answer("🚫 Доступ запрещён.")
        return

    # Текст после команды
    command_text = message.text[len("/broadcast"):].strip() if message.text else ""

    # Источник контента
    source = message.reply_to_message if message.reply_to_message else message

    # Проверка контента
    if not command_text and not source.text and not source.photo and not source.video and not source.document:
        await message.answer(
            "📢 <b>Как использовать:</b>\n\n"
            "<code>/broadcast текст</code> — текстовая рассылка\n\n"
            "Или ответьте <code>/broadcast</code> на фото/видео.",
            parse_mode="HTML"
        )
        return

    # Начало рассылки
    await message.answer("🔄 <b>Рассылка началась...</b>")

    async with AsyncSessionLocal() as session:
        result = await session.execute(select(User.telegram_id))
        user_ids = [row[0] for row in result.all()]

    total = len(user_ids)
    if total == 0:
        await message.answer("❌ Нет пользователей.")
        return

    sent = 0
    failed = 0
    header = "📢 <b>Сообщение от техподдержки MUN-Бот</b>\n\n"

    for uid in user_ids:
        try:
            if source.photo:
                caption = header + (source.caption or command_text or "")
                await message.bot.send_photo(uid, source.photo[-1].file_id, caption=caption, parse_mode="HTML")
            elif source.video:
                caption = header + (source.caption or command_text or "")
                await message.bot.send_video(uid, source.video.file_id, caption=caption, parse_mode="HTML")
            elif source.document:
                caption = header + (source.caption or command_text or "")
                await message.bot.send_document(uid, source.document.file_id, caption=caption, parse_mode="HTML")
            else:
                text = header + (command_text or safe(source.text or ""))
                await message.bot.send_message(uid, text, parse_mode="HTML")
            sent += 1
        except Exception as e:
            failed += 1
            logger.debug(f"Ошибка отправки {uid}: {e}")

    await message.answer(
        f"✅ <b>Рассылка завершена!</b>\n\n"
        f"Всего: <b>{total}</b>\n"
        f"Отправлено: <b>{sent}</b>\n"
        f"Не доставлено: <b>{failed}</b>",
        parse_mode="HTML",
        reply_markup=get_main_menu_keyboard("Глав Тех Специалист")
    )