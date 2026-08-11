"""
MVP-02: para cada produto de um CSV gerado por scrape_to_csv.py, busca no
Mercado Livre via GeckoAPI (serviço de extração de dados de terceiros,
https://geckoapi.com.br) e calcula preço mínimo/médio/máximo dos anúncios
compatíveis. Ainda não calcula lucro/ROI (isso é o MVP-03).

Histórico (por que não é chamada direta ao Mercado Livre):
- A API oficial de busca (/sites/MLB/search) responde 403 "forbidden"
  mesmo com access_token válido — problema generalizado, relatado por
  vários desenvolvedores desde ago/2025, sem solução oficial até hoje.
- Scraping direto via Playwright esbarrou na triagem antibot do Mercado
  Livre, que redireciona sessões "sem histórico" para uma tela de login
  (/gz/account-verification) antes de mostrar a busca.
A GeckoAPI terceiriza essa extração para um provedor especializado nisso,
então este script não faz mais scraping nem chama a API oficial do
Mercado Livre diretamente.

Configuração:
    1. Crie uma conta gratuita em https://dashboard.geckoapi.com.br
       (tem cota de créditos grátis para testar, sem cartão).
    2. Gere um token de API no dashboard.
    3. Exporte a variável de ambiente antes de rodar (nunca coloque o
       token direto no código):
           export GECKOAPI_TOKEN="seu_token"   # bash/WSL
           $env:GECKOAPI_TOKEN="seu_token"     # PowerShell

Cada produto do CSV consome créditos da sua conta GeckoAPI (uma chamada
por busca) — use --max-products para testar com poucos itens antes de
rodar o CSV inteiro.

Aviso sobre o schema de resposta: não tive acesso à documentação
completa da GeckoAPI ao escrever este script, então a extração dos
campos (título, preço, condição) usa múltiplos nomes de campo prováveis
como fallback. Na primeira busca, a resposta bruta da API é salva em
geckoapi_debug_sample.json — se os resultados vierem estranhos, veja
esse arquivo para ajustar os nomes de campo em parse_item().

Dependência: requests (pip install requests)

Uso:
    python compare_mercadolivre.py produtos.csv
    python compare_mercadolivre.py produtos.csv --max-products 3
    python compare_mercadolivre.py produtos.csv --output comparacao.csv --min-score 0.6
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import statistics
import time
import unicodedata

import requests

GECKOAPI_URL = "https://api.geckoapi.com.br/v1/extract"
DEBUG_SAMPLE_PATH = "geckoapi_debug_sample.json"

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
# "kit" é tratado à parte em looks_like_accessory(): só desqualifica
# quando não há confirmação de quantidade igual entre os títulos (ver
# QUANTITY_RE) — do contrário, um multipack genuíno como "Kit com 16
# unidades" seria descartado por engano.
ACCESSORY_MARKERS = {
    "capa", "capinha", "pelicula", "case", "suporte", "adaptador",
    "carregador", "cabo", "skin", "adesivo", "protetor", "bolsa", "stand",
    "compativel", "compatible", "peca", "reposicao", "acessorio", "kit",
}

PRICE_RE = re.compile(r"[\d.,]+")

# Detecta menção explícita a quantidade de itens no pacote (ex.: "16
# unidades", "kit com 4", "pack 2"). Quando ambos os títulos mencionam uma
# quantidade e elas são diferentes, o anúncio é descartado — sem isso, um
# pacote de 4 pilhas pode pontuar 100% de match com um pacote de 16 só
# porque as demais palavras do título são idênticas.
QUANTITY_RE = re.compile(
    r"(?:kit\s*(?:com|de)?\s*|pack\s*(?:com|de)?\s*|com\s*)?"
    r"(\d+)\s*(?:unidades|unidade|unid\.?|uni\.?|un\.?|pe(?:c|ç)as?|pcs?\.?)\b",
    re.IGNORECASE,
)
# Abreviação comum no Mercado Livre para quantidade, sem a palavra
# "unidades" (ex.: "Pilha AAA 900mah C/4 Elgin"). Encontramos um caso real
# em que isso deixou passar sem checagem — funcionou por coincidência,
# mas o gap existia.
QUANTITY_SHORTHAND_RE = re.compile(r"\bc/\s*(\d+)\b", re.IGNORECASE)

# Tamanhos de pilha: "AA" e "AAA" são produtos diferentes (tamanho físico
# e preço distintos), mas "AA" tem só 2 letras e era descartado pelo
# filtro de tamanho mínimo em normalize() — na prática, o score nunca
# soube diferenciar "Pilha AA" de "Pilha AAA". Ambos ficam protegidos
# aqui, com um filtro rígido equivalente ao de quantidade/modelo.
BATTERY_SIZE_RE = re.compile(r"\b(aaaa|aaa|aa|9v)\b", re.IGNORECASE)

# Detecta "<linha de produto> <número de geração/modelo>" para famílias
# onde o número sozinho (sem sufixo tipo "GB"/"unidades") indica um
# produto diferente — ex.: "iPhone 17" vs "iPhone 15". Sem isso, o score
# de similaridade pode ficar alto o bastante pra passar no filtro mesmo
# comparando gerações diferentes, já que a maioria das outras palavras do
# título (marca, cor, capacidade) é igual — problema real encontrado
# comparando um iPhone 17 da Amazon com um iPhone 15 do Mercado Livre.
MODEL_NUMBER_RE = re.compile(
    r"\b(iphone|ipad|galaxy|watch|redmi|echo|fire\s*tv|playstation|ps|xbox)\s+(\d{1,3})\b",
    re.IGNORECASE,
)

# Códigos de modelo alfanuméricos (ex.: "a57", "s21", "ip68") — formato
# letra(s)+dígitos, com sufixo opcional de letras. Quando o título da
# Amazon tem pelo menos um código desse formato e o do Mercado Livre
# também, mas nenhum código é igual entre os dois, é bem provável que
# sejam modelos diferentes da mesma marca — problema real encontrado
# comparando um Galaxy A57 com anúncios de Galaxy A06, S21, S23, A37,
# A56, A73 e até um Z Flip4 (todos "Samsung Galaxy", mas produtos bem
# diferentes), que passavam no score de palavras sem esse filtro. Exige
# sobreposição parcial (não igualdade total) para não rejeitar um match
# correto só porque um dos títulos omite um código secundário (ex.: só a
# Amazon menciona o "IP68").
MODEL_CODE_TOKEN_RE = re.compile(r"^[a-z]{1,6}\d{1,3}[a-z]{0,3}$")

# Capacidade de armazenamento em GB, ignorando menções a RAM (ex.: "8GB
# RAM" não conta, mas "128GB"/"256GB" contam). Usado para não confundir
# variações de armazenamento do mesmo modelo (ex.: Galaxy A57 128GB vs
# Galaxy A57 256GB), que têm preços bem diferentes apesar de "a57"
# aparecer nos dois títulos.
STORAGE_RE = re.compile(r"\b(\d{2,4})\s*gb\b(?!\s*(?:de\s+)?ram)", re.IGNORECASE)

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
    significativas (ignora stopwords e palavras muito curtas). Números são
    mantidos mesmo quando curtos (ex.: "16") — descartá-los pelo filtro de
    tamanho fazia o score não enxergar diferença entre "16 unidades" e
    "4 unidades", tratando pacotes de tamanhos diferentes como o mesmo
    produto."""
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    text = text.lower()
    words = re.findall(r"[a-z0-9]+", text)
    return {w for w in words if (w.isdigit() or len(w) > 2) and w not in STOPWORDS}


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


