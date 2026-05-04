from aiogram import Router, types, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    InlineKeyboardButton,
    FSInputFile,
    InlineKeyboardMarkup,
    Message,
    CallbackQuery
)
from aiogram.utils.keyboard import InlineKeyboardBuilder

from utils import safe
from sqlalchemy import select, func
from sqlalchemy.orm import joinedload

from database import ConferenceRating
from datetime import datetime, timedelta
import os

from database import (
    AsyncSessionLocal,
    Conference,
    Application,
    User,
    Role,
    ConferenceCreationRequest,
    SupportRequest,
    get_or_create_user,
    ConferenceRating  # ← теперь здесь
)

from keyboards import (
    get_conferences_keyboard,
    get_cancel_keyboard,
    get_main_menu_keyboard
)

from states import (
    ParticipantRegistration,
    CreateConferenceRequest,
    SupportAppeal,
    ConferenceRatingState  # ← добавлено
)

from config import CHIEF_ADMIN_IDS, TECH_SPECIALIST_ID

import logging

RUSSIAN_MONTHS = {
    'January': 'Января', 'February': 'Февраля', 'March': 'Марта',
    'April': 'Апреля', 'May': 'Мая', 'June': 'Июня',
    'July': 'Июля', 'August': 'Августа', 'September': 'Сентября',
    'October': 'Октября', 'November': 'Ноября', 'December': 'Декабря'
}

router = Router()

os.makedirs("qr_codes", exist_ok=True)
os.makedirs("posters", exist_ok=True)
os.makedirs("support_screenshots", exist_ok=True)
# =========================
# 🔒 GLOBAL BAN MIDDLEWARE
# =========================


# Валидация даты: минимум завтра, максимум 5 лет
def validate_conference_date(date_str: str) -> str | None:
    today = datetime.now().date()
    try:
        conf_date = datetime.strptime(date_str.strip(), "%Y-%m-%d").date()
    except ValueError:
        return "Неверный формат даты. Используйте строго ГГГГ-ММ-ДД."

    min_date = today + timedelta(days=1)
    max_date = today + timedelta(days=5 * 365 + 1)

    if conf_date < min_date:
        return f"Дата проведения не может быть раньше завтрашнего дня ({min_date.strftime('%d.%m.%Y')})."
    if conf_date > max_date:
        return "Дата проведения не может быть позже, чем через 5 лет."

    return None

# Форматирование даты
def format_conference_date(date_str: str) -> str:
    try:
        conf_date = datetime.strptime(date_str.strip(), "%Y-%m-%d")
        month_en = conf_date.strftime('%B')
        month_ru = RUSSIAN_MONTHS.get(month_en, month_en)
        return f"Дата проведения: {conf_date.strftime('%d')} {month_ru} {conf_date.strftime('%Y')}"
    except:
        return f"Дата: {date_str}"

# Список конференций
@router.message(Command("conferences"))
async def cmd_conferences(message: types.Message):
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(Conference).where(Conference.is_active == True)
        )
        conferences = result.scalars().all()

        if not conferences:
            await message.answer(
                "😔 Пока нет актуальных конференций.\n"
                "Следите за обновлениями или создайте свою!"
            )
            return

        for conf in conferences:
            text = f"<b>{conf.name}</b>\n"
            text += f"📍 {conf.city or 'Онлайн'}\n"
            text += f"📅 {format_conference_date(conf.date)}\n"
            fee_text = f"💸 Орг взнос: {int(conf.fee)} сом" if conf.fee > 0 else "🆓 Бесплатно"
            text += f"{fee_text}\n\n"
            if conf.description:
                text += f"<i>{conf.description}</i>\n\n"
            text += "Нажмите кнопку ниже, чтобы подать заявку:"

            builder = InlineKeyboardBuilder()
            builder.row(InlineKeyboardButton(text="Подать заявку", callback_data=f"select_conf_{conf.id}"))

            if conf.poster_path and os.path.exists(conf.poster_path):
                photo = FSInputFile(conf.poster_path)
                await message.answer_photo(photo, caption=text, reply_markup=builder.as_markup())
            else:
                await message.answer(text, reply_markup=builder.as_markup())

# Регистрация
@router.message(Command("register"))
async def cmd_register(message: types.Message):
    await cmd_conferences(message)

