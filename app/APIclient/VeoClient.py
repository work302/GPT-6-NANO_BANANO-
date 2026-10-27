import asyncio
import inspect
import logging
import time
import aiohttp
from runblob import RunBlob

from ..config import RUNBLOB_API_KEY

RUNBLOB_BASE_URL = "https://platform.runblob.io"

# Модели Veo в API RunBlob (POST /v1/veo/generate)
VEO_MODEL_IDS = {
    "veo-3-fast": "veo-3-fast",
    "veo-3-quality": "veo-3-quality",
    "veo_fast": "veo-3-fast",
    "veo_quality": "veo-3-quality",
}

VEO_DEFAULT_MODEL = "veo-3-fast"
VEO_DEFAULT_ASPECT_RATIO = "16:9"

VEO_MODEL_LABELS = {
    "veo-3-fast": "Veo 3.1 Fast",
    "veo-3-quality": "Veo 3.1 Quality",
}

VEO_MODEL_PRICES = {
    "veo-3-fast": 14,
    "veo-3-quality": 28,
}

VEO_ASPECT_RATIOS = ("16:9", "9:16")


def _normalize_veo_prompt(prompt: str) -> str:
    """
    По документации RunBlob (POST /v1/veo/generate) поле prompt
    должно содержать от 10 до 4000 символов.
    """
    cleaned = (prompt or "").strip()
    if len(cleaned) < 10:
        cleaned = f"{cleaned} — создать видео в высоком качестве"
    return cleaned[:4000]


async def _generate_veo_via_http(
    prompt: str,
    api_model: str,
    aspect_ratio: str = VEO_DEFAULT_ASPECT_RATIO,
    bytes_image: str | None = None,
    start_frame: str | None = None,
    end_frame: str | None = None,
    reference_images: list[str] | None = None,
    seed: int | None = None,
    watermark_text: str | None = None,
    timeout: float = 900.0,
) -> str:
    """Отправляет задачу в POST /v1/veo/generate и опрашивает GET /v1/veo/generations/{id}."""
    headers = {
        "Authorization": f"Bearer {RUNBLOB_API_KEY}",
        "Content-Type": "application/json",
    }

    payload: dict = {
        "prompt": _normalize_veo_prompt(prompt),
        "model": api_model,
        "aspect_ratio": aspect_ratio,
    }

    # В одном запросе Veo3 допускается только один режим:
    # 1) Text-to-Video (без картинок)
    # 2) Image-to-Video (bytes_image)
    # 3) Frame-to-Frame (start_frame + end_frame, только veo-3-fast)
    # 4) Reference Images (reference_images 1-3 фото, только veo-3-fast)
    if start_frame and end_frame:
        payload["model"] = "veo-3-fast"
        payload["start_frame"] = start_frame
        payload["end_frame"] = end_frame
    elif reference_images:
        payload["model"] = "veo-3-fast"
        payload["reference_images"] = list(reference_images[:3])
    elif bytes_image:
        payload["bytes_image"] = bytes_image

    if seed is not None:
        payload["seed"] = seed
    if watermark_text:
        payload["watermark_text"] = watermark_text[:14]

    client_timeout = aiohttp.ClientTimeout(total=60, connect=15, sock_read=45)
    async with aiohttp.ClientSession(timeout=client_timeout) as session:
        async with session.post(
            f"{RUNBLOB_BASE_URL}/v1/veo/generate",
            headers=headers,
            json=payload,
        ) as response:
            if response.status >= 400:
                error_body = await response.text()
                raise RuntimeError(
                    f"Ошибка создания задачи Veo (HTTP {response.status}): {error_body}"
                )
            create_data = await response.json()

        generation_id = create_data.get("generation_id") or create_data.get("task_uuid")
        if not generation_id:
            raise RuntimeError(f"RunBlob Veo не вернул generation_id: {create_data}")

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            await asyncio.sleep(6)
            async with session.get(
                f"{RUNBLOB_BASE_URL}/v1/veo/generations/{generation_id}",
                headers={"Authorization": f"Bearer {RUNBLOB_API_KEY}"},
            ) as status_response:
                if status_response.status >= 500:
                    continue
                if status_response.status >= 400:
                    error_body = await status_response.text()
                    raise RuntimeError(
                        f"Ошибка проверки статуса Veo (HTTP {status_response.status}): {error_body}"
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
                        f"RunBlob Veo завершил генерацию без ссылки на видео: {status_data}"
                    )
                return video_url

            if status in {"failed", "error", "cancelled"}:
                error_msg = (
                    status_data.get("message")
                    or status_data.get("error")
                    or "Генерация отклонена провайдером"
                )
                raise RuntimeError(f"RunBlob Veo завершился с ошибкой: {error_msg}")

    raise TimeoutError("Превышено время ожидания генерации видео Veo")


