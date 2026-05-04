from aiogram.utils.keyboard import ReplyKeyboardBuilder
from aiogram.types import KeyboardButton
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

def get_main_menu_keyboard(role: str):
    builder = ReplyKeyboardBuilder()
    builder.row(KeyboardButton(text="🔄 Обновить"))

    if role == "Участник":
        builder.row(
            KeyboardButton(text="🔍 Просмотр конференций"),
            KeyboardButton(text="📝 Подать заявку на участие")
        )
        builder.row(KeyboardButton(text="🏆 Лучшие конференции"))
        builder.row(KeyboardButton(text="➕ Создать конференцию"))
        builder.row(KeyboardButton(text="📩 Обращение к тех. специалисту"))

    elif role == "Организатор":
        builder.row(
            KeyboardButton(text="📋 Мои конференции"),
            KeyboardButton(text="📩 Заявки участников")
        )
        builder.row(
            KeyboardButton(text="🔍 Просмотр конференций"),
            KeyboardButton(text="📝 Подать заявку на участие")
        )
        builder.row(KeyboardButton(text="🏆 Лучшие конференции"))
        builder.row(KeyboardButton(text="🗃 Архив заявок"))
        builder.row(KeyboardButton(text="📩 Обращение к тех. специалисту"))

    elif role == "Глав Тех Специалист":
        builder.row(
            KeyboardButton(text="⚠ Бан/разбан пользователей"),
            KeyboardButton(text="🔑 Назначить роль другим пользователям")
        )
        builder.row(
            KeyboardButton(text="📩 Обращения пользователей"),
            KeyboardButton(text="📤 Экспорт обращений")
        )
        builder.row(
            KeyboardButton(text="📤 Экспорт данных бота"),
            KeyboardButton(text="📊 Статистика")
        )
        builder.row(KeyboardButton(text="👥 Все пользователи"))
        builder.row(
            KeyboardButton(text="🗂 Все конференции"),
            KeyboardButton(text="🗑 Удалить конференцию")
        )
        builder.row(KeyboardButton(text="📜 Логи действий"))  # ← добавь эту строку
        builder.row(KeyboardButton(text="📢 Рассылка всем пользователям"))

    elif role == "Админ":
        builder.row(
            KeyboardButton(text="📩 Просмотр заявок на конференции"),
            KeyboardButton(text="✏️ Заявки на редактирование")
        )
        builder.row(
            KeyboardButton(text="🗂 Все конференции"),
            KeyboardButton(text="🗑 Удалить конференцию")
        )
        builder.row(KeyboardButton(text="👥 Все пользователи"))
        builder.row(KeyboardButton(text="⚠ Бан/разбан пользователей"))
        builder.row(KeyboardButton(text="📊 Статистика"))
        builder.row(KeyboardButton(text="📩 Обращение к тех. специалисту"))

    elif role == "Главный Админ":
        builder.row(
            KeyboardButton(text="📩 Просмотр заявок на конференции"),
            KeyboardButton(text="✏️ Заявки на редактирование")
        )
        builder.row(
            KeyboardButton(text="📥 Посмотреть апелляции"),
            KeyboardButton(text="🗂 Все конференции")
        )
        builder.row(KeyboardButton(text="👥 Все пользователи"))
        builder.row(KeyboardButton(text="📊 Статистика"))
        builder.row(KeyboardButton(text="⚠ Бан/разбан пользователей"))
        builder.row(KeyboardButton(text="📤 Экспорт данных бота"))
        builder.row(KeyboardButton(text="📩 Обращение к тех. специалисту"))

    else:
        builder.row(KeyboardButton(text="📩 Обращение к тех. специалисту"))

    if role in ["Участник", "Организатор"]:
        builder.row(KeyboardButton(text="👤 Отслеживание статуса"))

    return builder.as_markup()

# Инлайн-клавиатура со списком конференций
def get_conferences_keyboard(conferences):
    builder = InlineKeyboardBuilder()
    for conf in conferences:
        text = f"{conf.name}"
        details = []
        if conf.city:
            details.append(conf.city)
        if conf.date:  # Одна дата
            details.append(conf.date)
        if details:
            text += f" ({', '.join(details)})"
        builder.button(text=text, callback_data=f"select_conf_{conf.id}")
    builder.adjust(1)
    return builder.as_markup()

# Кнопка отмены
def get_cancel_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_form")]
    ])

# Клавиатура для оценки конференции (1–5 звёзд)
def get_rating_keyboard(conference_id: int):
    builder = InlineKeyboardBuilder()
    for i in range(1, 6):
        builder.button(text=f"{'⭐' * i} {i}", callback_data=f"rate_conf_{conference_id}_{i}")
    builder.adjust(1)
    builder.row(InlineKeyboardButton(text="❌ Пропустить оценку", callback_data=f"skip_rating_{conference_id}"))
    return builder.as_markup()