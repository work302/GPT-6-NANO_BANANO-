import asyncio
import inspect
import logging
import time
import aiohttp
from runblob import RunBlob

from ..config import RUNBLOB_API_KEY

RUNBLOB_BASE_URL = "https://platform.runblob.io"

# Поддерживаемые модели Kling для интерфейса и API RunBlob
KLING_MODEL_IDS = {
    "kling_2.5_turbo": "kling_2.5_turbo",
    "kling_2.5_turbo_pro": "kling_2.5_turbo_pro",
    "2.5_turbo": "kling_2.5_turbo",
    "2.5_turbo_pro": "kling_2.5_turbo_pro",
    "kling_2.1_master": "kling_2.1_master",
    "2.1_master": "kling_2.1_master",
    "kling_2.1": "kling_2.1",
    "kling_2.1_pro": "kling_2.1_pro",
}

KLING_DEFAULT_MODEL = "kling_2.5_turbo"
KLING_DEFAULT_DURATION = "5"

# Названия моделей в сообщении и на кнопках
KLING_MODEL_LABELS = {
    "kling_2.5_turbo": "2.5 Turbo Pro",
    "kling_2.1_master": "2.1 Мастер",
}

KLING_MODEL_BUTTON_LABELS = {
    "kling_2.5_turbo": "⚡ Kling 2.5 Turbo",
    "kling_2.1_master": "👑 Kling 2.1 Мастер",
}

KLING_DURATIONS = ("5", "10")

# Стоимость в токенах в зависимости от модели и длительности (5 сек / 10 сек)
KLING_MODEL_PRICES = {
    "kling_2.5_turbo": {
        "5": 15,
        "10": 30,
    },
    "kling_2.1_master": {
        "5": 30,
        "10": 60,
    },
}

KLING_ASPECT_RATIOS = ("16:9", "9:16", "1:1")


def get_kling_price(model: str, duration: str | int) -> int:
    duration_key = str(duration)
    model_prices = KLING_MODEL_PRICES.get(model, KLING_MODEL_PRICES[KLING_DEFAULT_MODEL])
    return model_prices.get(duration_key, 15)


async def _generate_kling_via_http(
    prompt: str,
    api_model: str,
    duration: str,
    image_base64: str | None = None,
    image_url: str | None = None,
    aspect_ratio: str = "16:9",
    negative_prompt: str | None = None,
    cfg_scale: float | None = None,
    seed: int | None = None,
    timeout: float = 900.0,
) -> str:
    """Отправляет задачу в POST /v1/kling/generate и опрашивает GET /v1/kling/generations/{id}."""
    headers = {
        "Authorization": f"Bearer {RUNBLOB_API_KEY}",
        "Content-Type": "application/json",
    }

    payload: dict = {
        "model": api_model,
        "prompt": prompt,
        "duration": str(duration),
    }

    # По документации RunBlob: нельзя одновременно передавать image_base64 и image_url,
    # а в режиме Image-to-Video формат (aspect_ratio) определяется автоматически по фото.
    if image_base64:
        payload["image_base64"] = image_base64
    elif image_url:
        payload["image_url"] = image_url
    else:
        payload["aspect_ratio"] = aspect_ratio

    if negative_prompt and api_model == "kling_2.1_master":
        payload["negative_prompt"] = negative_prompt
    if cfg_scale is not None:
        payload["cfg_scale"] = cfg_scale
    if seed is not None:
        payload["seed"] = seed

    client_timeout = aiohttp.ClientTimeout(total=60, connect=15, sock_read=45)
    async with aiohttp.ClientSession(timeout=client_timeout) as session:
        async with session.post(
            f"{RUNBLOB_BASE_URL}/v1/kling/generate",
            headers=headers,
            json=payload,
        ) as response:
            if response.status >= 400:
                error_body = await response.text()
                raise RuntimeError(
                    f"Ошибка создания задачи Kling (HTTP {response.status}): {error_body}"
                )
            create_data = await response.json()

        generation_id = create_data.get("generation_id") or create_data.get("task_uuid")
        if not generation_id:
            raise RuntimeError(f"RunBlob Kling не вернул generation_id: {create_data}")

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            await asyncio.sleep(6)
            async with session.get(
                f"{RUNBLOB_BASE_URL}/v1/kling/generations/{generation_id}",
                headers={"Authorization": f"Bearer {RUNBLOB_API_KEY}"},
            ) as status_response:
                if status_response.status >= 500:
                    continue
                if status_response.status >= 400:
                    error_body = await status_response.text()
                    raise RuntimeError(
                        f"Ошибка проверки статуса Kling (HTTP {status_response.status}): {error_body}"
                    )
                status_data = await status_response.json()

            status = (status_data.get("status") or "").lower()
            if status == "completed":
                video_url = (
                    status_data.get("video_url")
                    or status_data.get("result_video_url")
                    or (status_data.get("video_urls") or [None])[0]
                )
                if not video_url:
                    raise RuntimeError(
                        f"RunBlob Kling завершил генерацию без ссылки на видео: {status_data}"
                    )
                return video_url

            if status in {"failed", "error", "cancelled"}:
                error_msg = (
                    status_data.get("message")
                    or status_data.get("error")
                    or "Генерация отклонена провайдером"
                )
                raise RuntimeError(f"RunBlob Kling завершился с ошибкой: {error_msg}")

    raise TimeoutError("Превышено время ожидания генерации видео Kling")