# Выбор конференции
@router.callback_query(F.data.startswith("select_conf_"))
async def select_conference(callback: types.CallbackQuery, state: FSMContext):
    conf_id = int(callback.data.split("_")[-1])

    async with AsyncSessionLocal() as session:
        conf = await session.get(Conference, conf_id)
        if not conf:
            await callback.answer("Конференция не найдена.", show_alert=True)
            return

        today = datetime.now().date()
        try:
            conf_date = datetime.strptime(conf.date.strip(), "%Y-%m-%d").date()
        except ValueError:
            await callback.answer("Ошибка в дате конференции.", show_alert=True)
            return
        if conf_date < today:
            await callback.answer("Нельзя подать заявку на конференцию, которая уже прошла.", show_alert=True)
            return

    user = await get_or_create_user(callback.from_user.id)
    if user.role == Role.ORGANIZER.value:
        organizer_confs = await session.execute(
            select(Conference.id).where(Conference.organizer_id == user.id)
        )
        organizer_conf_ids = [row[0] for row in organizer_confs.all()]
        if conf_id in organizer_conf_ids:
            await callback.answer("Вы не можете участвовать в своей собственной конференции!", show_alert=True)
            return

    await state.update_data(conference_id=conf_id)
    await state.set_state(ParticipantRegistration.full_name)

    await callback.message.edit_text(
        "✅ Конференция выбрана!\n\n"
        "<b>Заполните анкету участника</b>\n\n"
        "1. ФИО (полностью):",
        reply_markup=get_cancel_keyboard()
    )
    await callback.answer()

# Анкета участника — без изменений (все функции как в твоём коде)
@router.message(ParticipantRegistration.full_name)
async def process_full_name(message: types.Message, state: FSMContext):
    await state.update_data(full_name=message.text.strip())
    await state.set_state(ParticipantRegistration.age)
    await message.answer("2. Возраст (от 11 до 99 лет):", reply_markup=get_cancel_keyboard())

@router.message(ParticipantRegistration.age)
async def process_age(message: types.Message, state: FSMContext):
    text = message.text.strip()
    try:
        age = int(text)
        if age < 11 or age > 99:
            await message.answer("Возраст должен быть от 11 до 99 лет. Повторите ввод:")
            return
    except ValueError:
        await message.answer("Введите возраст цифрами (от 11 до 99 лет):")
        return

    await state.update_data(age=age)
    await state.set_state(ParticipantRegistration.email)
    await message.answer("3. Email:", reply_markup=get_cancel_keyboard())

@router.message(ParticipantRegistration.email)
async def process_email(message: types.Message, state: FSMContext):
    await state.update_data(email=message.text.strip())
    await state.set_state(ParticipantRegistration.institution)
    await message.answer("4. Учебное заведение:", reply_markup=get_cancel_keyboard())

@router.message(ParticipantRegistration.institution)
async def process_institution(message: types.Message, state: FSMContext):
    await state.update_data(institution=message.text.strip())
    await state.set_state(ParticipantRegistration.experience)
    await message.answer("5. Опыт участия в MUN (кратко, если есть):", reply_markup=get_cancel_keyboard())

@router.message(ParticipantRegistration.experience)
async def process_experience(message: types.Message, state: FSMContext):
    await state.update_data(experience=message.text.strip())
    await state.set_state(ParticipantRegistration.committee)
    await message.answer("6. Желаемый комитет:", reply_markup=get_cancel_keyboard())

