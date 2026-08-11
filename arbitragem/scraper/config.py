import os
from dataclasses import dataclass
from pathlib import Path

import yaml
from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class Category:
    slug: str
    name: str
    url: str


@dataclass(frozen=True)
class Settings:
    database_url: str
    max_pages_per_category: int
    request_delay_seconds: float
    headless: bool
    user_agent: str


def load_categories(path: Path = BASE_DIR / "config" / "categories.yaml") -> list[Category]:
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f)
    return [Category(**item) for item in data["categories"]]


def load_settings() -> Settings:
    return Settings(
        database_url=os.environ["DATABASE_URL"],
        max_pages_per_category=int(os.getenv("MAX_PAGES_PER_CATEGORY", "2")),
        request_delay_seconds=float(os.getenv("REQUEST_DELAY_SECONDS", "3")),
        headless=os.getenv("HEADLESS", "true").lower() != "false",
        user_agent=os.getenv(
            "USER_AGENT",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        ),
    )
