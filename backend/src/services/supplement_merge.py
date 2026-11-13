"""补传合并引擎：按测站+时刻合并迟到读数/更正值，以来源修订号裁决新旧。

裁决规则（需求对应）：
- 修订号大于已落地最新修订才可能产生新版本；同修订号必须整批同值，同版异值整批拒绝；
- 空值（null）不覆盖已有实测；显式撤回（withdrawn）才把实测变回缺失；
- 同修订号、同内容重复补传不产生新版本（幂等）；低于最新修订的旧批次拒绝。

本模块只做纯计算，不碰数据库，便于独立验证；事务与持久化在 services/supplement.py。
"""
from typing import Any

# 单条合并动作（写入 supplement 批次明细，供页面展示合并过程）
ACTION_LATE_FILL = "late_fill"          # 迟到读数，填补原本缺失
ACTION_CORRECTED = "corrected"          # 更正值，覆盖旧实测
ACTION_EMPTY_IGNORED = "empty_ignored"  # 空值不覆盖实测
ACTION_WITHDRAWN = "withdrawn"          # 显式撤回，变回缺失
ACTION_REDUNDANT = "redundant"          # 同值重复，无变化

# 批次状态
BATCH_APPLIED = "applied"      # 已接受并产生新版本
BATCH_NOOP = "noop"            # 已接受但无实际变化，版本不动（重复补传）
BATCH_REJECTED = "rejected"    # 整批拒绝（同版异值/旧修订号）


def point_key(station: str, time_hour: float) -> str:
    return f"{station}\u001f{time_hour!r}"


def effective_rows(dataset: dict) -> list[dict]:
    """当前生效观测行：优先 current_rows（补传后的当前值），兼容历史数据集。"""
    rows = dataset.get("current_rows")
    if rows is not None:
        return rows
    return [dict(r) for r in dataset["rows"]]


def point_meta(dataset: dict) -> dict[str, dict]:
    """每个测站+时刻的来源留痕索引，兼容历史数据集（首次补传时惰性建立）。"""
    meta = dataset.get("point_meta")
    if isinstance(meta, dict) and meta:
        return dict(meta)
    out = {}
    for r in effective_rows(dataset):
        out[point_key(r["station"], r["time_hour"])] = {
            "value": r["value"],
            "source_revision": 0,
            "updated_by": "",
            "updated_at": dataset.get("created_at", ""),
        }
    return out


def _same_payload(a: list[dict], b: list[dict]) -> bool:
    """同修订号幂等判定：忽略顺序，逐条比较三元组（撤回项不携带数值）。"""
    norm = lambda items: sorted(
        (str(x["station"]).strip(), float(x["time_hour"]),
         "withdrawn" if x.get("withdrawn") else ("null" if x.get("value") is None else float(x["value"])))
        for x in items
    )
    return norm(a) == norm(b)


