from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional
import uuid

app = FastAPI(title="KubeFleet Sample App - Config API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# In-memory store — swap to a database later
configs: dict[str, dict] = {}

# Seed some sample data
for i, (key, value, category) in enumerate([
    ("app.name", "kubefleet-sample", "general"),
    ("app.version", "1.0.0", "general"),
    ("log.level", "info", "logging"),
    ("log.format", "json", "logging"),
    ("db.host", "postgres.default.svc.cluster.local", "database"),
    ("db.port", "5432", "database"),
    ("cache.ttl", "300", "performance"),
    ("cache.max_size", "1000", "performance"),
], start=1):
    row_id = str(uuid.uuid4())
    configs[row_id] = {"id": row_id, "key": key, "value": value, "category": category}


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
    return list(configs.values())


@app.post("/api/configs", status_code=201)
def create_config(item: ConfigItem):
    row_id = str(uuid.uuid4())
    row = {"id": row_id, **item.model_dump()}
    configs[row_id] = row
    return row


@app.put("/api/configs/{config_id}")
def update_config(config_id: str, item: ConfigUpdate):
    if config_id not in configs:
        raise HTTPException(status_code=404, detail="Config not found")
    existing = configs[config_id]
    update_data = item.model_dump(exclude_unset=True)
    existing.update(update_data)
    configs[config_id] = existing
    return existing


@app.delete("/api/configs/{config_id}", status_code=204)
def delete_config(config_id: str):
    if config_id not in configs:
        raise HTTPException(status_code=404, detail="Config not found")
    del configs[config_id]


@app.get("/healthz")
def health():
    return {"status": "ok"}
