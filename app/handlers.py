import html
import re
import asyncio
import base64
import logging
import time
from io import BytesIO
from pathlib import Path
from typing import Any, Awaitable, Callable

import aiohttp
from PIL import Image

from aiogram import BaseMiddleware, F, Router
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LabeledPrice,
    Message,
    PreCheckoutQuery,
    TelegramObject,
)

from app.DataBase.db_main import (
    add_tokens,
    complete_premium_payment,
    complete_token_payment,
    get_premium_until,
    get_user_tokens,
    has_premium,
    init_db,
    register_user,
    spend_tokens,
)

from .APIclient.ChatGptFhoto import generate_chatgpt_image
from .APIclient.GeminiClient import (
    build_academic_docx_bytes,
    check_gemini_api,
    clear_active_dialog_messages,
    create_new_user_dialog,
    generate_gemini_chat_response,
    generate_gemini_service_response,
    get_user_session,
    get_user_text_model,
    is_remember_dialog_enabled,
    set_user_text_model,
    switch_user_dialog,
    toggle_remember_dialog,
)
from .APIclient.KlingAiClient import (
    KLING_DEFAULT_DURATION,
    KLING_DEFAULT_MODEL,
    KLING_DURATIONS,
    KLING_MODEL_LABELS,
    generate_kling_video,
    get_kling_price,
)
from .APIclient.NanoBananoClient import generate_nanobanano_image
from .APIclient.VeoClient import (
    VEO_ASPECT_RATIOS,
    VEO_DEFAULT_ASPECT_RATIO,
    VEO_DEFAULT_MODEL,
    VEO_MODEL_LABELS,
    generate_veo_video,
)
from .keyboards import (
    REQUIRED_CHANNEL_ID,
    REQUIRED_CHANNEL_URL,
    SUPPORT_CONTACT_URL,
    academic_work_back_kb,
    ai_tools_kb,
    build_kling_settings_keyboard,
    build_nano_settings_keyboard,
    build_profile_options_kb,
    build_referral_kb,
    build_tasks_channels_kb,
    build_text_models_keyboard,
    build_user_dialogs_keyboard,
    build_veo_keyboard,
    channel_sub_check_kb,
    chatgpt_otmena_kb,
    diploma_types_kb,
    free_tokens_menu_kb,
    gpt_cartinki_kb,
    image_generators_kb,
    image_tools_kb,
    kling_otmena_kb,
    kling_prompt_kb,
    menu_kb,
    nano_otmena_kb,
    pay_sub_adv_kb,
    pay_sub_max_kb,
    pay_sub_opt_kb,
    pay_sub_start_kb,
    pay_tok_100_kb,
    pay_tok_350_kb,
    pay_tok_1000_kb,
    profile_options_kb,
    recognition_back_kb,
    services_kb,
    services_menu_kb,
    study_service_back_kb,
    subscription_menu_kb,
    tokens_menu_kb,
    veo_otmena_kb,
    video_generators_kb,
)
from .nano_banano import (
    MODEL_DEFAULT,
    MODEL_NAMES,
    MODEL_RESOLUTIONS,
    RESOLUTION_DEFAULT,
)
from .prompts import ACADEMIC_WORK_TYPES, SERVICES, TEXT_CHAT_MODELS
from .yookassa_pay import (
    YOOKASSA_PREMIUM_PLANS,
    YOOKASSA_TOKEN_PACKAGES,
    build_subscription_tariff_keyboard_with_url,
    build_token_package_keyboard_with_url,
    credit_referrer_commission,
    get_referral_stats,
    register_referral_if_new,
    start_yookassa_payment_flow,
    subscription_offer_text,
    verify_and_fulfill_yookassa_payment,
)

user = Router()

_CACHED_BOT_USERNAME: str | None = None
_SUBSCRIBED_USERS_CACHE: dict[int, float] = {}
_SUB_CACHE_TTL_SEC = 60.0

# Callback-данные оплаты и тарифов никогда не блокируются проверкой канала
_PAYMENT_BYPASS_CALLBACKS = {
    "check_channel_sub",
    "pay_premium",
    "sub_start",
    "sub_opt",
    "sub_adv",
    "sub_max",
    "back_to_subs",
    "tokens_menu",
    "buy_tok_100",
    "buy_tok_350",
    "buy_tok_1000",
}
_PAYMENT_BYPASS_PREFIXES = (
    "yk_check:",
    "pay_rf_",
    "pay_sbp_",
    "pay_int_",
    "pay_tok_",
    "pay_stars_",
)


async def get_bot_username(bot) -> str:
    global _CACHED_BOT_USERNAME
    if not _CACHED_BOT_USERNAME:
        me = await bot.get_me()
        _CACHED_BOT_USERNAME = me.username or ""
    return _CACHED_BOT_USERNAME


async def is_user_subscribed_to_channel(bot, user_id: int, force_check: bool = False) -> bool:
    """
    Проверяет, подписан ли пользователь на обязательный Telegram-канал (REQUIRED_CHANNEL_ID).
    Кэширует положительный результат на 60 секунд для мгновенного отклика кнопок.
    Если бот ещё не добавлен в канал или канал не найден, не блокирует работу.
    """
    if not REQUIRED_CHANNEL_ID:
        return True

    now = time.monotonic()
    if not force_check:
        cached_until = _SUBSCRIBED_USERS_CACHE.get(user_id, 0.0)
        if cached_until > now:
            return True

    try:
        member = await bot.get_chat_member(chat_id=REQUIRED_CHANNEL_ID, user_id=user_id)
        if member.status in {"member", "administrator", "creator"}:
            _SUBSCRIBED_USERS_CACHE[user_id] = now + _SUB_CACHE_TTL_SEC
            return True
        if member.status == "restricted" and getattr(member, "is_member", False):
            _SUBSCRIBED_USERS_CACHE[user_id] = now + _SUB_CACHE_TTL_SEC
            return True
        _SUBSCRIBED_USERS_CACHE.pop(user_id, None)
        return False
    except (TelegramBadRequest, TelegramForbiddenError) as exc:
        logging.warning(
            "Не удалось проверить подписку на канал %s (убедитесь, что бот добавлен в администраторы канала): %s",
            REQUIRED_CHANNEL_ID,
            exc,
        )
        return True
    except Exception:
        logging.debug("Ошибка при проверке подписки на канал", exc_info=True)
        return True


class ChannelSubscriptionMiddleware(BaseMiddleware):
    """
    Middleware, проверяющий подписку пользователя на канал https://t.me/GPT6_NANO_BANANO
    перед использованием функций бота (не блокируя раздел покупки и оплаты).
    """

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        if isinstance(event, Message):
            if event.text and (
                event.text.startswith("/start")
                or event.text.startswith("/premium")
                or event.text == "купить премиум🚀"
            ):
                return await handler(event, data)
            if event.successful_payment is not None:
                return await handler(event, data)
            if event.from_user:
                subscribed = await is_user_subscribed_to_channel(event.bot, event.from_user.id)
                if not subscribed:
                    await event.answer(
                        "🔒 <b>Для использования бота необходимо подписаться на наш Telegram-канал!</b>\n\n"
                        f"👉 Канал: {REQUIRED_CHANNEL_URL}\n\n"
                        "Подпишитесь по кнопке ниже и нажмите «✅ Я подписался»:",
                        parse_mode="HTML",
                        reply_markup=channel_sub_check_kb,
                        disable_web_page_preview=True,
                    )
                    return None

        elif isinstance(event, CallbackQuery):
            cb_data = event.data or ""
            if cb_data in _PAYMENT_BYPASS_CALLBACKS or cb_data.startswith(_PAYMENT_BYPASS_PREFIXES):
                return await handler(event, data)
            if event.from_user:
                subscribed = await is_user_subscribed_to_channel(event.bot, event.from_user.id)
                if not subscribed:
                    await event.answer(
                        "Сначала подпишитесь на наш Telegram-канал!",
                        show_alert=True,
                    )
                    if event.message:
                        await event.message.answer(
                            "🔒 <b>Для использования бота необходимо подписаться на наш Telegram-канал!</b>\n\n"
                            f"👉 Канал: {REQUIRED_CHANNEL_URL}\n\n"
                            "Подпишитесь по кнопке ниже и нажмите «✅ Я подписался»:",
                            parse_mode="HTML",
                            reply_markup=channel_sub_check_kb,
                            disable_web_page_preview=True,
                        )
                    return None

        return await handler(event, data)


user.message.outer_middleware(ChannelSubscriptionMiddleware())
user.callback_query.outer_middleware(ChannelSubscriptionMiddleware())


CHATGPT_IMAGE_MAX_DOWNLOAD_BYTES = 20 * 1024 * 1024
VIDEO_MAX_DOWNLOAD_BYTES = 50 * 1024 * 1024

CHATGPT_IMAGE_DOWNLOAD_TIMEOUT = aiohttp.ClientTimeout(
    total=30,
    connect=10,
    sock_read=20,
)
VIDEO_DOWNLOAD_TIMEOUT = aiohttp.ClientTimeout(
    total=120,
    connect=15,
    sock_read=60,
)

# Эти обычные кнопки не должны случайно считаться промтом генерации.
CHATGPT_MENU_TEXTS = {
    "купить премиум🚀",
    "Нано банано🍌",
    "мой профиль👤",
    "генерация текста",
    "чатГПТ картинки🔥",
    "все нейросети🧠",
}

NANO_MENU_CALLBACKS = {
    "toggle_model_list",
    "toggle_format_list",
    "toggle_res_list",
    "set_model_nano_banana",
    "set_model_nano_banana_pro",
    "set_model_nano_banana_2",
    "set_format_original",
    "set_format_1_1",
    "set_format_16_9",
    "set_format_9_16",
    "set_res_standard",
    "set_res_2k",
    "set_res_4k",
    "confirm_generation",
}

KLING_MENU_CALLBACKS = {
    "kling_toggle_model",
    "kling_toggle_duration",
    "kling_set_model_kling_2.5_turbo",
    "kling_set_model_kling_2.1_master",
    "kling_set_dur_5",
    "kling_set_dur_10",
    "kling_confirm_settings",
}

VEO_MENU_CALLBACKS = {
    "veo_toggle_model",
    "veo_toggle_aspect",
    "veo_set_model_veo-3-fast",
    "veo_set_model_veo-3-quality",
    "veo_set_aspect_16_9",
    "veo_set_aspect_9_16",
}


class LimitedBytesIO(BytesIO):
    """BytesIO, ограничивающий размер загружаемой фотографии."""

    def write(self, data):
        if self.tell() + len(data) > CHATGPT_IMAGE_MAX_DOWNLOAD_BYTES:
            raise ValueError("Исходное фото превышает допустимый размер 20 МБ")
        return super().write(data)


def convert_image_to_png(image_bytes: bytes) -> bytes:
    with Image.open(BytesIO(image_bytes)) as image:
        png_buffer = BytesIO()
        image.convert("RGBA").save(png_buffer, format="PNG")
        return png_buffer.getvalue()


async def download_image_bytes(image_url: str) -> bytes:
    chunks = []
    downloaded_bytes = 0
    async with aiohttp.ClientSession(timeout=CHATGPT_IMAGE_DOWNLOAD_TIMEOUT) as session:
        async with session.get(image_url) as response:
            response.raise_for_status()
            content_length = response.headers.get("Content-Length")
            if content_length is not None and int(content_length) > CHATGPT_IMAGE_MAX_DOWNLOAD_BYTES:
                raise ValueError("Сгенерированное изображение превышает 20 МБ")

            async for chunk in response.content.iter_chunked(64 * 1024):
                downloaded_bytes += len(chunk)
                if downloaded_bytes > CHATGPT_IMAGE_MAX_DOWNLOAD_BYTES:
                    raise ValueError("Сгенерированное изображение превышает 20 МБ")
                chunks.append(chunk)
    return b"".join(chunks)


async def download_video_bytes(video_url: str) -> bytes:
    chunks = []
    downloaded_bytes = 0
    async with aiohttp.ClientSession(timeout=VIDEO_DOWNLOAD_TIMEOUT) as session:
        async with session.get(video_url) as response:
            response.raise_for_status()
            content_length = response.headers.get("Content-Length")
            if content_length is not None and int(content_length) > VIDEO_MAX_DOWNLOAD_BYTES:
                raise ValueError("Сгенерированное видео превышает 50 МБ")

            async for chunk in response.content.iter_chunked(64 * 1024):
                downloaded_bytes += len(chunk)
                if downloaded_bytes > VIDEO_MAX_DOWNLOAD_BYTES:
                    raise ValueError("Сгенерированное видео превышает 50 МБ")
                chunks.append(chunk)
    return b"".join(chunks)


async def download_telegram_photo_bytes(message: Message, file_id: str) -> bytes:
    image_buffer = LimitedBytesIO()
    await asyncio.wait_for(
        message.bot.download(file_id, destination=image_buffer, timeout=30),
        timeout=30,
    )
    return image_buffer.getvalue()


async def photo_to_data_uri(message: Message, file_id: str) -> str:
    raw_bytes = await download_telegram_photo_bytes(message, file_id)
    encoded_image = base64.b64encode(raw_bytes).decode("ascii")
    return f"data:image/jpeg;base64,{encoded_image}"


_SUPERSCRIPTS = str.maketrans({
    "0": "⁰", "1": "¹", "2": "²", "3": "³", "4": "⁴",
    "5": "⁵", "6": "⁶", "7": "⁷", "8": "⁸", "9": "⁹",
    "+": "⁺", "-": "⁻", "=": "⁼", "(": "⁽", ")": "⁾",
    "n": "ⁿ", "i": "ⁱ",
})

