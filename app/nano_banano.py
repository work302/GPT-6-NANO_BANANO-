from dataclasses import dataclass


MODEL_NAMES = {
    "nano_banana": "Nano Banana",
    "nano_banana_pro": "Nano Banana Pro",
    "nano_banana_2": "Nano Banana 2",
}

MODEL_PRICES = {
    "nano_banana": 2,
    "nano_banana_pro": 3,
    "nano_banana_2": 2,
}

MODEL_DEFAULT = "nano_banana"
FORMAT_LABELS = {
    "original": "📸 Исходный",
    "1:1": "⬜ 1:1",
    "16:9": "🖥 16:9",
    "9:16": "📱 9:16",
}
FORMAT_DEFAULT = "original"
RESOLUTION_LABELS = {
    "standard": "Стандартное",
    "2k": "2K",
    "4k": "4K",
}
MODEL_RESOLUTIONS = {
    "nano_banana": ("standard",),
    "nano_banana_pro": ("standard", "2k", "4k"),
    "nano_banana_2": ("standard", "2k", "4k"),
}
RESOLUTION_DEFAULT = "standard"


@dataclass(frozen=True)
class GenerationRequest:
    model: str
    format: str
    resolution: str
    prompt: str
    photo: str | None


def calculate_generation_cost(model: str) -> int:
    try:
        return MODEL_PRICES[model]
    except KeyError as error:
        raise ValueError(f"Unsupported Nano Banana model: {model}") from error
