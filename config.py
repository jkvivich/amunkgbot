import os
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
TECH_SPECIALIST_ID = int(os.getenv("TECH_SPECIALIST_ID"))
CHIEF_ADMIN_IDS_STR = os.getenv("CHIEF_ADMIN_IDS", "")
CHIEF_ADMIN_IDS = [int(id_str.strip()) for id_str in CHIEF_ADMIN_IDS_STR.split(",") if id_str.strip()]

# === НОВОЕ ===
DATABASE_URL = os.getenv("DATABASE_URL")      # будет от PostgreSQL на Railway
REDIS_URL = os.getenv("REDIS_URL")            # будет от Redis на Railway

if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN не найден!")
if not DATABASE_URL:
    print("ВНИМАНИЕ: DATABASE_URL не найден — используем SQLite для локальной разработки")