async def generate_veo_video(
    prompt: str,
    model: str = VEO_DEFAULT_MODEL,
    aspect_ratio: str = VEO_DEFAULT_ASPECT_RATIO,
    bytes_image: str | None = None,
    start_frame: str | None = None,
    end_frame: str | None = None,
    reference_images: list[str] | None = None,
    images: list[str] | None = None,
    seed: int | None = None,
    watermark_text: str | None = None,
    timeout: float = 900.0,
) -> str:
    """
    Универсальный клиент генерации видео Veo 3.1 через провайдера RunBlob.
    Поддерживает все режимы провайдера:
    - Text-to-Video (только текстовый запрос)
    - Image-to-Video (1 изображение в bytes_image или images=[img])
    - Frame-to-Frame (2 изображения: start_frame + end_frame или images=[img1, img2])
    - Reference Images (до 3 изображений: reference_images или images=[img1, img2, img3])
    """
    if not RUNBLOB_API_KEY:
        raise RuntimeError("RUNBLOB_API_KEY не задан в .env")

    if model not in VEO_MODEL_IDS:
        raise ValueError(f"Неизвестная модель Veo: {model}")

    if aspect_ratio not in VEO_ASPECT_RATIOS:
        raise ValueError(f"Недопустимый формат Veo: {aspect_ratio} (допустимо: 16:9 или 9:16)")

    # Если передан список images, автоматически распределяем по режимам провайдера
    if images:
        if len(images) == 1 and not bytes_image and not reference_images:
            bytes_image = images[0]
        elif len(images) == 2 and not (start_frame and end_frame) and not reference_images:
            start_frame, end_frame = images[0], images[1]
        elif len(images) >= 3 and not reference_images:
            reference_images = list(images[:3])

    if (start_frame and not end_frame) or (end_frame and not start_frame):
        raise ValueError("Для режима Frame-to-Frame необходимы оба кадра: start_frame и end_frame")

    api_model = VEO_MODEL_IDS[model]
    if (start_frame and end_frame) or reference_images:
        api_model = "veo-3-fast"

    normalized_prompt = _normalize_veo_prompt(prompt)

    # Сначала пробуем метод SDK RunBlob, если он присутствует в установленной версии пакета
    try:
        async with RunBlob(api_key=RUNBLOB_API_KEY) as client:
            veo_service = getattr(client, "veo", None) or getattr(client, "veo3", None)
            generate_fn = getattr(veo_service, "generate_and_wait", None) if veo_service else None
            if callable(generate_fn):
                sig = inspect.signature(generate_fn)
                candidate_kwargs = {
                    "prompt": normalized_prompt,
                    "model": api_model,
                    "aspect_ratio": aspect_ratio,
                    "bytes_image": bytes_image,
                    "start_frame": start_frame,
                    "end_frame": end_frame,
                    "reference_images": reference_images,
                    "seed": seed,
                    "watermark_text": watermark_text,
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
            "Переключаемся на прямой REST-запрос RunBlob для Veo",
            exc_info=True,
        )

    return await _generate_veo_via_http(
        prompt=normalized_prompt,
        api_model=api_model,
        aspect_ratio=aspect_ratio,
        bytes_image=bytes_image,
        start_frame=start_frame,
        end_frame=end_frame,
        reference_images=reference_images,
        seed=seed,
        watermark_text=watermark_text,
        timeout=timeout,
    )