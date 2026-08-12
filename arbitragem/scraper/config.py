from __future__ import annotations

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
    active: bool = True


AMAZON_BR_PREFIX = "https://www.amazon.com.br"


@dataclass(frozen=True)
class Settings:
    database_url: str
    max_pages_per_category: int
    request_delay_seconds: float
    headless: bool
    user_agent: str


def load_categories(
    path: Path = BASE_DIR / "config" / "categories.yaml",
    include_inactive: bool = False,
) -> list[Category]:
    """Carrega as categorias de config/categories.yaml.

    Por padrão devolve só as com `active: true` — para desativar uma
    categoria basta trocar esse valor no YAML, sem tocar em código.
    Use `include_inactive=True` para obter a lista completa (ex.: quando
    o usuário pede uma categoria específica via --category, mesmo que
    esteja inativa).
    """
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f)
    categories = [Category(**item) for item in data["categories"]]
    if not include_inactive:
        categories = [c for c in categories if c.active]
    return categories


def ensure_scrapeable(category: Category) -> None:
    """Garante que a categoria tem uma URL real da Amazon antes de ser usada.

    Departamentos ainda não verificados entram em categories.yaml com
    `url: ""` e `active: false`; isso evita que alguém ative um item sem
    preencher a URL correta e o scraper acabe batendo num endereço errado.
    """
    if not category.url or not category.url.startswith(AMAZON_BR_PREFIX):
        raise ValueError(
            f"Categoria '{category.slug}' ({category.name}) não tem uma URL "
            "válida da Amazon em config/categories.yaml. Confirme a URL real "
            "da página 'Mais vendidos' desse departamento antes de marcar "
            "active: true (ou de passar --category para ela)."
        )


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