_SUBSCRIPTS = str.maketrans({
    "0": "₀", "1": "₁", "2": "₂", "3": "₃", "4": "₄",
    "5": "₅", "6": "₆", "7": "₇", "8": "₈", "9": "₉",
    "+": "₊", "-": "₋", "=": "₌", "(": "₍", ")": "₎",
    "a": "ₐ", "e": "ₑ", "i": "ᵢ", "j": "ⱼ", "k": "ₖ",
    "m": "ₘ", "n": "ₙ", "o": "ₒ", "p": "ₚ", "r": "ᵣ",
    "s": "ₛ", "t": "ₜ", "u": "ᵤ", "v": "ᵥ", "x": "ₓ",
})

_LATEX_SYMBOLS = {
    r"\cdot": "·",
    r"\times": "×",
    r"\div": "÷",
    r"\pm": "±",
    r"\mp": "∓",
    r"\neq": "≠",
    r"\ne": "≠",
    r"\leq": "≤",
    r"\le": "≤",
    r"\geq": "≥",
    r"\ge": "≥",
    r"\approx": "≈",
    r"\infty": "∞",
    r"\pi": "π",
    r"\alpha": "α",
    r"\beta": "β",
    r"\gamma": "γ",
    r"\delta": "δ",
    r"\Delta": "Δ",
    r"\lambda": "λ",
    r"\mu": "μ",
    r"\sigma": "σ",
    r"\omega": "ω",
    r"\rightarrow": "→",
    r"\to": "→",
    r"\Rightarrow": "⇒",
    r"\Leftrightarrow": "⇔",
    r"\left": "",
    r"\right": "",
}


def _latex_to_clean_unicode(expr: str) -> str:
    """Преобразует LaTeX-формулу в красивую читаемую Unicode-строку."""
    s = expr.strip()

    # \frac{a}{b} -> a/b или (a)/(b)
    def _replace_frac(m: re.Match) -> str:
        num = m.group(1).strip()
        den = m.group(2).strip()
        num_str = f"({num})" if len(num) > 2 and any(c in num for c in "+- ") else num
        den_str = f"({den})" if len(den) > 2 and any(c in den for c in "+- ") else den
        return f"{num_str}/{den_str}"

    for _ in range(3):
        s = re.sub(r"\\frac\{([^{}]+)\}\{([^{}]+)\}", _replace_frac, s)

    # \sqrt{x} -> √(x) или √x
    s = re.sub(
        r"\\sqrt\{([^{}]+)\}",
        lambda m: f"√({m.group(1)})" if len(m.group(1)) > 2 else f"√{m.group(1)}",
        s,
    )

    for cmd, sym in _LATEX_SYMBOLS.items():
        s = s.replace(cmd, sym)

    # Степени ^{...} и ^x
    s = re.sub(r"\^\{([0-9+\-=()ni]+)\}", lambda m: m.group(1).translate(_SUPERSCRIPTS), s)
    s = re.sub(r"\^([0-9ni])", lambda m: m.group(1).translate(_SUPERSCRIPTS), s)

    # Индексы _{...} и _x
    s = re.sub(r"_\{([0-9+\-=()aeijkmoprstuvx]+)\}", lambda m: m.group(1).translate(_SUBSCRIPTS), s)
    s = re.sub(r"_([0-9aeijkmoprstuvx])", lambda m: m.group(1).translate(_SUBSCRIPTS), s)

    s = re.sub(r"\\text\{([^{}]*)\}", r"\1", s)
    s = s.replace("{", "").replace("}", "")
    return s.strip()


def format_ai_text_to_telegram_html(text: str) -> str:
    """
    Преобразует Markdown и LaTeX из ответа ИИ в красивый Telegram HTML,
    выделяя ключевые места, заголовки и формулы жирным курсивом (<b><i>...</i></b>).
    """
    if not text:
        return "Ответ пуст."

    placeholders: dict[str, str] = {}
    counter = 0

    def _stash(html_fragment: str) -> str:
        nonlocal counter
        counter += 1
        key = f"@@TGPH_{counter}@@"
        placeholders[key] = html_fragment
        return key

    working = text.replace("\r\n", "\n")

    # 1. Сохраняем многострочные блоки кода ```lang ... ```
    def _replace_code_block(m: re.Match) -> str:
        code_body = html.escape(m.group(2).strip("\n"))
        return _stash(f"<pre><code>{code_body}</code></pre>")

    working = re.sub(r"```([a-zA-Z0-9_+-]*)\n?(.*?)```", _replace_code_block, working, flags=re.DOTALL)

    # 2. Сохраняем инлайн-код `...`
    working = re.sub(
        r"`([^`\n]+)`",
        lambda m: _stash(f"<code>{html.escape(m.group(1))}</code>"),
        working,
    )

    # 3. Блочные формулы $$...$$ -> жирный курсив <b><i>...</i></b>
    def _replace_display_math(m: re.Match) -> str:
        clean_math = html.escape(_latex_to_clean_unicode(m.group(1)))
        return _stash(f"<b><i>{clean_math}</i></b>")

    working = re.sub(r"\$\$(.*?)\$\$", _replace_display_math, working, flags=re.DOTALL)

    # 4. Строчные формулы $...$ -> жирный курсив <b><i>...</i></b>
    def _replace_inline_math(m: re.Match) -> str:
        clean_math = html.escape(_latex_to_clean_unicode(m.group(1)))
        return _stash(f"<b><i>{clean_math}</i></b>")

    working = re.sub(r"(?<!\$)\$([^\$\n]+?)\$(?!\$)", _replace_inline_math, working)

    # 5. Экранируем оставшийся обычный текст для HTML
    working = html.escape(working)

    # 6. Убираем горизонтальные линии --- или ***
    working = re.sub(r"(?m)^\s*([-*_]){3,}\s*$", "", working)

    # 7. Заголовки Markdown (# ..., ## ..., ### ...) -> жирный курсив <b><i>...</i></b>
    working = re.sub(
        r"(?m)^\s*#{1,6}\s+(.+?)\s*$",
        lambda m: f"<b><i>{m.group(1).strip('*_ ')}</i></b>",
        working,
    )

    # 8. Тройные звёздочки ***текст*** -> жирный курсив <b><i>текст</i></b>
    working = re.sub(r"\*\*\*(.+?)\*\*\*", r"<b><i>\1</i></b>", working)
    working = re.sub(r"___(.+?)___", r"<b><i>\1</i></b>", working)

    # 9. Двойные звёздочки **текст** -> жирный курсив <b><i>текст</i></b> (чтобы ключевые места и подзаголовки выглядели ярко и выразительно)
    working = re.sub(r"\*\*(.+?)\*\*", r"<b><i>\1</i></b>", working)
    working = re.sub(r"__(.+?)__", r"<b><i>\1</i></b>", working)

    # 10. Одинарные звёздочки *текст* -> курсив <i>текст</i>
    working = re.sub(r"(?<!\*)\*([^\*\n]+?)\*(?!\*)", r"<i>\1</i>", working)

    # 11. Аккуратные маркеры списков (- пункт -> • пункт)
    working = re.sub(r"(?m)^(\s*)[-*]\s+", r"\1• ", working)

    # 12. Возвращаем сохранённые блоки кода и формул
    for key, fragment in placeholders.items():
        working = working.replace(key, fragment)

    # Схлопываем лишние пустые строки (не более 2 подряд)
    working = re.sub(r"\n{3,}", "\n\n", working).strip()
    return working



def _split_text_by_paragraphs(text: str, max_len: int = 3500) -> list[str]:
    """Разбивает длинный текст по абзацам и строкам, чтобы не разрывать слова и теги."""
    cleaned = (text or "").strip()
    if not cleaned:
        return ["Ответ пуст."]
    if len(cleaned) <= max_len:
        return [cleaned]

    chunks: list[str] = []
    current: list[str] = []
    current_len = 0

    for line in cleaned.split("\n"):
        line_len = len(line) + 1
        if current and current_len + line_len > max_len:
            chunks.append("\n".join(current))
            current = [line]
            current_len = len(line)
        elif line_len > max_len:
            if current:
                chunks.append("\n".join(current))
                current = []
                current_len = 0
            for i in range(0, len(line), max_len):
                chunks.append(line[i : i + max_len])
        else:
            current.append(line)
            current_len += line_len

    if current:
        chunks.append("\n".join(current))
    return chunks


async def send_long_message(
    message: Message,
    text: str,
    reply_markup=None,
) -> None:
    """
    Красиво форматирует ответ ИИ (выделяет ключевые места и формулы жирным курсивом <b><i>...</i></b>)
    и безопасно отправляет длинные сообщения по частям с parse_mode='HTML'.
    """
    raw_chunks = _split_text_by_paragraphs(text, max_len=3500)
    for idx, raw_chunk in enumerate(raw_chunks):
        is_last = idx == len(raw_chunks) - 1
        formatted_chunk = format_ai_text_to_telegram_html(raw_chunk)
        try:
            await message.answer(
                formatted_chunk,
                parse_mode="HTML",
                reply_markup=reply_markup if is_last else None,
            )
        except TelegramBadRequest:
            await message.answer(
                raw_chunk,
                reply_markup=reply_markup if is_last else None,
            )


class NanoBananoStates(StatesGroup):
    active = State()


class ChatGPTImagesStates(StatesGroup):
    waiting_for_prompt = State()


class KlingStates(StatesGroup):
    choosing_settings = State()
    waiting_for_input = State()


class VeoStates(StatesGroup):
    waiting_for_input = State()


class ImageRecognitionStates(StatesGroup):
    waiting_for_photo = State()


class StudyServiceStates(StatesGroup):
    waiting_for_academic_topic = State()
    waiting_for_math_input = State()
    waiting_for_summary_input = State()
    waiting_for_rewrite_input = State()
    waiting_for_script_input = State()


def generation_menu_text(data: dict) -> str:
    details = ["Настройте параметры генерации ниже."]
    if data.get("prompt"):
        details.append("📝 Текстовый запрос сохранён.")
    if data.get("photo"):
        details.append("📷 Фото для редактирования сохранено.")
    if data.get("model") == "nano_banana":
        details.append("Разрешение: Стандартное (фиксировано для этой модели).")
    return "\n\n".join(details)


def text_chat_settings_caption(user_id: int) -> str:
    """Формирует текст меню выбора текстовой модели в точности как на скриншоте 4."""
    model_key = get_user_text_model(user_id)
    model_cfg = TEXT_CHAT_MODELS[model_key]
    remember_str = "включено" if is_remember_dialog_enabled(user_id) else "выключено"
    price = model_cfg["price"]
    token_word = "токен" if price == 1 else "токена"
    return (
        f"Запоминать диалог: <b>{remember_str}</b>\n"
        f"Ваша текущая модель: <b>{model_cfg['name']}</b>\n"
        f"Стоимость генерации: <b>{price} {token_word}</b>\n\n"
        "🤯 С премиум подпиской: <b>бесплатно</b>\n\n"
        f"<i>{model_cfg['description']}</i>"
    )


def kling_settings_menu_text(model: str, duration: str) -> str:
    price = get_kling_price(model, duration)
    return (
        "⚙️ <b>Выберите параметры генерации Kling:</b>\n\n"
        f"Модель: {KLING_MODEL_LABELS[model]}\n"
        f"💰 Стоимость: {price} токенов\n"
        f"⏱️ Длительность: {duration} сек\n\n"
        "После выбора нажмите «Подтвердить ✅»."
    )


def kling_ready_prompt_text(model: str, duration: str) -> str:
    price = get_kling_price(model, duration)
    return (
        f"Модель: {KLING_MODEL_LABELS[model]}\n"
        f"💰 Стоимость: {price} токенов\n"
        f"⏱️ Длительность: {duration} сек\n\n"
        "📷 Отправьте фотографию и описание движения\n"
        "(можно в одном сообщении или отдельно)"
    )


def veo_welcome_text() -> str:
    return (
        "🕺 Хайповые ролики с бабушками и носорогами, генерация видео с твоим изображением и многое другое!\n\n"
        '📒 <a href="https://teletype.in/@cjournalgpt/ntvEKFwNRMQ">Инструкция по составлению запросов</a>\n\n'
        '📹 <a href="https://t.me/+Rc0wwdTLaNhjMzVi">Канал с примерами видео и запросами</a>\n\n'
        '🤖 <a href="https://t.me/GPT5_NANO_BANANO_BOT">Наш отдельный видеобот с дешевыми генерациями</a>\n\n'
        "💎 Стоимость 1 генерации: от 14 токенов\n\n"
        "👉 Пришлите ваш запрос или изображение с описанием, и я создам для вас видео ⤵️"
    )


async def track_process_bot_message(state: FSMContext, message: Message) -> None:
    data = await state.get_data()
    message_ids = list(data.get("process_bot_message_ids", []))
    if message.message_id not in message_ids:
        message_ids.append(message.message_id)
        await state.update_data(process_bot_message_ids=message_ids)


async def delete_message_safely(bot, chat_id: int, message_id: int) -> None:
    try:
        await bot.delete_message(chat_id=chat_id, message_id=message_id)
    except Exception:
        logging.debug("Не удалось удалить сообщение %s в чате %s", message_id, chat_id, exc_info=True)


