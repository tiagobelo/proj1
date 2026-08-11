import logging
from contextlib import contextmanager

import psycopg2
import psycopg2.extras

from scraper.config import Category
from scraper.models import ScrapedProduct

logger = logging.getLogger(__name__)


@contextmanager
def get_connection(database_url: str):
    conn = psycopg2.connect(database_url)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def get_or_create_category(conn, category: Category) -> int:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO amazon_category (slug, name, url)
            VALUES (%s, %s, %s)
            ON CONFLICT (slug) DO UPDATE SET name = EXCLUDED.name, url = EXCLUDED.url
            RETURNING id
            """,
            (category.slug, category.name, category.url),
        )
        return cur.fetchone()[0]


def upsert_product(conn, category_id: int, product: ScrapedProduct) -> int:
    """Atualiza o snapshot atual do produto e grava uma linha de histórico."""
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO amazon_product (
                asin, title, brand, category_id, rank, price, currency,
                rating, review_count, url, image_url, scraped_at
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, now())
            ON CONFLICT (asin) DO UPDATE SET
                title = EXCLUDED.title,
                brand = EXCLUDED.brand,
                category_id = EXCLUDED.category_id,
                rank = EXCLUDED.rank,
                price = EXCLUDED.price,
                currency = EXCLUDED.currency,
                rating = EXCLUDED.rating,
                review_count = EXCLUDED.review_count,
                url = EXCLUDED.url,
                image_url = EXCLUDED.image_url,
                scraped_at = now()
            RETURNING id
            """,
            (
                product.asin,
                product.title,
                product.brand,
                category_id,
                product.rank,
                product.price,
                product.currency,
                product.rating,
                product.review_count,
                product.url,
                product.image_url,
            ),
        )
        product_id = cur.fetchone()[0]

        cur.execute(
            """
            INSERT INTO amazon_product_history (
                product_id, rank, price, rating, review_count, scraped_at
            )
            VALUES (%s, %s, %s, %s, %s, now())
            """,
            (
                product_id,
                product.rank,
                product.price,
                product.rating,
                product.review_count,
            ),
        )
        return product_id
