from urllib.parse import quote
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.utils.keyboard import ReplyKeyboardBuilder

try:
    from .config import (
        REQUIRED_CHANNEL_ID,
        REQUIRED_CHANNEL_URL,
        SUPPORT_USERNAME,
        TASK_CHANNEL_1_URL,
        TASK_CHANNEL_2_URL,
        TASK_CHANNEL_3_URL,
    )
except Exception:
    SUPPORT_USERNAME = "@Rendix_dev"
    REQUIRED_CHANNEL_URL = "https://t.me/GPT6_NANO_BANANO"
    REQUIRED_CHANNEL_ID = "@GPT6_NANO_BANANO"
    TASK_CHANNEL_1_URL = "https://t.me/GPT6_NANO_BANANO"
    TASK_CHANNEL_2_URL = "https://t.me/GPT6_NANO_BANANO"
    TASK_CHANNEL_3_URL = "https://t.me/GPT6_NANO_BANANO"

from .APIclient.KlingAiClient import (
    KLING_DURATIONS,
    KLING_MODEL_BUTTON_LABELS,
    KLING_MODEL_LABELS,
    get_kling_price,
)
from .APIclient.VeoClient import (
    VEO_ASPECT_RATIOS,
    VEO_MODEL_LABELS,
    VEO_MODEL_PRICES,
)
from .nano_banano import (
    FORMAT_LABELS,
    MODEL_NAMES,
    MODEL_PRICES,
    MODEL_RESOLUTIONS,
    RESOLUTION_LABELS,
)
from .prompts import ACADEMIC_WORK_TYPES, TEXT_CHAT_MODELS


# ── НАСТРОЙКИ ССЫЛОК ДЛЯ ПРОФИЛЯ И ЗАДАНИЙ (МОЖНО МЕНЯТЬ НА СВОИ) ──
# Юзернейм техподдержки (можно указать с @ или без @):
SUPPORT_CONTACT_URL = f"https://t.me/{SUPPORT_USERNAME.lstrip('@')}"

# 3 Telegram-канала для раздела «Получить бесплатные токены -> Выполнить задания»:
TASK_CHANNELS = [
    {"title": "📢 Подписаться на канал №1", "url": TASK_CHANNEL_1_URL},
    {"title": "📢 Подписаться на канал №2", "url": TASK_CHANNEL_2_URL},
    {"title": "📢 Подписаться на канал №3", "url": TASK_CHANNEL_3_URL},
]


# Главное меню намеренно остаётся ReplyKeyboardMarkup (обычные кнопки под полем ввода).
_builder = ReplyKeyboardBuilder()
_builder.button(text="купить премиум🚀")
_builder.button(text="Нано банано🍌")
_builder.button(text="мой профиль👤")
_builder.button(text="генерация текста")
_builder.button(text="чатГПТ картинки🔥")
_builder.button(text="все нейросети🧠")
_builder.adjust(3, 3)
menu_kb = _builder.as_markup(resize_keyboard=True)


def build_profile_options_kb(bot_username: str | None = None) -> InlineKeyboardMarkup:
    """
    Клавиатура раздела «Мой профиль» с эмодзи 🚀 🤝 🦺 ➕:
    - 🚀 Купить премиум -> открывает покупку премиум-пакета
    - 🤝 Получить бесплатные токены -> открывает «Пригласить друга» и «Выполнить задания»
    - 🦺 Техническая поддержка -> переносит на аккаунт @Rendix_dev (или другой из SUPPORT_USERNAME)
    - ➕ Пригласить бота в группу -> открывает выбор любой группы/чата в Telegram
    """
    if bot_username:
        invite_group_btn = InlineKeyboardButton(
            text="➕ Пригласить бота в группу",
            url=f"https://t.me/{bot_username.lstrip('@')}?startgroup=true",
        )
    else:
        invite_group_btn = InlineKeyboardButton(
            text="➕ Пригласить бота в группу",
            callback_data="referal_chat",
        )

    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🚀 Купить премиум", callback_data="pay_premium")],
        [InlineKeyboardButton(text="🤝 Получить бесплатные токены", callback_data="free_token")],
        [InlineKeyboardButton(text="🦺 Техническая поддержка", url=SUPPORT_CONTACT_URL)],
        [invite_group_btn],
    ])


