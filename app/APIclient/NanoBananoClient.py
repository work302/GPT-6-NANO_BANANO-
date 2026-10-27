
from runblob import RunBlob
from ..config import RUNBLOB_API_KEY
from ..nano_banano import MODEL_RESOLUTIONS

MODEL_IDS = {
    "nano_banana": "standard",
    "nano_banana_pro": "pro",
    "nano_banana_2": "v2",
}

FORMAT_IDS = {
    "original": "auto",
    "1:1": "1:1",
    "16:9": "16:9",
    "9:16": "9:16",
}

RESOLUTION_IDS = {
    resolution: resolution
    for supported_resolutions in MODEL_RESOLUTIONS.values()
    for resolution in supported_resolutions
}


async def generate_nanobanano_image(
    prompt: str,
    model: str = "nano_banana",
    image_url: str | None = None,
    aspect_ratio: str = "original",
    quality: str = "standard",
    timeout: float = 600.0,
) -> str:
    if not RUNBLOB_API_KEY:
        raise RuntimeError("RUNBLOB_API_KEY не задан в .env")

    if model not in MODEL_IDS:
        raise ValueError(f"Неизвестная модель: {model}")

    if aspect_ratio not in FORMAT_IDS:
        raise ValueError(f"Неизвестный формат: {aspect_ratio}")

    if quality not in RESOLUTION_IDS:
        raise ValueError(f"Неизвестное качество: {quality}")

    if model == "nano_banana":
        quality = "standard"
    elif quality not in MODEL_RESOLUTIONS[model]:
        raise ValueError(
            f"Качество {quality!r} не поддерживается моделью {model}"
        )

    async with RunBlob(api_key=RUNBLOB_API_KEY) as client:
        result = await client.nanobanana.generate_and_wait(
            prompt=prompt,
            model=MODEL_IDS[model],
            quality=RESOLUTION_IDS[quality],
            aspect_ratio=FORMAT_IDS[aspect_ratio],
            images=[image_url] if image_url else None,
            timeout=timeout,
        )

        if not result.is_completed or not result.result_image_url:
            raise RuntimeError(
                f"RunBlob завершил генерацию без изображения: {result.message or result.status}"
            )
        return result.result_image_url
