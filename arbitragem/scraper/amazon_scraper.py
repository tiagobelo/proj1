"""
MVP-01: coleta as páginas "Mais vendidos" da Amazon.com.br para um conjunto
de categorias configurado em config/categories.yaml e grava os produtos no
PostgreSQL (tabelas amazon_product / amazon_product_history).

Uso:
    python -m scraper.amazon_scraper
    python -m scraper.amazon_scraper --category eletronicos
    python -m scraper.amazon_scraper --dry-run

Observações importantes:
- O HTML da Amazon muda com frequência e o site utiliza proteções
  anti-bot. Os seletores abaixo usam múltiplas estratégias de fallback,
  mas ainda assim exigirão manutenção periódica.
- Este scraper não tenta contornar CAPTCHA nem qualquer mecanismo de
  bloqueio: se a Amazon apresentar um desafio, a coleta é interrompida.
- Use um intervalo (REQUEST_DELAY_SECONDS) razoável entre páginas para
  reduzir a chance de bloqueio.
"""

from __future__ import annotations

import argparse
import logging
import re
import time

from playwright.sync_api import Page, TimeoutError as PlaywrightTimeoutError, sync_playwright

from scraper.config import Category, Settings, load_categories, load_settings
from scraper.db import get_connection, get_or_create_category, upsert_product
from scraper.models import ScrapedProduct

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

ASIN_RE = re.compile(r"/(?:dp|gp/product)/([A-Z0-9]{10})")
RATING_RE = re.compile(r"([\d,.]+)\s+de\s+5\s+estrelas", re.IGNORECASE)
PRICE_RE = re.compile(r"[\d.,]+")
PRICE_TEXT_RE = re.compile(r"R\$\s*\d{1,3}(?:\.\d{3})*(?:,\d{2})?")

CAPTCHA_MARKERS = ("Digite os caracteres", "Enter the characters you see", "/errors/validateCaptcha")


class BlockedError(RuntimeError):
    """A Amazon apresentou um CAPTCHA ou página de bloqueio."""


def _parse_price(text: str | None) -> float | None:
    if not text:
        return None
    match = PRICE_RE.search(text)
    if not match:
        return None
    normalized = match.group(0).replace(".", "").replace(",", ".")
    try:
        return float(normalized)
    except ValueError:
        return None


def _parse_rating(text: str | None) -> float | None:
    if not text:
        return None
    match = RATING_RE.search(text)
    if not match:
        return None
    try:
        return float(match.group(1).replace(",", "."))
    except ValueError:
        return None


def _parse_review_count(text: str | None) -> int | None:
    if not text:
        return None
    digits = re.sub(r"[^\d]", "", text)
    return int(digits) if digits else None


def _check_blocked(page: Page) -> None:
    content = page.content()
    if any(marker in content for marker in CAPTCHA_MARKERS):
        raise BlockedError(f"Bloqueio/CAPTCHA detectado em {page.url}")


def _extract_asin(href: str | None) -> str | None:
    if not href:
        return None
    match = ASIN_RE.search(href)
    return match.group(1) if match else None


def _extract_price(card) -> float | None:
    """Tenta seletores conhecidos e, se falhar, cai para uma busca por
    regex de 'R$ 99,90' em todo o texto do card. A Amazon costuma gerar
    classes CSS "hasheadas" por build (ex.: `_cDEzb_p13n-sc-price_3mJ9Z`),
    então seletores de classe exata quebram com frequência; o fallback por
    texto é mais resistente a essas mudanças de markup.
    """
    price_el = card.query_selector(
        ".a-price .a-offscreen, [class*='p13n-sc-price'], [class*='a-price'] .a-offscreen"
    )
    price_text = price_el.inner_text() if price_el else None

    if not price_text:
        match = PRICE_TEXT_RE.search(card.inner_text())
        price_text = match.group(0) if match else None

    return _parse_price(price_text)


