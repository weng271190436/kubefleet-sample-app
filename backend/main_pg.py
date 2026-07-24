from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional
import uuid
import os
import psycopg2
import psycopg2.extras
import time

app = FastAPI(title="KubeFleet Sample App - Config API (PostgreSQL)")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

DB_HOST = os.environ.get("DB_HOST", "localhost")
DB_PORT = os.environ.get("DB_PORT", "5432")
DB_NAME = os.environ.get("DB_NAME", "appdb")
DB_USER = os.environ.get("DB_USER", "postgres")
DB_PASSWORD = os.environ.get("DB_PASSWORD", "postgres")
READ_ONLY = os.environ.get("READ_ONLY", "false").lower() == "true"


def get_conn():
    """Get a database connection with retry."""
    for attempt in range(10):
        try:
            conn = psycopg2.connect(
                host=DB_HOST,
                port=DB_PORT,
                dbname=DB_NAME,
                user=DB_USER,
                password=DB_PASSWORD,
            )
            conn.autocommit = True
            return conn
        except psycopg2.OperationalError:
            if attempt < 9:
                time.sleep(2)
            else:
                raise


class ConfigItem(BaseModel):
    key: str
    value: str
    category: Optional[str] = "general"


class ConfigUpdate(BaseModel):
    key: Optional[str] = None
    value: Optional[str] = None
    category: Optional[str] = None


@app.get("/api/configs")
def list_configs():
    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT id::text, key, value, category FROM configs ORDER BY key")
            return cur.fetchall()
    finally:
        conn.close()


@app.post("/api/configs", status_code=201)
def create_config(item: ConfigItem):
    if READ_ONLY:
        raise HTTPException(status_code=403, detail="This replica is read-only")
    conn = get_conn()
    try:
        row_id = str(uuid.uuid4())
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "INSERT INTO configs (id, key, value, category) VALUES (%s, %s, %s, %s) RETURNING id::text, key, value, category",
                (row_id, item.key, item.value, item.category),
            )
            return cur.fetchone()
    finally:
        conn.close()


@app.put("/api/configs/{config_id}")
def update_config(config_id: str, item: ConfigUpdate):
    if READ_ONLY:
        raise HTTPException(status_code=403, detail="This replica is read-only")
    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT id::text, key, value, category FROM configs WHERE id = %s", (config_id,))
            existing = cur.fetchone()
            if not existing:
                raise HTTPException(status_code=404, detail="Config not found")
            update_data = item.model_dump(exclude_unset=True)
            if update_data:
                sets = ", ".join(f"{k} = %s" for k in update_data.keys())
                cur.execute(
                    f"UPDATE configs SET {sets} WHERE id = %s RETURNING id::text, key, value, category",
                    (*update_data.values(), config_id),
                )
                return cur.fetchone()
            return existing
    finally:
        conn.close()


@app.delete("/api/configs/{config_id}", status_code=204)
def delete_config(config_id: str):
    if READ_ONLY:
        raise HTTPException(status_code=403, detail="This replica is read-only")
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM configs WHERE id = %s", (config_id,))
            if cur.rowcount == 0:
                raise HTTPException(status_code=404, detail="Config not found")
    finally:
        conn.close()


@app.get("/healthz")
def health():
    try:
        conn = get_conn()
        conn.close()
        return {"status": "ok", "role": "replica" if READ_ONLY else "primary"}
    except Exception as e:
        raise HTTPException(status_code=503, detail=str(e))


@app.get("/api/cluster-info")
def cluster_info():
    return {
        "hostname": os.environ.get("HOSTNAME", "unknown"),
        "cluster": os.environ.get("CLUSTER_NAME", ""),
        "role": "replica (read-only)" if READ_ONLY else "primary (read-write)",
        "db_host": DB_HOST,
    }
