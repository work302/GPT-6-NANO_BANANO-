
import os
from pathlib import Path
from dotenv import load_dotenv

# Загружаем .env как из корня проекта, так и из папки app (если файл лежит там)
_CURRENT_DIR = Path(__file__).resolve().parent
load_dotenv(_CURRENT_DIR.parent / ".env")
load_dotenv(_CURRENT_DIR / ".env")
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
RUNBLOB_API_KEY = os.getenv("RUNBLOB_API_KEY", "").strip()
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
GEMINI_PROXY = os.getenv("GEMINI_PROXY", "").strip()

# Настройки ЮKassa (ID магазина и секретный ключ из личного кабинета ЮKassa)
YOOKASSA_SHOP_ID = os.getenv("YOOKASSA_SHOP_ID", "").strip()
YOOKASSA_SECRET_KEY = os.getenv("YOOKASSA_SECRET_KEY", "").strip()
YOOKASSA_RETURN_URL = os.getenv("YOOKASSA_RETURN_URL", "https://t.me/GPT6_NANO_BANANO").strip()

# Настройки техподдержки, обязательного канала и каналов для заданий (можно менять здесь или в .env)
SUPPORT_USERNAME = os.getenv("SUPPORT_USERNAME", "@Rendix_dev").strip()

REQUIRED_CHANNEL_URL = os.getenv("REQUIRED_CHANNEL_URL", "https://t.me/GPT6_NANO_BANANO").strip()
REQUIRED_CHANNEL_ID = os.getenv("REQUIRED_CHANNEL_ID", "@GPT6_NANO_BANANO").strip()


# 3 канала для раздела «Бесплатные токены -> Выполнить задания» (меняйте ссылки на нужные вам)
TASK_CHANNEL_1_URL = os.getenv("TASK_CHANNEL_1_URL", "https://t.me/CuttingEdgeNeuralNetworks").strip()
TASK_CHANNEL_2_URL = os.getenv("TASK_CHANNEL_2_URL", "https://t.me/neural_network_generationZ").strip()
TASK_CHANNEL_3_URL = os.getenv("TASK_CHANNEL_3_URL", "https://t.me/GPTclaudeNew").strip()