async def generate_kling_video(
    prompt: str,
    model: str = KLING_DEFAULT_MODEL,
    duration: str | int = KLING_DEFAULT_DURATION,
    image_base64: str | None = None,
    image_url: str | None = None,
    aspect_ratio: str = "16:9",
    negative_prompt: str | None = None,
    cfg_scale: float | None = None,
    seed: int | None = None,
    timeout: float = 900.0,
) -> str:
    """
    Универсальный обработчик генерации видео Kling через RunBlob
    для моделей Kling 2.5 Turbo и Kling 2.1 Мастер.
    Поддерживает как Image-to-Video (по фото и описанию движения), так и Text-to-Video.
    """
    if not RUNBLOB_API_KEY:
        raise RuntimeError("RUNBLOB_API_KEY не задан в .env")

    if model not in KLING_MODEL_IDS:
        raise ValueError(f"Неизвестная модель Kling: {model}")

    duration_str = str(duration)
    if duration_str not in KLING_DURATIONS:
        raise ValueError(f"Недопустимая длительность для Kling: {duration_str} (допустимо: 5 или 10)")

    if aspect_ratio not in KLING_ASPECT_RATIOS:
        raise ValueError(f"Неизвестный формат кадра: {aspect_ratio}")

    if image_base64 and image_url:
        raise ValueError("Нельзя одновременно передавать image_base64 и image_url")

    api_model = KLING_MODEL_IDS[model]

    # Сначала пробуем метод SDK RunBlob, если он реализован в установленной версии пакета
    try:
        async with RunBlob(api_key=RUNBLOB_API_KEY) as client:
            kling_service = getattr(client, "kling", None)
            generate_fn = getattr(kling_service, "generate_and_wait", None) if kling_service else None
            if callable(generate_fn):
                sig = inspect.signature(generate_fn)
                candidate_kwargs = {
                    "prompt": prompt,
                    "model": api_model,
                    "duration": duration_str,
                    "aspect_ratio": None if (image_base64 or image_url) else aspect_ratio,
                    "image_base64": image_base64,
                    "image_url": image_url,
                    "image": image_base64 or image_url,
                    "negative_prompt": negative_prompt,
                    "cfg_scale": cfg_scale,
                    "seed": seed,
                    "timeout": timeout,
                }
                accepts_var_kw = any(
                    p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()
                )
                call_kwargs = {
                    k: v
                    for k, v in candidate_kwargs.items()
                    if v is not None and (accepts_var_kw or k in sig.parameters)
                }
                result = await generate_fn(**call_kwargs)
                video_url = (
                    getattr(result, "video_url", None)
                    or getattr(result, "result_video_url", None)
                    or getattr(result, "result_url", None)
                )
                if getattr(result, "is_completed", True) and video_url:
                    return video_url
    except Exception:
        logging.debug(
            "Переключаемся на прямой REST-запрос RunBlob для Kling",
            exc_info=True,
        )

    return await _generate_kling_via_http(
        prompt=prompt,
        api_model=api_model,
        duration=duration_str,
        image_base64=image_base64,
        image_url=image_url,
        aspect_ratio=aspect_ratio,
        negative_prompt=negative_prompt,
        cfg_scale=cfg_scale,
        seed=seed,
        timeout=timeout,
    )