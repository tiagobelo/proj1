"""
Coleta as páginas "Mais vendidos" da Amazon.com.br para um ou mais
departamentos e grava tudo em um único CSV. Sem banco de dados — é o
primeiro passo do pipeline por arquivo:
    amazon_scrape_to_csv.py -> compare_mercadolivre.py -> generate_report.py

Departamentos: por padrão coleta todas as categorias com `active: true`
em config/categories.yaml (é lá que você ativa/desativa um departamento,
sem precisar mexer neste script). Use --category para escolher
departamentos específicos (funciona mesmo com active: false, útil para
testar um novo antes de ativá-lo) ou --url para uma URL avulsa, fora do
arquivo de categorias.

Dependências: playwright, pyyaml, python-dotenv (via scraper/config.py).

Instalação:
    pip install -r requirements.txt
    playwright install chromium

Uso:
    python amazon_scrape_to_csv.py
    python amazon_scrape_to_csv.py --category eletronicos --category informatica
    python amazon_scrape_to_csv.py --url https://www.amazon.com.br/gp/bestsellers/toys/ --output brinquedos.csv
    python amazon_scrape_to_csv.py --output produtos.csv --pages 2 --headed
"""

from __future__ import annotations

import argparse
import csv
import re
import time

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError, sync_playwright

from scraper.config import Category, ensure_scrapeable, load_categories

ASIN_RE = re.compile(r"/(?:dp|gp/product)/([A-Z0-9]{10})")
RATING_RE = re.compile(r"([\d,.]+)\s+de\s+5\s+estrelas", re.IGNORECASE)
PRICE_RE = re.compile(r"[\d.,]+")
PRICE_TEXT_RE = re.compile(r"R\$\s*\d{1,3}(?:\.\d{3})*(?:,\d{2})?")
CAPTCHA_MARKERS = ("Digite os caracteres", "Enter the characters you see", "/errors/validateCaptcha")

FIELDNAMES = [
    "category_slug",
    "category_name",
    "rank",
    "asin",
    "title",
    "price",
    "rating",
    "review_count",
    "url",
    "image_url",
]


class BlockedError(RuntimeError):
    """A Amazon apresentou um CAPTCHA ou página de bloqueio."""


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


def extract_price(card) -> float | None:
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

    return parse_price(price_text)


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

    price = extract_price(card)

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


def scrape_category(page, category: Category, pages: int, delay: float) -> list[dict]:
    """Coleta um único departamento (todas as páginas configuradas)."""
    products: list[dict] = []
    seen_asins: set[str] = set()

    for page_number in range(1, pages + 1):
        page_url = category.url if page_number == 1 else f"{category.url}?pg={page_number}"
        print(f"[{category.name}] Coletando página {page_number}: {page_url}")

        try:
            page.goto(page_url, wait_until="domcontentloaded", timeout=30_000)
            page.wait_for_selector(
                "#gridItemRoot, .p13n-sc-uncoverable-faceout, .zg-grid-general-faceout",
                timeout=15_000,
            )
        except PlaywrightTimeoutError:
            print("  Timeout esperando os produtos carregarem; encerrando departamento.")
            break

        if any(marker in page.content() for marker in CAPTCHA_MARKERS):
            raise BlockedError(f"Bloqueio/CAPTCHA detectado em {page_url}")

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
            product["category_slug"] = category.slug
            product["category_name"] = category.name
            products.append(product)
            found_here += 1

        print(f"  {found_here} produtos extraídos.")
        if found_here == 0:
            break

        if page_number < pages:
            time.sleep(delay)

    return products


def scrape(categories: list[Category], pages: int, delay: float, headless: bool) -> list[dict]:
    products: list[dict] = []

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
            for i, category in enumerate(categories):
                print(f"\n=== {category.name} ({category.slug}) ===")
                try:
                    products.extend(scrape_category(page, category, pages, delay))
                except BlockedError as exc:
                    print(f"  {exc} - pulando departamento '{category.name}'.")
                    continue

                if i < len(categories) - 1:
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
    parser = argparse.ArgumentParser(
        description="Coleta 'Mais vendidos' da Amazon.com.br (um ou mais departamentos) e salva em CSV"
    )
    parser.add_argument(
        "--category",
        dest="categories",
        action="append",
        help=(
            "Slug de departamento a coletar (repita para vários; veja config/categories.yaml). "
            "Funciona mesmo se o departamento estiver com active: false. "
            "Padrão: todos os departamentos com active: true."
        ),
    )
    parser.add_argument(
        "--url",
        help=(
            "URL avulsa de uma página 'Mais vendidos', fora de config/categories.yaml "
            "(ignora --category se os dois forem passados)."
        ),
    )
    parser.add_argument("--pages", type=int, default=1, help="Páginas por departamento (padrão: 1)")
    parser.add_argument(
        "--delay", type=float, default=3.0, help="Segundos de espera entre páginas/departamentos (padrão: 3)"
    )
    parser.add_argument("--output", default="produtos.csv", help="Caminho do arquivo CSV de saída")
    parser.add_argument("--headed", action="store_true", help="Abre o navegador visível (útil para depurar)")
    args = parser.parse_args()

    if args.url:
        categories = [Category(slug="adhoc", name="URL avulsa", url=args.url, active=True)]
    else:
        all_categories = load_categories(include_inactive=True)
        if args.categories:
            categories = [c for c in all_categories if c.slug in args.categories]
            missing = set(args.categories) - {c.slug for c in categories}
            if missing:
                raise SystemExit(
                    f"Categoria(s) não encontrada(s) em config/categories.yaml: {', '.join(sorted(missing))}"
                )
        else:
            categories = [c for c in all_categories if c.active]
            if not categories:
                raise SystemExit("Nenhuma categoria com active: true em config/categories.yaml.")

        for category in categories:
            ensure_scrapeable(category)

    products = scrape(categories, args.pages, args.delay, headless=not args.headed)
    save_csv(products, args.output)
    print(f"\n{len(products)} produtos salvos em {args.output}")


if __name__ == "__main__":
    main()
