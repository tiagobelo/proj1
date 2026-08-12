# Arbitragem Amazon → Mercado Livre

## MVP-01 — Amazon Scraper

Primeira entrega do MVP: coletar as páginas **"Mais vendidos"** da Amazon.com.br
para um conjunto de categorias. Nenhuma integração com o Mercado Livre ou
cálculo de arbitragem entra nesta etapa — o objetivo é apenas provar que
conseguimos coletar dados confiáveis.

Há dois jeitos de rodar a coleta, e ambos leem os mesmos departamentos de
[`config/categories.yaml`](config/categories.yaml):

- [`amazon_scrape_to_csv.py`](amazon_scrape_to_csv.py) — **ponto de entrada
  do pipeline por arquivo** (o que os MVPs seguintes usam). Sem banco de
  dados: coleta um ou mais departamentos e grava tudo em um único CSV.
- [`scraper/amazon_scraper.py`](scraper/amazon_scraper.py) — grava os
  produtos no PostgreSQL (útil para manter histórico de ranking/preço ao
  longo do tempo; ver seção "Modelo de dados" abaixo).

## Campos extraídos

| Campo            | Origem                                                              |
|-------------------|----------------------------------------------------------------------|
| `category_slug`  | Slug do departamento em `config/categories.yaml` (só no CSV multi-departamento) |
| `category_name`  | Nome do departamento em `config/categories.yaml` (só no CSV multi-departamento) |
| `asin`           | Extraído da URL do produto (`/dp/{ASIN}`)                           |
| `title`          | Atributo `alt` da imagem do produto (mais completo que o texto do card) |
| `rank`           | Badge de posição no ranking (`#1`, `#2`, ...), quando presente        |
| `price`          | Preço exibido no card (`a-price`/`a-offscreen`)                      |
| `rating`         | `aria-label` do componente de estrelas (ex.: "4,7 de 5 estrelas")    |
| `review_count`   | Contagem de avaliações ao lado das estrelas                          |
| `url`            | Link do produto                                                      |
| `image_url`      | `src` da imagem do produto                                           |

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

## Como rodar (sem banco, direto para CSV)

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium

# Coleta todos os departamentos com active: true em config/categories.yaml
python amazon_scrape_to_csv.py

# Coleta só departamentos específicos (funciona mesmo se active: false)
python amazon_scrape_to_csv.py --category eletronicos --category informatica

# URL avulsa, fora de config/categories.yaml
python amazon_scrape_to_csv.py --url https://www.amazon.com.br/gp/bestsellers/toys/ --output brinquedos.csv

# Mais de uma página por departamento, navegador visível (útil para depurar)
python amazon_scrape_to_csv.py --pages 2 --headed
```

## Como rodar (com PostgreSQL, para manter histórico)

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

Nos dois casos, os departamentos são configurados em
[`config/categories.yaml`](config/categories.yaml) — adicionar um não exige
alteração de código. O arquivo já traz os 28 departamentos de "Mais
vendidos" da Amazon.com.br; cada um tem um campo `active: true/false` —
para tirar um departamento da coleta padrão, basta trocar esse valor para
`false` (sem editar nenhum script). Departamentos com `url: ""` ainda não
tiveram a URL real confirmada e por isso já entram como `active: false`;
confirme a URL no site antes de ativá-los. Uma categoria inativa ainda pode
ser coletada isoladamente com `--category <slug>`.

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
`amazon_scrape_to_csv.py`, busca cada produto no Mercado Livre e calcula preço
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
(título normalizado), com nove filtros de matching para reduzir falsos
positivos:

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
   explicitamente uma quantidade de itens (ex.: "16 unidades", a
   abreviação "C/4", ou o formato "N Pilhas"/"N Baterias"), anúncios com
   quantidade diferente são descartados mesmo com score alto. Sem isso, um
   pacote de 4 pilhas e um de 16 pilhas do mesmo fabricante têm score de
   similaridade ~1.0 (só a palavra da quantidade muda) e seriam tratados
   como o mesmo produto — problema real encontrado em teste com dados
   reais, inclusive títulos como "16 Pilhas ... 4 Cartelas" que só o
   padrão "N Pilhas" pega (nem "unidades" nem "C/4" aparecem nesse
   formato). Quando nenhum dos dois títulos menciona quantidade em algum
   desses formatos, o filtro não se aplica — limitação conhecida, não dá
   pra confirmar quantidade nesse caso.
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
8. **Filtro de marca**: lista pequena de marcas conhecidas nas categorias
   já testadas (`elgin`, `duracell`, `panasonic`, `samsung`, `apple`,
   `anker` etc. — ver `KNOWN_BRANDS`). Quando os dois títulos mencionam
   alguma marca reconhecida mas nenhuma coincide, descarta — problema
   real: "Duracell Pilhas Alcalinas AA Pack 24 Unidades" batendo com "24
   Pilhas Alcalinas **Panasonic** Premium Aa", marca totalmente diferente,
   mas as demais palavras (pilhas, alcalinas, aa, 24, pequena) bastavam
   para passar no score.
9. **Filtro de linha de produto diferente**: lista pequena de nomes que
   identificam um produto diferente dentro da mesma marca quando aparecem
   como palavra solta antes de um número separado, formato que o filtro
   de código alfanumérico (item 6) não cobre (ex.: "Liberty 4 Pro", não
   "liberty4pro"). Problema real: fones "Soundcore Liberty 4 Pro" e
   "Soundcore Space 2/One" inflando a média do "Soundcore P30i" em até
   +144% — a lista atual só tem `liberty`/`space` (ver
   `DIFFERENT_LINE_MARKERS`), cresce sob demanda conforme aparecerem
   novos casos.
Os filtros 8 e 9 usam listas pequenas e específicas às categorias já
testadas (baterias, celulares Samsung/Apple, fones Anker) — ao testar
categorias novas, é esperado precisar adicionar marcas/linhas de produto
novas conforme problemas parecidos aparecerem.

Nenhum desses filtros (3 a 9) interfere em casos como "Galaxy A57"/"Echo
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
python compare_mercadolivre.py produtos.csv --ai-verify        # refinamento por IA, ver seção abaixo
```

