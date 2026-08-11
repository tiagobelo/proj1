# Arbitragem Amazon → Mercado Livre — MVP-01 (Amazon Scraper)

Primeira entrega do MVP: coletar as páginas **"Mais vendidos"** da Amazon.com.br
para um conjunto de categorias e gravar os produtos no PostgreSQL. Nenhuma
integração com o Mercado Livre ou cálculo de arbitragem entra nesta etapa —
o objetivo é apenas provar que conseguimos coletar dados confiáveis.

## Campos extraídos

| Campo          | Origem                                                              |
|----------------|----------------------------------------------------------------------|
| `asin`         | Extraído da URL do produto (`/dp/{ASIN}`)                           |
| `title`        | Atributo `alt` da imagem do produto (mais completo que o texto do card) |
| `rank`         | Badge de posição no ranking (`#1`, `#2`, ...), quando presente        |
| `price`        | Preço exibido no card (`a-price`/`a-offscreen`)                      |
| `rating`       | `aria-label` do componente de estrelas (ex.: "4,7 de 5 estrelas")    |
| `review_count` | Contagem de avaliações ao lado das estrelas                          |
| `url`          | Link do produto                                                      |
| `image_url`    | `src` da imagem do produto                                           |

Campos que a página de "Mais vendidos" **não** expõe de forma confiável
(marca estruturada, EAN/GTIN, variações) ficam para uma etapa futura —
serão necessários para o matching com o Mercado Livre (MVP-02).

## Identificação do ASIN

Cada card de produto contém um link `<a>` cujo `href` aponta para
`/dp/{ASIN}/...` ou `/gp/product/{ASIN}/...`. Extraímos o ASIN via regex
sobre esse link em vez de depender de atributos `data-asin`/`id`, que mudam
com mais frequência entre variações do layout da Amazon.

## Modelo de dados (PostgreSQL)

- `amazon_category` — categorias configuradas (slug, nome, URL da página).
- `amazon_product` — snapshot **atual** de cada produto (1 linha por ASIN,
  sobrescrita a cada scrape via `UPSERT`).
- `amazon_product_history` — 1 linha por scrape/produto, preservando
  ranking/preço/avaliações ao longo do tempo. Essa tabela é o que permitirá,
  futuramente, detectar promoções e variações de demanda.

Ver [`database/schema.sql`](database/schema.sql) para o DDL completo.

## Como rodar

```bash
cp .env.example .env
docker compose up -d          # sobe o Postgres e aplica database/schema.sql

python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium

# Coleta todas as categorias definidas em config/categories.yaml
python -m scraper.amazon_scraper

# Coleta só uma categoria específica
python -m scraper.amazon_scraper --category eletronicos

# Testa a extração sem gravar no banco
python -m scraper.amazon_scraper --dry-run
```

Categorias são configuradas em [`config/categories.yaml`](config/categories.yaml) —
adicionar uma categoria não exige alteração de código.

## Avisos importantes

- O HTML da Amazon muda com frequência; os seletores usam fallbacks, mas
  ainda exigem manutenção periódica.
- O scraper **não** tenta contornar CAPTCHA ou qualquer mecanismo
  anti-bot: ao detectar um bloqueio, a coleta daquela categoria é
  interrompida (`BlockedError`) e o restante continua normalmente.
- Use com um volume de coleta pequeno e um intervalo razoável entre
  requisições (`REQUEST_DELAY_SECONDS`), respeitando os termos de uso
  aplicáveis da Amazon. Este scraper é destinado a uso interno para
  validar a hipótese de negócio, não a operação em larga escala.

## Próximos passos (fora do escopo deste MVP)

- **MVP-02**: para cada produto coletado, buscar o equivalente no
  Mercado Livre via API oficial e calcular preço médio/mín/máx.
- **MVP-03**: calculadora de arbitragem (lucro, ROI, Opportunity Score) e
  dashboard de oportunidades.