@router.message(ParticipantRegistration.committee)
async def process_committee(message: types.Message, state: FSMContext):
    data = await state.get_data()
    conference_id = data["conference_id"]
    committee = message.text.strip()

    async with AsyncSessionLocal() as session:
        # Получаем пользователя
        user_result = await session.execute(
            select(User).where(User.telegram_id == message.from_user.id)
        )
        user = user_result.scalar_one()

        # Проверка: есть ли уже заявка на эту конференцию
        existing_app = await session.execute(
            select(Application)
            .where(
                Application.user_id == user.id,
                Application.conference_id == conference_id
            )
        )
        if existing_app.scalar_one_or_none():
            await message.answer(
                "❌ Вы уже подавали заявку на эту конференцию!\n\n"
                "Повторная заявка на одну и ту же конференцию невозможна.",
                reply_markup=get_main_menu_keyboard(user.role)
            )
            await state.clear()
            return

        # Если заявки нет — обновляем данные пользователя и создаём заявку
        user.full_name = data.get("full_name")
        user.age = data.get("age")
        user.email = data.get("email")
        user.institution = data.get("institution")
        user.experience = data.get("experience")

        application = Application(
            user_id=user.id,
            conference_id=conference_id,
            committee=committee,
            status="pending"
        )
        session.add(application)
        await session.commit()
        await session.refresh(application)

        conf = await session.get(Conference, conference_id)

        notify_text = (
            f"🔔 <b>Новая заявка на участие!</b>\n\n"
            f"🎯 Конференция: <b>{safe(conf.name)}</b>\n"
            f"👤 Участник: <b>{safe(data.get('full_name'))}</b>\n\n"
            f"<b>Анкета:</b>\n"
            f"• Возраст: {safe(data.get('age'))} лет\n"
            f"• Email: {safe(data.get('email'))}\n"
            f"• Учебное заведение: {safe(data.get('institution'))}\n"
            f"• Опыт MUN: {safe(data.get('experience')) or 'Нет'}\n"
            f"• Желаемый комитет: <b>{safe(committee)}</b>\n\n"
            f"ID заявки: <code>{application.id}</code>\n"
            f"Нажми кнопку ниже, чтобы обработать заявку."
        )

        if conf.organizer_id:
            try:
                await message.bot.send_message(conf.organizer.telegram_id, notify_text)
            except:
                pass

    db_user = await get_or_create_user(message.from_user.id, message.from_user.full_name)

    await message.answer(
        "✅ <b>Заявка успешно отправлена!</b>\n\n"
        "Организатор рассмотрит её в ближайшее время.\n"
        "Вы получите уведомление о результате.",
        reply_markup=get_main_menu_keyboard(db_user.role)
    )
    await state.clear()

# Создание конференции — с валидацией
@router.message(F.text == "➕ Создать конференцию")
async def cmd_create_conference(message: types.Message, state: FSMContext):
    async with AsyncSessionLocal() as session:
        user_result = await session.execute(select(User).where(User.telegram_id == message.from_user.id))
        user = user_result.scalar_one_or_none()

        if not user or user.role != "Участник":
            await message.answer("Эта функция доступна только Участникам.")
            return

        conf_count = await session.scalar(
            select(func.count(Conference.id)).where(Conference.organizer_id == user.id)
        )
        if conf_count > 0:
            await message.answer(
                "У вас уже есть активная конференция.\n"
                "Удалите её или дождитесь завершения, чтобы создать новую."
            )
            return

    await state.set_state(CreateConferenceRequest.name)
    await message.answer("Создание конференции. Введите название:", reply_markup=get_cancel_keyboard())

@router.message(CreateConferenceRequest.name)
async def process_conf_name(message: types.Message, state: FSMContext):
    await state.update_data(name=message.text.strip())
    await state.set_state(CreateConferenceRequest.description)
    await message.answer("Введите описание конференции:", reply_markup=get_cancel_keyboard())

@router.message(CreateConferenceRequest.description)
async def process_conf_description(message: types.Message, state: FSMContext):
    await state.update_data(description=message.text.strip())
    await state.set_state(CreateConferenceRequest.city)
    await message.answer("Город проведения (или 'Онлайн'):", reply_markup=get_cancel_keyboard())

@router.message(CreateConferenceRequest.city)
async def process_conf_city(message: types.Message, state: FSMContext):
    await state.update_data(city=message.text.strip())
    await state.set_state(CreateConferenceRequest.date)
    await message.answer("Дата проведения (формат: ГГГГ-ММ-ДД):", reply_markup=get_cancel_keyboard())

@router.message(CreateConferenceRequest.date)
async def process_conf_date(message: types.Message, state: FSMContext):
    date_str = message.text.strip()

    error = validate_conference_date(date_str)
    if error:
        await message.answer(f"Ошибка: {error}\nВведите дату заново (ГГГГ-ММ-ДД):")
        return

    await state.update_data(date=date_str)
    await state.set_state(CreateConferenceRequest.fee)
    await message.answer("Орг взнос в сомах (0 — бесплатно):", reply_markup=get_cancel_keyboard())

