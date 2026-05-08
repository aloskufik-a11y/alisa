"""
Database module — SQLite с WAL mode для конкурентного доступа.
"""
import sqlite3
import os
import logging

logger = logging.getLogger(__name__)

DB_PATH = os.getenv("DB_PATH") or os.path.join(os.path.dirname(__file__), "database.sqlite")


def _get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=10, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")   # Конкурентные читатели не блокируют писателей
    conn.execute("PRAGMA synchronous=NORMAL") # Баланс скорость/надёжность
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db():
    """Инициализация БД: создание таблиц, индексов, очистка устаревших записей."""
    with _get_conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS gifts (
                id        TEXT PRIMARY KEY,
                name      TEXT NOT NULL,
                price     REAL NOT NULL,
                source    TEXT NOT NULL,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)
        # Ускоряем поиск и очистку по времени
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_gifts_timestamp
            ON gifts (timestamp)
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_gifts_source
            ON gifts (source)
        """)
        # Таблица для daily digest: храним каждый успешный алерт с floor_price
        # и другими метаданными, чтобы строить топы по скидке/коллекции/маркету.
        # Отделена от gifts (которая просто dedup-кэш) для производительности.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS alerts_log (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                market       TEXT NOT NULL,
                gift_id      TEXT NOT NULL,
                name         TEXT NOT NULL,
                number       TEXT,
                price        REAL NOT NULL,
                floor_price  REAL,
                discount_pct REAL,
                model_name   TEXT,
                backdrop_name TEXT,
                rarity       TEXT,
                timestamp    DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_alerts_log_ts ON alerts_log (timestamp)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_alerts_log_market ON alerts_log (market)"
        )
        conn.commit()

    # Удаляем записи старше 14 дней при каждом старте
    deleted = cleanup_old_gifts(days=14)
    if deleted:
        logger.info(f"БД: удалено {deleted} устаревших записей")
    logger.info("БД: инициализирована (WAL mode)")


def is_gift_seen(gift_id: str) -> bool:
    with _get_conn() as conn:
        cursor = conn.execute("SELECT 1 FROM gifts WHERE id = ?", (gift_id,))
        return cursor.fetchone() is not None


def add_gift(gift_id: str, name: str, price: float, source: str) -> bool:
    """Добавляет запись. Возвращает True если запись была новой."""
    with _get_conn() as conn:
        cursor = conn.execute(
            "INSERT OR IGNORE INTO gifts (id, name, price, source) VALUES (?, ?, ?, ?)",
            (gift_id, name, price, source),
        )
        conn.commit()
        return cursor.rowcount > 0


def add_gifts_bulk(rows: list[tuple[str, str, float, str]]) -> int:
    """Атомарно добавляет N записей в один транзакционный коммит.

    rows: список кортежей (gift_id, name, price, source).
    Возвращает количество вставленных строк (тех, которых не было).

    Один commit на всю партию = ~10x быстрее чем N add_gift в цикле,
    так как WAL-fsync дорогой. Используется на fast-lane чтобы не задерживать
    отправку алертов на сериализацию sqlite.
    """
    if not rows:
        return 0
    with _get_conn() as conn:
        cursor = conn.executemany(
            "INSERT OR IGNORE INTO gifts (id, name, price, source) VALUES (?, ?, ?, ?)",
            rows,
        )
        conn.commit()
        # rowcount у executemany не всегда корректен (sqlite ≤3.32 возвращает -1),
        # но для INSERT OR IGNORE на свежих версиях возвращает сумму. Если -1,
        # консервативно вернём len(rows) (worst case = «все вставились»).
        return cursor.rowcount if cursor.rowcount >= 0 else len(rows)


def cleanup_old_gifts(days: int = 14) -> int:
    """Удаляет записи старше N дней. Возвращает количество удалённых."""
    with _get_conn() as conn:
        cursor = conn.execute(
            "DELETE FROM gifts WHERE timestamp < datetime('now', ?)",
            (f"-{days} days",),
        )
        conn.commit()
        return cursor.rowcount


def log_alert(market: str, gift: dict) -> None:
    """Записывает успешно отправленный алерт в alerts_log для daily digest.

    Никогда не падает — все ошибки логируются и проглатываются. Это вспомогательная
    запись, она не должна блокировать отправку самого алерта.
    """
    try:
        floor = gift.get("floor_price")
        price = gift.get("price")
        discount_pct = None
        if floor and price and floor > 0:
            discount_pct = round((float(floor) - float(price)) / float(floor) * 100, 2)

        rarity_obj = gift.get("rarities_pm") or {}
        if isinstance(rarity_obj, dict):
            rarity_str = ",".join(
                f"{k}:{v}" for k, v in rarity_obj.items() if v is not None
            )
        else:
            rarity_str = str(rarity_obj)[:64]

        with _get_conn() as conn:
            conn.execute(
                """
                INSERT INTO alerts_log
                  (market, gift_id, name, number, price, floor_price,
                   discount_pct, model_name, backdrop_name, rarity)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    market,
                    str(gift.get("id", "")),
                    str(gift.get("name", "")),
                    str(gift.get("number") or ""),
                    float(price or 0),
                    float(floor) if floor else None,
                    discount_pct,
                    str(gift.get("model_name") or "") or None,
                    str(gift.get("backdrop_name") or "") or None,
                    rarity_str or None,
                ),
            )
            conn.commit()
    except Exception:
        logger.exception("log_alert: не удалось записать алерт в БД")


def fetch_alerts_window(hours: int = 24) -> list[dict]:
    """Возвращает все алерты за последние N часов как list[dict].
    Используется daily_digest.compute_digest().
    """
    with _get_conn() as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT market, gift_id, name, number, price, floor_price,
                   discount_pct, model_name, backdrop_name, rarity, timestamp
              FROM alerts_log
             WHERE timestamp >= datetime('now', ?)
             ORDER BY timestamp DESC
            """,
            (f"-{int(hours)} hours",),
        ).fetchall()
    return [dict(r) for r in rows]


def get_stats() -> dict:
    """Возвращает статистику по БД (используется командой /status)."""
    with _get_conn() as conn:
        total = conn.execute("SELECT COUNT(*) FROM gifts").fetchone()[0]
        by_source = conn.execute(
            "SELECT source, COUNT(*) FROM gifts GROUP BY source ORDER BY COUNT(*) DESC"
        ).fetchall()
        today = conn.execute(
            "SELECT COUNT(*) FROM gifts WHERE timestamp >= date('now')"
        ).fetchone()[0]
    return {
        "total": total,
        "today": today,
        "by_source": {row[0]: row[1] for row in by_source},
    }