def _extract_product(card) -> ScrapedProduct | None:
    """Extrai um produto de um card da grade de 'Mais vendidos'.

    A estrutura da Amazon varia por card (id13n-sc-uncoverable-faceout,
    zg-item, etc.). Em vez de depender de uma classe específica, buscamos
    o link do produto (que sempre aponta para /dp/ASIN) e navegamos a
    partir dele.
    """
    link = card.query_selector("a.a-link-normal[href*='/dp/'], a.a-link-normal[href*='/gp/product/']")
    if link is None:
        return None

    href = link.get_attribute("href")
    asin = _extract_asin(href)
    if not asin:
        return None

    url = href if href.startswith("http") else f"https://www.amazon.com.br{href}"

    img = card.query_selector("img")
    image_url = img.get_attribute("src") if img else None
    # O alt text da imagem costuma trazer o título completo do produto,
    # mais confiável do que o texto truncado exibido no card.
    title = (img.get_attribute("alt") if img else None) or link.inner_text().strip()
    if not title:
        return None

    rank = None
    rank_el = card.query_selector(".zg-bdg-text, .p13n-sc-badge-tags, [class*='badge']")
    if rank_el:
        rank_match = re.search(r"\d+", rank_el.inner_text())
        if rank_match:
            rank = int(rank_match.group(0))

    price = _extract_price(card)

    rating_el = card.query_selector("[aria-label*='de 5 estrelas'], [aria-label*='out of 5 stars']")
    rating = _parse_rating(rating_el.get_attribute("aria-label") if rating_el else None)

    review_el = card.query_selector("a.a-link-normal[aria-label] span.a-size-small, span.a-size-small.a-color-base")
    review_count = _parse_review_count(review_el.inner_text() if review_el else None)

    return ScrapedProduct(
        asin=asin,
        title=title,
        url=url,
        rank=rank,
        price=price,
        rating=rating,
        review_count=review_count,
        image_url=image_url,
    )


def scrape_category(page: Page, category: Category, settings: Settings) -> list[ScrapedProduct]:
    products: list[ScrapedProduct] = []
    seen_asins: set[str] = set()

    for page_number in range(1, settings.max_pages_per_category + 1):
        url = category.url if page_number == 1 else f"{category.url}?pg={page_number}"
        logger.info("Coletando %s (página %d): %s", category.name, page_number, url)

        try:
            page.goto(url, wait_until="domcontentloaded", timeout=30_000)
            page.wait_for_selector("#gridItemRoot, .p13n-sc-uncoverable-faceout, .zg-grid-general-faceout", timeout=15_000)
        except PlaywrightTimeoutError:
            logger.warning("Timeout aguardando produtos em %s; encerrando categoria.", url)
            break

        _check_blocked(page)

        cards = page.query_selector_all(
            "#gridItemRoot, div.p13n-sc-uncoverable-faceout, div.zg-grid-general-faceout"
        )
        if not cards:
            logger.warning("Nenhum card encontrado em %s; a estrutura da página pode ter mudado.", url)
            break

        page_products = 0
        for card in cards:
            try:
                product = _extract_product(card)
            except Exception:
                logger.exception("Falha ao extrair um card em %s", url)
                continue
            if product is None or product.asin in seen_asins:
                continue
            seen_asins.add(product.asin)
            products.append(product)
            page_products += 1

        logger.info("%d produtos extraídos na página %d de %s", page_products, page_number, category.name)
        if page_products == 0:
            break

        time.sleep(settings.request_delay_seconds)

    return products


def run(category_slugs: list[str] | None = None, dry_run: bool = False) -> None:
    settings = load_settings()
    categories = load_categories()
    if category_slugs:
        categories = [c for c in categories if c.slug in category_slugs]
        if not categories:
            raise SystemExit(f"Nenhuma categoria encontrada para {category_slugs}")

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=settings.headless)
        context = browser.new_context(user_agent=settings.user_agent, locale="pt-BR")
        page = context.new_page()

        try:
            for category in categories:
                try:
                    products = scrape_category(page, category, settings)
                except BlockedError as exc:
                    logger.error("%s — pulando categoria '%s'.", exc, category.name)
                    continue

                logger.info("Total coletado em '%s': %d produtos", category.name, len(products))

                if dry_run:
                    for product in products:
                        logger.info(product)
                    continue

                with get_connection(settings.database_url) as conn:
                    category_id = get_or_create_category(conn, category)
                    for product in products:
                        upsert_product(conn, category_id, product)
                logger.info("Categoria '%s' salva no banco.", category.name)
        finally:
            browser.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Scraper de 'Mais vendidos' da Amazon.com.br")
    parser.add_argument(
        "--category",
        dest="categories",
        action="append",
        help="Slug de categoria a coletar (repita para várias). Padrão: todas em categories.yaml",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Não grava no banco; apenas imprime os produtos coletados.",
    )
    args = parser.parse_args()
    run(category_slugs=args.categories, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