@router.message(CreateConferenceRequest.fee)
async def process_conf_fee(message: types.Message, state: FSMContext):
    text = message.text.strip()
    if not text.replace('.', '', 1).replace('-', '', 1).isdigit():
        await message.answer("Введите корректное число (0 для бесплатной).")
        return
    await state.update_data(fee=float(text))
    await state.set_state(CreateConferenceRequest.qr_code)
    await message.answer(
        "Если конференция платная — отправьте фото QR-кода для оплаты.\n"
        "Если бесплатная — напишите 'нет'.",
        reply_markup=get_cancel_keyboard()
    )

@router.message(CreateConferenceRequest.qr_code, F.photo)
async def process_conf_qr_photo(message: types.Message, state: FSMContext):
    file_info = await message.bot.get_file(message.photo[-1].file_id)
    qr_path = f"qr_codes/qr_{message.from_user.id}_{message.message_id}.jpg"
    await message.bot.download_file(file_info.file_path, qr_path)
    await state.update_data(qr_code_path=qr_path)
    await state.set_state(CreateConferenceRequest.poster)
    await message.answer("Отправьте постер конференции (фото). Можно пропустить, написав 'нет':",
                         reply_markup=get_cancel_keyboard())

@router.message(CreateConferenceRequest.qr_code, F.text)
async def process_conf_qr_skip(message: types.Message, state: FSMContext):
    await state.update_data(qr_code_path=None)
    await state.set_state(CreateConferenceRequest.poster)
    await message.answer("Отправьте постер конференции (фото). Можно пропустить, написав 'нет':",
                         reply_markup=get_cancel_keyboard())

@router.message(CreateConferenceRequest.poster, F.photo)
async def process_conf_poster(message: types.Message, state: FSMContext):
    file_info = await message.bot.get_file(message.photo[-1].file_id)
    poster_path = f"posters/poster_{message.from_user.id}_{message.message_id}.jpg"
    await message.bot.download_file(file_info.file_path, poster_path)
    await state.update_data(poster_path=poster_path)
    await finish_conference_creation(message, state)

@router.message(CreateConferenceRequest.poster, F.text)
async def process_conf_poster_skip(message: types.Message, state: FSMContext):
    if message.text.lower().strip() == "нет":
        await state.update_data(poster_path=None)
        await finish_conference_creation(message, state)
    else:
        await message.answer("Отправьте фото постера или напишите 'нет'")

async def finish_conference_creation(message: types.Message, state: FSMContext):
    data = await state.get_data()

    async with AsyncSessionLocal() as session:
        # Получаем или создаём пользователя
        result = await session.execute(select(User).where(User.telegram_id == message.from_user.id))
        user = result.scalar_one_or_none()

        if user is None:
            user = User(
                telegram_id=message.from_user.id,
                username=message.from_user.username,
                full_name=message.from_user.full_name or message.from_user.first_name
            )
            session.add(user)
            await session.commit()
            await session.refresh(user)  # чтобы получить user.id

        user_id = user.id
        req = ConferenceCreationRequest(
            user_id=user_id,
            data=data,
            status="pending"
        )
        session.add(req)
        await session.commit()

        user = await session.get(User, user_id)
        notify_text = (
            f"🔔 <b>Новая заявка на создание конференции!</b>\n\n"
            f"От: {safe(user.full_name) or safe(user.telegram_id)}\n"
            f"Название: {safe(data['name'])}\n"
            f"Город: {safe(data.get('city', 'Онлайн'))}\n"
            f"Дата проведения: {safe(data['date'])}\n"
            f"Орг взнос: {int(data.get('fee', 0))} сом\n\n"
            f"ID заявки: <code>{req.id}</code>"
        )

        admins = (await session.execute(
            select(User.telegram_id).where(User.role.in_(["Админ", "Главный Админ"]))
        )).scalars().all()

        for admin_id in set(admins + CHIEF_ADMIN_IDS):
            try:
                await message.bot.send_message(admin_id, notify_text)
            except:
                pass

    await message.answer(
        "✅ <b>Заявка на создание конференции отправлена!</b>\n\n"
        f"Название: {data['name']}\n"
        f"Город: {data.get('city') or 'Онлайн'}\n"
        f"Дата проведения: {format_conference_date(data['date'])}\n"
        f"Орг взнос: {data.get('fee', 0)} сом\n\n"
        "Ожидайте одобрения Администратора.",
        reply_markup=get_main_menu_keyboard("Участник")
    )
    await state.clear()

