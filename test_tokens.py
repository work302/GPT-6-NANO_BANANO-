
import asyncio

from app.DataBase.db_main import get_user_tokens, spend_tokens


async def main():
    telegram_id = int(input("Введи свой Telegram ID: "))

    print("Баланс до списания:", await get_user_tokens(telegram_id))

    success = await spend_tokens(telegram_id, 100)

    if success:
        print("Успешно списано 3 токена")
    else:
        print("Списание не выполнено: пользователя нет или недостаточно токенов")

    print("Баланс после списания:", await get_user_tokens(telegram_id))


asyncio.run(main())
