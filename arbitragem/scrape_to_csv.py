"""
Script simples de validação: coleta uma página "Mais vendidos" da Amazon.com.br
e grava o resultado em um arquivo CSV. Sem banco de dados, sem configuração
externa — só para confirmar que a extração está funcionando antes de usar o
scraper completo (scraper/amazon_scraper.py), que grava no PostgreSQL.

Dependência única: playwright.

Instalação:
    pip install playwright
    playwright install chromium

Uso:
    python scrape_to_csv.py
    python scrape_to_csv.py --url https://www.amazon.com.br/gp/bestsellers/computers/ --pages 2
    python scrape_to_csv.py --output eletronicos.csv --headed
"""

from __future__ import annotations

import argparse
import csv
import re
import time

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError, sync_playwright

DEFAULT_URL = "https://www.amazon.com.br/gp/bestsellers/electronics/"

ASIN_RE = re.compile(r"/(?:dp|gp/product)/([A-Z0-9]{10})")
RATING_RE = re.compile(r"([\d,.]+)\s+de\s+5\s+estrelas", re.IGNORECASE)
PRICE_RE = re.compile(r"[\d.,]+")
CAPTCHA_MARKERS = ("Digite os caracteres", "Enter the characters you see", "/errors/validateCaptcha")

FIELDNAMES = ["rank", "asin", "title", "price", "rating", "review_count", "url", "image_url"]


def parse_price(text: str | None) -> float | None:
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


def parse_rating(text: str | None) -> float | None:
    if not text:
        return None
    match = RATING_RE.search(text)
    if not match:
        return None
    try:
        return float(match.group(1).replace(",", "."))
    except ValueError:
        return None


def parse_review_count(text: str | None) -> int | None:
    if not text:
        return None
    digits = re.sub(r"[^\d]", "", text)
    return int(digits) if digits else None


def extract_asin(href: str | None) -> str | None:
    if not href:
        return None
    match = ASIN_RE.search(href)
    return match.group(1) if match else None


def extract_product(card) -> dict | None:
    link = card.query_selector("a.a-link-normal[href*='/dp/'], a.a-link-normal[href*='/gp/product/']")
    if link is None:
        return None

    href = link.get_attribute("href")
    asin = extract_asin(href)
    if not asin:
        return None

    url = href if href.startswith("http") else f"https://www.amazon.com.br{href}"

    img = card.query_selector("img")
    image_url = img.get_attribute("src") if img else None
    title = (img.get_attribute("alt") if img else None) or link.inner_text().strip()
    if not title:
        return None

    rank = None
    rank_el = card.query_selector(".zg-bdg-text, .p13n-sc-badge-tags, [class*='badge']")
    if rank_el:
        rank_match = re.search(r"\d+", rank_el.inner_text())
        if rank_match:
            rank = int(rank_match.group(0))

    price_el = card.query_selector(".a-price .a-offscreen, .p13n-sc-price")
    price = parse_price(price_el.inner_text() if price_el else None)

    rating_el = card.query_selector("[aria-label*='de 5 estrelas'], [aria-label*='out of 5 stars']")
    rating = parse_rating(rating_el.get_attribute("aria-label") if rating_el else None)

    review_el = card.query_selector("a.a-link-normal[aria-label] span.a-size-small, span.a-size-small.a-color-base")
    review_count = parse_review_count(review_el.inner_text() if review_el else None)

    return {
        "rank": rank,
        "asin": asin,
        "title": title,
        "price": price,
        "rating": rating,
        "review_count": review_count,
        "url": url,
        "image_url": image_url,
    }


def scrape(url: str, pages: int, delay: float, headless: bool) -> list[dict]:
    products: list[dict] = []
    seen_asins: set[str] = set()

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=headless)
        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            ),
            locale="pt-BR",
        )
        page = context.new_page()

        try:
            for page_number in range(1, pages + 1):
                page_url = url if page_number == 1 else f"{url}?pg={page_number}"
                print(f"Coletando página {page_number}: {page_url}")

                try:
                    page.goto(page_url, wait_until="domcontentloaded", timeout=30_000)
                    page.wait_for_selector(
                        "#gridItemRoot, .p13n-sc-uncoverable-faceout, .zg-grid-general-faceout",
                        timeout=15_000,
                    )
                except PlaywrightTimeoutError:
                    print("  Timeout esperando os produtos carregarem; encerrando.")
                    break

                if any(marker in page.content() for marker in CAPTCHA_MARKERS):
                    print("  Bloqueio/CAPTCHA detectado pela Amazon. Interrompendo a coleta.")
                    break

                cards = page.query_selector_all(
                    "#gridItemRoot, div.p13n-sc-uncoverable-faceout, div.zg-grid-general-faceout"
                )
                if not cards:
                    print("  Nenhum produto encontrado nesta página; a estrutura do site pode ter mudado.")
                    break

                found_here = 0
                for card in cards:
                    try:
                        product = extract_product(card)
                    except Exception as exc:
                        print(f"  Aviso: falha ao ler um card ({exc})")
                        continue
                    if product is None or product["asin"] in seen_asins:
                        continue
                    seen_asins.add(product["asin"])
                    products.append(product)
                    found_here += 1

                print(f"  {found_here} produtos extraídos.")
                if found_here == 0:
                    break

                if page_number < pages:
                    time.sleep(delay)
        finally:
            browser.close()

    return products


def save_csv(products: list[dict], output: str) -> None:
    # utf-8-sig para o Excel exibir acentos corretamente ao abrir no Windows
    with open(output, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDNAMES)
        writer.writeheader()
        writer.writerows(products)


def main() -> None:
    parser = argparse.ArgumentParser(description="Coleta 'Mais vendidos' da Amazon.com.br e salva em CSV")
    parser.add_argument("--url", default=DEFAULT_URL, help="URL da página 'Mais vendidos' (padrão: Eletrônicos)")
    parser.add_argument("--pages", type=int, default=1, help="Quantidade de páginas a coletar (padrão: 1)")
    parser.add_argument("--delay", type=float, default=3.0, help="Segundos de espera entre páginas (padrão: 3)")
    parser.add_argument("--output", default="produtos.csv", help="Caminho do arquivo CSV de saída")
    parser.add_argument("--headed", action="store_true", help="Abre o navegador visível (útil para depurar)")
    args = parser.parse_args()

    products = scrape(args.url, args.pages, args.delay, headless=not args.headed)
    save_csv(products, args.output)
    print(f"\n{len(products)} produtos salvos em {args.output}")


if __name__ == "__main__":
    main()
