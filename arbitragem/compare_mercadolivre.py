"""
MVP-02: para cada produto de um CSV gerado por scrape_to_csv.py, busca no
Mercado Livre (site MLB — Brasil) os anúncios correspondentes e calcula
preço mínimo/médio/máximo. Ainda não calcula lucro/ROI (isso é o MVP-03) —
o objetivo aqui é só validar se conseguimos encontrar o "mesmo produto" com
confiança razoável.

A API pública de busca (/sites/MLB/search) está fechada para apps comuns
de desenvolvedor: mesmo com um access_token válido, ela responde
403 {"message":"forbidden"} (confirmado em teste real). Por isso, assim
como fizemos com a Amazon, este script busca fazendo scraping da página
pública de resultados do Mercado Livre — não precisa mais de client_id,
client_secret nem access_token.

Como a página de "Mais vendidos" da Amazon não expõe marca/modelo/EAN de
forma estruturada, o matching usado aqui é por título normalizado (nível 3
do plano de identificação em camadas): compara o conjunto de palavras
significativas do título da Amazon com o de cada resultado do Mercado
Livre e descarta os que têm pouca sobreposição, além de descartar
anúncios que parecem ser acessórios/kits em vez do produto em si.

Dependência: playwright (mesma já usada em scrape_to_csv.py)
    pip install playwright
    playwright install chromium

Uso:
    python compare_mercadolivre.py produtos.csv
    python compare_mercadolivre.py produtos.csv --output comparacao.csv
    python compare_mercadolivre.py produtos.csv --min-score 0.6 --delay 1.5
    python compare_mercadolivre.py produtos.csv --headed
"""

from __future__ import annotations

import argparse
import csv
import re
import statistics
import time
import unicodedata
from urllib.parse import quote

from playwright.sync_api import Page, TimeoutError as PlaywrightTimeoutError, sync_playwright

SEARCH_URL_TEMPLATE = "https://lista.mercadolivre.com.br/{query}"

STOPWORDS = {
    "de", "da", "do", "das", "dos", "com", "para", "e", "ou", "um", "uma",
    "uns", "umas", "no", "na", "nos", "nas", "o", "a", "os", "as", "em",
    "por", "mais", "recente", "geracao", "original", "novo", "nova", "the",
}

# Palavras que costumam indicar que o anúncio é um acessório/peça de
# reposição para o produto, não o produto em si (ex.: "Capinha para Echo
# Dot"). Se aparecerem no título do Mercado Livre mas não no da Amazon, o
# anúncio é descartado mesmo que o score de similaridade seja alto — o
# score sozinho não distingue "Echo Dot" de "capinha para Echo Dot".
ACCESSORY_MARKERS = {
    "capa", "capinha", "pelicula", "case", "suporte", "adaptador",
    "carregador", "cabo", "skin", "adesivo", "protetor", "bolsa",
    "compativel", "compatible", "peca", "reposicao", "acessorio", "kit",
}

# Seletores conhecidos dos cards de resultado. O Mercado Livre já usou (e
# ainda mistura, dependendo da categoria/experimento) tanto a marcação
# "ui-search-*" mais antiga quanto a mais nova "poly-*". Mantemos os dois
# como fallback, igual fizemos com a Amazon.
CARD_SELECTOR = "li.ui-search-layout__item, div.ui-search-result__wrapper, div.poly-card, div[class*='poly-card']"
TITLE_SELECTOR = "h2.ui-search-item__title, .ui-search-item__title, h3.poly-component__title, a.poly-component__title, [class*='poly-component__title']"
LINK_SELECTOR = "a.ui-search-link, a.ui-search-item__group__element, a.poly-component__title, a[href*='mercadolivre.com.br'], a[href*='/MLB-']"

PRICE_RE = re.compile(r"[\d.,]+")
PRICE_TEXT_RE = re.compile(r"R\$\s*\d{1,3}(?:\.\d{3})*(?:,\d{2})?")
CAPTCHA_MARKERS = ("Confirme que você não é um robô", "captcha", "Acesso bloqueado")

OUTPUT_FIELDNAMES = [
    "asin",
    "amazon_title",
    "amazon_price",
    "amazon_url",
    "ml_listing_count",
    "ml_min_price",
    "ml_avg_price",
    "ml_max_price",
    "ml_median_price",
    "ml_best_match_title",
    "ml_best_match_score",
    "ml_best_match_url",
    "diff_avg_vs_amazon",
    "diff_avg_pct",
]