# Обращение к тех. специалисту — с сохранением скриншота
@router.message(F.text == "📩 Обращение к тех. специалисту")
async def start_support_appeal(message: types.Message, state: FSMContext):
    await state.set_state(SupportAppeal.message)
    await message.answer(
        "📩 <b>Обращение в техподдержку</b>\n\n"
        "Опишите вашу проблему или вопрос.\n"
        "По желанию можете прикрепить скриншот (фото).",
        reply_markup=get_cancel_keyboard()
    )

@router.message(SupportAppeal.message, F.photo)
async def save_support_appeal_with_photo(message: types.Message, state: FSMContext):
    file_info = await message.bot.get_file(message.photo[-1].file_id)
    screenshot_path = f"support_screenshots/support_{message.from_user.id}_{message.message_id}.jpg"
    await message.bot.download_file(file_info.file_path, screenshot_path)

    text = message.caption or "Без текста (только скриншот)"

    # 🔹 Гарантируем, что пользователь есть
    db_user = await get_or_create_user(
        message.from_user.id,
        message.from_user.full_name
    )

    async with AsyncSessionLocal() as session:
        req = SupportRequest(
            user_id=db_user.id,
            message=text,
            screenshot_path=screenshot_path,
            status="pending"
        )
        session.add(req)
        await session.commit()
        await session.refresh(req)

        notify_text = (
            f"🆘 Новое обращение в техподдержку!\n\n"
            f"От: {message.from_user.full_name or message.from_user.id}\n"
            f"Текст: {text}\n"
            f"ID обращения: <code>{req.id}</code>"
        )

        try:
            await message.bot.send_photo(
                TECH_SPECIALIST_ID,
                message.photo[-1].file_id,
                caption=notify_text
            )
        except Exception as e:
            print(f"Ошибка отправки фото теху: {e}")

    await message.answer(
        "✅ Ваше обращение с скриншотом отправлено в техподдержку.\n"
        "Мы ответим вам в ближайшее время.",
        reply_markup=get_main_menu_keyboard(db_user.role)
    )

    await state.clear()

@router.message(SupportAppeal.message, F.text)
async def save_support_appeal_text_only(message: types.Message, state: FSMContext):
    # Используем готовую функцию
    db_user = await get_or_create_user(
        message.from_user.id,
        message.from_user.full_name or message.from_user.first_name
    )

    async with AsyncSessionLocal() as session:
        req = SupportRequest(
            user_id=db_user.id,
            message=message.text,
            screenshot_path=None,
            status="pending"
        )
        session.add(req)
        await session.commit()
        await session.refresh(req)

        notify_text = (
            f"🆘 Новое обращение в техподдержку!\n\n"
            f"От: {message.from_user.full_name or message.from_user.id}\n"
            f"Текст: {message.text}\n"
            f"ID обращения: <code>{req.id}</code>"
        )
        try:
            await message.bot.send_message(TECH_SPECIALIST_ID, notify_text)
        except Exception as e:
            print(f"Ошибка отправки текста теху: {e}")

    await message.answer(
        "✅ Ваше обращение отправлено в техподдержку.\n"
        "Мы ответим вам в ближайшее время.",
        reply_markup=get_main_menu_keyboard(db_user.role)
    )
    await state.clear()

# Помощь
@router.message(Command("help"))
async def cmd_help(message: types.Message):
    db_user = await get_or_create_user(message.from_user.id, message.from_user.full_name)
    await message.answer(
        "ℹ️ <b>Помощь</b>\n\n"
        "Если у вас проблемы с ботом — используйте кнопку \"Обращение к тех. специалисту\"\n"
        "По вопросам MUN — обратитесь к организатору вашей конференции.",
        reply_markup=get_main_menu_keyboard(db_user.role)
    )