O CSV de saída traz, por produto: `ml_listing_count`, `ml_min_price`,
`ml_avg_price`, `ml_max_price`, `ml_median_price`, o anúncio de melhor
match (`ml_best_match_title/score/url`), a categoria oficial do Mercado
Livre do melhor match (`ml_category_id`, `ml_domain_id`), o EAN quando o
anúncio tiver (`ml_ean`) e a diferença bruta em relação ao preço da
Amazon (`diff_avg_vs_amazon`, `diff_avg_pct`) — ainda sem descontar
comissão, frete ou impostos (isso é o MVP-03).

### Categoria, EAN e verificação por IA

- **Categoria automática**: a GeckoAPI retorna `categoryId`/`domainId` —
  a classificação oficial de categoria do Mercado Livre — para cada
  anúncio, sem custo extra. `generate_report.py` usa `ml_domain_id` para
  escolher a comissão certa automaticamente (ver MVP-03 abaixo), em vez
  de você ter que informar manualmente.
- **Atalho por EAN**: quando o CSV de entrada (gerado por
  `amazon_scrape_to_csv.py`) tem uma coluna `ean` preenchida e ela bate com o
  `ean` de um anúncio do Mercado Livre, esse anúncio é tratado como o
  mesmo produto com confiança máxima, pulando todos os filtros
  heurísticos. **Limitação atual**: a página de "Mais vendidos" da
  Amazon não expõe EAN, então esse atalho não ativa com os dados de hoje
  — precisaria de scraping das páginas de detalhe de cada produto
  (fora do escopo atual). O mecanismo já está pronto caso isso mude.
- **Verificação por IA (`--ai-verify`)**: depois dos filtros
  determinísticos (score, quantidade, marca, modelo, armazenamento etc.),
  manda os candidatos sobreviventes de cada produto — em um único lote,
  não um por anúncio — para a API do DeepSeek, pedindo para confirmar
  quais são de fato o mesmo produto. É um refinamento opcional, não
  substitui os filtros: só reduz ainda mais a lista deles quando
  encontra algo que os filtros de palavra/regex não pegam. Em qualquer
  falha (rede, resposta inesperada), mantém os candidatos que já
  passaram nos filtros em vez de descartar dados.

  ```bash
  export DEEPSEEK_API_KEY="sua_chave"   # gerada em platform.deepseek.com
  python compare_mercadolivre.py produtos.csv --ai-verify
  ```

  Cada produto do CSV gera uma chamada à API do DeepSeek (não uma por
  anúncio candidato) — custo baixo, mas ainda assim real; teste primeiro
  com `--max-products` antes de rodar um CSV grande.

### Outros avisos importantes

- A condição "novo"/"usado" é inferida a partir de um campo `condition`
  da API, se vier preenchido, com heurística de fallback no título — pode
  errar em casos limítrofes, mas é razoável para o MVP.
- Se a GeckoAPI retornar erro de rede ou HTTP (ex.: token inválido, sem
  créditos), o script registra o erro no console e segue para o próximo
  produto em vez de travar a execução inteira.

