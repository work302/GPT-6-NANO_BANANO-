from runblob import RunBlob

from ..config import RUNBLOB_API_KEY


async def generate_chatgpt_image(
    prompt: str,
    images: list[str] | None = None,
    timeout: float = 900.0,
) -> str:
    if not RUNBLOB_API_KEY:
        raise RuntimeError("RUNBLOB_API_KEY не задан в конфигурации")

    async with RunBlob(api_key=RUNBLOB_API_KEY) as client:
        result = await client.chatgpt.generate_and_wait(
            prompt=prompt,
            images=images,
            timeout=timeout,
        )

    if not result.is_completed or not result.result_image_url:
        raise RuntimeError("RunBlob не вернул готовое изображение")

    return result.result_image_url