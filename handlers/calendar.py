# handlers/calendar.py
from aiogram import Router, types, F
from aiogram.fsm.context import FSMContext
from aiogram.utils.keyboard import InlineKeyboardBuilder
from datetime import datetime, timedelta

router = Router()

MONTHS = ["Янв", "Фев", "Мар", "Апр", "Май", "Июн", "Июл", "Авг", "Сен", "Окт", "Ноя", "Дек"]

def calendar_keyboard(year: int, month: int):
    builder = InlineKeyboardBuilder()
    builder.row(types.InlineKeyboardButton(text=f"« {year-1}", callback_data=f"cal_year_{year-1}_{month}"),
                types.InlineKeyboardButton(text=f"{year}", callback_data="ignore"),
                types.InlineKeyboardButton(text=f"{year+1} »", callback_data=f"cal_year_{year+1}_{month}"))

    builder.row(types.InlineKeyboardButton(text=f"« {MONTHS[month-2]}", callback_data=f"cal_month_{year}_{month-1}"),
                types.InlineKeyboardButton(text=f"{MONTHS[month-1]}", callback_data="ignore"),
                types.InlineKeyboardButton(text=f"{MONTHS[month % 12]} »", callback_data=f"cal_month_{year}_{month+1}"))

    # дни
    start_day = datetime(year, month, 1).weekday()
    days_in_month = (datetime(year, month+1, 1) - timedelta(days=1)).day

    for i in range(42):
        if i < start_day or i >= start_day + days_in_month:
            builder.button(text=" ", callback_data="ignore")
        else:
            day = i - start_day + 1
            builder.button(text=str(day), callback_data=f"cal_day_{year}_{month}_{day}")
        if (i + 1) % 7 == 0:
            builder.adjust(7)
    return builder.as_markup()