# Базовая клавиатура профиля для совместимости
profile_options_kb = build_profile_options_kb()


# Меню при нажатии «🤝 Получить бесплатные токены»
free_tokens_menu_kb = InlineKeyboardMarkup(inline_keyboard=[
    [InlineKeyboardButton(text="👥 Пригласить друга", callback_data="free_invite_friend")],
    [InlineKeyboardButton(text="📋 Выполнить задания", callback_data="free_tasks")],
    [InlineKeyboardButton(text="⬅️ Назад", callback_data="back_to_profile")],
])


def build_referral_kb(ref_link: str) -> InlineKeyboardMarkup:
    """Клавиатура под реферальным сообщением."""
    share_url = (
        f"https://t.me/share/url?url={quote(ref_link)}"
        f"&text={quote('Заходи в крутого AI-бота для генерации фото, видео и текста!')}"
    )
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📤 Поделиться ссылкой", url=share_url)],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="free_token")],
    ])


def build_tasks_channels_kb() -> InlineKeyboardMarkup:
    """Клавиатура с 3 каналами в разделе «Выполнить задания»."""
    rows = [
        [InlineKeyboardButton(text=ch["title"], url=ch["url"])]
        for ch in TASK_CHANNELS
    ]
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="free_token")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


# Клавиатура обязательной подписки на канал бота https://t.me/GPT6_NANO_BANANO
channel_sub_check_kb = InlineKeyboardMarkup(inline_keyboard=[
    [InlineKeyboardButton(text="📢 Подписаться на канал", url=REQUIRED_CHANNEL_URL)],
    [InlineKeyboardButton(text="✅ Я подписался", callback_data="check_channel_sub")],
])


# У каждого генератора свой callback «Назад»: не пытаемся угадывать экран по FSM-состоянию.
nano_otmena_kb = InlineKeyboardMarkup(inline_keyboard=[
    [InlineKeyboardButton(text="⬅️ Назад", callback_data="back_from_nano")],
])

chatgpt_otmena_kb = InlineKeyboardMarkup(inline_keyboard=[
    [InlineKeyboardButton(text="⬅️ Назад", callback_data="back_from_chatgpt")],
])

kling_otmena_kb = InlineKeyboardMarkup(inline_keyboard=[
    [InlineKeyboardButton(text="⬅️ Назад", callback_data="back_from_kling")],
])

kling_prompt_kb = InlineKeyboardMarkup(inline_keyboard=[
    [InlineKeyboardButton(text="⬅️ Назад", callback_data="back_to_kling_settings")],
])

veo_otmena_kb = InlineKeyboardMarkup(inline_keyboard=[
    [InlineKeyboardButton(text="⬅️ Назад", callback_data="back_from_veo")],
])

# Совместимость со старым кодом, где импортировалась общая клавиатура.
otmena_kb = nano_otmena_kb


# Верхний уровень раздела «Все нейросети» (как на скриншоте 1).
ai_tools_kb = InlineKeyboardMarkup(inline_keyboard=[
    [InlineKeyboardButton(text="🤖 Генерация текста (общение с ботом)", callback_data="text_generation")],
    [InlineKeyboardButton(text="🌄 Генерация картинок и работа с ними", callback_data="image_tools")],
    [InlineKeyboardButton(text="📹 Генерация видео", callback_data="video_generation")],
    [InlineKeyboardButton(text="🎓 Сервисы для учебы и работы", callback_data="study_services")],
    [InlineKeyboardButton(text="⬅️ Назад", callback_data="back_to_main_menu")],
])


