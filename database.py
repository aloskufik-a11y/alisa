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


def cleanup_old_gifts(days: int = 14) -> int:
    """Удаляет записи старше N дней. Возвращает количество удалённых."""
    with _get_conn() as conn:
        cursor = conn.execute(
            "DELETE FROM gifts WHERE timestamp < datetime('now', ?)",
            (f"-{days} days",),
        )
        conn.commit()
        return cursor.rowcount


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
