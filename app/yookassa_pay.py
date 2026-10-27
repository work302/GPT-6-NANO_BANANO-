import asyncio
import base64
import json
import logging
import os
import socket
import sqlite3
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

import aiohttp
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup
from dotenv import load_dotenv

_CURRENT_DIR = Path(__file__).resolve().parent
load_dotenv(_CURRENT_DIR.parent / ".env")
load_dotenv(_CURRENT_DIR / ".env")

try:
    from .config import (
        YOOKASSA_RETURN_URL as _CFG_RETURN_URL,
        YOOKASSA_SECRET_KEY as _CFG_SECRET_KEY,
        YOOKASSA_SHOP_ID as _CFG_SHOP_ID,
    )
except Exception:
    try:
        from app.config import (
            YOOKASSA_RETURN_URL as _CFG_RETURN_URL,
            YOOKASSA_SECRET_KEY as _CFG_SECRET_KEY,
            YOOKASSA_SHOP_ID as _CFG_SHOP_ID,
        )
    except Exception:
        _CFG_SHOP_ID = ""
        _CFG_SECRET_KEY = ""
        _CFG_RETURN_URL = "https://t.me/GPT6_NANO_BANANO"

try:
    from .DataBase.db_main import (
        DB_PATH,
        add_tokens,
        complete_premium_payment,
        complete_token_payment,
        get_user_tokens,
    )
except Exception:
    from app.DataBase.db_main import (
        DB_PATH,
        add_tokens,
        complete_premium_payment,
        complete_token_payment,
        get_user_tokens,
    )

YOOKASSA_API_BASE = "https://api.yookassa.ru/v3/payments"
# Увеличенный таймаут для надёжного соединения на Windows и через провайдеров РФ
YOOKASSA_HTTP_TIMEOUT = aiohttp.ClientTimeout(total=4.5, connect=3.5, sock_read=3.5)


def _get_yookassa_credentials() -> tuple[str, str, str]:
    """Возвращает актуальные (shop_id, secret_key, return_url) из .env или config.py."""
    shop_id = (os.getenv("YOOKASSA_SHOP_ID") or _CFG_SHOP_ID or "").strip()
    secret_key = (os.getenv("YOOKASSA_SECRET_KEY") or _CFG_SECRET_KEY or "").strip()
    return_url = (os.getenv("YOOKASSA_RETURN_URL") or _CFG_RETURN_URL or "").strip()
    if not return_url.startswith(("http://", "https://")):
        return_url = "https://t.me/GPT6_NANO_BANANO"
    return shop_id, secret_key, return_url


@dataclass
class YooKassaPaymentData:
    id: str
    status: str
    paid: bool
    confirmation_url: str | None
    amount_value: str
    amount_currency: str
    metadata: dict


# Тарифы подписок Premium для оплаты через ЮKassa (в рублях)
YOOKASSA_PREMIUM_PLANS: dict[str, dict] = {
    "599": {
        "code": "start",
        "title": "СТАРТОВЫЙ",
        "price": "599.00",
        "price_label": "599,00",
        "display_price": "599 ₽",
        "full_price": "599 ₽",
        "tokens": 150,
    },
    "999": {
        "code": "opt",
        "title": "ОПТИМАЛЬНЫЙ",
        "price": "999.00",
        "price_label": "999,00",
        "display_price": "999 ₽",
        "full_price": "1249 ₽",
        "tokens": 350,
    },
    "1439": {
        "code": "adv",
        "title": "ПРОДВИНУТЫЙ",
        "price": "1439.00",
        "price_label": "1439,00",
        "display_price": "1439 ₽",
        "full_price": "2210 ₽",
        "tokens": 700,
    },
    "2390": {
        "code": "max",
        "title": "ВЫГОДНЫЙ",
        "price": "2390.00",
        "price_label": "2390,00",
        "display_price": "2390 ₽",
        "full_price": "3990 ₽",
        "tokens": 1500,
    },
}

# Пакеты токенов для оплаты через ЮKassa (в рублях)
YOOKASSA_TOKEN_PACKAGES: dict[str, dict] = {
    "359": {
        "code": "100",
        "tokens": 100,
        "title": "Пакет 100 токенов",
        "price": "359.00",
        "price_label": "359,00",
        "display_price": "359 ₽",
        "stars_callback": "pay_tok_stars_100",
    },
    "863": {
        "code": "350",
        "tokens": 350,
        "title": "Пакет 350 токенов",
        "price": "863.00",
        "price_label": "863,00",
        "display_price": "863 ₽",
        "stars_callback": "pay_tok_stars_350",
    },
    "2199": {
        "code": "1000",
        "tokens": 1000,
        "title": "Пакет 1000 токенов",
        "price": "2199.00",
        "price_label": "2199,00",
        "display_price": "2199 ₽",
        "stars_callback": "pay_tok_stars_1000",
    },
}

