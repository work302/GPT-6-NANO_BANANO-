import asyncio
import aiosqlite

from app.DataBase.db_main import init_db, register_user, DB_PATH


async def main():
    await init_db()

    await register_user(123456789, "test_user")

    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT telegram_id, username, tokens FROM users WHERE telegram_id = ?",
            (123456789,)
        ) as cursor:
            user = await cursor.fetchone()

    print(user)


asyncio.run(main())