# Меню «Генерация картинок и работа с ними».
image_tools_kb = InlineKeyboardMarkup(inline_keyboard=[
    [InlineKeyboardButton(text="🎨 Генерация картинок", callback_data="image_generation")],
    [InlineKeyboardButton(text="🔍 Распознать содержимое картинки", callback_data="image_recognition")],
    [InlineKeyboardButton(text="⬅️ Назад", callback_data="back_to_ai_tools")],
])


# Меню выбора видео-генераторов («Генерация видео»).
video_generators_kb = InlineKeyboardMarkup(inline_keyboard=[
    [InlineKeyboardButton(text="🎬 Kling", callback_data="open_kling")],
    [InlineKeyboardButton(text="🕺 Veo 3.1", callback_data="open_veo")],
    [InlineKeyboardButton(text="⬅️ Назад", callback_data="back_to_ai_tools")],
])


# Дополнительное меню сервисов.
services_kb = ai_tools_kb


# Меню выбора двух генераторов картинок (кнопки выглядят как в главном меню).
image_generators_kb = InlineKeyboardMarkup(inline_keyboard=[
    [InlineKeyboardButton(text="Нано банано🍌", callback_data="open_nano_banana")],
    [InlineKeyboardButton(text="чатГПТ картинки🔥", callback_data="open_chatgpt_images")],
    [InlineKeyboardButton(text="⬅️ Назад", callback_data="back_to_image_tools")],
])


# Клавиатура, которая открывается при возврате назад из генераторов картинок.
gpt_cartinki_kb = image_generators_kb


recognition_back_kb = InlineKeyboardMarkup(inline_keyboard=[
    [InlineKeyboardButton(text="⬅️ Назад", callback_data="back_to_image_tools")],
])


# Меню «Сервисы для учёбы и работы» (как на скриншоте 2).
services_menu_kb = InlineKeyboardMarkup(inline_keyboard=[
    [InlineKeyboardButton(text="📚 Диплом, реферат, эссе", callback_data="service_diploma")],
    [InlineKeyboardButton(text="📸 Решение задач по фото", callback_data="service_photo")],
    [InlineKeyboardButton(text="🚀 Краткое содержание видео и статей", callback_data="service_summary")],
    [InlineKeyboardButton(text="✏️ Уникализация текста", callback_data="service_rewrite")],
    [InlineKeyboardButton(text="🗂 Генерация сценариев", callback_data="service_script")],
    [InlineKeyboardButton(text="← Назад", callback_data="back_to_ai_tools")],
])


# Меню выбора типа студенческой работы в разделе «Диплом, реферат, эссе» (как на скриншоте 3).
diploma_types_kb = InlineKeyboardMarkup(inline_keyboard=[
    [InlineKeyboardButton(text="🧠 Эссе", callback_data="academic_type_essay")],
    [InlineKeyboardButton(text="👨‍🎓 Курсовая работа", callback_data="academic_type_coursework")],
    [InlineKeyboardButton(text="📖 Дипломная работа", callback_data="academic_type_diploma")],
    [InlineKeyboardButton(text="📕 Реферат", callback_data="academic_type_referat")],
    [InlineKeyboardButton(text="← Назад", callback_data="study_services")],
])

# Кнопка «Назад» внутри конкретного учебного сервиса
study_service_back_kb = InlineKeyboardMarkup(inline_keyboard=[
    [InlineKeyboardButton(text="← Назад", callback_data="study_services")],
])

academic_work_back_kb = InlineKeyboardMarkup(inline_keyboard=[
    [InlineKeyboardButton(text="← Назад", callback_data="service_diploma")],
])