async def cleanup_tracked_messages(
    bot,
    chat_id: int,
    state: FSMContext,
    exclude_message_id: int | None = None,
) -> None:
    """Удаляет все сохранённые служебные сообщения текущего шага перед сменой экрана."""
    data = await state.get_data()
    ids_to_delete: list[int] = []
    for msg_id in data.get("process_bot_message_ids", []):
        if msg_id and msg_id not in ids_to_delete:
            ids_to_delete.append(msg_id)
    for key in (
        "chatgpt_welcome_message_id",
        "chatgpt_prompt_message_id",
        "chatgpt_photo_prompt_message_id",
        "kling_active_menu_message_id",
        "veo_active_menu_message_id",
    ):
        msg_id = data.get(key)
        if msg_id and msg_id not in ids_to_delete:
            ids_to_delete.append(msg_id)

    for msg_id in ids_to_delete:
        if msg_id != exclude_message_id:
            await delete_message_safely(bot, chat_id, msg_id)


async def replace_inline_screen(
    callback: CallbackQuery,
    text: str,
    reply_markup,
    parse_mode: str | None = None,
) -> Message | None:
    """Плавно обновляет экран меню (редактирует текст, а если нельзя — переотправляет)."""
    message = callback.message
    if message is None:
        return None
    chat_id = message.chat.id
    try:
        return await message.edit_text(
            text=text,
            reply_markup=reply_markup,
            parse_mode=parse_mode,
        )
    except Exception:
        await delete_message_safely(callback.bot, chat_id, message.message_id)
        return await callback.bot.send_message(
            chat_id=chat_id,
            text=text,
            reply_markup=reply_markup,
            parse_mode=parse_mode,
        )


async def show_generation_menu(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    menu_message = await message.answer(
        generation_menu_text(data),
        reply_markup=build_nano_settings_keyboard(
            model=data["model"],
            image_format=data["format"],
            resolution=data["resolution"],
            expanded=data.get("expanded"),
        ),
    )
    await track_process_bot_message(state, menu_message)


async def edit_generation_menu(callback: CallbackQuery, data: dict) -> None:
    if callback.message is None:
        return
    try:
        await callback.message.edit_text(
            generation_menu_text(data),
            reply_markup=build_nano_settings_keyboard(
                model=data["model"],
                image_format=data["format"],
                resolution=data["resolution"],
                expanded=data.get("expanded"),
            ),
        )
    except TelegramBadRequest as error:
        if "message is not modified" not in str(error).lower():
            raise


async def start_nano_banano(
    message: Message,
    state: FSMContext,
    back_target: str = "generator_menu",
) -> None:
    await cleanup_tracked_messages(message.bot, message.chat.id, state)
    await state.clear()
    await state.update_data(
        model=MODEL_DEFAULT,
        format="original",
        resolution=RESOLUTION_DEFAULT,
        expanded=None,
        prompt=None,
        photo=None,
        prompt_message_id=None,
        awaiting_photo_description=False,
        nano_back_target=back_target,
        process_bot_message_ids=[],
    )
    await state.set_state(NanoBananoStates.active)

    welcome_message = await message.answer("✅ Теперь в чате: Nano Banana 🍌")
    await track_process_bot_message(state, welcome_message)

    caption = (
        "🍌 Создавайте и генерируйте фотки с Нано Банано!\n\n"
        "Эта нейронка — убийца картинок от ChatGPT!\n\n"
        "Выберите модель, формат и разрешение после отправки запроса.\n\n"
        '‼️ Обязательно прочитайте <a href="https://teletype.in/@cjournalgpt/ntvEKFwNRMQ">инструкцию</a>\n'
        '📒 Примеры запросов смотрите в <a href="https://t.me/+Rc0wwdTLaNhjMzVi">нашем канале</a>\n\n'
        "Отправь запрос на создание картинки или отправь фото с описанием изменений ⤵️"
    )
    photo_path = Path(__file__).resolve().parent.parent / "photo_2026-10-23_23-47-21.jpg"
    if photo_path.is_file():
        instructions_message = await message.answer_photo(
            photo=FSInputFile(str(photo_path)),
            caption=caption,
            parse_mode="HTML",
            reply_markup=nano_otmena_kb,
        )
    else:
        logging.warning("Не найдено изображение-инструкция: %s", photo_path)
        instructions_message = await message.answer(
            caption + "\n\n(Изображение-инструкция пока недоступно.)",
            parse_mode="HTML",
            reply_markup=nano_otmena_kb,
        )
    await track_process_bot_message(state, instructions_message)


WELCOME_START_TEXT = (
    "🤖 Добро пожаловать в GPT-5 NANO BANANO BOT!\n"
    "Твоё персональное AI-пространство для общения, творчества, учёбы и работы — всё в одном Telegram-боте.\n\n"
    "🚀 Что ты можешь делать?\n"
    "💬 GPT-чат — задавай вопросы, создавай тексты, разбирайся в сложных темах и пиши код с помощью ИИ.\n"
    "🧠 Разные нейросети — выбирай подходящие инструменты под свои задачи.\n"
    "👁 Анализ изображений — распознавай объекты, извлекай текст с фотографий и получай ответы по содержимому изображений.\n"
    "🎨 Генерация изображений — создавай уникальные картинки и редактируй фотографии с помощью Nano Banana и GPT Images.\n"
    "🎬 Генерация видео — превращай текстовые идеи и фотографии в динамичные видеоролики.\n"
    "📚 Учёба и работа — готовь конспекты и работай с текстами.\n"
    "💎 Единый баланс токенов — используй доступные инструменты в рамках одного баланса.\n\n"
    "👇 Выбирай нужный раздел в меню или просто напиши свой вопрос в чат!"
)


@user.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    await cleanup_tracked_messages(message.bot, message.chat.id, state)
    await state.clear()

    await register_user(
        message.from_user.id,
        message.from_user.username,
    )

    # Обрабатываем реферальный параметр /start ref_123456
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) == 2 and parts[1].startswith("ref_"):
        ref_str = parts[1].removeprefix("ref_").strip()
        if ref_str.isdigit():
            referrer_id = int(ref_str)
            credited_ref = await register_referral_if_new(
                referred_id=message.from_user.id,
                referrer_id=referrer_id,
            )
            if credited_ref:
                try:
                    await message.bot.send_message(
                        chat_id=referrer_id,
                        text=(
                            "🎉 По вашей реферальной ссылке присоединился новый пользователь!\n"
                            "🎁 Вам начислено <b>+30 бонусных токенов</b>!"
                        ),
                        parse_mode="HTML",
                    )
                except Exception:
                    pass

    # Проверяем обязательную подписку на канал https://t.me/GPT6_NANO_BANANO
    subscribed = await is_user_subscribed_to_channel(message.bot, message.from_user.id)
    if not subscribed:
        await message.answer(
            "🔒 <b>Для использования бота необходимо подписаться на наш Telegram-канал!</b>\n\n"
            f"👉 Канал: {REQUIRED_CHANNEL_URL}\n\n"
            "Подпишитесь по кнопке ниже и нажмите «✅ Я подписался»:",
            parse_mode="HTML",
            reply_markup=channel_sub_check_kb,
            disable_web_page_preview=True,
        )
        return

    await message.answer(WELCOME_START_TEXT, reply_markup=menu_kb)


@user.callback_query(F.data == "check_channel_sub")
async def check_channel_sub_callback(callback: CallbackQuery):
    subscribed = await is_user_subscribed_to_channel(
        callback.bot,
        callback.from_user.id,
        force_check=True,
    )
    if not subscribed:
        await callback.answer(
            "❌ Вы ещё не подписались на канал! Подпишитесь и нажмите кнопку снова.",
            show_alert=True,
        )
        return

    await callback.answer("✅ Подписка подтверждена! Добро пожаловать!")
    if callback.message:
        await delete_message_safely(callback.bot, callback.message.chat.id, callback.message.message_id)
        await callback.bot.send_message(
            chat_id=callback.message.chat.id,
            text=WELCOME_START_TEXT,
            reply_markup=menu_kb,
        )


@user.message(Command("check_gemini"))
async def check_gemini_command(message: Message):
    """Команда для быстрой проверки подключения к Gemini API через Google AI Studio."""
    ok, details = await check_gemini_api()
    if ok:
        await message.answer(f"✅ Gemini API (Google AI Studio) успешно подключён!\nОтвет: {details}")
    else:
        await message.answer(f"❌ Ошибка проверки Gemini API:\n{details}")


async def build_profile_text(user_id: int, first_name: str) -> str:
    tokens = await get_user_tokens(user_id)
    premium_until = await get_premium_until(user_id)

    if premium_until:
        subscription_text = f"💎 Premium активен до: {premium_until:%d.%m.%Y %H:%M} UTC"
    else:
        subscription_text = "Подписка: бесплатный доступ"

    return (
        f"👋 Привет, {first_name}\n\n"
        f"Ваш баланс: {tokens} токенов\n"
        f"{subscription_text}\n\n"
        "* Бесплатные токены в боте ограничены, "
        "они обновляются каждые 24 часа в 00:00 по Московскому времени.\n\n"
        "👇 Нажмите на кнопку ниже, чтобы приобрести подписку по акции"
    )


@user.message(F.text == "мой профиль👤")
async def my_proff(message: Message, state: FSMContext):
    await cleanup_tracked_messages(message.bot, message.chat.id, state)
    await state.clear()

    bot_username = await get_bot_username(message.bot)
    text = await build_profile_text(message.from_user.id, message.from_user.first_name)
    await message.answer(
        text,
        reply_markup=build_profile_options_kb(bot_username),
    )


@user.message(Command("profile"))
async def profile_command(message: Message, state: FSMContext):
    await cleanup_tracked_messages(message.bot, message.chat.id, state)
    await state.clear()

    bot_username = await get_bot_username(message.bot)
    text = await build_profile_text(message.from_user.id, message.from_user.first_name)
    await message.answer(
        text,
        reply_markup=build_profile_options_kb(bot_username),
    )