# 🏆 ТОП-3 лучших конференций (улучшенная версия)
@router.message(F.text == "🏆 Лучшие конференции")
async def show_top_conferences(message: types.Message):
    async with AsyncSessionLocal() as session:
        # Показываем ВСЕ конференции (активные + завершённые) — рейтинг должен быть честным
        result = await session.execute(
            select(Conference)
            .options(joinedload(Conference.ratings))
        )
        conferences = result.scalars().unique().all()

    if not conferences:
        await message.answer("😔 Пока нет конференций для рейтинга.")
        return

    # Сортировка: средний рейтинг ↓ + количество оценок ↓ (чтобы при равном рейтинге выигрывала та, у кого больше голосов)
    top = sorted(
        [conf for conf in conferences if conf.get_average_rating() is not None],
        key=lambda c: (c.get_average_rating(), len(c.ratings)),
        reverse=True
    )[:3]

    if not top:
        await message.answer(
            "⭐ Пока никто не поставил оценки!\n\n"
            "Будь первым — после завершения конференции тебе придёт запрос на оценку 😊"
        )
        return

    # 🔥 СУПЕР-КРАСИВЫЙ ТЕКСТ
    text = (
        "🏆 <b>ТОП-3 ЛУЧШИХ КОНФЕРЕНЦИЙ MUN</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━\n\n"
    )

    medals = ["🥇", "🥈", "🥉"]

    for i, conf in enumerate(top, 1):
        avg = conf.get_average_rating()
        count = len(conf.ratings)
        status = "✅ Завершена" if conf.is_completed else "🔥 Активна"

        # Звёздочки в строку
        stars = "⭐" * int(avg)

        text += f"{medals[i-1]} <b>{conf.name}</b>\n"
        text += f"   📍 {conf.city or 'Онлайн'} | 📅 {conf.date}\n"
        text += f"   {stars} <b>{avg}</b> ({count} оценок)\n"
        text += f"   {status}\n"

        if conf.description:
            desc = conf.description[:120] + "..." if len(conf.description) > 120 else conf.description
            text += f"   📝 {desc}\n"

        text += "━━━━━━━━━━━━━━━━━━━━━━━\n\n"

    text += "💡 <i>Рейтинг обновляется автоматически после каждой оценки.</i>"

    await message.answer(text, parse_mode="HTML")

# ⭐ Обработка оценки конференции
@router.callback_query(F.data.startswith("rate_conf_"))
async def process_rating(callback: types.CallbackQuery, state: FSMContext):
    _, conf_id, rating_str = callback.data.split("_")
    conf_id = int(conf_id)
    rating = int(rating_str)

    await state.update_data(conference_id=conf_id, rating=rating)
    await state.set_state(ConferenceRatingState.review)

    await callback.message.edit_text(
        f"Вы поставили <b>{'⭐' * rating}</b> ({rating}/5)\n\n"
        "Напишите отзыв (или нажмите «Пропустить»):",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="⏭ Пропустить", callback_data=f"skip_review_{conf_id}")
        ]])
    )
    await callback.answer()