def build_text_models_keyboard(
    selected_model: str,
    remember_dialog: bool,
) -> InlineKeyboardMarkup:
    """Клавиатура раздела «Генерация текста (общение с ботом)» (как на скриншоте 4)."""
    def _btn(model_key: str) -> InlineKeyboardButton:
        title = TEXT_CHAT_MODELS[model_key]["name"]
        label = f"▶️ {title}" if model_key == selected_model else title
        return InlineKeyboardButton(text=label, callback_data=f"chat_set_model_{model_key}")

    memory_label = (
        "Выключить запоминание диалога"
        if remember_dialog
        else "Включить запоминание диалога"
    )

    rows = [
        [_btn("gpt_5_luna")],
        [_btn("gpt_5_sol"), _btn("gpt_5_terra")],
        [_btn("claude_opus_4_8"), _btn("claude_fable")],
        [_btn("grok_4_1")],
        [InlineKeyboardButton(text=memory_label, callback_data="chat_toggle_memory")],
        [InlineKeyboardButton(text="Мои диалоги", callback_data="chat_my_dialogs")],
        [InlineKeyboardButton(text="К списку нейросетей", callback_data="back_to_ai_tools")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def build_user_dialogs_keyboard(session: dict) -> InlineKeyboardMarkup:
    """Клавиатура раздела «Мои диалоги» для переключения и создания новых диалогов."""
    active_id = session.get("active_dialog_id", 1)
    dialogs: dict = session.get("dialogs", {})
    rows = []
    for dialog_id, dialog_data in sorted(dialogs.items()):
        prefix = "▶️ " if dialog_id == active_id else "💬 "
        msg_count = len(dialog_data.get("messages", [])) // 2
        title = dialog_data.get("title") or f"Диалог #{dialog_id}"
        rows.append([
            InlineKeyboardButton(
                text=f"{prefix}{title} ({msg_count} сообщ.)",
                callback_data=f"chat_switch_dialog_{dialog_id}",
            )
        ])

    rows.extend([
        [InlineKeyboardButton(text="➕ Начать новый диалог", callback_data="chat_new_dialog")],
        [InlineKeyboardButton(text="🗑 Очистить текущий диалог", callback_data="chat_clear_dialog")],
        [InlineKeyboardButton(text="← Назад", callback_data="text_generation")],
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


subscription_menu_kb = InlineKeyboardMarkup(inline_keyboard=[
    [InlineKeyboardButton(text="💎 СТАРТОВЫЙ ЗА 599 ₽", callback_data="sub_start")],
    [InlineKeyboardButton(text="🔥 ОПТИМАЛЬНЫЙ ЗА 999 ₽ (-20%)", callback_data="sub_opt")],
    [InlineKeyboardButton(text="💎 ПРОДВИНУТЫЙ ЗА 1439 ₽ (-35%)", callback_data="sub_adv")],
    [InlineKeyboardButton(text="💎 ВЫГОДНЫЙ ЗА 2390 ₽ (-40%)", callback_data="sub_max")],
    [InlineKeyboardButton(text="📦 Докупить токены", callback_data="tokens_menu")],
])


tokens_menu_kb = InlineKeyboardMarkup(inline_keyboard=[
    [InlineKeyboardButton(text="💎 100 токенов ЗА 359 ₽", callback_data="buy_tok_100")],
    [InlineKeyboardButton(text="💎 350 токенов ЗА 863 ₽ (-35%)", callback_data="buy_tok_350")],
    [InlineKeyboardButton(text="🔥 1000 токенов ЗА 2199 ₽ (-40%)", callback_data="buy_tok_1000")],
    [InlineKeyboardButton(text="⬅️ Назад", callback_data="back_to_subs")],
])


def _payment_keyboard(price_label: str, price_code: str, back_callback: str) -> InlineKeyboardMarkup:
    """Резервная клавиатура оплаты подписки."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(
                text=f"🇷🇺 Оплатить картой РФ — {price_label} ₽",
                callback_data=f"pay_rf_{price_code}",
            )
        ],
        [
            InlineKeyboardButton(
                text=f"🇷🇺 Оплатить по СБП — {price_label} ₽",
                callback_data=f"pay_sbp_{price_code}",
            )
        ],
        [
            InlineKeyboardButton(
                text="🌍 Международная карта",
                callback_data=f"pay_int_{price_code}",
            )
        ],
        [
            InlineKeyboardButton(
                text="⭐ Telegram Stars",
                callback_data=f"pay_stars_{price_code}",
            )
        ],
        [
            InlineKeyboardButton(
                text="⬅️ Назад",
                callback_data=back_callback,
            )
        ],
    ])


pay_sub_start_kb = _payment_keyboard("599,00", "599", "back_to_subs")
pay_sub_opt_kb = _payment_keyboard("999,00", "999", "back_to_subs")
pay_sub_adv_kb = _payment_keyboard("1439,00", "1439", "back_to_subs")
pay_sub_max_kb = _payment_keyboard("2390,00", "2390", "back_to_subs")


def _token_payment_keyboard(
    price_label: str,
    price_code: str,
    stars_callback: str,
) -> InlineKeyboardMarkup:
    """Резервная клавиатура оплаты пакетов токенов."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(
                text="⭐ Купить за Stars",
                callback_data=stars_callback,
            )
        ],
        [
            InlineKeyboardButton(
                text=f"🇷🇺 Оплатить картой РФ — {price_label} ₽",
                callback_data=f"pay_tok_rf_{price_code}",
            )
        ],
        [
            InlineKeyboardButton(
                text=f"🇷🇺 Оплатить по СБП — {price_label} ₽",
                callback_data=f"pay_tok_sbp_{price_code}",
            )
        ],
        [
            InlineKeyboardButton(
                text="🌍 Международная карта",
                callback_data=f"pay_tok_int_{price_code}",
            )
        ],
        [
            InlineKeyboardButton(
                text="⬅️ Назад",
                callback_data="tokens_menu",
            )
        ],
    ])


