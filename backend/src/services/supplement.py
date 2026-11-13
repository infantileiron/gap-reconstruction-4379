import csv
import io
from fastapi import HTTPException
from src.utils import repository as repo
from src.utils.identity import new_id, now
from src.constants.domain import MAX_ROWS
from src.services.supplement_merge import (
    plan_supplement, effective_rows,
    BATCH_APPLIED, BATCH_REJECTED, _same_payload,
)

_WITHDRAW_TOKENS = {"withdraw", "withdrawn", "撤回", "nulled"}


def parse_items(csv_text: str) -> list[dict]:
    """补传 CSV：station,time_hour,value（必填表头），可选 withdrawn 列；
    value 空/NA/null 为空值；withdrawn=1/true/yes 或 value=撤回标记 表示显式撤回。"""
    reader = csv.DictReader(io.StringIO(csv_text.lstrip("﻿")))
    if not {"station", "time_hour", "value"}.issubset(reader.fieldnames or []):
        raise ValueError("补传CSV需要station,time_hour,value表头；撤回可用withdrawn列或值填withdraw")
    items = []
    for n, r in enumerate(reader, 2):
        station = (r.get("station") or "").strip()
        if not station:
            raise ValueError(f"第{n}行测站为空")
        try:
            time_hour = float(r["time_hour"])
        except (KeyError, TypeError, ValueError):
            raise ValueError(f"第{n}行时刻无效")
        raw_value = (r.get("value") or "").strip()
        withdrawn_token = (r.get("withdrawn") or "").strip().lower() in ("1", "true", "yes", "y", "撤回")
        withdrawn = withdrawn_token or raw_value.lower() in _WITHDRAW_TOKENS
        if withdrawn:
            items.append({"station": station, "time_hour": time_hour, "value": None, "withdrawn": True})
            continue
        if raw_value.lower() in ("", "na", "null"):
            items.append({"station": station, "time_hour": time_hour, "value": None, "withdrawn": False})
        else:
            try:
                value = float(raw_value)
            except ValueError:
                raise ValueError(f"第{n}行数值无效")
            items.append({"station": station, "time_hour": time_hour, "value": value, "withdrawn": False})
    return items


def _coerce_items(data) -> list[dict]:
    if data.items:
        items = [x.model_dump() for x in data.items]
    elif data.csv_text.strip():
        items = parse_items(data.csv_text)
    else:
        raise ValueError("请通过items或csv_text提供至少一条补传记录")
    if not items:
        raise ValueError("补传批次不能为空")
    if len(items) > MAX_ROWS:
        raise ValueError(f"单批最多{MAX_ROWS}条补传记录")
    return items


def list_supplements(dataset_id: str | None = None):
    rows = repo.all_rows("supplements")
    if dataset_id:
        rows = [r for r in rows if r["dataset_id"] == dataset_id]
    return sorted(rows, key=lambda r: r["created_at"])


def detail(id: str):
    return repo.get("supplements", id)


def create_supplement(data, actor: str):
    items = _coerce_items(data)
    holder: dict = {}
    # 行级锁串行化同一数据集的读改写：并发补传不会互相覆盖（PostgreSQL FOR UPDATE / SQLite 写锁）
    with repo.locked_row("datasets", data.dataset_id) as (conn, dataset):
        prior_batches = [b for b in repo.all_on(conn, "supplements")
                         if b["dataset_id"] == data.dataset_id and b["status"] != BATCH_REJECTED]
        prior_same = next((b for b in reversed(prior_batches)
                           if b["source_revision"] == data.source_revision), None)
        # 幂等重播：同修订号且同内容，直接返回原批次，不写新记录、不产生新版本
        if prior_same is not None and _same_payload(prior_same["items"], items):
            holder["batch"] = dict(prior_same, replayed=True)
        else:
            plan = plan_supplement(dataset, data.source_revision, items, batch_history=prior_batches)
            batch_id = new_id("sup")
            ts = now()
            version_before = int(dataset.get("current_version", 0))
            if plan["status"] == BATCH_APPLIED:
                # 留痕里补上操作人与时间（纯引擎不接触身份/时钟）
                for m in plan["point_meta"].values():
                    if m.get("source_revision") == data.source_revision and not m.get("updated_by"):
                        m["updated_by"] = actor
                        m["updated_at"] = ts
                dataset["current_rows"] = plan["current_rows"]
                dataset["point_meta"] = plan["point_meta"]
                dataset["current_revision"] = data.source_revision
                dataset["current_version"] = version_before + 1
                dataset["updated_at"] = ts
            version_after = int(dataset.get("current_version", 0))
            batch = {
                "id": batch_id,
                "dataset_id": data.dataset_id,
                "name": data.name,
                "note": data.note,
                "source_revision": data.source_revision,
                "status": plan["status"],
                "reject_reason": plan["reject_reason"],
                "reject_detail": plan["reject_detail"],
                "item_count": len(items),
                "change_count": plan["changes"],
                "items": items,
                "details": plan["details"],
                "data_version_before": version_before,
                "data_version_after": version_after,
                "actor": actor,
                "replayed": False,
                "created_at": ts,
            }
            dataset.setdefault("supplement_batches", []).append({
                "batch_id": batch_id, "source_revision": data.source_revision,
                "status": plan["status"], "change_count": plan["changes"], "created_at": ts,
            })
            repo.upsert_on(conn, "supplements", batch)
            repo.upsert_on(conn, "datasets", dataset)
            holder["batch"] = batch
    if holder["batch"]["status"] == BATCH_REJECTED:
        raise HTTPException(409, holder["batch"]["reject_reason"])
    return holder["batch"]


def dataset_version_trail(dataset_id: str) -> dict:
    dataset = repo.get("datasets", dataset_id)
    return {
        "dataset_id": dataset_id,
        "current_version": int(dataset.get("current_version", 0)),
        "current_revision": int(dataset.get("current_revision", 0)),
        "observed": sum(r["value"] is not None for r in effective_rows(dataset)),
        "missing": sum(r["value"] is None for r in effective_rows(dataset)),
        "batches": dataset.get("supplement_batches", []),
    }