@router.message(ConferenceRatingState.review)
@router.callback_query(F.data.startswith("skip_review_"))
async def save_rating(event: types.Message | types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    conf_id = data["conference_id"]
    rating = data["rating"]
    review = None

    if isinstance(event, types.Message):
        review = event.text.strip()
    else:  # skip
        review = None

    async with AsyncSessionLocal() as session:
        conf = await session.get(Conference, conf_id)
        user = await get_or_create_user(event.from_user.id)

        rating_obj = ConferenceRating(
            user_id=user.id,
            conference_id=conf_id,
            rating=rating,
            review=review
        )
        session.add(rating_obj)
        await session.commit()

    await event.answer("✅ Спасибо за оценку! Ваше мнение очень важно.", show_alert=True)
    if isinstance(event, types.Message):
        await event.answer("✅ Оценка сохранена!", reply_markup=get_main_menu_keyboard(user.role))
    await state.clear()



# Отмена
@router.callback_query(F.data == "cancel_form")
async def cancel_form(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.edit_text("Форма отменена.", reply_markup=get_main_menu_keyboard("Участник"))
    await callback.answer()

async def is_user_banned(telegram_id: int) -> bool:
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(User).where(User.telegram_id == telegram_id)
        )
        user = result.scalar_one_or_none()
        return bool(user and user.is_banned)


async def block_if_banned(event):
    user_id = event.from_user.id

    if await is_user_banned(user_id):
        if isinstance(event, CallbackQuery):
            await event.answer(
                "🚫 Вы заблокированы и не можете пользоваться ботом.",
                show_alert=True
            )
        elif isinstance(event, Message):
            await event.answer(
                "🚫 Вы заблокированы и не можете пользоваться ботом."
            )
        return True


    return False

@router.message(Command("stats"))
async def stats(message: Message):
    async with AsyncSessionLocal() as session:
        users = await session.scalar(select(func.count(User.id)))
        banned = await session.scalar(select(func.count(User.id)).where(User.is_banned))
        confs = await session.scalar(select(func.count(Conference.id)))
        apps = await session.scalar(select(func.count(Application.id)))

    await message.answer(
        f"📊 <b>Статистика</b>\n\n"
        f"👤 Пользователи: {users}\n"
        f"🚫 Забанены: {banned}\n"
        f"🏛 Конференции: {confs}\n"
        f"📄 Заявки: {apps}"
    )

# =========================
# ОТСЛЕЖИВАНИЕ СТАТУСА ЗАЯВОК (с пагинацией)
# =========================
status_pagination = {}  # хранит индекс для каждого пользователя


@router.message(F.text == "👤 Отслеживание статуса")
async def my_applications_status(message: types.Message):
    user_id = message.from_user.id

    async with AsyncSessionLocal() as session:
        user = await get_or_create_user(user_id)
        result = await session.execute(
            select(Application)
            .options(joinedload(Application.conference))
            .where(Application.user_id == user.id)
            .order_by(Application.id.desc())
        )
        applications = result.scalars().all()

    if not applications:
        await message.answer(
            "📭 У вас пока нет заявок на конференции.",
            reply_markup=get_main_menu_keyboard(user.role)
        )
        return

    # Сохраняем пагинацию
    status_pagination[user_id] = {"applications": applications, "index": 0}
    await show_my_application_status(message, applications, 0)


async def show_my_application_status(target: types.Message | types.CallbackQuery, apps: list, index: int):
    app = apps[index]
    conf = app.conference

    status_emoji = {
        "pending": "⏳",
        "approved": "✅",
        "payment_pending": "💳",
        "payment_sent": "📸",
        "confirmed": "🎟",
        "link_sent": "🔗",
        "rejected": "❌"
    }.get(app.status, "❓")

    text = f"<b>Заявка {index + 1} из {len(apps)}</b>\n\n"
    text += f"🎯 Конференция: <b>{conf.name}</b>\n"
    text += f"📅 Дата: {conf.date}\n"
    text += f"{status_emoji} Статус: <b>{app.status}</b>\n"
    text += f"📋 Комитет: {app.committee or '—'}\n"

    if app.reject_reason:
        text += f"\n❌ Причина отклонения: {app.reject_reason}"

    # ==================== КНОПКИ ====================
    builder = InlineKeyboardBuilder()

    # Проверяем, можно ли показывать кнопку оценки
    user_id = target.from_user.id
    today = datetime.now().date()

    try:
        conf_date = datetime.strptime(conf.date.strip(), "%Y-%m-%d").date()
        can_rate = (
            app.status in ["confirmed", "link_sent"] and
            conf_date < today
        )

        if can_rate:
            # Проверяем, уже оценил ли пользователь эту конференцию
            async with AsyncSessionLocal() as session:
                rating_result = await session.execute(
                    select(ConferenceRating).where(
                        ConferenceRating.user_id == user_id,
                        ConferenceRating.conference_id == conf.id
                    )
                )
                already_rated = rating_result.scalar_one_or_none() is not None

            if not already_rated:
                builder.row(
                    InlineKeyboardButton(
                        text="⭐ Оценить конференцию",
                        callback_data=f"rate_conf_{conf.id}"
                    )
                )
    except:
        pass  # если дата кривая — просто не показываем кнопку

    # Навигация
    nav = []
    if index > 0:
        nav.append(InlineKeyboardButton(text="◀ Назад", callback_data=f"nav_status_{index - 1}"))
    if index < len(apps) - 1:
        nav.append(InlineKeyboardButton(text="▶ Вперёд", callback_data=f"nav_status_{index + 1}"))
    if nav:
        builder.row(*nav)

    builder.row(InlineKeyboardButton(text="🔙 Главное меню", callback_data="back_to_menu"))

    # Отправка/редактирование
    if isinstance(target, types.Message):
        await target.answer(text, reply_markup=builder.as_markup())
    else:
        await target.message.edit_text(text, reply_markup=builder.as_markup())


# Навигация по заявкам
@router.callback_query(F.data.startswith("nav_status_"))
async def navigate_status(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    index = int(callback.data.split("_")[-1])

    data = status_pagination.get(user_id)
    if not data or index < 0 or index >= len(data["applications"]):
        await callback.answer("Сессия истекла или конец списка.", show_alert=True)
        return

    data["index"] = index
    await show_my_application_status(callback, data["applications"], index)
    await callback.answer(f"{index + 1}/{len(data['applications'])}")