def extract_quantity(title: str) -> int | None:
    """Extrai a quantidade de itens do pacote mencionada no título, se
    houver (ex.: "com 16 unidades" -> 16, ou a abreviação "C/4" -> 4).
    Retorna None quando o título não menciona quantidade explicitamente."""
    match = QUANTITY_RE.search(title) or QUANTITY_SHORTHAND_RE.search(title)
    return int(match.group(1)) if match else None


def extract_battery_size(title: str) -> str | None:
    match = BATTERY_SIZE_RE.search(title)
    return match.group(1).lower() if match else None


def has_battery_size_mismatch(amazon_title: str, ml_title: str) -> bool:
    amazon_size = extract_battery_size(amazon_title)
    ml_size = extract_battery_size(ml_title)
    return amazon_size is not None and ml_size is not None and amazon_size != ml_size


def looks_like_accessory(amazon_title: str, ml_title: str, amazon_words: set[str], ml_words: set[str]) -> bool:
    extra_accessory_words = (ml_words & ACCESSORY_MARKERS) - amazon_words
    if not extra_accessory_words:
        return False
    # "kit" sozinho não indica acessório quando os dois títulos confirmam
    # explicitamente a mesma quantidade (ex.: "Kit com 16 unidades" e "com
    # 16 unidades" são o mesmo multipack, não um kit de acessórios). Outras
    # palavras da lista (capinha, case, suporte...) continuam desqualificando
    # normalmente.
    if extra_accessory_words == {"kit"}:
        amazon_qty = extract_quantity(amazon_title)
        ml_qty = extract_quantity(ml_title)
        if amazon_qty is not None and ml_qty is not None and amazon_qty == ml_qty:
            return False
    return True


