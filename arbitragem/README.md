# Arbitragem Amazon → Mercado Livre

## MVP-01 — Amazon Scraper

Primeira entrega do MVP: coletar as páginas **"Mais vendidos"** da Amazon.com.br
para um conjunto de categorias e gravar os produtos no PostgreSQL. Nenhuma
integração com o Mercado Livre ou cálculo de arbitragem entra nesta etapa —
o objetivo é apenas provar que conseguimos coletar dados confiáveis.

Há também [`scrape_to_csv.py`](scrape_to_csv.py), uma versão standalone (só
com Playwright como dependência, sem banco) para validar rapidamente a
extração antes de configurar o Postgres.

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

## MVP-02 — Comparação com o Mercado Livre

[`compare_mercadolivre.py`](compare_mercadolivre.py) lê o CSV gerado pelo
`scrape_to_csv.py`, busca cada produto no Mercado Livre (site `MLB` —
Brasil) e calcula preço mínimo/médio/máximo dos anúncios que provavelmente
são o mesmo produto. Ainda **não** calcula lucro/ROI — isso é o MVP-03.

**A busca é feita por scraping da página pública de resultados, não pela
API.** Testamos a API oficial (`/sites/MLB/search`) e, mesmo com um
`access_token` válido gerado via OAuth, ela responde
`403 {"message":"forbidden"}` — o endpoint está fechado para apps comuns
de desenvolvedor, só disponível para integradores certificados. Como isso
é essencialmente o mesmo bloqueio que já tínhamos identificado do lado da
Amazon (Creators API exige aprovação prévia), seguimos a mesma solução:
scraping da página pública, sem precisar de `client_id`/`client_secret`/
`access_token`.

### Como funciona o matching

A página de "Mais vendidos" da Amazon não expõe marca/modelo/EAN de forma
estruturada, então usamos o nível 3 do plano de identificação em camadas
(título normalizado), com dois filtros para reduzir falsos positivos:

1. **Score de similaridade**: sobreposição de palavras significativas entre
   o título da Amazon e o do anúncio do Mercado Livre (`--min-score`,
   padrão `0.5`).
2. **Filtro de acessório**: descarta anúncios que contenham palavras como
   `capinha`, `case`, `suporte`, `kit`, `carregador` etc. quando essas
   palavras não aparecem também no título da Amazon — sem isso, uma
   "Capinha para Echo Dot" pode pontuar alto no score e ser confundida com
   o produto em si.

Além disso, só considera anúncios com `condition == "new"` (produto novo,
já que o objetivo é revenda) e remove outliers de preço pela regra do IQR
antes de calcular a média.

### Como rodar

```bash
pip install -r requirements.txt   # playwright já usado no MVP-01
playwright install chromium       # se ainda não tiver rodado

python compare_mercadolivre.py produtos.csv
python compare_mercadolivre.py produtos.csv --output comparacao.csv --min-score 0.6
python compare_mercadolivre.py produtos.csv --headed   # navegador visível, útil para depurar
```

O CSV de saída traz, por produto: `ml_listing_count`, `ml_min_price`,
`ml_avg_price`, `ml_max_price`, `ml_median_price`, o anúncio de melhor
match (`ml_best_match_title/score/url`) e a diferença bruta em relação ao
preço da Amazon (`diff_avg_vs_amazon`, `diff_avg_pct`) — ainda sem
descontar comissão, frete ou impostos.

### Triagem antibot / tela de verificação de conta

O Mercado Livre pode redirecionar sessões que parecem automatizadas (sem
cookies, indo direto a um link profundo) para uma tela
`/gz/account-verification` pedindo login antes de mostrar a busca — não é
um CAPTCHA, é um pedido de autenticação mesmo. Para reduzir a chance
disso:

- O script mantém um **perfil de navegador persistente** em
  `arbitragem/.ml_browser_profile/` (cookies salvos entre execuções, git-
  ignorado) em vez de abrir uma sessão anônima do zero a cada vez.
- Antes de qualquer busca, ele visita a página inicial do Mercado Livre
  (`warm_up()`) e tenta fechar o banner de cookies, simulando uma
  navegação mais parecida com a de uma pessoa.

Isso reduz a chance de bloqueio, mas **não elimina**: a triagem do
Mercado Livre é uma heurística deles, fora do nosso controle. Se mesmo
assim aparecer a tela de verificação, o script detecta isso
(`BlockedError`) e para a execução com uma mensagem clara, em vez de
retornar silenciosamente 0 resultados para tudo (como acontecia antes).
Nesse caso, rodar de novo mais tarde costuma ajudar; se for recorrente,
a única forma de contornar de verdade seria autenticar com uma conta real
do Mercado Livre — o que traz risco de restrição na conta e não deve ser
feito sem decidir isso conscientemente antes.

### Outros avisos importantes

- Assim como no scraper da Amazon, o HTML do Mercado Livre muda com
  frequência; os seletores usam fallbacks (`CARD_SELECTOR`,
  `TITLE_SELECTOR`, `LINK_SELECTOR` em `compare_mercadolivre.py`), mas
  ainda exigem manutenção periódica.
- A condição "novo"/"usado" é inferida pela presença da palavra "usado"
  no texto do card — o Mercado Livre normalmente só rotula anúncios
  usados explicitamente. É uma heurística simples, não uma leitura
  estruturada do campo `condition`.
- O script não tenta contornar CAPTCHA; se detectado, a execução para
  (veja seção acima).

## Próximos passos (fora do escopo deste MVP)

- **MVP-03**: calculadora de arbitragem (lucro, ROI, Opportunity Score) e
  dashboard de oportunidades, descontando comissão do Mercado Livre, frete
  e impostos sobre `ml_avg_price`.