## MVP-03 — Relatório de oportunidades (XLSX)

[`generate_report.py`](generate_report.py) lê o CSV gerado pelo
`compare_mercadolivre.py` e gera uma planilha XLSX com lucro estimado,
margem, ROI e uma classificação (🟢 excelente / 🟡 moderada / 🔴 não
recomendada) por produto — a etapa final que transforma os dados brutos
em decisão de compra. Não usa banco de dados: todo o pipeline roda em
cima de arquivos (`amazon_scrape_to_csv.py` → `compare_mercadolivre.py` →
`generate_report.py`). Veja também [`generate_pdf_report.py`](#mvp-03b--pdf-para-sellersclientes)
logo abaixo, para gerar um PDF apresentável só com os produtos
excelentes, pronto para enviar a terceiros.

### Sobre as taxas do Mercado Livre

A partir de mar/2026 o Mercado Livre passou a calcular o custo
operacional por **peso e dimensão** do produto em vez de uma taxa fixa
simples por faixa de preço, e a comissão varia por categoria (10-14% no
anúncio clássico, 15-19% no premium). Como este pipeline não coleta
peso/dimensão de nenhum dos dois marketplaces, o custo fixo/frete/imposto
usados no relatório continuam sendo **estimativas configuráveis** — não
valores exatos. Confirme os números reais no Simulador de Custos oficial
(dentro do Seller Center) antes de decidir uma compra:
https://www.mercadolivre.com.br/ajuda/formas-de-pagamento/custos

A **comissão** é escolhida automaticamente por categoria:
`generate_report.py` lê `ml_domain_id` do CSV (capturado pelo
`compare_mercadolivre.py` via GeckoAPI) e consulta
[`config/ml_fees.yaml`](config/ml_fees.yaml) para achar o percentual —
sem precisar você informar na mão. O arquivo vem com estimativas
uniformes (12%) para os domínios já testados (pilhas, celulares, fones,
smartwatch), dentro da faixa de 11-13% que a categoria "Eletrônicos e
Informática" costuma ter no anúncio clássico — edite o YAML conforme for
testando categorias novas e confirmando os valores reais. Passar
`--commission-pct` explicitamente ignora esse mapa e usa um valor único
fixo para todas as linhas, como antes.

O **custo fixo** também é escolhido automaticamente, mas por **faixa de
preço de venda** (o Mercado Livre historicamente não cobra custo fixo
acima de um certo preço, só a comissão — e cobra valores diferentes
conforme a faixa abaixo disso): `generate_report.py` consulta
[`config/ml_fixed_fee_tiers.yaml`](config/ml_fixed_fee_tiers.yaml). Esse
arquivo vem **vazio de propósito** — não temos como confirmar as faixas
reais neste ambiente (sem acesso à Amazon nem ao Mercado Livre), e desde
mar/2026 o valor real depende de peso/dimensão, que este pipeline não
coleta. Enquanto o YAML estiver vazio, o script usa R$ 6,00 fixo para
todas as linhas (comportamento anterior) e avisa no console. Preencha as
faixas com os valores do Simulador de Custos para um cálculo mais
preciso. Passar `--fixed-fee` explicitamente ignora o YAML e usa um valor
único fixo para todas as linhas, como antes.

### Como rodar

```bash
pip install -r requirements.txt   # inclui openpyxl e PyYAML

python generate_report.py comparacao.csv
python generate_report.py comparacao.csv --output oportunidades.xlsx \
  --shipping-cost 15 --tax-pct 4 \
  --min-margin-pct 15 --min-roi-pct 20

# força uma comissão única para todas as linhas, ignorando config/ml_fees.yaml
python generate_report.py comparacao.csv --commission-pct 12

# força um custo fixo único para todas as linhas, ignorando config/ml_fixed_fee_tiers.yaml
python generate_report.py comparacao.csv --fixed-fee 6
```

Parâmetros (todos opcionais, com padrão):

| Parâmetro | Padrão | Significado |
|---|---|---|
| `--price-basis` | `median` | Qual preço do Mercado Livre usar como venda esperada (`median`/`avg`/`min`) — mediana é mais resistente a outliers que a média |
| `--commission-pct` | *(automático por categoria)* | Comissão do Mercado Livre em %. Se omitido, escolhe por `ml_domain_id` via `config/ml_fees.yaml`; se informado, vale fixo para todas as linhas |
| `--fees-config` | `config/ml_fees.yaml` | Caminho do YAML de comissão por categoria |
| `--fixed-fee` | *(automático por faixa de preço)* | Custo fixo em R$. Se omitido, escolhe pela faixa de preço de venda via `config/ml_fixed_fee_tiers.yaml` (fallback R$ 6,00 se o YAML estiver vazio); se informado, vale fixo para todas as linhas |
| `--fixed-fee-config` | `config/ml_fixed_fee_tiers.yaml` | Caminho do YAML de custo fixo por faixa de preço |
| `--shipping-cost` | `0.0` | Frete estimado em R$ que o vendedor absorve — script avisa se ficar em 0 |
| `--tax-pct` | `0.0` | Imposto sobre a venda em % (depende do seu regime tributário) |
| `--min-margin-pct` / `--min-roi-pct` | `15.0` / `20.0` | Limiares para classificar como 🟢 excelente oportunidade |

A planilha traz uma coluna "Categoria ML" (o `ml_domain_id` usado) e
"Comissão (%)" (o percentual efetivamente aplicado naquela linha), para
auditar qual taxa foi usada em cada produto.

O relatório calcula, por produto: `lucro líquido = preço de venda -
custo Amazon - comissão - custo fixo - frete - imposto`, `margem = lucro
/ preço de venda`, `ROI = lucro / custo`. Produtos sem nenhum match no
Mercado Livre (`ml_listing_count = 0`) aparecem como ⚪ "sem dados
suficientes", sem travar o relatório.

**Um resultado com tudo vermelho não é necessariamente um bug do
script** — em teste real, um produto com diferença bruta de +19,6% entre
Amazon e Mercado Livre virou prejuízo depois de somar comissão (12%),
custo fixo (R$6) e frete (R$15): a comissão sozinha sobre um preço de
venda alto pode superar toda a margem bruta aparente. É exatamente esse
tipo de alerta que o relatório existe para dar antes da compra.

## MVP-03b — PDF para sellers/clientes

[`generate_pdf_report.py`](generate_pdf_report.py) lê o mesmo CSV e usa
os mesmos parâmetros de custo do `generate_report.py` (reaproveita
`compute_opportunities()` de lá, então os dois relatórios nunca
divergem), mas gera um **PDF apresentável, com só os produtos 🟢
"excelente oportunidade"** — pensado para ser enviado a sellers/clientes
interessados em revender, não para uso interno.

Cada produto vira um card com preço de custo (Amazon), preço de venda
esperado (Mercado Livre), lucro estimado, margem, ROI, comissão, custo
fixo e frete aplicados, e links diretos para o anúncio na Amazon e no
Mercado Livre. Os cards vêm ordenados por margem (do melhor para o
pior). Se nenhum produto for classificado como excelente na rodada, o
PDF é gerado mesmo assim, com uma mensagem explicando isso — nunca falha
silenciosamente nem manda um PDF vazio sem explicação.

O PDF traz, logo no topo, um **aviso de que o custo fixo e o frete usados
são estimativas configuráveis** (não os valores exatos que o Mercado
Livre vai cobrar — ver nota sobre mar/2026 acima) e que os valores reais
devem ser confirmados no Simulador de Custos antes de qualquer decisão
de compra/revenda. O mesmo aviso, resumido, é repetido no rodapé de
todas as páginas, já que este é um documento que pode ser encaminhado
adiante e nem sempre é lido do início ao fim.

### Como rodar

```bash
pip install -r requirements.txt   # inclui reportlab

python generate_pdf_report.py comparacao.csv
python generate_pdf_report.py comparacao.csv --output top_oportunidades.pdf --shipping-cost 15
python generate_pdf_report.py comparacao.csv --min-margin-pct 20 --min-roi-pct 25 \
  --title "Oportunidades de Revenda — Semana 32"
```

Aceita os mesmos parâmetros de custo do `generate_report.py`
(`--commission-pct`, `--fees-config`, `--fixed-fee`,
`--fixed-fee-config`, `--shipping-cost`, `--tax-pct`,
`--min-margin-pct`, `--min-roi-pct`) — rode os dois scripts com os
mesmos argumentos para o XLSX (uso interno, todas as classificações) e o
PDF (uso externo, só os excelentes) baterem entre si. `--title`
personaliza o texto do topo do PDF (por padrão "Oportunidades de Revenda
— Amazon → Mercado Livre").

## Próximos passos (fora do escopo deste MVP)

- Refinamentos adicionais de matching conforme aparecerem em categorias
  novas (ver limitações documentadas na seção de matching acima).
- Eventual dashboard/interface além da planilha XLSX, se o volume de
  produtos testados crescer o suficiente para justificar.
