import json, sqlite3, threading
from contextlib import contextmanager
from dataclasses import asdict
from fastapi import HTTPException
from src.config.settings import DATABASE_URL
TABLES = ("datasets", "masks", "studies", "runs", "reviews", "reports", "users", "audit", "supplements")
_LOCK = threading.RLock()
def connect():
    if DATABASE_URL.startswith("sqlite:///"):
        conn = sqlite3.connect(DATABASE_URL.removeprefix("sqlite:///"), timeout=30)
        return conn, "?"
    import psycopg
    return psycopg.connect(DATABASE_URL), "%s"
def init_db():
    conn,_ = connect()
    try:
        if not DATABASE_URL.startswith("sqlite:///"):
            conn.execute("SELECT pg_advisory_xact_lock(2309)")
        for table in TABLES:
            conn.execute(f"CREATE TABLE IF NOT EXISTS {table} (id TEXT PRIMARY KEY, payload TEXT NOT NULL)")
        conn.commit()
    finally: conn.close()
def all_rows(table):
    assert table in TABLES
    conn,_ = connect()
    try: return [json.loads(row[0]) for row in conn.execute(f"SELECT payload FROM {table} ORDER BY id").fetchall()]
    finally: conn.close()
def get(table, id):
    assert table in TABLES
    conn,q = connect()
    try:
        row = conn.execute(f"SELECT payload FROM {table} WHERE id={q}", (id,)).fetchone()
        if row is None: raise HTTPException(404, "记录不存在")
        return json.loads(row[0])
    finally: conn.close()
def put(table, item, insert_only=False):
    assert table in TABLES
    if not isinstance(item, dict): item = asdict(item)
    conn,q = connect()
    clause = "DO NOTHING" if insert_only else "DO UPDATE SET payload=excluded.payload"
    try:
        with _LOCK:
            conn.execute(f"INSERT INTO {table} (id,payload) VALUES ({q},{q}) ON CONFLICT(id) {clause}", (item["id"], json.dumps(item, ensure_ascii=False, allow_nan=False)))
            conn.commit()
        return item
    finally: conn.close()
def _dump(conn, table, item, insert_only=False):
    assert table in TABLES
    if not isinstance(item, dict): item = asdict(item)
    q = "?" if isinstance(conn, sqlite3.Connection) else "%s"
    clause = "DO NOTHING" if insert_only else "DO UPDATE SET payload=excluded.payload"
    conn.execute(f"INSERT INTO {table} (id,payload) VALUES ({q},{q}) ON CONFLICT(id) {clause}", (item["id"], json.dumps(item, ensure_ascii=False, allow_nan=False)))
    return item
def upsert_on(conn, table, item, insert_only=False):
    """在已持有连接/事务时写入（配合 locked_row 使用），不自行提交。"""
    return _dump(conn, table, item, insert_only)
def all_on(conn, table):
    assert table in TABLES
    return [json.loads(row[0]) for row in conn.execute(f"SELECT payload FROM {table} ORDER BY id").fetchall()]
@contextmanager
def locked_row(table, id):
    """串行化对同一行（同一数据集）的补传：PostgreSQL 用行级 FOR UPDATE，SQLite 用写事务锁。

    回调内通过 upsert_on/all_on 在同一连接、同一事务里读写；正常退出提交，异常回滚，
    保证读改写不会因并发补传而丢失更新。
    """
    assert table in TABLES
    conn,q = connect()
    is_sqlite = isinstance(conn, sqlite3.Connection)
    try:
        if is_sqlite:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(f"SELECT payload FROM {table} WHERE id=?", (id,)).fetchone()
        else:
            row = conn.execute(f"SELECT payload FROM {table} WHERE id={q} FOR UPDATE", (id,)).fetchone()
        if row is None:
            conn.rollback()
            raise HTTPException(404, "记录不存在")
        item = json.loads(row[0])
        yield conn,item
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
