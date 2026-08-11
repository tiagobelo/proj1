-- MVP-01: Amazon Best Sellers scraper
-- Armazena o snapshot mais recente de cada produto (amazon_product) e o
-- histórico de ranking/preço ao longo do tempo (amazon_product_history),
-- que futuramente alimentará a análise de demanda/promoção.

CREATE TABLE IF NOT EXISTS amazon_category (
    id          SERIAL PRIMARY KEY,
    slug        TEXT UNIQUE NOT NULL,
    name        TEXT NOT NULL,
    url         TEXT NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Estado atual (1 linha por ASIN, atualizada a cada scrape)
CREATE TABLE IF NOT EXISTS amazon_product (
    id             SERIAL PRIMARY KEY,
    asin           TEXT UNIQUE NOT NULL,
    title          TEXT NOT NULL,
    brand          TEXT,
    category_id    INTEGER REFERENCES amazon_category(id),
    rank           INTEGER,
    price          NUMERIC(10, 2),
    currency       TEXT NOT NULL DEFAULT 'BRL',
    rating         NUMERIC(2, 1),
    review_count   INTEGER,
    url            TEXT NOT NULL,
    image_url      TEXT,
    first_seen_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    scraped_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_amazon_product_category ON amazon_product(category_id);
CREATE INDEX IF NOT EXISTS idx_amazon_product_rank ON amazon_product(rank);

-- Histórico: 1 linha por scrape/produto, permite acompanhar variação de
-- ranking e preço (ex.: detectar promoções e picos de demanda).
CREATE TABLE IF NOT EXISTS amazon_product_history (
    id            SERIAL PRIMARY KEY,
    product_id    INTEGER NOT NULL REFERENCES amazon_product(id) ON DELETE CASCADE,
    rank          INTEGER,
    price         NUMERIC(10, 2),
    rating        NUMERIC(2, 1),
    review_count  INTEGER,
    scraped_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_amazon_product_history_product_scraped
    ON amazon_product_history(product_id, scraped_at DESC);