# Защита от гонок (race condition) и двойного начисления:
_PAYMENT_LOCKS: dict[str, asyncio.Lock] = {}
_PROCESSED_PAYMENTS: set[str] = set()

# Храним реферальные таблицы в основной базе bot.db
_REFERRAL_DB_PATH = DB_PATH


def _init_referral_db() -> None:
    _REFERRAL_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(_REFERRAL_DB_PATH, timeout=10.0) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS referrals (
                referred_id INTEGER PRIMARY KEY,
                referrer_id INTEGER NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS referral_stats (
                user_id INTEGER PRIMARY KEY,
                invited_count INTEGER NOT NULL DEFAULT 0,
                earned_tokens REAL NOT NULL DEFAULT 0.0
            )
            """
        )
        conn.commit()


def get_referral_stats(user_id: int) -> tuple[int, float]:
    """Возвращает (количество приглашённых, заработано токенов) для пользователя."""
    _init_referral_db()
    with sqlite3.connect(_REFERRAL_DB_PATH, timeout=10.0) as conn:
        row = conn.execute(
            "SELECT invited_count, earned_tokens FROM referral_stats WHERE user_id = ?",
            (user_id,),
        ).fetchone()
        if not row:
            return 0, 0.0
        return int(row[0]), round(float(row[1]), 1)


async def register_referral_if_new(referred_id: int, referrer_id: int) -> bool:
    """
    Привязывает нового приглашённого пользователя к рефереру (только 1 раз)
    и начисляет рефереру +30 бонусных токенов.
    """
    if referred_id == referrer_id or referrer_id <= 0:
        return False

    _init_referral_db()
    with sqlite3.connect(_REFERRAL_DB_PATH, timeout=10.0) as conn:
        existing = conn.execute(
            "SELECT referrer_id FROM referrals WHERE referred_id = ?",
            (referred_id,),
        ).fetchone()
        if existing is not None:
            return False

        conn.execute(
            "INSERT INTO referrals (referred_id, referrer_id) VALUES (?, ?)",
            (referred_id, referrer_id),
        )
        conn.execute(
            """
            INSERT INTO referral_stats (user_id, invited_count, earned_tokens)
            VALUES (?, 1, 30.0)
            ON CONFLICT(user_id) DO UPDATE SET
                invited_count = invited_count + 1,
                earned_tokens = earned_tokens + 30.0
            """,
            (referrer_id,),
        )
        conn.commit()

    await add_tokens(referrer_id, 30)
    return True


async def credit_referrer_commission(buyer_id: int, purchased_tokens: int) -> tuple[int | None, int]:
    """
    Начисляет пригласившему пользователю 30% от купленных другом токенов.
    Возвращает (referrer_id, bonus_tokens) или (None, 0).
    """
    if purchased_tokens <= 0:
        return None, 0

    _init_referral_db()
    with sqlite3.connect(_REFERRAL_DB_PATH, timeout=10.0) as conn:
        row = conn.execute(
            "SELECT referrer_id FROM referrals WHERE referred_id = ?",
            (buyer_id,),
        ).fetchone()
        if not row:
            return None, 0

        referrer_id = int(row[0])
        bonus_exact = round(purchased_tokens * 0.3, 1)
        bonus_int = max(1, int(round(bonus_exact)))

        conn.execute(
            """
            INSERT INTO referral_stats (user_id, invited_count, earned_tokens)
            VALUES (?, 0, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                earned_tokens = earned_tokens + ?
            """,
            (referrer_id, bonus_exact, bonus_exact),
        )
        conn.commit()

    await add_tokens(referrer_id, bonus_int)
    return referrer_id, bonus_int


def _get_payment_lock(payment_id: str) -> asyncio.Lock:
    if payment_id not in _PAYMENT_LOCKS:
        _PAYMENT_LOCKS[payment_id] = asyncio.Lock()
    return _PAYMENT_LOCKS[payment_id]


def subscription_offer_text(price_key: str) -> str:
    """
    Формирует текст при выборе пакета подписки:
    «💳 Вы оформляете подписку за (сумма) и получаете кол во токенов на 30 дней.
    По окончании срока действия тарифа, он будет автоматически продлен по полной стоимости – (сумма) на 30 дней.»
    """
    plan = YOOKASSA_PREMIUM_PLANS[price_key]
    return (
        f"💳 Вы оформляете подписку за {plan['display_price']} и получаете "
        f"{plan['tokens']} токенов на 30 дней.\n\n"
        f"По окончании срока действия тарифа, он будет автоматически продлен "
        f"по полной стоимости – {plan['full_price']} на 30 дней."
    )


def _parse_payment_json(data: dict) -> YooKassaPaymentData:
    confirmation = data.get("confirmation") or {}
    amount = data.get("amount") or {}
    return YooKassaPaymentData(
        id=str(data.get("id", "")),
        status=str(data.get("status", "pending")),
        paid=bool(data.get("paid", False)),
        confirmation_url=confirmation.get("confirmation_url"),
        amount_value=str(amount.get("value", "0.00")),
        amount_currency=str(amount.get("currency", "RUB")),
        metadata=dict(data.get("metadata") or {}),
    )


def _build_receipt_payload(base_payload: dict, amount_value: str, description: str) -> dict:
    """Формирует тело чека 54-ФЗ для магазинов ЮKassa с обязательной фискализацией."""
    return {
        **base_payload,
        "receipt": {
            "customer": {
                "email": "customer@example.com",
            },
            "items": [
                {
                    "description": description[:128],
                    "quantity": "1.00",
                    "amount": {
                        "value": amount_value,
                        "currency": "RUB",
                    },
                    "vat_code": 1,
                    "payment_mode": "full_payment",
                    "payment_subject": "service",
                }
            ],
        },
    }


def _urllib_yookassa_request(
    method: str,
    url: str,
    shop_id: str,
    secret_key: str,
    payload: dict | None = None,
    timeout: float = 3.5,
) -> tuple[int, dict]:
    """
    Резервный синхронный запрос через стандартный стек urllib Python.
    Использует системные сокеты Windows, что помогает, если aiohttp блокируется TUN/VPN адаптерами.
    """
    auth_str = f"{shop_id}:{secret_key}"
    auth_b64 = base64.b64encode(auth_str.encode("utf-8")).decode("ascii")
    headers = {
        "Authorization": f"Basic {auth_b64}",
        "Content-Type": "application/json",
        "Idempotence-Key": str(uuid.uuid4()),
    }
    data_bytes = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(url, data=data_bytes, headers=headers, method=method)

    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            resp_bytes = response.read()
            resp_json = json.loads(resp_bytes.decode("utf-8"))
            return response.status, resp_json
    except urllib.error.HTTPError as http_err:
        err_bytes = http_err.read()
        try:
            err_json = json.loads(err_bytes.decode("utf-8"))
        except Exception:
            err_json = {"description": str(http_err)}
        return http_err.code, err_json


async def _create_payment_in_yookassa(
    amount_value: str,
    description: str,
    metadata: dict,
) -> YooKassaPaymentData:
    """
    Создаёт платёж в ЮKassa.
    1. Сначала пробует асинхронный aiohttp (принудительно IPv4, чтобы исключить зависание IPv6 на Windows).
    2. При таймауте aiohttp автоматически пробует системный стек urllib.
    3. Если магазин требует 54-ФЗ, автоматически повторяет запрос с объектом receipt.
    """
    shop_id, secret_key, return_url = _get_yookassa_credentials()
    if not shop_id or not secret_key:
        raise RuntimeError("Не заданы YOOKASSA_SHOP_ID или YOOKASSA_SECRET_KEY в .env")

    base_payload: dict = {
        "amount": {
            "value": amount_value,
            "currency": "RUB",
        },
        "capture": True,
        "confirmation": {
            "type": "redirect",
            "return_url": return_url,
        },
        "description": description[:128],
        "metadata": metadata,
    }

    # Попытка 1: aiohttp с принудительным IPv4 (AF_INET) и trust_env=True
    auth = aiohttp.BasicAuth(login=shop_id, password=secret_key)
    connector = aiohttp.TCPConnector(family=socket.AF_INET, ssl=True)
    aiohttp_connection_failed = False

    try:
        async with aiohttp.ClientSession(
            connector=connector,
            timeout=YOOKASSA_HTTP_TIMEOUT,
            trust_env=True,
        ) as session:
            headers = {
                "Idempotence-Key": str(uuid.uuid4()),
                "Content-Type": "application/json",
            }
            async with session.post(
                YOOKASSA_API_BASE,
                json=base_payload,
                headers=headers,
                auth=auth,
            ) as resp:
                resp_json = await resp.json(content_type=None)
                if resp.status in (200, 201):
                    return _parse_payment_json(resp_json)

                err_desc = str(resp_json.get("description") or resp_json.get("code") or resp.status)
                err_param = str(resp_json.get("parameter") or "")

                # Если требуется чек 54-ФЗ
                if resp.status == 400 and ("receipt" in err_desc.lower() or "receipt" in err_param.lower()):
                    payload_with_receipt = _build_receipt_payload(base_payload, amount_value, description)
                    retry_headers = {
                        "Idempotence-Key": str(uuid.uuid4()),
                        "Content-Type": "application/json",
                    }
                    async with session.post(
                        YOOKASSA_API_BASE,
                        json=payload_with_receipt,
                        headers=retry_headers,
                        auth=auth,
                    ) as retry_resp:
                        retry_json = await retry_resp.json(content_type=None)
                        if retry_resp.status in (200, 201):
                            return _parse_payment_json(retry_json)
                        retry_err = str(retry_json.get("description") or retry_json.get("code") or retry_resp.status)
                        raise RuntimeError(f"Ошибка ЮKassa ({retry_resp.status}): {retry_err}")

                raise RuntimeError(f"Ошибка ЮKassa ({resp.status}): {err_desc}")

    except (aiohttp.ClientConnectorError, aiohttp.ConnectionTimeoutError, asyncio.TimeoutError) as net_err:
        logging.warning("aiohttp не смог подключиться к ЮKassa (%s), пробуем резервный запрос через urllib...", net_err)
        aiohttp_connection_failed = True

    # Попытка 2: Резервный вызов через стандартный стек urllib в потоке
    if aiohttp_connection_failed:
        try:
            status_code, resp_json = await asyncio.to_thread(
                _urllib_yookassa_request,
                method="POST",
                url=YOOKASSA_API_BASE,
                shop_id=shop_id,
                secret_key=secret_key,
                payload=base_payload,
                timeout=3.5,
            )
            if status_code in (200, 201):
                return _parse_payment_json(resp_json)

            err_desc = str(resp_json.get("description") or resp_json.get("code") or status_code)
            err_param = str(resp_json.get("parameter") or "")

            if status_code == 400 and ("receipt" in err_desc.lower() or "receipt" in err_param.lower()):
                payload_with_receipt = _build_receipt_payload(base_payload, amount_value, description)
                retry_code, retry_json = await asyncio.to_thread(
                    _urllib_yookassa_request,
                    method="POST",
                    url=YOOKASSA_API_BASE,
                    shop_id=shop_id,
                    secret_key=secret_key,
                    payload=payload_with_receipt,
                    timeout=3.5,
                )
                if retry_code in (200, 201):
                    return _parse_payment_json(retry_json)
                retry_err = str(retry_json.get("description") or retry_json.get("code") or retry_code)
                raise RuntimeError(f"Ошибка ЮKassa ({retry_code}): {retry_err}")

            raise RuntimeError(f"Ошибка ЮKassa ({status_code}): {err_desc}")

        except (urllib.error.URLError, TimeoutError, OSError) as url_err:
            logging.error("Резервный запрос к api.yookassa.ru через urllib также завершился ошибкой: %s", url_err)
            raise RuntimeError(
                "Таймаут соединения с api.yookassa.ru. "
                "Если на компьютере включён VPN (для Gemini/Telegram), ЮKassa может блокировать зарубежные IP. "
                "Добавьте api.yookassa.ru в исключения вашего VPN (раздельное туннелирование / Bypass)."
            ) from url_err


async def _fetch_payment_from_yookassa(payment_id: str) -> YooKassaPaymentData:
    """Асинхронно запрашивает актуальный статус платежа у REST API ЮKassa с fallback."""
    shop_id, secret_key, _ = _get_yookassa_credentials()
    if not shop_id or not secret_key:
        raise RuntimeError("Не заданы YOOKASSA_SHOP_ID или YOOKASSA_SECRET_KEY в .env")

    url = f"{YOOKASSA_API_BASE}/{payment_id}"
    auth = aiohttp.BasicAuth(login=shop_id, password=secret_key)
    connector = aiohttp.TCPConnector(family=socket.AF_INET, ssl=True)

    try:
        async with aiohttp.ClientSession(
            connector=connector,
            timeout=YOOKASSA_HTTP_TIMEOUT,
            trust_env=True,
        ) as session:
            async with session.get(url, auth=auth) as resp:
                resp_json = await resp.json(content_type=None)
                if resp.status == 200:
                    return _parse_payment_json(resp_json)
                err_desc = str(resp_json.get("description") or resp_json.get("code") or resp.status)
                raise RuntimeError(f"Ошибка проверки платежа ЮKassa ({resp.status}): {err_desc}")
    except (aiohttp.ClientConnectorError, aiohttp.ConnectionTimeoutError, asyncio.TimeoutError):
        status_code, resp_json = await asyncio.to_thread(
            _urllib_yookassa_request,
            method="GET",
            url=url,
            shop_id=shop_id,
            secret_key=secret_key,
            payload=None,
            timeout=3.5,
        )
        if status_code == 200:
            return _parse_payment_json(resp_json)
        err_desc = str(resp_json.get("description") or resp_json.get("code") or status_code)
        raise RuntimeError(f"Ошибка проверки платежа ЮKassa ({status_code}): {err_desc}")


def _subscription_kb_with_url(price_key: str, payment_url: str, payment_id: str) -> InlineKeyboardMarkup:
    plan = YOOKASSA_PREMIUM_PLANS[price_key]
    price_label = plan["price_label"]
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=f"🇷🇺 Оплатить картой РФ — {price_label} ₽",
                    url=payment_url,
                )
            ],
            [
                InlineKeyboardButton(
                    text=f"🇷🇺 Оплатить по СБП — {price_label} ₽",
                    url=payment_url,
                )
            ],
            [
                InlineKeyboardButton(
                    text="🌍 Международная карта",
                    url=payment_url,
                )
            ],
            [
                InlineKeyboardButton(
                    text="⭐ Telegram Stars",
                    callback_data=f"pay_stars_{price_key}",
                )
            ],
            [
                InlineKeyboardButton(
                    text="✅ Я оплатил (проверить оплату)",
                    callback_data=f"yk_check:{payment_id}",
                )
            ],
            [
                InlineKeyboardButton(
                    text="⬅️ Назад",
                    callback_data="back_to_subs",
                )
            ],
        ]
    )


def _token_kb_with_url(price_key: str, payment_url: str, payment_id: str) -> InlineKeyboardMarkup:
    pkg = YOOKASSA_TOKEN_PACKAGES[price_key]
    price_label = pkg["price_label"]
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="⭐ Купить за Stars",
                    callback_data=pkg["stars_callback"],
                )
            ],
            [
                InlineKeyboardButton(
                    text=f"🇷🇺 Оплатить картой РФ — {price_label} ₽",
                    url=payment_url,
                )
            ],
            [
                InlineKeyboardButton(
                    text=f"🇷🇺 Оплатить по СБП — {price_label} ₽",
                    url=payment_url,
                )
            ],
            [
                InlineKeyboardButton(
                    text="🌍 Международная карта",
                    url=payment_url,
                )
            ],
            [
                InlineKeyboardButton(
                    text="✅ Я оплатил (проверить оплату)",
                    callback_data=f"yk_check:{payment_id}",
                )
            ],
            [
                InlineKeyboardButton(
                    text="⬅️ Назад",
                    callback_data="tokens_menu",
                )
            ],
        ]
    )


def _fallback_subscription_keyboard(price_key: str) -> InlineKeyboardMarkup:
    plan = YOOKASSA_PREMIUM_PLANS[price_key]
    price_label = plan["price_label"]
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=f"🇷🇺 Оплатить картой РФ — {price_label} ₽",
                    callback_data=f"pay_rf_{price_key}",
                )
            ],
            [
                InlineKeyboardButton(
                    text=f"🇷🇺 Оплатить по СБП — {price_label} ₽",
                    callback_data=f"pay_sbp_{price_key}",
                )
            ],
            [
                InlineKeyboardButton(
                    text="🌍 Международная карта",
                    callback_data=f"pay_int_{price_key}",
                )
            ],
            [
                InlineKeyboardButton(
                    text="⭐ Telegram Stars",
                    callback_data=f"pay_stars_{price_key}",
                )
            ],
            [
                InlineKeyboardButton(
                    text="⬅️ Назад",
                    callback_data="back_to_subs",
                )
            ],
        ]
    )


def _fallback_token_keyboard(price_key: str) -> InlineKeyboardMarkup:
    pkg = YOOKASSA_TOKEN_PACKAGES[price_key]
    price_label = pkg["price_label"]
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="⭐ Купить за Stars",
                    callback_data=pkg["stars_callback"],
                )
            ],
            [
                InlineKeyboardButton(
                    text=f"🇷🇺 Оплатить картой РФ — {price_label} ₽",
                    callback_data=f"pay_tok_rf_{price_key}",
                )
            ],
            [
                InlineKeyboardButton(
                    text=f"🇷🇺 Оплатить по СБП — {price_label} ₽",
                    callback_data=f"pay_tok_sbp_{price_key}",
                )
            ],
            [
                InlineKeyboardButton(
                    text="🌍 Международная карта",
                    callback_data=f"pay_tok_int_{price_key}",
                )
            ],
            [
                InlineKeyboardButton(
                    text="⬅️ Назад",
                    callback_data="tokens_menu",
                )
            ],
        ]
    )


async def build_subscription_tariff_keyboard_with_url(
    user_id: int,
    price_key: str,
    bot=None,
    chat_id: int | None = None,
) -> InlineKeyboardMarkup:
    """
    Создаёт платёж в ЮKassa сразу при открытии тарифа и встраивает ссылку
    на оплату (confirmation_url) прямо в кнопки:
    - 🇷🇺 Оплатить картой РФ
    - 🇷🇺 Оплатить по СБП
    - 🌍 Международная карта
    Плюс добавляет кнопку «✅ Я оплатил (проверить оплату)» и запускает фоновую проверку.
    Если ключи ЮKassa ещё не заданы или недоступны, мгновенно возвращает резервную клавиатуру.
    """
    plan = YOOKASSA_PREMIUM_PLANS[price_key]
    payment_url = None
    payment_id = None

    shop_id, secret_key, _ = _get_yookassa_credentials()
    if shop_id and secret_key:
        try:
            payment = await _create_payment_in_yookassa(
                amount_value=plan["price"],
                description=f"Подписка {plan['title']} на 30 дней ({plan['tokens']} токенов), ID {user_id}",
                metadata={
                    "telegram_id": str(user_id),
                    "product_type": "premium",
                    "product_code": plan["code"],
                    "tokens": str(plan["tokens"]),
                    "price_key": price_key,
                },
            )
            payment_id = payment.id
            payment_url = payment.confirmation_url
        except Exception as exc:
            logging.warning("Не удалось сразу сгенерировать ссылку ЮKassa для тарифа %s: %s", price_key, exc)

    if payment_url and payment_id:
        if bot is not None and chat_id is not None:
            asyncio.create_task(
                _background_poll_yookassa_payment(
                    bot=bot,
                    chat_id=chat_id,
                    invoice_message_id=None,
                    user_id=user_id,
                    payment_id=payment_id,
                )
            )
        return _subscription_kb_with_url(price_key, payment_url, payment_id)

    return _fallback_subscription_keyboard(price_key)


async def build_token_package_keyboard_with_url(
    user_id: int,
    price_key: str,
    bot=None,
    chat_id: int | None = None,
) -> InlineKeyboardMarkup:
    """
    Создаёт платёж в ЮKassa сразу при открытии пакета токенов и встраивает
    ссылку на оплату (confirmation_url) прямо в кнопки оплаты.
    """
    pkg = YOOKASSA_TOKEN_PACKAGES[price_key]
    payment_url = None
    payment_id = None

    shop_id, secret_key, _ = _get_yookassa_credentials()
    if shop_id and secret_key:
        try:
            payment = await _create_payment_in_yookassa(
                amount_value=pkg["price"],
                description=f"Покупка {pkg['tokens']} токенов (ID {user_id})",
                metadata={
                    "telegram_id": str(user_id),
                    "product_type": "tokens",
                    "product_code": pkg["code"],
                    "tokens": str(pkg["tokens"]),
                    "price_key": price_key,
                },
            )
            payment_id = payment.id
            payment_url = payment.confirmation_url
        except Exception as exc:
            logging.warning("Не удалось сразу сгенерировать ссылку ЮKassa для пакета %s: %s", price_key, exc)

    if payment_url and payment_id:
        if bot is not None and chat_id is not None:
            asyncio.create_task(
                _background_poll_yookassa_payment(
                    bot=bot,
                    chat_id=chat_id,
                    invoice_message_id=None,
                    user_id=user_id,
                    payment_id=payment_id,
                )
            )
        return _token_kb_with_url(price_key, payment_url, payment_id)

    return _fallback_token_keyboard(price_key)


async def verify_and_fulfill_yookassa_payment(
    payment_id: str,
    expected_user_id: int | None = None,
    bot=None,
) -> tuple[str, str]:
    """
    Проверяет статус платежа в ЮKassa и начисляет токены / активирует подписку
    ТОЛЬКО если status == 'succeeded' и paid == True.
    Гарантирует защиту от двойного начисления.
    """
    lock = _get_payment_lock(payment_id)
    async with lock:
        if payment_id in _PROCESSED_PAYMENTS:
            return (
                "already_processed",
                "✅ Этот платёж уже был успешно обработан, токены зачислены на ваш баланс.",
            )

        try:
            payment = await _fetch_payment_from_yookassa(payment_id)
        except Exception:
            logging.exception("Ошибка при запросе статуса платежа ЮKassa %s", payment_id)
            return (
                "error",
                "⚠️ Не удалось связаться с ЮKassa для проверки статуса. Попробуйте нажать кнопку через пару секунд.",
            )

        status = payment.status
        paid = payment.paid

        if status in {"pending", "waiting_for_capture"} or not paid:
            return (
                "pending",
                "⏳ Платёж ещё не завершён.\n"
                "Перейдите по кнопке оплаты, завершите платёж и нажмите «✅ Я оплатил».",
            )

        if status == "canceled":
            return (
                "canceled",
                "❌ Платёж был отменён или время на оплату истекло. Выберите пакет заново.",
            )

        if status != "succeeded":
            return (
                "pending",
                f"⏳ Статус платежа: {status}. Подождите пару секунд и проверьте снова.",
            )

        metadata = payment.metadata
        meta_telegram_id = metadata.get("telegram_id", "")
        product_type = metadata.get("product_type", "")
        product_code = metadata.get("product_code", "")

        if not meta_telegram_id or not str(meta_telegram_id).isdigit():
            logging.error("В платеже ЮKassa %s отсутствует корректный telegram_id: %s", payment_id, metadata)
            return ("error", "❌ В метаданных платежа не найден ID пользователя. Обратитесь в поддержку.")

        telegram_id = int(meta_telegram_id)
        if expected_user_id is not None and telegram_id != expected_user_id:
            return ("error", "❌ Этот платёж принадлежит другому пользователю.")

        paid_decimal = Decimal(payment.amount_value)
        paid_currency = payment.amount_currency

        # Обработка покупки ПАКЕТА ТОКЕНОВ
        if product_type == "tokens":
            pkg = next(
                (item for item in YOOKASSA_TOKEN_PACKAGES.values() if item["code"] == product_code),
                None,
            )
            if pkg is None:
                return ("error", "❌ Неизвестный пакет токенов. Обратитесь в поддержку.")

            expected_decimal = Decimal(pkg["price"])
            if paid_currency != "RUB" or paid_decimal < expected_decimal:
                return ("error", "❌ Сумма платежа не совпадает со стоимостью пакета.")

            tokens_to_add = int(pkg["tokens"])
            amount_int = int(expected_decimal)

            credited = await complete_token_payment(
                provider="yookassa",
                provider_payment_id=payment.id,
                telegram_id=telegram_id,
                product_code=product_code,
                amount=amount_int,
                currency="RUB",
                tokens=tokens_to_add,
            )
            _PROCESSED_PAYMENTS.add(payment_id)

            if not credited:
                return (
                    "already_processed",
                    "✅ Этот платёж уже обработан ранее, токены начислены на ваш баланс.",
                )

            # Начисляем 30% реферальный бонус пригласившему пользователю, если он есть
            referrer_id, ref_bonus = await credit_referrer_commission(telegram_id, tokens_to_add)
            if referrer_id and ref_bonus > 0 and bot is not None:
                try:
                    await bot.send_message(
                        chat_id=referrer_id,
                        text=f"🎁 Ваш приглашённый друг приобрёл токены! Вам начислено +{ref_bonus} токенов (30%).",
                    )
                except Exception:
                    pass

            new_balance = await get_user_tokens(telegram_id)
            return (
                "credited",
                f"🎉 <b>Оплата прошла успешно!</b>\n\n"
                f"🪙 Начислено: <b>+{tokens_to_add} токенов</b>\n"
                f"💎 Ваш текущий баланс: <b>{new_balance} токенов</b>",
            )

        # Обработка покупки ПОДПИСКИ PREMIUM
        if product_type == "premium":
            plan = next(
                (item for item in YOOKASSA_PREMIUM_PLANS.values() if item["code"] == product_code),
                None,
            )
            if plan is None:
                return ("error", "❌ Неизвестный тариф подписки. Обратитесь в поддержку.")

            expected_decimal = Decimal(plan["price"])
            if paid_currency != "RUB" or paid_decimal < expected_decimal:
                return ("error", "❌ Сумма платежа не совпадает со стоимостью тарифа.")

            amount_int = int(expected_decimal)
            included_tokens = int(plan.get("tokens", 0))

            # complete_premium_payment атомарно активирует подписку И начисляет included_tokens в одной транзакции
            expires_at = await complete_premium_payment(
                provider="yookassa",
                provider_payment_id=payment.id,
                telegram_id=telegram_id,
                product_code=product_code,
                amount=amount_int,
                currency="RUB",
                telegram_payment_charge_id=payment.id,
                tokens=included_tokens,
            )
            _PROCESSED_PAYMENTS.add(payment_id)

            if expires_at is None:
                return (
                    "already_processed",
                    "✅ Этот платёж уже обработан ранее, подписка Premium активна.",
                )

            if included_tokens > 0:
                referrer_id, ref_bonus = await credit_referrer_commission(telegram_id, included_tokens)
                if referrer_id and ref_bonus > 0 and bot is not None:
                    try:
                        await bot.send_message(
                            chat_id=referrer_id,
                            text=f"🎁 Ваш приглашённый друг оформил подписку! Вам начислено +{ref_bonus} токенов (30%).",
                        )
                    except Exception:
                        pass

            new_balance = await get_user_tokens(telegram_id)
            return (
                "credited",
                f"💎 <b>Оплата прошла успешно! Подписка {plan['title']} активирована!</b>\n\n"
                f"🪙 Начислено по тарифу: <b>+{included_tokens} токенов</b>\n"
                f"📅 Действует до: <b>{expires_at:%d.%m.%Y %H:%M} UTC</b>\n"
                f"💰 Ваш баланс: <b>{new_balance} токенов</b>",
            )

        return ("error", "❌ Неизвестный тип заказа. Обратитесь в поддержку.")


async def _background_poll_yookassa_payment(
    bot,
    chat_id: int,
    invoice_message_id: int | None,
    user_id: int,
    payment_id: str,
) -> None:
    """
    Фоновая проверка статуса платежа каждые 10 секунд в течение 15 минут.
    Как только оплата в ЮKassa проходит, бот автоматически начисляет токены 1 раз и уведомляет пользователя.
    """
    for _ in range(90):
        await asyncio.sleep(10)
        if payment_id in _PROCESSED_PAYMENTS:
            return

        status_code, msg_text = await verify_and_fulfill_yookassa_payment(
            payment_id=payment_id,
            expected_user_id=user_id,
            bot=bot,
        )
        if status_code == "credited":
            if invoice_message_id:
                try:
                    await bot.delete_message(chat_id=chat_id, message_id=invoice_message_id)
                except Exception:
                    pass
            await bot.send_message(chat_id=chat_id, text=msg_text, parse_mode="HTML")
            return
        if status_code in {"already_processed", "canceled", "error"}:
            return


async def start_yookassa_payment_flow(
    callback: CallbackQuery,
    callback_data: str,
) -> None:
    """
    Обработчик нажатия на кнопку оплаты, если при открытии тарифа ссылка не была встроена
    (например, не были указаны YOOKASSA_SHOP_ID / YOOKASSA_SECRET_KEY в .env или был сбой сети).
    """
    shop_id, secret_key, _ = _get_yookassa_credentials()
    if not shop_id or not secret_key:
        await callback.answer(
            "⚠️ Для оплаты рублями укажите YOOKASSA_SHOP_ID и YOOKASSA_SECRET_KEY в файле .env и перезапустите бота!",
            show_alert=True,
        )
        return

    try:
        await callback.answer()
    except TelegramBadRequest:
        pass

    price_key = callback_data.split("_")[-1]
    if callback.message is None:
        return

    chat_id = callback.message.chat.id
    user_id = callback.from_user.id

    try:
        if price_key in YOOKASSA_PREMIUM_PLANS:
            plan = YOOKASSA_PREMIUM_PLANS[price_key]
            payment = await _create_payment_in_yookassa(
                amount_value=plan["price"],
                description=f"Подписка {plan['title']} на 30 дней ({plan['tokens']} токенов), ID {user_id}",
                metadata={
                    "telegram_id": str(user_id),
                    "product_type": "premium",
                    "product_code": plan["code"],
                    "tokens": str(plan["tokens"]),
                    "price_key": price_key,
                },
            )
            if not payment.confirmation_url:
                raise RuntimeError("ЮKassa не вернула ссылку на оплату (confirmation_url).")

            asyncio.create_task(
                _background_poll_yookassa_payment(
                    bot=callback.bot,
                    chat_id=chat_id,
                    invoice_message_id=callback.message.message_id,
                    user_id=user_id,
                    payment_id=payment.id,
                )
            )
            kb = _subscription_kb_with_url(price_key, payment.confirmation_url, payment.id)
            try:
                await callback.message.edit_text(
                    subscription_offer_text(price_key),
                    reply_markup=kb,
                )
            except TelegramBadRequest:
                pass
            await callback.answer("✅ Ссылка на оплату сформирована! Нажмите на кнопку оплаты ещё раз.")
            return

        if price_key in YOOKASSA_TOKEN_PACKAGES:
            pkg = YOOKASSA_TOKEN_PACKAGES[price_key]
            payment = await _create_payment_in_yookassa(
                amount_value=pkg["price"],
                description=f"Покупка {pkg['tokens']} токенов (ID {user_id})",
                metadata={
                    "telegram_id": str(user_id),
                    "product_type": "tokens",
                    "product_code": pkg["code"],
                    "tokens": str(pkg["tokens"]),
                    "price_key": price_key,
                },
            )
            if not payment.confirmation_url:
                raise RuntimeError("ЮKassa не вернула ссылку на оплату (confirmation_url).")

            asyncio.create_task(
                _background_poll_yookassa_payment(
                    bot=callback.bot,
                    chat_id=chat_id,
                    invoice_message_id=callback.message.message_id,
                    user_id=user_id,
                    payment_id=payment.id,
                )
            )
            kb = _token_kb_with_url(price_key, payment.confirmation_url, payment.id)
            try:
                await callback.message.edit_text(
                    f"💳 Вы оформляете покупку пакета на {pkg['tokens']} токенов за {pkg['display_price']}.",
                    reply_markup=kb,
                )
            except TelegramBadRequest:
                pass
            await callback.answer("✅ Ссылка на оплату сформирована! Нажмите на кнопку оплаты ещё раз.")
            return

        await callback.answer("Неизвестный тариф.", show_alert=True)
    except Exception as exc:
        err_text = str(exc)
        if "api.yookassa.ru" in err_text or "Timeout" in err_text or "Connection" in err_text:
            logging.warning("Сбой подключения к ЮKassa: %s", err_text)
            user_hint = (
                "⚠️ Не удалось подключиться к ЮKassa (таймаут соединения).\n\n"
                "💡 Если у вас на ПК включён VPN (для Gemini/Telegram): "
                "добавьте api.yookassa.ru в список исключений VPN (Bypass / раздельное туннелирование)."
            )
        else:
            logging.exception("Ошибка создания платежа ЮKassa при нажатии на кнопку %s", callback_data)
            user_hint = f"⚠️ Ошибка создания платежа: {err_text[:140]}"

        if callback.message:
            try:
                await callback.message.answer(user_hint, parse_mode="HTML")
            except Exception:
                pass
        try:
            await callback.answer("⚠️ Ошибка связи с ЮKassa (проверьте VPN)", show_alert=True)
        except TelegramBadRequest:
            pass