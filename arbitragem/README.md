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
`scrape_to_csv.py`, busca cada produto no Mercado Livre e calcula preço
mínimo/médio/máximo dos anúncios que provavelmente são o mesmo produto.
Ainda **não** calcula lucro/ROI — isso é o MVP-03.

### Por que via GeckoAPI, e não a API oficial nem scraping direto

Tentamos dois caminhos antes deste:

1. **API oficial do Mercado Livre** (`/sites/MLB/search`): mesmo com um
   `access_token` válido gerado via OAuth, responde
   `403 {"message":"forbidden"}` — endpoint fechado para apps comuns de
   desenvolvedor, problema generalizado desde ago/2025 e sem solução
   oficial até hoje (mesmo tipo de bloqueio que a Amazon Creators API
   impõe exigindo aprovação prévia).
2. **Scraping direto via Playwright**: esbarrou na triagem antibot do
   Mercado Livre, que redireciona sessões "sem histórico" para uma tela
   de login (`/gz/account-verification`) antes de mostrar a busca.

A solução atual usa a [GeckoAPI](https://geckoapi.com.br/), um provedor
terceiro especializado em extração de dados de marketplaces (Mercado
Livre, Shopee, Temu etc.) — eles absorvem o trabalho de lidar com
antibot/CAPTCHA como parte do serviço, e nós só consumimos uma API
HTTP normal.

### Configuração da GeckoAPI

1. Crie uma conta gratuita em https://dashboard.geckoapi.com.br (tem cota
   de créditos grátis para testar, sem cartão).
2. Gere um token de API no dashboard.
3. Exporte a variável de ambiente (**nunca** coloque o token direto no
   código ou no CSV):
   ```bash
   export GECKOAPI_TOKEN="seu_token"   # bash/WSL
   ```
   ```powershell
   $env:GECKOAPI_TOKEN="seu_token"     # PowerShell
   ```

Cada produto do CSV consome créditos da conta — use `--max-products` para
testar com poucos itens antes de rodar o CSV inteiro.

### Aviso sobre o schema de resposta da API

Não tivemos acesso à documentação completa da GeckoAPI (bloqueada no
ambiente onde este código foi escrito), então `parse_item()` em
`compare_mercadolivre.py` tenta múltiplos nomes de campo prováveis como
fallback (`name`/`title`, `url`/`link`/`permalink`, preço como número,
string ou objeto aninhado). Na primeira busca de cada execução, a
resposta bruta da API é salva em `geckoapi_debug_sample.json`
(git-ignorado) — se os resultados vierem estranhos (título vazio, preço
sempre em branco), esse arquivo mostra o formato real e é só ajustar os
nomes de campo em `parse_item()`.

### Como funciona o matching

A página de "Mais vendidos" da Amazon não expõe marca/modelo/EAN de forma
estruturada, então usamos o nível 3 do plano de identificação em camadas
(título normalizado), com sete filtros para reduzir falsos positivos:

1. **Score de similaridade**: sobreposição de palavras significativas entre
   o título da Amazon e o do anúncio do Mercado Livre (`--min-score`,
   padrão `0.5`).
2. **Filtro de acessório**: descarta anúncios que contenham palavras como
   `capinha`, `case`, `suporte`, `stand`, `kit`, `carregador` etc. quando
   essas palavras não aparecem também no título da Amazon — sem isso, uma
   "Capinha para Echo Dot" pode pontuar alto no score e ser confundida com
   o produto em si (problema real: um "Stand" — base/suporte — para Echo
   Dot estava definindo o preço mínimo errado num teste). Exceção: "kit"
   sozinho não desqualifica quando os dois títulos confirmam a mesma
   quantidade (ver item 3) — senão um multipack genuíno descrito como
   "Kit com 16 unidades" seria descartado à toa.
3. **Filtro de quantidade**: quando os dois títulos mencionam
   explicitamente uma quantidade de itens (ex.: "16 unidades", ou a
   abreviação "C/4"), anúncios com quantidade diferente são descartados
   mesmo com score alto. Sem isso, um pacote de 4 pilhas e um de 16 pilhas
   do mesmo fabricante têm score de similaridade ~1.0 (só a palavra da
   quantidade muda) e seriam tratados como o mesmo produto — problema real
   encontrado em teste com dados reais. Quando nenhum dos dois títulos
   menciona quantidade (nem a forma longa nem a abreviada), o filtro não
   se aplica — limitação conhecida, não dá pra confirmar quantidade nesse
   caso.
