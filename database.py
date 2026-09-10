import hashlib
import hmac
import os
import secrets
import sqlite3


DB_NAME = os.getenv("CHAT2ORDER_DB", "chat2order.db")
PASSWORD_ITERATIONS = 310_000


def hash_password(password: str) -> str:
    """Hash a password with a per-password salt using the stdlib only."""
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, PASSWORD_ITERATIONS
    )
    return f"pbkdf2_sha256${PASSWORD_ITERATIONS}${salt.hex()}${digest.hex()}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, iterations, salt, expected = encoded.split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        digest = hashlib.pbkdf2_hmac(
            "sha256",
            password.encode("utf-8"),
            bytes.fromhex(salt),
            int(iterations),
        )
        return hmac.compare_digest(digest.hex(), expected)
    except (ValueError, TypeError):
        return False


def _columns(conn, table: str) -> set[str]:
    return {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}


def _add_column(conn, table: str, definition: str) -> None:
    name = definition.split()[0]
    if name not in _columns(conn, table):
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {definition}")


def _ensure_schema(conn):
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS stores (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL UNIQUE,
            password_hash TEXT NOT NULL,
            store_id INTEGER,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    # Keep the old tables and add fields in place so existing SQLite databases
    # and their data remain usable.
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS products (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            price REAL DEFAULT 0,
            store_id INTEGER NOT NULL,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            customer_message TEXT,
            items_json TEXT,
            status TEXT DEFAULT 'Chờ xác nhận',
            store_id INTEGER NOT NULL,
            total_amount REAL DEFAULT 0,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS line_user_stores (
            line_user_id TEXT PRIMARY KEY,
            store_id INTEGER NOT NULL,
            display_name TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS line_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id TEXT UNIQUE,
            line_user_id TEXT NOT NULL,
            store_id INTEGER,
            message_text TEXT NOT NULL,
            reply_text TEXT,
            order_id INTEGER,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    _add_column(conn, "users", "store_id INTEGER")
    _add_column(conn, "products", "store_id INTEGER")
    _add_column(conn, "orders", "store_id INTEGER")
    _add_column(conn, "orders", "total_amount REAL DEFAULT 0")
    _add_column(conn, "stores", "subscription_plan TEXT DEFAULT 'free'")
    _add_column(conn, "stores", "subscription_status TEXT DEFAULT 'active'")
    _add_column(conn, "stores", "subscription_expires_at TEXT")
    conn.execute(
        """
        UPDATE stores
        SET subscription_plan='free',
            subscription_status='active'
        WHERE subscription_plan IS NULL OR subscription_status IS NULL
        """
    )

    default_store = conn.execute("SELECT id FROM stores ORDER BY id LIMIT 1").fetchone()
    if default_store is None:
        cur.execute("INSERT INTO stores (name) VALUES (?)", ("ร้านค้าเริ่มต้น",))
        default_store_id = cur.lastrowid
    else:
        default_store_id = default_store["id"]

    # Legacy rows had no owner. Assign them to the first store before serving
    # requests, rather than exposing them to every newly registered account.
    conn.execute(
        "UPDATE users SET store_id=? WHERE store_id IS NULL", (default_store_id,)
    )
    conn.execute(
        "UPDATE products SET store_id=? WHERE store_id IS NULL", (default_store_id,)
    )
    conn.execute(
        "UPDATE orders SET store_id=? WHERE store_id IS NULL", (default_store_id,)
    )

    # Preserve the previous development login as a real, persisted user.
    username = os.getenv("CHAT2ORDER_USERNAME", "admin").strip()
    password = os.getenv("CHAT2ORDER_PASSWORD", "admin123")
    if username and conn.execute(
        "SELECT 1 FROM users WHERE username=?", (username,)
    ).fetchone() is None:
        cur.execute(
            "INSERT INTO users (username, password_hash, store_id) VALUES (?, ?, ?)",
            (username, hash_password(password), default_store_id),
        )
    conn.commit()


def get_conn():
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    _ensure_schema(conn)
    return conn


def init_db():
    conn = get_conn()
    conn.close()
