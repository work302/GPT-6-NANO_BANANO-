import asyncio
import logging
from aiogram import Bot, Dispatcher
from aiogram.filters import CommandStart
from .config import BOT_TOKEN
from .handlers import user
from aiogram import Router
from .DataBase.db_main import init_db


async def main():
    logging.basicConfig(level=logging.INFO)
    await init_db()
    dp = Dispatcher()
    bot = Bot(token=BOT_TOKEN)
    dp.include_router(user)
    await dp.start_polling(bot)

if __name__ == "__main__":
        asyncio.run(main())
    