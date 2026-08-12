from __future__ import annotations

from dataclasses import dataclass


@dataclass
class ScrapedProduct:
    """Um item extraído de uma página 'Mais vendidos' da Amazon."""

    asin: str
    title: str
    url: str
    rank: int | None = None
    price: float | None = None
    currency: str = "BRL"
    brand: str | None = None
    rating: float | None = None
    review_count: int | None = None
    image_url: str | None = None