def has_quantity_mismatch(amazon_title: str, ml_title: str) -> bool:
    amazon_qty = extract_quantity(amazon_title)
    ml_qty = extract_quantity(ml_title)
    return amazon_qty is not None and ml_qty is not None and amazon_qty != ml_qty


def extract_model_number(title: str) -> tuple[str, int] | None:
    """Extrai (linha_de_produto, número) de padrões tipo "iPhone 17" ->
    ("iphone", 17). Retorna None quando o título não segue esse padrão —
    a maioria dos casos (ex.: "Echo Dot 5a Geração", "Galaxy A57") não
    bate porque o número vem colado a uma letra, e nesses casos a
    diferenciação já é feita naturalmente pelo score de palavras."""
    match = MODEL_NUMBER_RE.search(title)
    if not match:
        return None
    return match.group(1).lower().replace(" ", ""), int(match.group(2))


def has_model_number_mismatch(amazon_title: str, ml_title: str) -> bool:
    amazon_model = extract_model_number(amazon_title)
    ml_model = extract_model_number(ml_title)
    if amazon_model is None or ml_model is None:
        return False
    return amazon_model[0] == ml_model[0] and amazon_model[1] != ml_model[1]


def extract_model_codes(words: set[str]) -> set[str]:
    return {w for w in words if MODEL_CODE_TOKEN_RE.match(w)}


def has_model_code_mismatch(amazon_words: set[str], ml_words: set[str]) -> bool:
    """Quando os dois títulos têm pelo menos um código de modelo (ex.:
    "a57", "ip68") mas nenhum deles é igual, provavelmente são produtos
    diferentes — mesmo que outras palavras (marca, "5g", "câmera") se
    sobreponham o bastante para um score alto. Basta UMA coincidência
    (ex.: "a57" em comum) para não rejeitar, já que um anúncio pode
    omitir um código secundário sem deixar de ser o mesmo produto."""
    amazon_codes = extract_model_codes(amazon_words)
    ml_codes = extract_model_codes(ml_words)
    if not amazon_codes or not ml_codes:
        return False
    return amazon_codes.isdisjoint(ml_codes)


def extract_storage_options(title: str) -> set[int]:
    return {int(m) for m in STORAGE_RE.findall(title)}


def has_storage_mismatch(amazon_title: str, ml_title: str) -> bool:
    """Mesma lógica de tolerância do filtro de código de modelo: só
    rejeita quando NENHUMA capacidade mencionada coincide entre os dois
    títulos. Problema real encontrado: um Galaxy A57 128GB (Amazon)
    batendo com um Galaxy A57 256GB (Mercado Livre) — mesmo modelo, preço
    bem diferente."""
    amazon_storage = extract_storage_options(amazon_title)
    ml_storage = extract_storage_options(ml_title)
    if not amazon_storage or not ml_storage:
        return False
    return amazon_storage.isdisjoint(ml_storage)


def parse_price_value(value) -> float | None:
    """A GeckoAPI pode retornar o preço como número puro, string
    formatada ('149,90' ou '149.90') ou um objeto aninhado
    (ex.: {'value': 149.9}) — como não confirmamos o schema exato,
    tratamos os formatos mais prováveis."""
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, dict):
        for key in ("value", "amount", "current", "price"):
            if key in value:
                return parse_price_value(value[key])
        return None
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        match = PRICE_RE.search(text)
        if not match:
            return None
        raw = match.group(0)
        normalized = raw.replace(".", "").replace(",", ".") if "," in raw else raw
        try:
            return float(normalized)
        except ValueError:
            return None
    return None


def find_items(payload) -> list[dict]:
    """Localiza a lista de anúncios dentro da resposta da API, tentando
    as chaves de envelope mais comuns em APIs desse tipo."""
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ("results", "items", "products", "data", "listings", "plp"):
            value = payload.get(key)
            if isinstance(value, list):
                return value
            if isinstance(value, dict):
                nested = find_items(value)
                if nested:
                    return nested
    return []