4. **Filtro de tamanho de pilha**: "AA" e "AAA" são produtos diferentes,
   mas "AA" tem só 2 caracteres e o score de palavras normalmente descarta
   palavras tão curtas — na prática, o matching nunca soube diferenciar
   "Pilha AA" de "Pilha AAA" até esse filtro existir. Mesmo padrão de
   filtro rígido dos itens 3 e 5, específico para essa categoria.
5. **Filtro de número de modelo/geração**: para famílias de produto tipo
   "iPhone N" (`iphone`, `ipad`, `galaxy`, `watch`, `redmi`, `echo`,
   `fire tv`, `playstation`/`ps`, `xbox` seguidos de um número solto),
   descarta o anúncio se o número de geração for diferente — problema real
   encontrado comparando um "iPhone 17" da Amazon com um "iPhone 15" do
   Mercado Livre, que pontuava alto porque as demais palavras do título
   (marca, capacidade, cor) eram idênticas.
6. **Filtro de código de modelo alfanumérico**: quando os dois títulos têm
   pelo menos um token no formato "letra(s)+dígitos" (ex.: "a57", "s21",
   "p30i", "ip68") mas nenhum deles coincide, o anúncio é descartado —
   basta uma coincidência para não rejeitar (um anúncio pode omitir um
   código secundário, tipo o "IP68", sem deixar de ser o mesmo produto).
   Esse foi o achado mais sério até agora: um "Galaxy A57" da Amazon
   estava sendo comparado com anúncios de Galaxy A06, S21, S23, A37, A56,
   A73 e até um Z Flip4 (todos "Samsung Galaxy", mas produtos totalmente
   diferentes) porque o score de palavras genérico (marca, "5G", "câmera",
   "RAM") não bastava para descartá-los. De quebra, o mesmo filtro também
   passou a descartar variações de modelo de fones (ex.: Soundcore P20i,
   P31i, P40i, R50i comparados com o P30i correto) sem nenhum código
   específico para isso.
7. **Filtro de capacidade de armazenamento**: quando os dois títulos
   mencionam GB de armazenamento (ignorando menções a "RAM") mas nenhuma
   capacidade coincide, o anúncio é descartado — pega o caso de um mesmo
   modelo em capacidades diferentes (ex.: Galaxy A57 128GB da Amazon
   batendo com um Galaxy A57 256GB no Mercado Livre, que o filtro de
   código de modelo sozinho não rejeitaria, já que "a57" aparece nos dois).

Nenhum desses filtros (3 a 7) interfere em casos como "Galaxy A57"/"Echo
Dot 5a" quando o número vem colado a uma letra sem indicar um produto
diferente — o score de palavras já resolve esses casos sozinho.

Além disso, só considera anúncios com `condition == "new"` (produto novo,
já que o objetivo é revenda) e remove outliers de preço pela regra do IQR
antes de calcular a média.

O console imprime, para cada produto, todos os anúncios que passaram no
filtro (preço, score e título), ordenados por preço — útil para investigar
números estranhos no CSV (ex.: um preço mínimo muito baixo) sem precisar
rodar de novo só para depurar.

### Como rodar

```bash
pip install -r requirements.txt   # inclui requests

export GECKOAPI_TOKEN="seu_token"

python compare_mercadolivre.py produtos.csv --max-products 3   # teste rápido, gasta poucos créditos
python compare_mercadolivre.py produtos.csv --output comparacao.csv --min-score 0.6
python compare_mercadolivre.py produtos.csv --power-seller     # só vendedores com selo PowerSeller/MercadoLíder
```

O CSV de saída traz, por produto: `ml_listing_count`, `ml_min_price`,
`ml_avg_price`, `ml_max_price`, `ml_median_price`, o anúncio de melhor
match (`ml_best_match_title/score/url`) e a diferença bruta em relação ao
preço da Amazon (`diff_avg_vs_amazon`, `diff_avg_pct`) — ainda sem
descontar comissão, frete ou impostos.

### Outros avisos importantes

- A condição "novo"/"usado" é inferida a partir de um campo `condition`
  da API, se vier preenchido, com heurística de fallback no título — pode
  errar em casos limítrofes, mas é razoável para o MVP.
- Se a GeckoAPI retornar erro de rede ou HTTP (ex.: token inválido, sem
  créditos), o script registra o erro no console e segue para o próximo
  produto em vez de travar a execução inteira.

## Próximos passos (fora do escopo deste MVP)

- **MVP-03**: calculadora de arbitragem (lucro, ROI, Opportunity Score) e
  dashboard de oportunidades, descontando comissão do Mercado Livre, frete
  e impostos sobre `ml_avg_price`.