def normalize(text: str) -> set[str]:
    """Remove acentos/pontuação e retorna o conjunto de palavras
    significativas (ignora stopwords e palavras muito curtas)."""
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    text = text.lower()
    words = re.findall(r"[a-z0-9]+", text)
    return {w for w in words if len(w) > 2 and w not in STOPWORDS}


def build_query(title: str, max_words: int = 6) -> str:
    """Reduz o título (muitas vezes cheio de texto de marketing) a poucas
    palavras-chave significativas, na ordem em que aparecem no título."""
    title = title.split("|")[0]  # remove sufixos de marketing após '|'
    significant = normalize(title)
    ordered_words = re.findall(r"[A-Za-zÀ-ÿ0-9]+", title)

    query_words: list[str] = []
    seen_lower: set[str] = set()
    for word in ordered_words:
        lower = word.lower()
        if lower in significant and lower not in seen_lower:
            query_words.append(word)
            seen_lower.add(lower)
        if len(query_words) >= max_words:
            break

    return " ".join(query_words) if query_words else title[:60]


def match_score(amazon_title: str, ml_title: str) -> float:
    """Sobreposição de palavras significativas entre os dois títulos,
    normalizada pelo menor conjunto (mais tolerante a títulos de tamanhos
    bem diferentes do que o índice de Jaccard)."""
    a = normalize(amazon_title)
    b = normalize(ml_title)
    if not a or not b:
        return 0.0
    return len(a & b) / min(len(a), len(b))


def looks_like_accessory(amazon_words: set[str], ml_words: set[str]) -> bool:
    extra_accessory_words = (ml_words & ACCESSORY_MARKERS) - amazon_words
    return bool(extra_accessory_words)


def parse_price_text(text: str | None) -> float | None:
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


def extract_price(card) -> float | None:
    """O preço do Mercado Livre normalmente vem em dois spans separados
    (parte inteira e centavos, ex.: 'andes-money-amount__fraction' = '149'
    e 'andes-money-amount__cents' = '90'). Se essa estrutura não bater,
    cai para uma busca por regex de 'R$ 149,90' em todo o texto do card —
    o mesmo fallback que já usamos no scraper da Amazon."""
    fraction_el = card.query_selector("[class*='andes-money-amount__fraction']")
    if fraction_el:
        cents_el = card.query_selector("[class*='andes-money-amount__cents']")
        fraction = fraction_el.inner_text().strip()
        cents = cents_el.inner_text().strip() if cents_el else "00"
        price = parse_price_text(f"{fraction},{cents}")
        if price is not None:
            return price

    match = PRICE_TEXT_RE.search(card.inner_text())
    return parse_price_text(match.group(0)) if match else None


def extract_condition(card) -> str:
    """O Mercado Livre normalmente só exibe uma etiqueta explícita para
    anúncios usados; produtos novos costumam vir sem essa marcação. É uma
    heurística simples — pode errar em títulos que mencionem 'usado' por
    outro motivo, mas é razoável para o MVP."""
    text = card.inner_text().lower()
    return "used" if "usado" in text else "new"


def extract_item(card) -> dict | None:
    title_el = card.query_selector(TITLE_SELECTOR)
    link_el = card.query_selector(LINK_SELECTOR)
    if title_el is None or link_el is None:
        return None

    title = title_el.inner_text().strip()
    url = link_el.get_attribute("href")
    if not title or not url:
        return None

    return {
        "title": title,
        "permalink": url,
        "price": extract_price(card),
        "condition": extract_condition(card),
    }


def search_mercadolivre(page: Page, query: str, limit: int = 50) -> list[dict]:
    url = SEARCH_URL_TEMPLATE.format(query=quote(query.replace(" ", "-")))

    try:
        page.goto(url, wait_until="domcontentloaded", timeout=30_000)
        page.wait_for_selector(CARD_SELECTOR, timeout=15_000)
    except PlaywrightTimeoutError:
        # Pode ser página sem resultados, ou a estrutura do site mudou.
        return []

    content = page.content()
    if any(marker.lower() in content.lower() for marker in CAPTCHA_MARKERS):
        print("  Aviso: possível bloqueio/CAPTCHA do Mercado Livre; pulando esta busca.")
        return []

    cards = page.query_selector_all(CARD_SELECTOR)
    results = []
    for card in cards[:limit]:
        try:
            item = extract_item(card)
        except Exception as exc:
            print(f"  Aviso: falha ao ler um card ({exc})")
            continue
        if item:
            results.append(item)
    return results


