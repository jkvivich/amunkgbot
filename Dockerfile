FROM python:3.12-slim

WORKDIR /app

# Устанавливаем зависимости
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Копируем весь код
COPY . .

# Создаём папки для медиа
RUN mkdir -p qr_codes posters payments support_screenshots

CMD ["python", "bot.py"]