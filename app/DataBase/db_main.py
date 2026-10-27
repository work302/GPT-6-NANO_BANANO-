import aiosqlite
from pathlib import Path
from datetime import datetime, timedelta, timezone

DB_PATH = Path(__file__).resolve().parent / "bot.db"


async def init_db():
    async with aiosqlite.connect(DB_PATH, timeout=15.0) as db:
        await db.execute("PRAGMA journal_mode=WAL")
        await db.execute("PRAGMA busy_timeout=10000")
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                telegram_id INTEGER PRIMARY KEY,
                username TEXT,
                tokens INTEGER NOT NULL DEFAULT 10
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS subscriptions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_id INTEGER NOT NULL,
                product_code TEXT NOT NULL,
                started_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                expires_at TEXT,
                status TEXT NOT NULL DEFAULT 'active',
                telegram_payment_charge_id TEXT UNIQUE
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS payments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                provider TEXT NOT NULL,
                provider_payment_id TEXT NOT NULL,
                telegram_id INTEGER NOT NULL,
                product_type TEXT NOT NULL,
                product_code TEXT NOT NULL,
                amount INTEGER NOT NULL,
                currency TEXT NOT NULL,
                tokens_granted INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'pending',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                paid_at TEXT,
                UNIQUE(provider, provider_payment_id)
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS token_transactions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_id INTEGER NOT NULL,
                amount INTEGER NOT NULL,
                operation TEXT NOT NULL,
                reason TEXT,
                payment_id INTEGER UNIQUE,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS referrals (
                referred_id INTEGER PRIMARY KEY,
                referrer_id INTEGER NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS referral_stats (
                user_id INTEGER PRIMARY KEY,
                invited_count INTEGER NOT NULL DEFAULT 0,
                earned_tokens REAL NOT NULL DEFAULT 0.0
            )
        """)
        await db.commit()


async def register_user(telegram_id: int, username: str | None = None):
    async with aiosqlite.connect(DB_PATH, timeout=15.0) as db:
        await db.execute("PRAGMA busy_timeout=10000")
        await db.execute(
            "INSERT OR IGNORE INTO users (telegram_id, username) VALUES (?, ?)",
            (telegram_id, username),
        )
        if username:
            await db.execute(
                "UPDATE users SET username = ? WHERE telegram_id = ? AND (username IS NULL OR username != ?)",
                (username, telegram_id, username),
            )
        await db.commit()


async def get_user_tokens(telegram_id: int) -> int:
    async with aiosqlite.connect(DB_PATH, timeout=15.0) as db:
        await db.execute("PRAGMA busy_timeout=10000")
        await db.execute(
            "INSERT OR IGNORE INTO users (telegram_id, username) VALUES (?, NULL)",
            (telegram_id,),
        )
        await db.commit()
        async with db.execute(
            "SELECT tokens FROM users WHERE telegram_id = ?", (telegram_id,)
        ) as cursor:
            row = await cursor.fetchone()
    return int(row[0]) if row else 0


async def spend_tokens(telegram_id: int, amount: int) -> bool:
    if amount <= 0:
        raise ValueError("Количество токенов должно быть больше нуля")
    async with aiosqlite.connect(DB_PATH, timeout=15.0) as db:
        await db.execute("PRAGMA busy_timeout=10000")
        await db.execute(
            "INSERT OR IGNORE INTO users (telegram_id, username) VALUES (?, NULL)",
            (telegram_id,),
        )
        cursor = await db.execute(
            """UPDATE users SET tokens = tokens - ?
               WHERE telegram_id = ? AND tokens >= ?""",
            (amount, telegram_id, amount),
        )
        await db.commit()
        return cursor.rowcount == 1


async def add_tokens(telegram_id: int, amount: int) -> bool:
    if amount <= 0:
        raise ValueError("Количество токенов должно быть больше нуля")
    async with aiosqlite.connect(DB_PATH, timeout=15.0) as db:
        await db.execute("PRAGMA busy_timeout=10000")
        await db.execute(
            "INSERT OR IGNORE INTO users (telegram_id, username) VALUES (?, NULL)",
            (telegram_id,),
        )
        cursor = await db.execute(
            "UPDATE users SET tokens = tokens + ? WHERE telegram_id = ?",
            (amount, telegram_id),
        )
        await db.commit()
        return cursor.rowcount == 1


def _parse_datetime(value: str) -> datetime:
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


async def get_premium_until(telegram_id: int) -> datetime | None:
    async with aiosqlite.connect(DB_PATH, timeout=15.0) as db:
        await db.execute("PRAGMA busy_timeout=10000")
        async with db.execute(
            """SELECT expires_at FROM subscriptions
               WHERE telegram_id = ? AND status = 'active'
               ORDER BY expires_at DESC LIMIT 1""",
            (telegram_id,),
        ) as cursor:
            row = await cursor.fetchone()
    if not row or not row[0]:
        return None
    expires_at = _parse_datetime(row[0])
    if expires_at <= datetime.now(timezone.utc):
        return None
    return expires_at


async def has_premium(telegram_id: int) -> bool:
    return await get_premium_until(telegram_id) is not None


async def activate_premium(
    telegram_id: int, product_code: str, payment_charge_id: str
) -> datetime:
    now = datetime.now(timezone.utc)
    current_until = await get_premium_until(telegram_id)
    base_time = current_until if (current_until and current_until > now) else now
    expires_at = base_time + timedelta(days=30)

    async with aiosqlite.connect(DB_PATH, timeout=15.0) as db:
        await db.execute("PRAGMA busy_timeout=10000")
        await db.execute(
            "INSERT OR IGNORE INTO users (telegram_id, username) VALUES (?, NULL)",
            (telegram_id,),
        )
        await db.execute(
            """INSERT INTO subscriptions (
                   telegram_id, product_code, started_at, expires_at,
                   status, telegram_payment_charge_id
               ) VALUES (?, ?, ?, ?, 'active', ?)""",
            (
                telegram_id, product_code, now.isoformat(),
                expires_at.isoformat(), payment_charge_id,
            ),
        )
        await db.commit()
    return expires_at


async def complete_token_payment(
    *,
    provider: str,
    provider_payment_id: str,
    telegram_id: int,
    product_code: str,
    amount: int,
    currency: str,
    tokens: int,
) -> bool:
    """Атомарно начисляет токены 1 раз; повторный платёжный ID не начисляется снова."""
    if amount <= 0 or tokens <= 0:
        raise ValueError("Сумма и количество токенов должны быть больше нуля")
    now = datetime.now(timezone.utc).isoformat()

    async with aiosqlite.connect(DB_PATH, timeout=15.0) as db:
        await db.execute("PRAGMA busy_timeout=10000")
        await db.execute("BEGIN IMMEDIATE")
        try:
            # Гарантируем, что пользователь есть в таблице users
            await db.execute(
                "INSERT OR IGNORE INTO users (telegram_id, username) VALUES (?, NULL)",
                (telegram_id,),
            )

            await db.execute(
                """INSERT OR IGNORE INTO payments (
                       provider, provider_payment_id, telegram_id, product_type,
                       product_code, amount, currency, tokens_granted, status
                   ) VALUES (?, ?, ?, 'tokens', ?, ?, ?, ?, 'pending')""",
                (
                    provider, provider_payment_id, telegram_id, product_code,
                    amount, currency, tokens,
                ),
            )
            async with db.execute(
                """SELECT id, telegram_id, product_type, product_code, amount,
                          currency, tokens_granted, status
                   FROM payments WHERE provider = ? AND provider_payment_id = ?""",
                (provider, provider_payment_id),
            ) as cursor:
                payment = await cursor.fetchone()

            if not payment:
                await db.rollback()
                raise RuntimeError("Не удалось найти платёж")

            (
                payment_id, saved_user, ptype, pcode, saved_amount,
                saved_currency, saved_tokens, status,
            ) = payment

            if (
                saved_user != telegram_id
                or ptype != "tokens"
                or pcode != product_code
                or saved_amount != amount
                or saved_currency != currency
                or saved_tokens != tokens
            ):
                await db.rollback()
                raise ValueError("Данные платежа не совпадают с сохранёнными")

            if status == "paid":
                await db.rollback()
                return False

            cursor = await db.execute(
                "UPDATE users SET tokens = tokens + ? WHERE telegram_id = ?",
                (tokens, telegram_id),
            )
            if cursor.rowcount != 1:
                await db.rollback()
                raise ValueError("Пользователь не найден в базе данных")

            await db.execute(
                """INSERT INTO token_transactions
                   (telegram_id, amount, operation, reason, payment_id)
                   VALUES (?, ?, 'credit', ?, ?)""",
                (telegram_id, tokens, f"Покупка токенов: {product_code}", payment_id),
            )

            await db.execute(
                "UPDATE payments SET status = 'paid', paid_at = ? WHERE id = ?",
                (now, payment_id),
            )
            await db.commit()
            return True
        except Exception:
            await db.rollback()
            raise


async def complete_premium_payment(
    *,
    provider: str,
    provider_payment_id: str,
    telegram_id: int,
    product_code: str,
    amount: int,
    currency: str,
    telegram_payment_charge_id: str,
    tokens: int = 0,
) -> datetime | None:
    """
    Активирует Premium один раз и (если передан параметр tokens > 0) атомарно
    начисляет входящие в тариф токены в рамках одной транзакции.
    Возвращает дату окончания подписки или None, если платёж уже был обработан.
    """
    if amount <= 0:
        raise ValueError("Сумма платежа должна быть больше нуля")
    now = datetime.now(timezone.utc)

    async with aiosqlite.connect(DB_PATH, timeout=15.0) as db:
        await db.execute("PRAGMA busy_timeout=10000")
        await db.execute("BEGIN IMMEDIATE")
        try:
            # Гарантируем наличие пользователя в таблице users
            await db.execute(
                "INSERT OR IGNORE INTO users (telegram_id, username) VALUES (?, NULL)",
                (telegram_id,),
            )

            await db.execute(
                """INSERT OR IGNORE INTO payments (
                       provider, provider_payment_id, telegram_id, product_type,
                       product_code, amount, currency, tokens_granted, status
                   ) VALUES (?, ?, ?, 'premium', ?, ?, ?, ?, 'pending')""",
                (
                    provider, provider_payment_id, telegram_id, product_code,
                    amount, currency, max(0, int(tokens)),
                ),
            )
            async with db.execute(
                """SELECT id, telegram_id, product_type, product_code, amount,
                          currency, status
                   FROM payments WHERE provider = ? AND provider_payment_id = ?""",
                (provider, provider_payment_id),
            ) as cursor:
                payment = await cursor.fetchone()

            if not payment:
                await db.rollback()
                raise RuntimeError("Не удалось найти платёж")

            (
                payment_id, saved_user, ptype, pcode, saved_amount,
                saved_currency, status,
            ) = payment
            if (
                saved_user != telegram_id
                or ptype != "premium"
                or pcode != product_code
                or saved_amount != amount
                or saved_currency != currency
            ):
                await db.rollback()
                raise ValueError("Данные платежа не совпадают с сохранёнными")

            if status == "paid":
                await db.rollback()
                return None

            # Если у пользователя уже есть активная подписка, продлеваем её от текущей даты окончания
            async with db.execute(
                """SELECT expires_at FROM subscriptions
                   WHERE telegram_id = ? AND status = 'active'
                   ORDER BY expires_at DESC LIMIT 1""",
                (telegram_id,),
            ) as sub_cursor:
                active_row = await sub_cursor.fetchone()

            base_time = now
            if active_row and active_row[0]:
                existing_exp = _parse_datetime(active_row[0])
                if existing_exp > now:
                    base_time = existing_exp

            expires_at = base_time + timedelta(days=30)

            try:
                await db.execute(
                    """INSERT INTO subscriptions (
                           telegram_id, product_code, started_at, expires_at,
                           status, telegram_payment_charge_id
                       ) VALUES (?, ?, ?, ?, 'active', ?)""",
                    (
                        telegram_id, product_code, now.isoformat(),
                        expires_at.isoformat(), telegram_payment_charge_id,
                    ),
                )
            except aiosqlite.IntegrityError:
                await db.rollback()
                return None

            if tokens > 0:
                await db.execute(
                    "UPDATE users SET tokens = tokens + ? WHERE telegram_id = ?",
                    (int(tokens), telegram_id),
                )
                await db.execute(
                    """INSERT INTO token_transactions
                       (telegram_id, amount, operation, reason, payment_id)
                       VALUES (?, ?, 'credit', ?, ?)""",
                    (telegram_id, int(tokens), f"Подписка Premium: {product_code}", payment_id),
                )

            await db.execute(
                "UPDATE payments SET status = 'paid', paid_at = ? WHERE id = ?",
                (now.isoformat(), payment_id),
            )
            await db.commit()
            return expires_at
        except Exception:
            await db.rollback()
            raise