@user.callback_query(F.data == "back_to_profile")
async def back_to_profile_callback(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.answer()
    if callback.message is None:
        return
    bot_username = await get_bot_username(callback.bot)
    text = await build_profile_text(callback.from_user.id, callback.from_user.first_name)
    await replace_inline_screen(
        callback,
        text=text,
        reply_markup=build_profile_options_kb(bot_username),
    )


@user.message(F.text == "Нано банано🍌")
async def nano_banano(message: Message, state: FSMContext):
    await start_nano_banano(message, state, back_target="generator_menu")


@user.message(
    NanoBananoStates.active,
    F.text,
    ~F.text.startswith("/"),
    ~F.text.in_(CHATGPT_MENU_TEXTS),
)
async def nano_text_input(message: Message, state: FSMContext):
    prompt = (message.text or "").strip()
    if not prompt:
        await message.answer("Отправьте текстовый запрос для генерации.")
        return

    data = await state.get_data()
    if data.get("awaiting_photo_description") and data.get("photo"):
        await state.update_data(
            prompt=prompt,
            prompt_message_id=message.message_id,
            awaiting_photo_description=False,
            expanded=None,
        )
    else:
        await state.update_data(
            prompt=prompt,
            photo=None,
            prompt_message_id=message.message_id,
            awaiting_photo_description=False,
            expanded=None,
        )
    await show_generation_menu(message, state)


@user.message(NanoBananoStates.active, F.photo)
async def nano_photo_input(message: Message, state: FSMContext):
    photo_file_id = message.photo[-1].file_id
    prompt = (message.caption or "").strip()
    await state.update_data(
        prompt=prompt or None,
        photo=photo_file_id,
        prompt_message_id=None,
        awaiting_photo_description=not bool(prompt),
        expanded=None,
    )
    if not prompt:
        clarification_message = await message.answer(
            "📷 Фото сохранено. Добавьте текстовое описание изменений."
        )
        await track_process_bot_message(state, clarification_message)
        return
    await show_generation_menu(message, state)


@user.callback_query(F.data.in_(NANO_MENU_CALLBACKS))
async def nano_settings_callback(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    if callback.message is None:
        return

    data = await state.get_data()
    if not data.get("model"):
        await callback.bot.send_message(
            chat_id=callback.message.chat.id,
            text="Настройки устарели. Нажмите «Нано банано🍌» и отправьте новый запрос.",
        )
        return

    callback_data = callback.data
    if callback_data.startswith("toggle_"):
        list_name = {
            "toggle_model_list": "model",
            "toggle_format_list": "format",
            "toggle_res_list": "resolution",
        }[callback_data]
        if list_name == "resolution" and len(MODEL_RESOLUTIONS[data["model"]]) == 1:
            data["resolution"] = "standard"
            data["expanded"] = None
            await state.update_data(resolution="standard", expanded=None)
            await edit_generation_menu(callback, data)
            return
        data["expanded"] = None if data.get("expanded") == list_name else list_name
        await state.update_data(expanded=data["expanded"])
        await edit_generation_menu(callback, data)
        return

    model_choices = {
        "set_model_nano_banana": "nano_banana",
        "set_model_nano_banana_pro": "nano_banana_pro",
        "set_model_nano_banana_2": "nano_banana_2",
    }
    format_choices = {
        "set_format_original": "original",
        "set_format_1_1": "1:1",
        "set_format_16_9": "16:9",
        "set_format_9_16": "9:16",
    }
    resolution_choices = {
        "set_res_standard": "standard",
        "set_res_2k": "2k",
        "set_res_4k": "4k",
    }

    if callback_data in model_choices:
        data["model"] = model_choices[callback_data]
        if len(MODEL_RESOLUTIONS[data["model"]]) == 1:
            data["resolution"] = "standard"
    elif callback_data in format_choices:
        data["format"] = format_choices[callback_data]
    elif callback_data in resolution_choices:
        requested_resolution = resolution_choices[callback_data]
        if requested_resolution not in MODEL_RESOLUTIONS[data["model"]]:
            await callback.message.answer("Это разрешение недоступно для выбранной модели.")
            return
        data["resolution"] = requested_resolution
    elif callback_data == "confirm_generation":
        prompt = data.get("prompt")
        if not prompt:
            await callback.message.answer("Сначала отправьте текстовый запрос для генерации.")
            return

        progress_message = None
        try:
            progress_message = await callback.message.edit_text(
                "⏳ Генерирую изображение…",
                reply_markup=None,
            )
            input_image = None
            if data.get("photo"):
                input_image = await photo_to_data_uri(callback.message, data["photo"])

            image_url = await generate_nanobanano_image(
                prompt=prompt,
                model=data["model"],
                image_url=input_image,
                aspect_ratio=data["format"],
                quality=data["resolution"],
                timeout=600.0,
            )
            image_bytes = await download_image_bytes(image_url)

            photo = BufferedInputFile(convert_image_to_png(image_bytes), filename="nanobanana.png")
            await callback.message.answer_photo(
                photo=photo,
                caption=f"🍌 Готово!\nМодель: {MODEL_NAMES[data['model']]}",
                reply_markup=nano_otmena_kb,
            )
            if data.get("photo"):
                for message_id in data.get("process_bot_message_ids", []):
                    if progress_message and message_id != progress_message.message_id:
                        await delete_message_safely(callback.bot, callback.message.chat.id, message_id)
                prompt_message_id = data.get("prompt_message_id")
                if prompt_message_id:
                    await delete_message_safely(callback.bot, callback.message.chat.id, prompt_message_id)

            await state.update_data(
                prompt=None,
                photo=None,
                prompt_message_id=None,
                awaiting_photo_description=False,
            )
        except Exception:
            logging.exception("Ошибка генерации Nano Banana")
            await callback.message.answer("❌ Ошибка генерации. Подробности смотри в терминале.")
        finally:
            if progress_message:
                await delete_message_safely(
                    callback.bot,
                    callback.message.chat.id,
                    progress_message.message_id,
                )
        return

    data["expanded"] = None
    await state.update_data(
        model=data["model"],
        format=data["format"],
        resolution=data["resolution"],
        expanded=None,
    )
    await edit_generation_menu(callback, data)


async def show_chatgpt_images(
    message: Message,
    state: FSMContext,
    back_target: str = "generator_menu",
    edit_existing: bool = False,
) -> None:
    await cleanup_tracked_messages(message.bot, message.chat.id, state)
    if edit_existing:
        await delete_message_safely(message.bot, message.chat.id, message.message_id)
    await state.clear()

    welcome_message = await message.answer("✅ Теперь в чате: GPT Images")
    prompt_text = (
        "✨ <b>Создавайте и генерируйте картинки с GPT Images!</b>\n\n"
        "Эта нейронка — конкурент 🍌 Nano Banana.\n\n"
        "💎 Цена: 3 токена.\n\n"
        '<a href="https://t.me/+Rc0wwdTLaNhjMzVi">Наш канал с примерами запросов</a>\n\n'
        "Отправь запрос на создание картинки или отправь фото с описанием изменений ⤵️"
    )
    prompt_message = await message.answer(
        prompt_text,
        reply_markup=chatgpt_otmena_kb,
        parse_mode="HTML",
    )
    await state.update_data(
        chatgpt_welcome_message_id=welcome_message.message_id,
        chatgpt_prompt_message_id=prompt_message.message_id,
        chatgpt_back_target=back_target,
        source_photo_file_id=None,
        chatgpt_photo_prompt_message_id=None,
        process_bot_message_ids=[welcome_message.message_id, prompt_message.message_id],
    )
    await state.set_state(ChatGPTImagesStates.waiting_for_prompt)


async def process_chatgpt_images_prompt(
    message: Message,
    state: FSMContext,
    prompt: str,
    photo_file_id: str | None = None,
) -> None:
    data = await state.get_data()
    photo_file_id = photo_file_id or data.get("source_photo_file_id")
    progress_message = None
    generation_succeeded = False
    try:
        progress_message = await message.answer("⏳ Генерирую изображение…")
        input_images = None
        if photo_file_id:
            input_images = [await photo_to_data_uri(message, photo_file_id)]
        image_url = await generate_chatgpt_image(
            prompt=prompt,
            images=input_images,
            timeout=900.0,
        )
        image_bytes = await download_image_bytes(image_url)
        image = BufferedInputFile(convert_image_to_png(image_bytes), filename="chatgpt-image.png")
        await message.answer_photo(
            photo=image,
            caption="✨ Готово! ChatGPT Images",
            reply_markup=chatgpt_otmena_kb,
        )
        generation_succeeded = True
    except Exception as error:
        logging.error("ChatGPT Images request failed (%s)", type(error).__name__, exc_info=True)
        await message.answer("Не удалось создать изображение. Попробуйте отправить описание ещё раз.")
    finally:
        if progress_message:
            await delete_message_safely(message.bot, message.chat.id, progress_message.message_id)

    if generation_succeeded:
        for message_id in (
            data.get("chatgpt_prompt_message_id"),
            data.get("chatgpt_photo_prompt_message_id"),
        ):
            if message_id:
                await delete_message_safely(message.bot, message.chat.id, message_id)
        await state.update_data(
            chatgpt_prompt_message_id=None,
            chatgpt_photo_prompt_message_id=None,
            source_photo_file_id=None,
        )


@user.message(ChatGPTImagesStates.waiting_for_prompt, F.photo)
async def chatgpt_images_photo(message: Message, state: FSMContext):
    photo = message.photo[-1]
    caption = (message.caption or "").strip()
    await state.update_data(source_photo_file_id=photo.file_id)
    if caption:
        await process_chatgpt_images_prompt(message, state, caption, photo_file_id=photo.file_id)
        return
    description_message = await message.answer(
        "📷 Фото сохранено. Отправьте текстовое описание изменений."
    )
    await track_process_bot_message(state, description_message)
    await state.update_data(chatgpt_photo_prompt_message_id=description_message.message_id)


@user.message(
    ChatGPTImagesStates.waiting_for_prompt,
    F.text,
    ~F.text.startswith("/"),
    ~F.text.in_(CHATGPT_MENU_TEXTS),
)
async def chatgpt_images_prompt(message: Message, state: FSMContext):
    prompt = (message.text or "").strip()
    if not prompt:
        await message.answer("Отправьте текстовое описание изображения.")
        return
    await process_chatgpt_images_prompt(message, state, prompt)


@user.message(F.text == "чатГПТ картинки🔥")
async def chatgpt_photo(message: Message, state: FSMContext):
    await show_chatgpt_images(message, state, back_target="generator_menu")


# ---------- KLING AI ВИДЕО ----------
async def start_kling(message: Message, state: FSMContext) -> None:
    await cleanup_tracked_messages(message.bot, message.chat.id, state)
    await state.clear()

    welcome_message = await message.answer("Теперь в чате: Kling")
    settings_message = await message.answer(
        kling_settings_menu_text(KLING_DEFAULT_MODEL, KLING_DEFAULT_DURATION),
        reply_markup=build_kling_settings_keyboard(
            model=KLING_DEFAULT_MODEL,
            duration=KLING_DEFAULT_DURATION,
            expanded=None,
        ),
        parse_mode="HTML",
    )

    await state.update_data(
        kling_model=KLING_DEFAULT_MODEL,
        kling_duration=KLING_DEFAULT_DURATION,
        kling_expanded=None,
        kling_photo_file_id=None,
        kling_prompt=None,
        kling_active_menu_message_id=settings_message.message_id,
        process_bot_message_ids=[welcome_message.message_id, settings_message.message_id],
    )
    await state.set_state(KlingStates.choosing_settings)


@user.callback_query(F.data == "open_kling")
async def open_kling_handler(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    if callback.message is None:
        return
    source_message = callback.message
    await delete_message_safely(callback.bot, source_message.chat.id, source_message.message_id)
    await start_kling(source_message, state)


@user.callback_query(F.data.in_(KLING_MENU_CALLBACKS))
async def kling_settings_callback(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    if callback.message is None:
        return

    chat_id = callback.message.chat.id
    data = await state.get_data()
    model = data.get("kling_model", KLING_DEFAULT_MODEL)
    duration = str(data.get("kling_duration", KLING_DEFAULT_DURATION))
    expanded = data.get("kling_expanded")

    cb = callback.data
    if cb == "kling_toggle_model":
        expanded = None if expanded == "model" else "model"
    elif cb == "kling_toggle_duration":
        expanded = None if expanded == "duration" else "duration"
    elif cb.startswith("kling_set_model_"):
        model = cb.replace("kling_set_model_", "")
        expanded = None
    elif cb.startswith("kling_set_dur_"):
        duration = cb.replace("kling_set_dur_", "")
        expanded = None
    elif cb == "kling_confirm_settings":
        await delete_message_safely(callback.bot, chat_id, callback.message.message_id)
        ready_message = await callback.bot.send_message(
            chat_id=chat_id,
            text=kling_ready_prompt_text(model, duration),
            reply_markup=kling_prompt_kb,
        )
        await track_process_bot_message(state, ready_message)
        await state.update_data(
            kling_model=model,
            kling_duration=duration,
            kling_expanded=None,
            kling_active_menu_message_id=ready_message.message_id,
        )
        await state.set_state(KlingStates.waiting_for_input)
        return

    await delete_message_safely(callback.bot, chat_id, callback.message.message_id)
    new_menu_message = await callback.bot.send_message(
        chat_id=chat_id,
        text=kling_settings_menu_text(model, duration),
        reply_markup=build_kling_settings_keyboard(
            model=model,
            duration=duration,
            expanded=expanded,
        ),
        parse_mode="HTML",
    )
    await track_process_bot_message(state, new_menu_message)
    await state.update_data(
        kling_model=model,
        kling_duration=duration,
        kling_expanded=expanded,
        kling_active_menu_message_id=new_menu_message.message_id,
    )


@user.callback_query(F.data == "back_to_kling_settings")
async def back_to_kling_settings_handler(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    if callback.message is None:
        return
    chat_id = callback.message.chat.id
    data = await state.get_data()
    model = data.get("kling_model", KLING_DEFAULT_MODEL)
    duration = str(data.get("kling_duration", KLING_DEFAULT_DURATION))

    await delete_message_safely(callback.bot, chat_id, callback.message.message_id)
    settings_message = await callback.bot.send_message(
        chat_id=chat_id,
        text=kling_settings_menu_text(model, duration),
        reply_markup=build_kling_settings_keyboard(
            model=model,
            duration=duration,
            expanded=None,
        ),
        parse_mode="HTML",
    )
    await track_process_bot_message(state, settings_message)
    await state.update_data(
        kling_expanded=None,
        kling_photo_file_id=None,
        kling_prompt=None,
        kling_active_menu_message_id=settings_message.message_id,
    )
    await state.set_state(KlingStates.choosing_settings)


@user.callback_query(F.data == "back_from_kling")
async def back_from_kling_handler(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    if callback.message is None:
        return
    chat_id = callback.message.chat.id
    await cleanup_tracked_messages(
        callback.bot,
        chat_id,
        state,
        exclude_message_id=callback.message.message_id,
    )
    if callback.message.video is None:
        await delete_message_safely(callback.bot, chat_id, callback.message.message_id)
    await state.clear()
    await callback.bot.send_message(
        chat_id=chat_id,
        text="Выберите нейросеть для генерации видео:",
        reply_markup=video_generators_kb,
    )


async def execute_kling_generation(
    message: Message,
    state: FSMContext,
    photo_file_id: str,
    prompt: str,
) -> None:
    data = await state.get_data()
    model = data.get("kling_model", KLING_DEFAULT_MODEL)
    duration = str(data.get("kling_duration", KLING_DEFAULT_DURATION))

    await cleanup_tracked_messages(message.bot, message.chat.id, state)
    await state.update_data(
        process_bot_message_ids=[],
        kling_active_menu_message_id=None,
    )

    progress_message = await message.answer("⏳ Генерирую видео в Kling… Это может занять несколько минут.")
    try:
        image_data_uri = await photo_to_data_uri(message, photo_file_id)
        video_url = await generate_kling_video(
            prompt=prompt,
            model=model,
            duration=duration,
            image_base64=image_data_uri,
            timeout=900.0,
        )
        try:
            video_bytes = await download_video_bytes(video_url)
            video_file = BufferedInputFile(video_bytes, filename="kling-video.mp4")
            await message.answer_video(
                video=video_file,
                caption=(
                    f"🎬 Готово! Kling ({KLING_MODEL_LABELS.get(model, model)}, {duration} сек)"
                ),
                reply_markup=kling_otmena_kb,
            )
        except Exception:
            await message.answer_video(
                video=video_url,
                caption=(
                    f"🎬 Готово! Kling ({KLING_MODEL_LABELS.get(model, model)}, {duration} сек)"
                ),
                reply_markup=kling_otmena_kb,
            )

        await state.update_data(
            kling_photo_file_id=None,
            kling_prompt=None,
            process_bot_message_ids=[],
        )
        await state.set_state(KlingStates.waiting_for_input)
    except Exception:
        logging.exception("Ошибка генерации видео Kling")
        err_msg = await message.answer(
            "❌ Не удалось сгенерировать видео в Kling. Попробуйте отправить фото и описание ещё раз.",
            reply_markup=kling_otmena_kb,
        )
        await track_process_bot_message(state, err_msg)
    finally:
        await delete_message_safely(message.bot, message.chat.id, progress_message.message_id)


@user.message(KlingStates.choosing_settings, F.photo)
@user.message(KlingStates.waiting_for_input, F.photo)
async def kling_photo_input(message: Message, state: FSMContext):
    photo_file_id = message.photo[-1].file_id
    caption = (message.caption or "").strip()
    data = await state.get_data()
    saved_prompt = data.get("kling_prompt")

    prompt = caption or saved_prompt
    if prompt:
        await execute_kling_generation(message, state, photo_file_id=photo_file_id, prompt=prompt)
        return

    active_menu_id = data.get("kling_active_menu_message_id")
    if active_menu_id:
        await delete_message_safely(message.bot, message.chat.id, active_menu_id)

    ask_prompt_msg = await message.answer(
        "📷 Фотография сохранена. Теперь отправьте текстовое описание движения:",
        reply_markup=kling_prompt_kb,
    )
    await track_process_bot_message(state, ask_prompt_msg)
    await state.update_data(
        kling_photo_file_id=photo_file_id,
        kling_active_menu_message_id=ask_prompt_msg.message_id,
    )
    await state.set_state(KlingStates.waiting_for_input)


@user.message(
    KlingStates.choosing_settings,
    F.text,
    ~F.text.startswith("/"),
    ~F.text.in_(CHATGPT_MENU_TEXTS),
)
@user.message(
    KlingStates.waiting_for_input,
    F.text,
    ~F.text.startswith("/"),
    ~F.text.in_(CHATGPT_MENU_TEXTS),
)
async def kling_text_input(message: Message, state: FSMContext):
    prompt = (message.text or "").strip()
    if not prompt:
        return

    data = await state.get_data()
    photo_file_id = data.get("kling_photo_file_id")
    if photo_file_id:
        await execute_kling_generation(message, state, photo_file_id=photo_file_id, prompt=prompt)
        return

    active_menu_id = data.get("kling_active_menu_message_id")
    if active_menu_id:
        await delete_message_safely(message.bot, message.chat.id, active_menu_id)

    ask_photo_msg = await message.answer(
        "📝 Описание движения сохранено. Теперь отправьте фотографию:",
        reply_markup=kling_prompt_kb,
    )
    await track_process_bot_message(state, ask_photo_msg)
    await state.update_data(
        kling_prompt=prompt,
        kling_active_menu_message_id=ask_photo_msg.message_id,
    )
    await state.set_state(KlingStates.waiting_for_input)


# ---------- VEO 3.1 ВИДЕО ----------
async def start_veo(message: Message, state: FSMContext) -> None:
    await cleanup_tracked_messages(message.bot, message.chat.id, state)
    await state.clear()

    welcome_message = await message.answer("Теперь в чате: Veo 3.1")
    info_message = await message.answer(
        veo_welcome_text(),
        reply_markup=build_veo_keyboard(
            model=VEO_DEFAULT_MODEL,
            aspect_ratio=VEO_DEFAULT_ASPECT_RATIO,
            expanded=None,
        ),
        parse_mode="HTML",
        disable_web_page_preview=True,
    )

    await state.update_data(
        veo_model=VEO_DEFAULT_MODEL,
        veo_aspect_ratio=VEO_DEFAULT_ASPECT_RATIO,
        veo_expanded=None,
        veo_photos=[],
        veo_active_menu_message_id=info_message.message_id,
        process_bot_message_ids=[welcome_message.message_id, info_message.message_id],
    )
    await state.set_state(VeoStates.waiting_for_input)


@user.callback_query(F.data == "open_veo")
async def open_veo_handler(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    if callback.message is None:
        return
    source_message = callback.message
    await delete_message_safely(callback.bot, source_message.chat.id, source_message.message_id)
    await start_veo(source_message, state)


@user.callback_query(F.data.in_(VEO_MENU_CALLBACKS))
async def veo_settings_callback(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    if callback.message is None:
        return

    chat_id = callback.message.chat.id
    data = await state.get_data()
    model = data.get("veo_model", VEO_DEFAULT_MODEL)
    aspect_ratio = data.get("veo_aspect_ratio", VEO_DEFAULT_ASPECT_RATIO)
    expanded = data.get("veo_expanded")

    cb = callback.data
    if cb == "veo_toggle_model":
        expanded = None if expanded == "model" else "model"
    elif cb == "veo_toggle_aspect":
        expanded = None if expanded == "aspect" else "aspect"
    elif cb.startswith("veo_set_model_"):
        model = cb.replace("veo_set_model_", "")
        expanded = None
    elif cb.startswith("veo_set_aspect_"):
        aspect_ratio = cb.replace("veo_set_aspect_", "").replace("_", ":")
        expanded = None

    await delete_message_safely(callback.bot, chat_id, callback.message.message_id)
    new_info_message = await callback.bot.send_message(
        chat_id=chat_id,
        text=veo_welcome_text(),
        reply_markup=build_veo_keyboard(
            model=model,
            aspect_ratio=aspect_ratio,
            expanded=expanded,
        ),
        parse_mode="HTML",
        disable_web_page_preview=True,
    )
    await track_process_bot_message(state, new_info_message)
    await state.update_data(
        veo_model=model,
        veo_aspect_ratio=aspect_ratio,
        veo_expanded=expanded,
        veo_active_menu_message_id=new_info_message.message_id,
    )


@user.callback_query(F.data == "back_from_veo")
async def back_from_veo_handler(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    if callback.message is None:
        return
    chat_id = callback.message.chat.id
    await cleanup_tracked_messages(
        callback.bot,
        chat_id,
        state,
        exclude_message_id=callback.message.message_id,
    )
    if callback.message.video is None:
        await delete_message_safely(callback.bot, chat_id, callback.message.message_id)
    await state.clear()
    await callback.bot.send_message(
        chat_id=chat_id,
        text="Выберите нейросеть для генерации видео:",
        reply_markup=video_generators_kb,
    )


async def execute_veo_generation(
    message: Message,
    state: FSMContext,
    prompt: str,
    photo_file_ids: list[str] | None = None,
) -> None:
    data = await state.get_data()
    model = data.get("veo_model", VEO_DEFAULT_MODEL)
    aspect_ratio = data.get("veo_aspect_ratio", VEO_DEFAULT_ASPECT_RATIO)
    photo_file_ids = list(photo_file_ids or data.get("veo_photos") or [])

    await cleanup_tracked_messages(message.bot, message.chat.id, state)
    await state.update_data(
        process_bot_message_ids=[],
        veo_active_menu_message_id=None,
    )

    progress_message = await message.answer("⏳ Генерирую видео в Veo 3.1… Это может занять несколько минут.")
    try:
        encoded_images: list[str] = []
        for file_id in photo_file_ids[:3]:
            encoded_images.append(await photo_to_data_uri(message, file_id))

        video_url = await generate_veo_video(
            prompt=prompt,
            model=model,
            aspect_ratio=aspect_ratio,
            images=encoded_images if encoded_images else None,
            timeout=900.0,
        )
        try:
            video_bytes = await download_video_bytes(video_url)
            video_file = BufferedInputFile(video_bytes, filename="veo-video.mp4")
            await message.answer_video(
                video=video_file,
                caption=f"🕺 Готово! {VEO_MODEL_LABELS.get(model, 'Veo 3.1')}",
                reply_markup=veo_otmena_kb,
            )
        except Exception:
            await message.answer_video(
                video=video_url,
                caption=f"🕺 Готово! {VEO_MODEL_LABELS.get(model, 'Veo 3.1')}",
                reply_markup=veo_otmena_kb,
            )

        await state.update_data(veo_photos=[], process_bot_message_ids=[])
        await state.set_state(VeoStates.waiting_for_input)
    except Exception:
        logging.exception("Ошибка генерации видео Veo 3.1")
        err_msg = await message.answer(
            "❌ Не удалось сгенерировать видео в Veo 3.1. Попробуйте отправить запрос ещё раз.",
            reply_markup=veo_otmena_kb,
        )
        await track_process_bot_message(state, err_msg)
    finally:
        await delete_message_safely(message.bot, message.chat.id, progress_message.message_id)


@user.message(VeoStates.waiting_for_input, F.photo)
async def veo_photo_input(message: Message, state: FSMContext):
    photo_file_id = message.photo[-1].file_id
    caption = (message.caption or "").strip()
    data = await state.get_data()
    photos = list(data.get("veo_photos") or [])
    if len(photos) < 3:
        photos.append(photo_file_id)
    else:
        photos = [photo_file_id]

    if caption:
        await execute_veo_generation(message, state, prompt=caption, photo_file_ids=photos)
        return

    active_menu_id = data.get("veo_active_menu_message_id")
    if active_menu_id:
        await delete_message_safely(message.bot, message.chat.id, active_menu_id)

    count = len(photos)
    if count == 1:
        hint = (
            "📷 Фото сохранено (1/3).\n"
            "Отправьте текстовое описание для анимации фото или пришлите ещё 1–2 изображения "
            "(2 фото — начальный и конечный кадры, 3 фото — референсы):"
        )
    elif count == 2:
        hint = (
            "📷 Сохранено 2 фото (начальный и конечный кадры).\n"
            "Отправьте текстовое описание перехода или пришлите 3-е фото для режима референсов:"
        )
    else:
        hint = "📷 Сохранено 3 фото (режим референсов). Отправьте текстовое описание видео:"

    hint_msg = await message.answer(hint, reply_markup=veo_otmena_kb)
    await track_process_bot_message(state, hint_msg)
    await state.update_data(
        veo_photos=photos,
        veo_active_menu_message_id=hint_msg.message_id,
    )


@user.message(
    VeoStates.waiting_for_input,
    F.text,
    ~F.text.startswith("/"),
    ~F.text.in_(CHATGPT_MENU_TEXTS),
)
async def veo_text_input(message: Message, state: FSMContext):
    prompt = (message.text or "").strip()
    if not prompt:
        return
    await execute_veo_generation(message, state, prompt=prompt)


# ---------- ГЕНЕРАЦИЯ ТЕКСТА (ОБЫЧНЫЙ ЧАТ И ВЫБОР МОДЕЛЕЙ НА БАЗЕ GEMINI) ----------
async def show_text_generation_screen(
    bot,
    chat_id: int,
    user_id: int,
    state: FSMContext,
) -> None:
    await cleanup_tracked_messages(bot, chat_id, state)
    await state.clear()

    model_key = get_user_text_model(user_id)
    model_name = TEXT_CHAT_MODELS[model_key]["name"]
    remember = is_remember_dialog_enabled(user_id)

    welcome_msg = await bot.send_message(
        chat_id=chat_id,
        text=f"Теперь в чате: {model_name}",
    )

    caption = text_chat_settings_caption(user_id)
    keyboard = build_text_models_keyboard(model_key, remember)

    banner_path = Path(__file__).resolve().parent.parent / "chat_banner.jpg"
    if banner_path.is_file():
        menu_msg = await bot.send_photo(
            chat_id=chat_id,
            photo=FSInputFile(str(banner_path)),
            caption=caption,
            parse_mode="HTML",
            reply_markup=keyboard,
        )
    else:
        menu_msg = await bot.send_message(
            chat_id=chat_id,
            text=caption,
            parse_mode="HTML",
            reply_markup=keyboard,
        )

    await state.update_data(
        process_bot_message_ids=[welcome_msg.message_id, menu_msg.message_id],
    )


@user.callback_query(F.data == "text_generation")
async def text_generation(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    if callback.message is None:
        return
    chat_id = callback.message.chat.id
    await delete_message_safely(callback.bot, chat_id, callback.message.message_id)
    await show_text_generation_screen(callback.bot, chat_id, callback.from_user.id, state)


@user.message(F.text == "генерация текста")
async def text_generation_reply(message: Message, state: FSMContext):
    await show_text_generation_screen(message.bot, message.chat.id, message.from_user.id, state)


@user.callback_query(F.data.startswith("chat_set_model_"))
async def chat_set_model_callback(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    if callback.message is None:
        return
    model_key = callback.data.replace("chat_set_model_", "")
    set_user_text_model(callback.from_user.id, model_key)
    chat_id = callback.message.chat.id
    await delete_message_safely(callback.bot, chat_id, callback.message.message_id)
    await show_text_generation_screen(callback.bot, chat_id, callback.from_user.id, state)


@user.callback_query(F.data == "chat_toggle_memory")
async def chat_toggle_memory_callback(callback: CallbackQuery, state: FSMContext):
    remember = toggle_remember_dialog(callback.from_user.id)
    status_label = "включено" if remember else "выключено"
    await callback.answer(f"Запоминание диалога: {status_label}")
    if callback.message is None:
        return
    chat_id = callback.message.chat.id
    await delete_message_safely(callback.bot, chat_id, callback.message.message_id)
    await show_text_generation_screen(callback.bot, chat_id, callback.from_user.id, state)


@user.callback_query(F.data == "chat_my_dialogs")
async def chat_my_dialogs_callback(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    if callback.message is None:
        return
    session = get_user_session(callback.from_user.id)
    await replace_inline_screen(
        callback,
        "💬 <b>Мои диалоги</b>\nВыберите диалог для продолжения или начните новый:",
        build_user_dialogs_keyboard(session),
        parse_mode="HTML",
    )


@user.callback_query(F.data == "chat_new_dialog")
async def chat_new_dialog_callback(callback: CallbackQuery, state: FSMContext):
    new_id = create_new_user_dialog(callback.from_user.id)
    await callback.answer(f"Создан Диалог #{new_id}")
    if callback.message is None:
        return
    session = get_user_session(callback.from_user.id)
    await replace_inline_screen(
        callback,
        f"✅ Вы переключились на новый <b>Диалог #{new_id}</b>.\nМожете сразу отправить свой вопрос в чат!",
        build_user_dialogs_keyboard(session),
        parse_mode="HTML",
    )


@user.callback_query(F.data == "chat_clear_dialog")
async def chat_clear_dialog_callback(callback: CallbackQuery, state: FSMContext):
    clear_active_dialog_messages(callback.from_user.id)
    await callback.answer("История текущего диалога очищена")
    if callback.message is None:
        return
    session = get_user_session(callback.from_user.id)
    await replace_inline_screen(
        callback,
        "🗑 История текущего диалога очищена.",
        build_user_dialogs_keyboard(session),
    )


@user.callback_query(F.data.startswith("chat_switch_dialog_"))
async def chat_switch_dialog_callback(callback: CallbackQuery, state: FSMContext):
    dialog_id = int(callback.data.replace("chat_switch_dialog_", ""))
    switch_user_dialog(callback.from_user.id, dialog_id)
    await callback.answer(f"Выбран диалог #{dialog_id}")
    if callback.message is None:
        return
    session = get_user_session(callback.from_user.id)
    await replace_inline_screen(
        callback,
        f"💬 Активный диалог: <b>#{dialog_id}</b>.\nНапишите сообщение в чат, чтобы продолжить общение.",
        build_user_dialogs_keyboard(session),
        parse_mode="HTML",
    )


# ---------- РАСПОЗНАВАНИЕ КАРТИНОК И СЕРВИСЫ ДЛЯ УЧЁБЫ И РАБОТЫ (НА БАЗЕ GEMINI) ----------
@user.callback_query(F.data == "image_recognition")
async def image_recognition(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    if callback.message is None:
        return
    chat_id = callback.message.chat.id
    await cleanup_tracked_messages(callback.bot, chat_id, state)
    await delete_message_safely(callback.bot, chat_id, callback.message.message_id)
    await state.clear()

    welcome_msg = await callback.bot.send_message(
        chat_id=chat_id,
        text="Теперь в чате: Распознавание изображений 🔍",
    )
    prompt_msg = await callback.bot.send_message(
        chat_id=chat_id,
        text=(
            "🔍 <b>Распознавание содержимого картинки</b>\n\n"
            f"💎 Стоимость: {SERVICES['vision']['price']} токена\n\n"
            f"{SERVICES['vision']['hint']} ⤵️"
        ),
        parse_mode="HTML",
        reply_markup=recognition_back_kb,
    )
    await state.update_data(
        process_bot_message_ids=[welcome_msg.message_id, prompt_msg.message_id],
    )
    await state.set_state(ImageRecognitionStates.waiting_for_photo)


@user.message(ImageRecognitionStates.waiting_for_photo, F.photo)
async def handle_image_recognition_photo(message: Message, state: FSMContext):
    photo_file_id = message.photo[-1].file_id
    user_caption = (message.caption or "").strip()
    price = SERVICES["vision"]["price"]

    if not await spend_tokens(message.from_user.id, price):
        await message.answer(
            f"❌ Недостаточно токенов (требуется {price}). Пополните баланс в разделе «купить премиум🚀».",
            reply_markup=recognition_back_kb,
        )
        return

    progress_msg = await message.answer("⏳ Анализирую изображение…")
    try:
        image_bytes = await download_telegram_photo_bytes(message, photo_file_id)
        answer = await generate_gemini_service_response(
            system_prompt=SERVICES["vision"]["prompt"],
            user_text=user_caption or "Опиши всё, что видишь на изображении, и извлеки текст, если он есть.",
            image_bytes=image_bytes,
        )
        await cleanup_tracked_messages(message.bot, message.chat.id, state)
        await state.update_data(process_bot_message_ids=[])
        await send_long_message(message, answer, reply_markup=recognition_back_kb)
    except Exception as exc:
        logging.exception("Ошибка распознавания картинки через Gemini")
        await message.answer(
            f"❌ Не удалось распознать изображение: {exc}",
            reply_markup=recognition_back_kb,
        )
    finally:
        await delete_message_safely(message.bot, message.chat.id, progress_msg.message_id)


@user.message(
    ImageRecognitionStates.waiting_for_photo,
    F.text,
    ~F.text.startswith("/"),
    ~F.text.in_(CHATGPT_MENU_TEXTS),
)
async def handle_image_recognition_text_only(message: Message):
    await message.answer(
        "📷 Пожалуйста, отправьте фотографию, содержимое которой нужно распознать.",
        reply_markup=recognition_back_kb,
    )


@user.callback_query(F.data == "study_services")
async def study_services(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    if callback.message is None:
        return
    chat_id = callback.message.chat.id
    await cleanup_tracked_messages(
        callback.bot,
        chat_id,
        state,
        exclude_message_id=callback.message.message_id,
    )
    await state.clear()
    await delete_message_safely(callback.bot, chat_id, callback.message.message_id)

    model_name = TEXT_CHAT_MODELS[get_user_text_model(callback.from_user.id)]["name"]
    welcome_msg = await callback.bot.send_message(
        chat_id=chat_id,
        text=f"Теперь в чате: {model_name}",
    )
    menu_msg = await callback.bot.send_message(
        chat_id=chat_id,
        text="💬 Выберите раздел, который вас интересует.",
        reply_markup=services_menu_kb,
    )
    await state.update_data(
        process_bot_message_ids=[welcome_msg.message_id, menu_msg.message_id],
    )


@user.callback_query(F.data == "service_diploma")
async def service_diploma_menu(callback: CallbackQuery, state: FSMContext):
    """Экран «Диплом, реферат, эссе» в точности как на скриншоте 3."""
    await callback.answer()
    if callback.message is None:
        return
    chat_id = callback.message.chat.id
    await cleanup_tracked_messages(callback.bot, chat_id, state)
    await delete_message_safely(callback.bot, chat_id, callback.message.message_id)
    await state.clear()

    welcome_msg = await callback.bot.send_message(
        chat_id=chat_id,
        text="Теперь в чате: Диплом, реферат, эссе",
    )
    menu_msg = await callback.bot.send_message(
        chat_id=chat_id,
        text=(
            "Выберите тип работы. После генерации вы получите файл Word "
            "с текстом работы. Результат необходимо проверить и доработать.\n\n"
            "Стоимость: 30 токенов за работу"
        ),
        reply_markup=diploma_types_kb,
    )
    await state.update_data(
        process_bot_message_ids=[welcome_msg.message_id, menu_msg.message_id],
    )


@user.callback_query(F.data.startswith("academic_type_"))
async def select_academic_work_type(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    if callback.message is None:
        return
    work_type = callback.data.replace("academic_type_", "")
    if work_type not in ACADEMIC_WORK_TYPES:
        return

    cfg = ACADEMIC_WORK_TYPES[work_type]
    chat_id = callback.message.chat.id
    await delete_message_safely(callback.bot, chat_id, callback.message.message_id)

    ask_msg = await callback.bot.send_message(
        chat_id=chat_id,
        text=(
            f"📌 Выбрано: <b>{cfg['title']}</b>\n"
            f"💎 Стоимость: {cfg['price']} токенов\n\n"
            f"✍️ {cfg['hint']}"
        ),
        parse_mode="HTML",
        reply_markup=academic_work_back_kb,
    )
    await track_process_bot_message(state, ask_msg)
    await state.update_data(academic_work_type=work_type)
    await state.set_state(StudyServiceStates.waiting_for_academic_topic)


@user.message(
    StudyServiceStates.waiting_for_academic_topic,
    F.text,
    ~F.text.startswith("/"),
    ~F.text.in_(CHATGPT_MENU_TEXTS),
)
async def process_academic_work_topic(message: Message, state: FSMContext):
    topic = (message.text or "").strip()
    if not topic:
        return

    data = await state.get_data()
    work_type = data.get("academic_work_type", "essay")
    cfg = ACADEMIC_WORK_TYPES.get(work_type, ACADEMIC_WORK_TYPES["essay"])
    price = cfg["price"]

    if not await spend_tokens(message.from_user.id, price):
        await message.answer(
            f"❌ Недостаточно токенов (для генерации требуется {price} токенов).\n"
            "Пополните баланс в разделе «купить премиум🚀».",
            reply_markup=academic_work_back_kb,
        )
        return

    progress_msg = await message.answer(
        f"⏳ Генерирую работу («{cfg['title']}») и формирую файл Word… Это займёт около минуты."
    )
    try:
        generated_text = await generate_gemini_service_response(
            system_prompt=cfg["prompt"],
            user_text=f"Тема и требования: {topic}",
        )
        docx_bytes = build_academic_docx_bytes(
            topic=topic,
            work_type_title=cfg["title"],
            text_content=generated_text,
        )
        safe_filename = f"{cfg['title'].replace(' ', '_')}.docx"
        document = BufferedInputFile(docx_bytes, filename=safe_filename)

        await cleanup_tracked_messages(message.bot, message.chat.id, state)
        await state.update_data(process_bot_message_ids=[])

        await message.answer_document(
            document=document,
            caption=(
                f"✅ Ваша работа готова!\n"
                f"📄 Тип: {cfg['title']}\n"
                f"📌 Тема: {topic}"
            ),
            reply_markup=academic_work_back_kb,
        )
    except Exception as exc:
        logging.exception("Ошибка генерации академической работы через Gemini")
        await message.answer(
            f"❌ Не удалось сгенерировать работу: {exc}",
            reply_markup=academic_work_back_kb,
        )
    finally:
        await delete_message_safely(message.bot, message.chat.id, progress_msg.message_id)


@user.callback_query(F.data.in_({"service_photo", "service_summary", "service_rewrite", "service_script"}))
async def start_study_service_handler(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    if callback.message is None:
        return

    mapping = {
        "service_photo": ("math", StudyServiceStates.waiting_for_math_input),
        "service_summary": ("summary", StudyServiceStates.waiting_for_summary_input),
        "service_rewrite": ("rewrite", StudyServiceStates.waiting_for_rewrite_input),
        "service_script": ("script", StudyServiceStates.waiting_for_script_input),
    }
    service_key, target_state = mapping[callback.data]
    svc = SERVICES[service_key]

    chat_id = callback.message.chat.id
    await cleanup_tracked_messages(callback.bot, chat_id, state)
    await delete_message_safely(callback.bot, chat_id, callback.message.message_id)
    await state.clear()

    welcome_msg = await callback.bot.send_message(
        chat_id=chat_id,
        text=f"Теперь в чате: {svc['title']}",
    )
    info_msg = await callback.bot.send_message(
        chat_id=chat_id,
        text=(
            f"📌 <b>{svc['title']}</b>\n"
            f"💎 Стоимость: {svc.get('price', 2)} токена\n\n"
            f"👉 {svc['hint']} ⤵️"
        ),
        parse_mode="HTML",
        reply_markup=study_service_back_kb,
    )
    await state.update_data(
        active_service_key=service_key,
        process_bot_message_ids=[welcome_msg.message_id, info_msg.message_id],
    )
    await state.set_state(target_state)


@user.message(StudyServiceStates.waiting_for_math_input, F.photo)
async def process_math_photo(message: Message, state: FSMContext):
    photo_file_id = message.photo[-1].file_id
    caption = (message.caption or "").strip()
    svc = SERVICES["math"]
    price = svc.get("price", 3)

    if not await spend_tokens(message.from_user.id, price):
        await message.answer(
            f"❌ Недостаточно токенов (требуется {price}). Пополните баланс в разделе «купить премиум🚀».",
            reply_markup=study_service_back_kb,
        )
        return

    progress_msg = await message.answer("⏳ Распознаю условие задачи на фото и готовлю пошаговое решение…")
    try:
        image_bytes = await download_telegram_photo_bytes(message, photo_file_id)
        answer = await generate_gemini_service_response(
            system_prompt=svc["prompt"],
            user_text=caption or "Реши задачу на фото подробно по шагам.",
            image_bytes=image_bytes,
        )
        await cleanup_tracked_messages(message.bot, message.chat.id, state)
        await state.update_data(process_bot_message_ids=[])
        await send_long_message(message, answer, reply_markup=study_service_back_kb)
    except Exception as exc:
        logging.exception("Ошибка решения задачи по фото через Gemini")
        await message.answer(
            f"❌ Ошибка при решении задачи: {exc}",
            reply_markup=study_service_back_kb,
        )
    finally:
        await delete_message_safely(message.bot, message.chat.id, progress_msg.message_id)


@user.message(
    StudyServiceStates.waiting_for_math_input,
    F.text,
    ~F.text.startswith("/"),
    ~F.text.in_(CHATGPT_MENU_TEXTS),
)
@user.message(
    StudyServiceStates.waiting_for_summary_input,
    F.text,
    ~F.text.startswith("/"),
    ~F.text.in_(CHATGPT_MENU_TEXTS),
)
@user.message(
    StudyServiceStates.waiting_for_rewrite_input,
    F.text,
    ~F.text.startswith("/"),
    ~F.text.in_(CHATGPT_MENU_TEXTS),
)
@user.message(
    StudyServiceStates.waiting_for_script_input,
    F.text,
    ~F.text.startswith("/"),
    ~F.text.in_(CHATGPT_MENU_TEXTS),
)
async def process_study_service_text(message: Message, state: FSMContext):
    user_text = (message.text or "").strip()
    if not user_text:
        return

    data = await state.get_data()
    service_key = data.get("active_service_key", "summary")
    svc = SERVICES.get(service_key, SERVICES["summary"])
    price = svc.get("price", 2)

    if not await spend_tokens(message.from_user.id, price):
        await message.answer(
            f"❌ Недостаточно токенов (требуется {price}). Пополните баланс в разделе «купить премиум🚀».",
            reply_markup=study_service_back_kb,
        )
        return

    progress_msg = await message.answer("⏳ Выполняю ваш запрос…")
    try:
        answer = await generate_gemini_service_response(
            system_prompt=svc["prompt"],
            user_text=user_text,
        )
        await cleanup_tracked_messages(message.bot, message.chat.id, state)
        await state.update_data(process_bot_message_ids=[])
        await send_long_message(message, answer, reply_markup=study_service_back_kb)
    except Exception as exc:
        logging.exception("Ошибка сервиса %s через Gemini", service_key)
        await message.answer(
            f"❌ Произошла ошибка: {exc}",
            reply_markup=study_service_back_kb,
        )
    finally:
        await delete_message_safely(message.bot, message.chat.id, progress_msg.message_id)


# ---------- НАВИГАЦИЯ ПО МЕНЮ ----------
@user.message(F.text == "все нейросети🧠")
async def list_neiro(message: Message, state: FSMContext):
    await cleanup_tracked_messages(message.bot, message.chat.id, state)
    await state.clear()
    model_name = TEXT_CHAT_MODELS[get_user_text_model(message.from_user.id)]["name"]
    welcome_msg = await message.answer(f"Теперь в чате: {model_name}")
    menu_msg = await message.answer(
        "🤩 Выберите интересующий вас сервис из списка ниже",
        reply_markup=ai_tools_kb,
    )
    await state.update_data(
        process_bot_message_ids=[welcome_msg.message_id, menu_msg.message_id],
    )


@user.callback_query(F.data == "image_tools")
async def image_tools(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await state.clear()
    await replace_inline_screen(callback, "Генерация картинок и работа с ними:", image_tools_kb)


@user.callback_query(F.data == "image_generation")
async def image_generation(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await state.clear()
    await replace_inline_screen(callback, "Выберите генератор изображений:", image_generators_kb)


@user.callback_query(F.data == "open_nano_banana")
async def open_nano_banana(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    if callback.message is None:
        return
    source_message = callback.message
    await delete_message_safely(callback.bot, source_message.chat.id, source_message.message_id)
    await start_nano_banano(source_message, state, back_target="generator_menu")


@user.callback_query(F.data == "open_chatgpt_images")
async def open_chatgpt_images(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    if callback.message is not None:
        await show_chatgpt_images(
            callback.message,
            state,
            back_target="generator_menu",
            edit_existing=True,
        )


@user.callback_query(F.data == "other_services")
async def other_services(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await state.clear()
    await replace_inline_screen(
        callback,
        "🤩 Выберите интересующий вас сервис из списка ниже",
        services_kb,
    )


async def send_inline_screen_after_back(
    callback: CallbackQuery,
    text: str,
    keyboard,
) -> None:
    """Удаляет предыдущее сообщение и отправляет новый экран меню."""
    message = callback.message
    if message is None:
        return
    chat_id = message.chat.id
    await delete_message_safely(callback.bot, chat_id, message.message_id)
    await callback.bot.send_message(chat_id=chat_id, text=text, reply_markup=keyboard)


@user.callback_query(F.data == "back_to_ai_tools")
async def back_to_ai_tools(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    if callback.message is None:
        return
    chat_id = callback.message.chat.id
    await cleanup_tracked_messages(
        callback.bot,
        chat_id,
        state,
        exclude_message_id=callback.message.message_id,
    )
    await state.clear()
    await delete_message_safely(callback.bot, chat_id, callback.message.message_id)

    model_name = TEXT_CHAT_MODELS[get_user_text_model(callback.from_user.id)]["name"]
    welcome_msg = await callback.bot.send_message(
        chat_id=chat_id,
        text=f"Теперь в чате: {model_name}",
    )
    menu_msg = await callback.bot.send_message(
        chat_id=chat_id,
        text="🤩 Выберите интересующий вас сервис из списка ниже",
        reply_markup=ai_tools_kb,
    )
    await state.update_data(
        process_bot_message_ids=[welcome_msg.message_id, menu_msg.message_id],
    )


@user.callback_query(F.data == "back_to_image_tools")
async def back_to_image_tools(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    if callback.message is not None:
        await cleanup_tracked_messages(
            callback.bot,
            callback.message.chat.id,
            state,
            exclude_message_id=callback.message.message_id,
        )
    await state.clear()
    await send_inline_screen_after_back(
        callback,
        "Генерация картинок и работа с ними:",
        image_tools_kb,
    )


@user.callback_query(F.data == "back_to_main_menu")
async def back_to_main_menu(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    if callback.message is not None:
        await cleanup_tracked_messages(
            callback.bot,
            callback.message.chat.id,
            state,
            exclude_message_id=callback.message.message_id,
        )
    await state.clear()
    await send_inline_screen_after_back(callback, "Главное меню:", menu_kb)


@user.callback_query(F.data.in_({"back_from_nano", "back_from_chatgpt"}))
async def back_from_generators(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    message = callback.message
    if message is None:
        return

    chat_id = message.chat.id
    await cleanup_tracked_messages(
        callback.bot,
        chat_id,
        state,
        exclude_message_id=message.message_id,
    )
    await state.clear()
    await delete_message_safely(callback.bot, chat_id, message.message_id)

    await callback.bot.send_message(
        chat_id=chat_id,
        text="Выберите генератор изображений:",
        reply_markup=gpt_cartinki_kb,
    )


@user.callback_query(F.data == "video_generation")
async def video_generation(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    if callback.message is not None:
        await cleanup_tracked_messages(
            callback.bot,
            callback.message.chat.id,
            state,
            exclude_message_id=callback.message.message_id,
        )
    await state.clear()
    await replace_inline_screen(
        callback,
        "Выберите нейросеть для генерации видео:",
        video_generators_kb,
    )


# ---------- ПРОФИЛЬ / БЕСПЛАТНЫЕ ТОКЕНЫ / РЕФЕРАЛЫ / ЗАДАНИЯ ----------
PREMIUM_INTRO_TEXT = (
    "⚡️ Премиум-подписка даёт безлимит на общение с ботом, а также токены, "
    "которые вы тратите на использование нейросетей.\n\n"
    "<b>Выберите свой тариф ниже:</b>"
)


@user.callback_query(F.data == "pay_premium")
async def profile_premium(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.answer()
    await replace_inline_screen(
        callback,
        text=PREMIUM_INTRO_TEXT,
        reply_markup=subscription_menu_kb,
        parse_mode="HTML",
    )


@user.callback_query(F.data == "free_token")
async def free_token(callback: CallbackQuery, state: FSMContext):
    """Открывает меню с 2 кнопками: 👥 Пригласить друга и 📋 Выполнить задания."""
    await state.clear()
    await callback.answer()
    await replace_inline_screen(
        callback,
        text="🎁 <b>Получить бесплатные токены</b>\n\nВыберите удобный способ получения бонусных токенов:",
        reply_markup=free_tokens_menu_kb,
        parse_mode="HTML",
    )


@user.callback_query(F.data == "free_invite_friend")
async def free_invite_friend_callback(callback: CallbackQuery, state: FSMContext):
    """Показывает реферальную ссылку и статистику пользователя."""
    await state.clear()
    await callback.answer()
    if callback.message is None:
        return

    bot_username = await get_bot_username(callback.bot)
    ref_link = f"https://t.me/{bot_username}?start=ref_{callback.from_user.id}"
    invited_count, earned_tokens = get_referral_stats(callback.from_user.id)

    text = (
        "👥 Пригласи друга в бот и получи бонус!\n\n"
        "🎁 За каждого приглашенного друга, который воспользуется ботом, вы получите 30 токенов! "
        "Так же вы будете получать 30% от купленных им токенов.\n\n"
        "🔗 Ваша реферальная ссылка:\n"
        f"{ref_link}\n\n"
        f"✅ Приглашено вами: {invited_count}\n"
        f"💵 Заработано токенов: {earned_tokens}"
    )

    chat_id = callback.message.chat.id
    try:
        await callback.message.edit_text(
            text=text,
            reply_markup=build_referral_kb(ref_link),
            disable_web_page_preview=True,
        )
    except Exception:
        await delete_message_safely(callback.bot, chat_id, callback.message.message_id)
        await callback.bot.send_message(
            chat_id=chat_id,
            text=text,
            reply_markup=build_referral_kb(ref_link),
            disable_web_page_preview=True,
        )


@user.callback_query(F.data == "free_tasks")
async def free_tasks_callback(callback: CallbackQuery, state: FSMContext):
    """Показывает 3 кнопки подписки на Telegram-каналы."""
    await state.clear()
    await callback.answer()
    await replace_inline_screen(
        callback,
        text="📋 <b>Выполните задания</b>\n\nПодпишитесь на наши Telegram-каналы ниже:",
        reply_markup=build_tasks_channels_kb(),
        parse_mode="HTML",
    )


@user.callback_query(F.data == "tex_pd")
async def tex_pd(callback: CallbackQuery):
    await callback.answer()
    if callback.message is not None:
        await callback.message.answer(
            f"🦺 Связаться с технической поддержкой: {SUPPORT_CONTACT_URL}"
        )


@user.callback_query(F.data == "referal_chat")
async def referal_chat(callback: CallbackQuery):
    await callback.answer()
    if callback.message is None:
        return
    bot_username = await get_bot_username(callback.bot)
    add_group_url = f"https://t.me/{bot_username}?startgroup=true"
    add_channel_url = f"https://t.me/{bot_username}?startchannel=true"
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Добавить бота в группу / чат", url=add_group_url)],
        [InlineKeyboardButton(text="📢 Добавить бота в канал", url=add_channel_url)],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="back_to_profile")],
    ])
    await replace_inline_screen(
        callback,
        text="➕ Выберите, куда вы хотите пригласить бота:",
        reply_markup=kb,
    )


# ---------- ПОДПИСКИ / ТОКЕНЫ / ОПЛАТА ЮKASSA И TELEGRAM STARS ----------
@user.message(F.text == "купить премиум🚀")
async def paymen_prem(message: Message, state: FSMContext):
    await cleanup_tracked_messages(message.bot, message.chat.id, state)
    await state.clear()
    await message.answer(
        PREMIUM_INTRO_TEXT,
        reply_markup=subscription_menu_kb,
        parse_mode="HTML",
    )


@user.message(Command("premium"))
async def premium_command(message: Message, state: FSMContext):
    await cleanup_tracked_messages(message.bot, message.chat.id, state)
    await state.clear()
    await message.answer(
        PREMIUM_INTRO_TEXT,
        reply_markup=subscription_menu_kb,
        parse_mode="HTML",
    )


async def _open_subscription_tariff(
    callback: CallbackQuery,
    price_key: str,
    state: FSMContext | None = None,
) -> None:
    """
    Открывает выбранный тариф подписки:
    - отправляет текст «💳 Вы оформляете подписку за (сумма) и получаете кол во токенов на 30 дней...»
    - сразу генерирует платёжную ссылку ЮKassa и встраивает её в кнопки оплаты.
    - не удаляет старое сообщение заранее, чтобы экран никогда не оставался пустым.
    """
    if state is not None:
        await state.clear()

    try:
        await callback.answer()
    except TelegramBadRequest:
        pass

    if callback.message is None:
        return

    chat_id = callback.message.chat.id
    text = subscription_offer_text(price_key)

    kb = await build_subscription_tariff_keyboard_with_url(
        user_id=callback.from_user.id,
        price_key=price_key,
        bot=callback.bot,
        chat_id=chat_id,
    )

    try:
        await callback.message.edit_text(
            text=text,
            reply_markup=kb,
        )
    except Exception:
        await delete_message_safely(callback.bot, chat_id, callback.message.message_id)
        await callback.bot.send_message(
            chat_id=chat_id,
            text=text,
            reply_markup=kb,
        )


@user.callback_query(F.data == "sub_start")
async def sub_start(callback: CallbackQuery, state: FSMContext):
    await _open_subscription_tariff(callback, "599", state)


@user.callback_query(F.data == "sub_opt")
async def sub_opt(callback: CallbackQuery, state: FSMContext):
    await _open_subscription_tariff(callback, "999", state)


@user.callback_query(F.data == "sub_adv")
async def sub_adv(callback: CallbackQuery, state: FSMContext):
    await _open_subscription_tariff(callback, "1439", state)


@user.callback_query(F.data == "sub_max")
async def sub_max(callback: CallbackQuery, state: FSMContext):
    await _open_subscription_tariff(callback, "2390", state)


@user.callback_query(F.data == "back_to_subs")
async def back_to_subs(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.answer()
    await replace_inline_screen(
        callback,
        text=PREMIUM_INTRO_TEXT,
        reply_markup=subscription_menu_kb,
        parse_mode="HTML",
    )


@user.callback_query(F.data == "tokens_menu")
async def tokens_menu_handler(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.answer()
    await replace_inline_screen(
        callback,
        text="📦 <b>Выберите пакет токенов:</b>",
        reply_markup=tokens_menu_kb,
        parse_mode="HTML",
    )


async def _open_token_package(
    callback: CallbackQuery,
    price_key: str,
    state: FSMContext | None = None,
) -> None:
    """Открывает пакет токенов со встроенной в кнопки платёжной ссылкой ЮKassa."""
    if state is not None:
        await state.clear()

    try:
        await callback.answer()
    except TelegramBadRequest:
        pass

    if callback.message is None:
        return

    chat_id = callback.message.chat.id
    pkg = YOOKASSA_TOKEN_PACKAGES[price_key]
    text = (
        f"💳 Вы оформляете покупку пакета на {pkg['tokens']} токенов "
        f"за {pkg['display_price']}."
    )

    kb = await build_token_package_keyboard_with_url(
        user_id=callback.from_user.id,
        price_key=price_key,
        bot=callback.bot,
        chat_id=chat_id,
    )

    try:
        await callback.message.edit_text(
            text=text,
            reply_markup=kb,
        )
    except Exception:
        await delete_message_safely(callback.bot, chat_id, callback.message.message_id)
        await callback.bot.send_message(
            chat_id=chat_id,
            text=text,
            reply_markup=kb,
        )


@user.callback_query(F.data == "buy_tok_100")
async def buy_tok_100(callback: CallbackQuery, state: FSMContext):
    await _open_token_package(callback, "359", state)


@user.callback_query(F.data == "buy_tok_350")
async def buy_tok_350(callback: CallbackQuery, state: FSMContext):
    await _open_token_package(callback, "863", state)


@user.callback_query(F.data == "buy_tok_1000")
async def buy_tok_1000(callback: CallbackQuery, state: FSMContext):
    await _open_token_package(callback, "2199", state)


# Резервный обработчик, если в .env не были указаны ключи ЮKassa в момент открытия тарифа
@user.callback_query(
    F.data.startswith("pay_rf_")
    | F.data.startswith("pay_sbp_")
    | F.data.startswith("pay_int_")
    | F.data.startswith("pay_tok_rf_")
    | F.data.startswith("pay_tok_sbp_")
    | F.data.startswith("pay_tok_int_")
)
async def handle_yookassa_create_payment(callback: CallbackQuery):
    try:
        await callback.answer()
    except TelegramBadRequest:
        pass
    await start_yookassa_payment_flow(callback, callback.data)


# Проверка статуса платежа ЮKassa по кнопке «✅ Я оплатил (проверить оплату)»
@user.callback_query(F.data.startswith("yk_check:"))
async def handle_yookassa_check_payment(callback: CallbackQuery):
    payment_id = callback.data.split(":", 1)[1].strip()
    if not payment_id:
        await callback.answer("Некорректный ID платежа.", show_alert=True)
        return

    status_code, msg_text = await verify_and_fulfill_yookassa_payment(
        payment_id=payment_id,
        expected_user_id=callback.from_user.id,
        bot=callback.bot,
    )

    if status_code == "credited":
        await callback.answer("✅ Оплата подтверждена!", show_alert=False)
        if callback.message is not None:
            await delete_message_safely(callback.bot, callback.message.chat.id, callback.message.message_id)
            await callback.bot.send_message(
                chat_id=callback.message.chat.id,
                text=msg_text,
                parse_mode="HTML",
            )
    elif status_code == "already_processed":
        await callback.answer(msg_text, show_alert=True)
        if callback.message is not None:
            await delete_message_safely(callback.bot, callback.message.chat.id, callback.message.message_id)
    else:
        await callback.answer(msg_text, show_alert=True)


# ---------- TELEGRAM STARS (XTR) ----------
PREMIUM_PLANS = {
    "599": {"code": "start", "title": "Premium Start", "stars": 400, "tokens": 150},
    "999": {"code": "opt", "title": "Premium Opt", "stars": 650, "tokens": 350},
    "1439": {"code": "adv", "title": "Premium Adv", "stars": 950, "tokens": 700},
    "2390": {"code": "max", "title": "Premium Max", "stars": 1600, "tokens": 1500},
}

TOKEN_PLANS = {
    "100": {"tokens": 100, "stars": 150, "title": "100 токенов"},
    "350": {"tokens": 350, "stars": 360, "title": "350 токенов"},
    "1000": {"tokens": 1000, "stars": 800, "title": "1000 токенов"},
}


@user.callback_query(F.data.startswith("pay_stars_"))
async def pay_stars(callback: CallbackQuery):
    price_key = callback.data.removeprefix("pay_stars_")
    plan = PREMIUM_PLANS.get(price_key)

    if not plan:
        await callback.answer("Неизвестный тариф", show_alert=True)
        return

    payload = f"premium:{callback.from_user.id}:{plan['code']}"

    await callback.message.answer_invoice(
        title=plan["title"],
        description="Подписка Premium на 30 дней",
        payload=payload,
        currency="XTR",
        prices=[
            LabeledPrice(
                label=plan["title"],
                amount=plan["stars"],
            )
        ],
        provider_token="",
    )
    await callback.answer()


@user.callback_query(F.data.startswith("pay_tok_stars_"))
async def pay_tokens_stars(callback: CallbackQuery):
    package_code = callback.data.removeprefix("pay_tok_stars_")
    plan = TOKEN_PLANS.get(package_code)

    if plan is None:
        await callback.answer("Неизвестный пакет", show_alert=True)
        return

    if not await has_premium(callback.from_user.id):
        await callback.answer(
            "Для покупки токенов сначала нужна активная подписка Premium.",
            show_alert=True,
        )
        return

    payload = f"tokens:{callback.from_user.id}:{package_code}"

    await callback.message.answer_invoice(
        title=plan["title"],
        description=f"Покупка: {plan['tokens']} токенов",
        payload=payload,
        currency="XTR",
        prices=[
            LabeledPrice(
                label=plan["title"],
                amount=plan["stars"],
            )
        ],
        provider_token="",
    )
    await callback.answer()


@user.pre_checkout_query()
async def process_pre_checkout(query: PreCheckoutQuery):
    parts = query.invoice_payload.split(":")

    if len(parts) != 3:
        await query.answer(
            ok=False,
            error_message="Некорректный заказ.",
        )
        return

    product_type, telegram_id, product_code = parts

    if (
        not telegram_id.isdigit()
        or int(telegram_id) != query.from_user.id
        or query.currency != "XTR"
    ):
        await query.answer(
            ok=False,
            error_message="Данные заказа не совпадают.",
        )
        return

    if product_type == "premium":
        plan = next(
            (
                item for item in PREMIUM_PLANS.values()
                if item["code"] == product_code
            ),
            None,
        )
        expected_stars = plan["stars"] if plan else None
    elif product_type == "tokens":
        plan = TOKEN_PLANS.get(product_code)
        expected_stars = plan["stars"] if plan else None
    else:
        expected_stars = None

    if expected_stars is None or query.total_amount != expected_stars:
        await query.answer(
            ok=False,
            error_message="Товар или сумма заказа не совпадают.",
        )
        return

    await query.answer(ok=True)


@user.message(F.successful_payment)
async def successful_payment_handler(message: Message):
    payment = message.successful_payment
    parts = payment.invoice_payload.split(":")

    if len(parts) != 3:
        await message.answer(
            "Не удалось определить оплаченный товар. Обратись в поддержку."
        )
        return

    product_type, telegram_id, product_code = parts

    if (
        not telegram_id.isdigit()
        or int(telegram_id) != message.from_user.id
        or payment.currency != "XTR"
    ):
        await message.answer(
            "Не удалось подтвердить заказ. Обратись в поддержку."
        )
        return

    provider_payment_id = payment.telegram_payment_charge_id

    try:
        if product_type == "premium":
            plan = next(
                (
                    item for item in PREMIUM_PLANS.values()
                    if item["code"] == product_code
                ),
                None,
            )

            if plan is None or payment.total_amount != plan["stars"]:
                await message.answer(
                    "Данные платежа не совпадают с заказом. Обратись в поддержку."
                )
                return

            included_tokens = int(plan.get("tokens", 0))
            expires_at = await complete_premium_payment(
                provider="telegram_stars",
                provider_payment_id=provider_payment_id,
                telegram_id=message.from_user.id,
                product_code=product_code,
                amount=payment.total_amount,
                currency=payment.currency,
                telegram_payment_charge_id=provider_payment_id,
                tokens=included_tokens,
            )

            if expires_at is None:
                await message.answer(
                    "Платёж уже обработан или подписка активирована ранее. "
                    "Если Premium не появился, обратись в поддержку."
                )
                return

            if included_tokens > 0:
                await credit_referrer_commission(message.from_user.id, included_tokens)

            await message.answer(
                f"💎 Premium {product_code} успешно активирован!\n"
                f"🪙 Начислено: +{included_tokens} токенов\n"
                f"Подписка действует до: {expires_at:%d.%m.%Y %H:%M} UTC"
            )

        elif product_type == "tokens":
            plan = TOKEN_PLANS.get(product_code)

            if plan is None or payment.total_amount != plan["stars"]:
                await message.answer(
                    "Данные платежа не совпадают с заказом. Обратись в поддержку."
                )
                return

            credited = await complete_token_payment(
                provider="telegram_stars",
                provider_payment_id=provider_payment_id,
                telegram_id=message.from_user.id,
                product_code=product_code,
                amount=payment.total_amount,
                currency=payment.currency,
                tokens=plan["tokens"],
            )

            if not credited:
                await message.answer(
                    "Этот платёж уже обработан или токены уже начислены. "
                    "Если баланс неверный, обратись в поддержку."
                )
                return

            await credit_referrer_commission(message.from_user.id, int(plan["tokens"]))

            await message.answer(
                f"🪙 Успешно куплено {plan['tokens']} токенов!\n"
                "Токены начислены на твой баланс."
            )

        else:
            await message.answer(
                "Неизвестный товар. Обратись в поддержку."
            )

    except Exception:
        logging.exception("Ошибка обработки успешного платежа Telegram Stars")
        await message.answer(
            "Оплата получена, но заказ не удалось обработать автоматически. "
            "Обратись в поддержку — повторно платить не нужно."
        )


# ---------- АДМИН-КОМАНДА НАЧИСЛЕНИЯ ТОКЕНОВ ----------
ADMIN_ID = 7777063009


@user.message(Command("addtokens"))
async def add_tokens_command(message: Message):
    if message.from_user.id != ADMIN_ID:
        return

    parts = (message.text or "").split()

    if len(parts) != 2 or not parts[1].isdigit():
        await message.answer("Использование: /addtokens 100")
        return

    amount = int(parts[1])

    if amount <= 0:
        await message.answer("Количество должно быть больше нуля.")
        return

    success = await add_tokens(message.from_user.id, amount)

    if success:
        balance = await get_user_tokens(message.from_user.id)
        await message.answer(
            f"Начислено {amount} токенов.\n"
            f"Текущий баланс: {balance}"
        )
    else:
        await message.answer("Пользователь не найден в базе данных.")


# ---------- ОБЫЧНЫЙ ИИ-ЧАТ ПО УМОЛЧАНИЮ (РАБОТАЕТ ВСЕГДА ВНЕ СПЕЦ-РЕЖИМОВ) ----------
@user.message(F.photo)
async def default_chat_photo_handler(message: Message):
    """Анализ фото и ответы по картинкам прямо в обычном чате через Gemini."""
    model_key = get_user_text_model(message.from_user.id)
    price = TEXT_CHAT_MODELS[model_key]["price"]

    if not await has_premium(message.from_user.id):
        if not await spend_tokens(message.from_user.id, price):
            await message.answer(
                f"❌ Недостаточно токенов (требуется {price}). Пополните баланс в разделе «купить премиум🚀»."
            )
            return

    photo_file_id = message.photo[-1].file_id
    caption = (message.caption or "").strip()
    thinking_msg = await message.answer("💭 Думаю…")
    try:
        image_bytes = await download_telegram_photo_bytes(message, photo_file_id)
        answer = await generate_gemini_chat_response(
            user_id=message.from_user.id,
            user_text=caption or "Проанализируй это изображение и ответь по его содержимому.",
            image_bytes=image_bytes,
        )
        await send_long_message(message, answer)
    except Exception as exc:
        logging.exception("Ошибка ответа Gemini по фото в обычном чате")
        await message.answer(f"❌ Ошибка при обращении к ИИ: {exc}")
    finally:
        await delete_message_safely(message.bot, message.chat.id, thinking_msg.message_id)


@user.message(
    F.text,
    ~F.text.startswith("/"),
    ~F.text.in_(CHATGPT_MENU_TEXTS),
)
async def default_chat_text_handler(message: Message):
    """Обычный чат с ИИ по умолчанию (с сохранением истории переписки через Gemini)."""
    user_text = (message.text or "").strip()
    if not user_text:
        return

    model_key = get_user_text_model(message.from_user.id)
    price = TEXT_CHAT_MODELS[model_key]["price"]

    # С активной подпиской Premium общение в текстовом чате бесплатное!
    if not await has_premium(message.from_user.id):
        if not await spend_tokens(message.from_user.id, price):
            await message.answer(
                f"❌ Недостаточно токенов (требуется {price}). Пополните баланс в разделе «купить премиум🚀»."
            )
            return

    thinking_msg = await message.answer("💭 Думаю…")
    try:
        answer = await generate_gemini_chat_response(
            user_id=message.from_user.id,
            user_text=user_text,
        )
        await send_long_message(message, answer)
    except Exception as exc:
        logging.exception("Ошибка ответа Gemini в обычном чате")
        await message.answer(f"❌ Ошибка при обращении к ИИ: {exc}")
    finally:
        await delete_message_safely(message.bot, message.chat.id, thinking_msg.message_id)









       