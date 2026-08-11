"""
MVP-02: para cada produto de um CSV gerado por scrape_to_csv.py, busca no
Mercado Livre (site MLB — Brasil) os anúncios correspondentes e calcula
preço mínimo/médio/máximo. Ainda não calcula lucro/ROI (isso é o MVP-03) —
o objetivo aqui é só validar se conseguimos encontrar o "mesmo produto" com
confiança razoável.

Como a página de "Mais vendidos" da Amazon não expõe marca/modelo/EAN de
forma estruturada, o matching usado aqui é por título normalizado (nível 3
do plano de identificação em camadas): compara o conjunto de palavras
significativas do título da Amazon com o de cada resultado do Mercado
Livre e descarta os que têm pouca sobreposição.

Dependência: requests (pip install requests)

Uso:
    python compare_mercadolivre.py produtos.csv
    python compare_mercadolivre.py produtos.csv --output comparacao.csv
    python compare_mercadolivre.py produtos.csv --min-score 0.6 --delay 1.5

Autenticação:
    A busca é tentada primeiro sem token (vários endpoints públicos do
    Mercado Livre ainda funcionam assim). Se a API responder 401, configure
    a variável de ambiente ML_ACCESS_TOKEN — veja instruções no README.md.
"""

from __future__ import annotations

import argparse
import csv
import os
import re
import statistics
import time
import unicodedata

import requests

ML_SEARCH_URL = "https://api.mercadolibre.com/sites/MLB/search"

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


class AuthRequiredError(RuntimeError):
    """A API do Mercado Livre exige um token de acesso para esta busca."""


def search_mercadolivre(query: str, token: str | None, limit: int = 50) -> list[dict]:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    params = {"q": query, "limit": limit}
    resp = requests.get(ML_SEARCH_URL, params=params, headers=headers, timeout=15)
    if resp.status_code in (401, 403):
        raise AuthRequiredError(
            f"Mercado Livre respondeu {resp.status_code} — a busca pública exige "
            "autenticação. Configure a variável de ambiente ML_ACCESS_TOKEN com um "
            "token de aplicativo (veja README.md, seção 'Autenticação na API do "
            "Mercado Livre')."
        )
    resp.raise_for_status()
    return resp.json().get("results", [])


def looks_like_accessory(amazon_words: set[str], ml_words: set[str]) -> bool:
    extra_accessory_words = (ml_words & ACCESSORY_MARKERS) - amazon_words
    return bool(extra_accessory_words)


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
    asin: str,
    amazon_title: str,
    amazon_price: str,
    amazon_url: str,
    token: str | None,
    min_score: float,
    delay: float,
) -> dict:
    query = build_query(amazon_title)
    print(f"Buscando no Mercado Livre: '{query}'")

    try:
        results = search_mercadolivre(query, token)
    except AuthRequiredError:
        raise
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
    parser.add_argument("--delay", type=float, default=1.0, help="Segundos entre buscas (padrão 1.0)")
    args = parser.parse_args()

    token = os.getenv("ML_ACCESS_TOKEN")
    if not token:
        print("Aviso: ML_ACCESS_TOKEN não configurado; tentando buscar sem autenticação.\n")

    try:
        search_mercadolivre("teste", token, limit=1)
    except AuthRequiredError as exc:
        raise SystemExit(f"\n{exc}") from None

    with open(args.input_csv, encoding="utf-8-sig") as f:
        amazon_products = list(csv.DictReader(f))

    rows = []
    for product in amazon_products:
        try:
            row = compare_product(
                product["asin"],
                product["title"],
                product.get("price", ""),
                product["url"],
                token,
                args.min_score,
                args.delay,
            )
        except AuthRequiredError as exc:
            raise SystemExit(f"\n{exc}") from None
        rows.append(row)

    with open(args.output, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=OUTPUT_FIELDNAMES)
        writer.writeheader()
        writer.writerows(rows)

    print(f"\n{len(rows)} produtos comparados. Resultado em {args.output}")


if __name__ == "__main__":
    main()
