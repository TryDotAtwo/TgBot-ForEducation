import os
from dotenv import load_dotenv

load_dotenv()

# Проверка наличия токена
if not os.getenv("BOT_TOKEN"):
    raise ValueError("❌ Токен бота не найден в файле .env!")

BOT_TOKEN = os.getenv("BOT_TOKEN")
MAX_QUESTIONS = 20