pay_tok_100_kb = _token_payment_keyboard("359,00", "359", "pay_tok_stars_100")
pay_tok_350_kb = _token_payment_keyboard("863,00", "863", "pay_tok_stars_350")
pay_tok_1000_kb = _token_payment_keyboard("2199,00", "2199", "pay_tok_stars_1000")


def build_nano_settings_keyboard(
    model: str,
    image_format: str,
    resolution: str,
    expanded: str | None,
) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(
        text=(
            f"Модель: {MODEL_NAMES[model]} "
            f"💎 {MODEL_PRICES[model]} токена "
            f"{'🔼' if expanded == 'model' else '🔽'}"
        ),
        callback_data="toggle_model_list",
    )]]

    if expanded == "model":
        for model_id, model_name in MODEL_NAMES.items():
            selected = model_id == model
            rows.append([InlineKeyboardButton(
                text=f"{model_name} 💎 {MODEL_PRICES[model_id]} токена{' ✅' if selected else ''}",
                callback_data=f"set_model_{model_id}",
            )])

    rows.append([InlineKeyboardButton(
        text=(
            f"Формат: {FORMAT_LABELS[image_format]} "
            f"{'🔼' if expanded == 'format' else '🔽'}"
        ),
        callback_data="toggle_format_list",
    )])

    if expanded == "format":
        format_buttons = []
        for format_id, label in FORMAT_LABELS.items():
            format_buttons.append(InlineKeyboardButton(
                text=f"{label}{' ✅' if format_id == image_format else ''}",
                callback_data=f"set_format_{format_id.replace(':', '_')}",
            ))
        if format_buttons:
            rows.append(format_buttons[:2])
        if len(format_buttons) > 2:
            rows.append(format_buttons[2:])

    if len(MODEL_RESOLUTIONS[model]) > 1:
        rows.append([InlineKeyboardButton(
            text=(
                f"Разрешение: {RESOLUTION_LABELS[resolution]} "
                f"{'🔼' if expanded == 'resolution' else '🔽'}"
            ),
            callback_data="toggle_res_list",
        )])
        if expanded == "resolution":
            rows.append([
                InlineKeyboardButton(
                    text=f"{RESOLUTION_LABELS[option]}{' ✅' if option == resolution else ''}",
                    callback_data=f"set_res_{option}",
                )
                for option in MODEL_RESOLUTIONS[model]
            ])

    rows.extend([
        [InlineKeyboardButton(text="Подтвердить ✅", callback_data="confirm_generation")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="back_from_nano")],
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def build_kling_settings_keyboard(
    model: str,
    duration: str,
    expanded: str | None = None,
) -> InlineKeyboardMarkup:
    """Инлайн-клавиатура выбора модели (Kling 2.5 Turbo | 2.1 Мастер) и длительности (5 | 10 сек)."""
    price = get_kling_price(model, duration)
    rows = [[InlineKeyboardButton(
        text=(
            f"Модель: {KLING_MODEL_LABELS[model]} "
            f"(💰 {price} ток.) "
            f"{'🔼' if expanded == 'model' else '🔽'}"
        ),
        callback_data="kling_toggle_model",
    )]]

    if expanded == "model":
        for model_id, btn_label in KLING_MODEL_BUTTON_LABELS.items():
            selected = model_id == model
            model_price = get_kling_price(model_id, duration)
            rows.append([InlineKeyboardButton(
                text=f"{btn_label} — {model_price} токенов{' ✅' if selected else ''}",
                callback_data=f"kling_set_model_{model_id}",
            )])

    rows.append([InlineKeyboardButton(
        text=(
            f"⏱️ Длительность: {duration} сек "
            f"{'🔼' if expanded == 'duration' else '🔽'}"
        ),
        callback_data="kling_toggle_duration",
    )])

    if expanded == "duration":
        rows.append([
            InlineKeyboardButton(
                text=f"{dur} сек{' ✅' if str(duration) == dur else ''}",
                callback_data=f"kling_set_dur_{dur}",
            )
            for dur in KLING_DURATIONS
        ])

    rows.extend([
        [InlineKeyboardButton(text="Подтвердить ✅", callback_data="kling_confirm_settings")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="back_from_kling")],
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def build_veo_keyboard(
    model: str = "veo-3-fast",
    aspect_ratio: str = "16:9",
    expanded: str | None = None,
) -> InlineKeyboardMarkup:
    """Инлайн-клавиатура для раздела Veo 3.1 (выбор модели, формата и кнопка Назад)."""
    rows = [[InlineKeyboardButton(
        text=(
            f"Модель: {VEO_MODEL_LABELS.get(model, 'Veo 3.1 Fast')} "
            f"💎 {VEO_MODEL_PRICES.get(model, 14)} токенов "
            f"{'🔼' if expanded == 'model' else '🔽'}"
        ),
        callback_data="veo_toggle_model",
    )]]

    if expanded == "model":
        for model_id, label in VEO_MODEL_LABELS.items():
            selected = model_id == model
            rows.append([InlineKeyboardButton(
                text=f"{label} 💎 {VEO_MODEL_PRICES[model_id]} токенов{' ✅' if selected else ''}",
                callback_data=f"veo_set_model_{model_id}",
            )])

    rows.append([InlineKeyboardButton(
        text=(
            f"Формат: {aspect_ratio} "
            f"{'🔼' if expanded == 'aspect' else '🔽'}"
        ),
        callback_data="veo_toggle_aspect",
    )])

    if expanded == "aspect":
        rows.append([
            InlineKeyboardButton(
                text=f"{ratio}{' ✅' if ratio == aspect_ratio else ''}",
                callback_data=f"veo_set_aspect_{ratio.replace(':', '_')}",
            )
            for ratio in VEO_ASPECT_RATIOS
        ])

    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="back_from_veo")])
    return InlineKeyboardMarkup(inline_keyboard=rows)