def parse_item(raw: dict) -> dict | None:
    title = raw.get("name") or raw.get("title") or raw.get("productName")
    url = raw.get("url") or raw.get("link") or raw.get("permalink")
    if not title or not url:
        return None

    price = parse_price_value(raw.get("price"))

    condition_raw = str(raw.get("condition", "")).lower()
    if condition_raw:
        condition = "used" if "usado" in condition_raw or "used" in condition_raw else "new"
    else:
        condition = "used" if "usado" in title.lower() else "new"

    return {"title": title, "permalink": url, "price": price, "condition": condition}


def search_mercadolivre(
    query: str,
    token: str,
    power_seller: bool | None = None,
    save_debug: bool = False,
) -> list[dict]:
    body = {
        "target": "mercadolivre.com.br",
        "type": "plp",
        "page": 1,
        "keyword": query,
    }
    if power_seller is not None:
        body["powerSeller"] = power_seller

    resp = requests.post(
        GECKOAPI_URL,
        headers={"Authorization": f"Bearer {token}"},
        json=body,
        timeout=30,
    )
    resp.raise_for_status()
    payload = resp.json()

    if save_debug:
        with open(DEBUG_SAMPLE_PATH, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        print(f"  Resposta bruta da GeckoAPI salva em {DEBUG_SAMPLE_PATH} (para conferência)")

    results = []
    for raw in find_items(payload):
        item = parse_item(raw)
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
        if looks_like_accessory(amazon_title, ml_title, amazon_words, ml_words):
            continue
        if has_quantity_mismatch(amazon_title, ml_title):
            continue
        if has_battery_size_mismatch(amazon_title, ml_title):
            continue
        if has_model_number_mismatch(amazon_title, ml_title):
            continue
        if has_model_code_mismatch(amazon_words, ml_words):
            continue
        if has_storage_mismatch(amazon_title, ml_title):
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
    token: str,
    min_score: float,
    delay: float,
    power_seller: bool | None,
    save_debug: bool,
) -> dict:
    query = build_query(amazon_title)
    print(f"Buscando no Mercado Livre (GeckoAPI): '{query}'")

    try:
        results = search_mercadolivre(query, token, power_seller=power_seller, save_debug=save_debug)
    except requests.exceptions.RequestException as exc:
        print(f"  Erro na busca: {exc}")
        results = []

    matches = filter_and_score(amazon_title, results, min_score)
    print(f"  {len(results)} resultados brutos, {len(matches)} passaram no filtro de matching")
    for m in sorted(matches, key=lambda m: m.get("price") or 0):
        print(f"    R$ {m.get('price')} (score {round(m['_match_score'], 2)}) - {m['title']}")

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
    parser = argparse.ArgumentParser(description="Compara produtos da Amazon com anúncios no Mercado Livre via GeckoAPI")
    parser.add_argument("input_csv", help="CSV gerado por scrape_to_csv.py")
    parser.add_argument("--output", default="comparacao.csv", help="Caminho do CSV de saída")
    parser.add_argument("--min-score", type=float, default=0.5, help="Score mínimo de matching (0 a 1, padrão 0.5)")
    parser.add_argument("--delay", type=float, default=1.0, help="Segundos entre buscas (padrão 1.0)")
    parser.add_argument("--max-products", type=int, default=None, help="Processa só os N primeiros produtos do CSV (útil para testar sem gastar muitos créditos)")
    parser.add_argument("--power-seller", action="store_true", help="Filtra só vendedores com selo PowerSeller/MercadoLíder")
    args = parser.parse_args()

    token = os.getenv("GECKOAPI_TOKEN")
    if not token:
        raise SystemExit(
            "Defina a variável de ambiente GECKOAPI_TOKEN com seu token da GeckoAPI "
            "antes de rodar (veja instruções no topo de compare_mercadolivre.py ou no README.md)."
        )

    with open(args.input_csv, encoding="utf-8-sig") as f:
        amazon_products = list(csv.DictReader(f))

    if args.max_products:
        amazon_products = amazon_products[: args.max_products]

    rows = []
    for i, product in enumerate(amazon_products):
        row = compare_product(
            product["asin"],
            product["title"],
            product.get("price", ""),
            product["url"],
            token,
            args.min_score,
            args.delay,
            power_seller=True if args.power_seller else None,
            save_debug=(i == 0),
        )
        rows.append(row)

    with open(args.output, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=OUTPUT_FIELDNAMES)
        writer.writeheader()
        writer.writerows(rows)

    print(f"\n{len(rows)} produtos comparados. Resultado em {args.output}")


if __name__ == "__main__":
    main()
