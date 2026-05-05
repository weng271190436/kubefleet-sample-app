from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional
import uuid
import os

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
    ("transaction.daily_limit", "50000", "limits"),
    ("transaction.single_transfer_max", "10000", "limits"),
    ("transaction.international_fee_pct", "1.5", "fees"),
    ("transaction.domestic_fee", "0.50", "fees"),
    ("transaction.wire_transfer_fee", "25.00", "fees"),
    ("auth.session_timeout_min", "15", "security"),
    ("auth.max_failed_attempts", "5", "security"),
    ("auth.mfa_required", "true", "security"),
    ("auth.password_expiry_days", "90", "security"),
    ("interest.savings_apy", "4.25", "rates"),
    ("interest.checking_apy", "0.50", "rates"),
    ("interest.cd_12month_apy", "5.10", "rates"),
    ("interest.mortgage_30yr_fixed", "6.875", "rates"),
    ("compliance.kyc_verification", "enhanced", "compliance"),
    ("compliance.aml_screening", "enabled", "compliance"),
    ("compliance.pci_dss_mode", "strict", "compliance"),
    ("notification.low_balance_threshold", "100", "alerts"),
    ("notification.large_transaction_alert", "5000", "alerts"),
    ("notification.fraud_detection", "realtime", "alerts"),
    ("maintenance.next_window", "2026-05-10T02:00:00Z", "operations"),
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


@app.get("/api/cluster-info")
def cluster_info():
    return {"hostname": os.environ.get("HOSTNAME", "unknown")}