def plan_supplement(dataset: dict, revision: int, items: list[dict], batch_history: list[dict] | None = None) -> dict:
    """纯函数：在给定数据集快照上规划一次补传，返回规划结果，不修改入参。

    batch_history: 该数据集已接受（非拒绝）的完整批次记录，用于同修订号裁决；
      缺失时回退到 dataset['supplement_batches']。同修订号只要历史出现过，
      内容相同才幂等放行，内容不同一律整批拒绝——即使历史批次是无变化的 noop。

    返回:
      status: applied / noop / rejected
      reject_reason / reject_detail: 整批拒绝时的原因与冲突明细
      current_rows / point_meta: 合并后的新生效行与留痕（applied 时才与入参不同）
      details: 逐条合并动作明细
      changes: 实际改变当前值的条数
    """
    current = effective_rows(dataset)
    meta = point_meta(dataset)
    head_revision = int(dataset.get("current_revision") or 0)
    history = batch_history if batch_history is not None else dataset.get("supplement_batches", [])
    index = {point_key(r["station"], r["time_hour"]): i for i, r in enumerate(current)}

    # —— 1. 形态与范围预校验（422 类，不落地）——
    seen = set()
    clean: list[dict] = []
    for n, raw in enumerate(items, 1):
        station = str(raw.get("station", "")).strip()
        if not station or len(station) > 30:
            raise ValueError(f"第{n}条测站编号无效")
        try:
            time_hour = float(raw["time_hour"])
        except (KeyError, TypeError, ValueError):
            raise ValueError(f"第{n}条时刻无效")
        key = point_key(station, time_hour)
        if key in seen:
            raise ValueError(f"第{n}条与本批其他记录测站/时刻重复：{station} @ {time_hour}")
        seen.add(key)
        withdrawn = bool(raw.get("withdrawn"))
        value = raw.get("value", None)
        if withdrawn:
            if value is not None:
                raise ValueError(f"第{n}条显式撤回不能同时携带数值")
        elif value is not None:
            try:
                value = float(value)
            except (TypeError, ValueError):
                raise ValueError(f"第{n}条数值无效")
            if value != value or value in (float("inf"), float("-inf")) or abs(value) > 1_000_000:
                raise ValueError(f"第{n}条数值须为有限值且绝对值不超过1e6")
        if key not in index:
            raise ValueError(
                f"第{n}条 {station} @ {time_hour} 在原观测档案中不存在；补传只合并既有测站时刻，不新增时点"
            )
        clean.append({"station": station, "time_hour": time_hour, "value": value, "withdrawn": withdrawn})

    # —— 2. 修订号裁决（在调用方持有的行锁内，因此判断与写入原子）——
    prior_same = next((b for b in reversed(history)
                       if b.get("source_revision") == revision), None)
    if prior_same is not None:
        # 同一修订号在历史中出现过：内容相同才幂等，否则同版异值整批拒绝
        if _same_payload(prior_same.get("items", []), clean):
            return {"status": BATCH_NOOP, "idempotent_of": prior_same.get("batch_id"),
                    "reject_reason": "", "reject_detail": [], "current_rows": current,
                    "point_meta": meta, "details": list(prior_same.get("details", [])), "changes": 0}
        conflicts = _batch_conflicts(prior_same.get("items", []), clean)
        details = [{"station": it["station"], "time_hour": it["time_hour"],
                    "old_value": None, "incoming_value": None if it["withdrawn"] else it["value"],
                    "source_revision": revision, "withdrawn": it["withdrawn"],
                    "action": "rejected_with_batch"} for it in clean]
        return {"status": BATCH_REJECTED,
                "reject_reason": f"来源修订号 {revision} 已使用过且内容不同，同版异值整批拒绝；请使用更高修订号",
                "reject_detail": conflicts, "current_rows": current, "point_meta": meta,
                "details": details, "changes": 0}
    if revision <= head_revision:
        # 修订号未在历史出现、但不高于已落地最新修订（例如被跳过的旧号）：旧批次拒绝
        return {"status": BATCH_REJECTED,
                "reject_reason": f"来源修订号 {revision} 低于当前最新修订 {head_revision}，旧批次拒绝",
                "reject_detail": [], "current_rows": current, "point_meta": meta,
                "details": [], "changes": 0}

    # —— 3. 逐点合并（更高修订号）——
    new_rows = [dict(r) for r in current]
    new_meta = {k: dict(v) for k, v in meta.items()}
    details: list[dict[str, Any]] = []
    changes = 0
    for item in clean:
        key = point_key(item["station"], item["time_hour"])
        idx = index[key]
        old_value = new_rows[idx]["value"]
        record = {"station": item["station"], "time_hour": item["time_hour"],
                  "old_value": old_value, "incoming_value": None if item["withdrawn"] else item["value"],
                  "source_revision": revision, "withdrawn": item["withdrawn"]}
        if item["withdrawn"]:
            if old_value is None:
                record["action"] = ACTION_REDUNDANT
                record["new_value"] = None
            else:
                record["action"] = ACTION_WITHDRAWN
                record["new_value"] = None
                new_rows[idx]["value"] = None
                changes += 1
        elif item["value"] is None:
            # 空值不覆盖实测（无论缺失还是有值都不改动）
            record["action"] = ACTION_EMPTY_IGNORED
            record["new_value"] = old_value
        elif old_value is None:
            record["action"] = ACTION_LATE_FILL
            record["new_value"] = item["value"]
            new_rows[idx]["value"] = item["value"]
            changes += 1
        elif old_value == item["value"]:
            record["action"] = ACTION_REDUNDANT
            record["new_value"] = old_value
        else:
            record["action"] = ACTION_CORRECTED
            record["new_value"] = item["value"]
            new_rows[idx]["value"] = item["value"]
            changes += 1
        if record["action"] in (ACTION_LATE_FILL, ACTION_CORRECTED, ACTION_WITHDRAWN):
            new_meta[key] = {"value": record["new_value"], "source_revision": revision,
                             "updated_by": "", "updated_at": ""}
        details.append(record)

    status = BATCH_APPLIED if changes else BATCH_NOOP
    return {"status": status, "idempotent_of": "", "reject_reason": "", "reject_detail": [],
            "current_rows": new_rows, "point_meta": new_meta, "details": details, "changes": changes}


def _item_value(it: dict):
    return ("withdrawn", None) if it.get("withdrawn") else ("value", it.get("value"))


def _batch_conflicts(prior_items: list[dict], incoming: list[dict]) -> list[dict]:
    """列出同一修订号下，历史批次与本次内容取值不同的测站时刻。"""
    held = {point_key(str(x["station"]).strip(), float(x["time_hour"])): _item_value(x)
            for x in prior_items}
    conflicts = []
    for it in incoming:
        key = point_key(it["station"], it["time_hour"])
        kind, incoming_value = _item_value(it)
        incoming_value = None if kind == "withdrawn" else incoming_value
        if key in held:
            held_kind, held_value = held[key]
            held_value = None if held_kind == "withdrawn" else held_value
            if held_value != incoming_value:
                conflicts.append({"station": it["station"], "time_hour": it["time_hour"],
                                  "held_value": held_value, "incoming_value": incoming_value})
    return conflicts