def filter_and_score(amazon_title: str, results: list[dict], min_score: float) -> list[dict]:
    amazon_words = normalize(amazon_title)
    filtered = []
    for item in results:
        if item.get("condition") != "new":
            continue
        ml_title = item.get("title", "")
        ml_words = normalize(ml_title)
        if looks_like_accessory(amazon_words, ml_words):
            continue
        score = match_score(amazon_title, ml_title)
        if score < min_score:
            continue
        item["_match_score"] = score
        filtered.append(item)
    return filtered


def remove_price_outliers(prices: list[float]) -> list[float]:
    """Remove outliers pela regra do IQR, para que um único anúncio fora
    da curva não distorça a média. Com poucas amostras, mantém tudo."""
    if len(prices) < 4:
        return prices
    ordered = sorted(prices)
    mid = len(ordered) // 2
    q1 = statistics.median(ordered[:mid])
    q3 = statistics.median(ordered[mid + (len(ordered) % 2):])
    iqr = q3 - q1
    lower, upper = q1 - 1.5 * iqr, q3 + 1.5 * iqr
    cleaned = [p for p in ordered if lower <= p <= upper]
    return cleaned or ordered


def compare_product(
    page: Page,
    asin: str,
    amazon_title: str,
    amazon_price: str,
    amazon_url: str,
    min_score: float,
    delay: float,
) -> dict:
    query = build_query(amazon_title)
    print(f"Buscando no Mercado Livre: '{query}'")

    try:
        results = search_mercadolivre(page, query)
    except Exception as exc:
        print(f"  Erro na busca: {exc}")
        results = []

    matches = filter_and_score(amazon_title, results, min_score)
    print(f"  {len(results)} resultados brutos, {len(matches)} passaram no filtro de matching")

    row = {
        "asin": asin,
        "amazon_title": amazon_title,
        "amazon_price": amazon_price,
        "amazon_url": amazon_url,
        "ml_listing_count": len(matches),
        "ml_min_price": "",
        "ml_avg_price": "",
        "ml_max_price": "",
        "ml_median_price": "",
        "ml_best_match_title": "",
        "ml_best_match_score": "",
        "ml_best_match_url": "",
        "diff_avg_vs_amazon": "",
        "diff_avg_pct": "",
    }

    if matches:
        prices = [m["price"] for m in matches if m.get("price")]
        clean_prices = remove_price_outliers(prices) if prices else []
        best = max(matches, key=lambda m: m["_match_score"])

        if clean_prices:
            row["ml_min_price"] = round(min(clean_prices), 2)
            row["ml_avg_price"] = round(statistics.mean(clean_prices), 2)
            row["ml_max_price"] = round(max(clean_prices), 2)
            row["ml_median_price"] = round(statistics.median(clean_prices), 2)

        row["ml_best_match_title"] = best.get("title", "")
        row["ml_best_match_score"] = round(best["_match_score"], 2)
        row["ml_best_match_url"] = best.get("permalink", "")

        if amazon_price and row["ml_avg_price"] != "":
            try:
                diff = row["ml_avg_price"] - float(amazon_price)
                row["diff_avg_vs_amazon"] = round(diff, 2)
                row["diff_avg_pct"] = round(diff / float(amazon_price) * 100, 1)
            except ValueError:
                pass

    time.sleep(delay)
    return row


def main() -> None:
    parser = argparse.ArgumentParser(description="Compara produtos da Amazon com anúncios no Mercado Livre")
    parser.add_argument("input_csv", help="CSV gerado por scrape_to_csv.py")
    parser.add_argument("--output", default="comparacao.csv", help="Caminho do CSV de saída")
    parser.add_argument("--min-score", type=float, default=0.5, help="Score mínimo de matching (0 a 1, padrão 0.5)")
    parser.add_argument("--delay", type=float, default=2.0, help="Segundos entre buscas (padrão 2.0)")
    parser.add_argument("--headed", action="store_true", help="Abre o navegador visível (útil para depurar)")
    args = parser.parse_args()

    with open(args.input_csv, encoding="utf-8-sig") as f:
        amazon_products = list(csv.DictReader(f))

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=not args.headed)
        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            ),
            locale="pt-BR",
        )
        page = context.new_page()

        rows = []
        try:
            for product in amazon_products:
                row = compare_product(
                    page,
                    product["asin"],
                    product["title"],
                    product.get("price", ""),
                    product["url"],
                    args.min_score,
                    args.delay,
                )
                rows.append(row)
        finally:
            browser.close()

    with open(args.output, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=OUTPUT_FIELDNAMES)
        writer.writeheader()
        writer.writerows(rows)

    print(f"\n{len(rows)} produtos comparados. Resultado em {args.output}")


if __name__ == "__main__":
    main()
