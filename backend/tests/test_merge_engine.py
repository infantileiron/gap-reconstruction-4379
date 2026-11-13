"""补传合并引擎纯函数测试：修订号裁决、同版异值整批拒绝、空值不覆盖、显式撤回、幂等。"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.services.supplement_merge import (
    plan_supplement, effective_rows, point_meta, point_key,
    BATCH_APPLIED, BATCH_NOOP, BATCH_REJECTED,
    ACTION_LATE_FILL, ACTION_CORRECTED, ACTION_EMPTY_IGNORED,
    ACTION_WITHDRAWN, ACTION_REDUNDANT,
)


def base_dataset():
    rows = [
        {"station": "LP-01", "time_hour": 0.0, "value": 10.0, "truth": 10.1},
        {"station": "LP-01", "time_hour": 1.0, "value": None, "truth": 10.2},
        {"station": "LP-01", "time_hour": 2.0, "value": 12.0, "truth": 11.9},
        {"station": "LP-02", "time_hour": 0.0, "value": 5.0, "truth": 5.0},
    ]
    return {"id": "ds-x", "rows": rows, "current_rows": [dict(r) for r in rows],
            "point_meta": {}, "current_revision": 0, "current_version": 0,
            "supplement_batches": [], "created_at": "t0"}


class MergeEngineTest(unittest.TestCase):
    def test_late_fill_missing_point(self):
        ds = base_dataset()
        plan = plan_supplement(ds, 1, [{"station": "LP-01", "time_hour": 1.0, "value": 10.2, "withdrawn": False}])
        self.assertEqual(plan["status"], BATCH_APPLIED)
        self.assertEqual(plan["changes"], 1)
        self.assertEqual(plan["details"][0]["action"], ACTION_LATE_FILL)
        merged = plan["current_rows"]
        self.assertEqual(merged[1]["value"], 10.2)
        # 原始 rows 不被修改（导入原样保留）
        self.assertIsNone(ds["current_rows"][1]["value"])
        self.assertEqual(plan["point_meta"][point_key("LP-01", 1.0)]["source_revision"], 1)

    def test_correction_overwrites_observed(self):
        ds = base_dataset()
        plan = plan_supplement(ds, 1, [{"station": "LP-01", "time_hour": 2.0, "value": 12.5, "withdrawn": False}])
        self.assertEqual(plan["status"], BATCH_APPLIED)
        self.assertEqual(plan["details"][0]["action"], ACTION_CORRECTED)
        self.assertEqual(plan["current_rows"][2]["value"], 12.5)

    def test_null_does_not_overwrite_observed(self):
        ds = base_dataset()
        plan = plan_supplement(ds, 1, [{"station": "LP-01", "time_hour": 2.0, "value": None, "withdrawn": False}])
        self.assertEqual(plan["status"], BATCH_NOOP)
        self.assertEqual(plan["details"][0]["action"], ACTION_EMPTY_IGNORED)
        self.assertEqual(plan["current_rows"][2]["value"], 12.0)
        # 空值对缺失点同样不改动
        plan2 = plan_supplement(ds, 1, [{"station": "LP-01", "time_hour": 1.0, "value": None, "withdrawn": False}])
        self.assertEqual(plan2["details"][0]["action"], ACTION_EMPTY_IGNORED)
        self.assertIsNone(plan2["current_rows"][1]["value"])

    def test_explicit_withdraw_returns_to_missing(self):
        ds = base_dataset()
        plan = plan_supplement(ds, 1, [{"station": "LP-01", "time_hour": 2.0, "value": None, "withdrawn": True}])
        self.assertEqual(plan["status"], BATCH_APPLIED)
        self.assertEqual(plan["details"][0]["action"], ACTION_WITHDRAWN)
        self.assertIsNone(plan["current_rows"][2]["value"])
        # 撤回已缺失点不产生变化
        plan2 = plan_supplement(ds, 1, [{"station": "LP-01", "time_hour": 1.0, "value": None, "withdrawn": True}])
        self.assertEqual(plan2["status"], BATCH_NOOP)
        self.assertEqual(plan2["details"][0]["action"], ACTION_REDUNDANT)

    def test_revision_arbitration_higher_wins_lower_rejected(self):
        ds = base_dataset()
        p1 = plan_supplement(ds, 3, [{"station": "LP-01", "time_hour": 2.0, "value": 13.0, "withdrawn": False}])
        self.assertEqual(p1["status"], BATCH_APPLIED)
        ds["current_rows"] = p1["current_rows"]; ds["point_meta"] = p1["point_meta"]
        ds["current_revision"] = 3; ds["current_version"] = 1
        ds["supplement_batches"].append({"batch_id": "b1", "source_revision": 3, "status": "applied"})
        # 旧修订号整批拒绝
        p_old = plan_supplement(ds, 2, [{"station": "LP-01", "time_hour": 2.0, "value": 99.0, "withdrawn": False}])
        self.assertEqual(p_old["status"], BATCH_REJECTED)
        self.assertIn("低于当前最新修订", p_old["reject_reason"])
        self.assertEqual(ds["current_rows"][2]["value"], 13.0)
        # 更高修订号可以再更正
        p4 = plan_supplement(ds, 4, [{"station": "LP-01", "time_hour": 2.0, "value": 13.5, "withdrawn": False}])
        self.assertEqual(p4["status"], BATCH_APPLIED)
        self.assertEqual(p4["current_rows"][2]["value"], 13.5)

    def test_same_revision_different_values_reject_whole_batch(self):
        ds = base_dataset()
        p1 = plan_supplement(ds, 1, [
            {"station": "LP-01", "time_hour": 1.0, "value": 10.2, "withdrawn": False},
            {"station": "LP-01", "time_hour": 2.0, "value": 12.5, "withdrawn": False},
        ])
        self.assertEqual(p1["status"], BATCH_APPLIED)
        ds["current_rows"] = p1["current_rows"]; ds["point_meta"] = p1["point_meta"]
        ds["current_revision"] = 1; ds["current_version"] = 1
        ds["supplement_batches"].append({"batch_id": "b1", "source_revision": 1, "status": "applied",
                                         "items": [{"station": "LP-01", "time_hour": 1.0, "value": 10.2},
                                                   {"station": "LP-01", "time_hour": 2.0, "value": 12.5}]})
        # 同修订号、其中一个值不同：整批拒绝（包括本来相同的那条）
        p_bad = plan_supplement(ds, 1, [
            {"station": "LP-01", "time_hour": 1.0, "value": 10.2, "withdrawn": False},
            {"station": "LP-01", "time_hour": 2.0, "value": 99.9, "withdrawn": False},
        ])
        self.assertEqual(p_bad["status"], BATCH_REJECTED)
        self.assertIn("同版异值整批拒绝", p_bad["reject_reason"])
        self.assertEqual(len(p_bad["reject_detail"]), 1)
        self.assertEqual(p_bad["reject_detail"][0]["held_value"], 12.5)
        # 已落地值完全不变
        self.assertEqual(ds["current_rows"][2]["value"], 12.5)

    def test_duplicate_same_revision_same_payload_is_idempotent(self):
        ds = base_dataset()
        items = [{"station": "LP-01", "time_hour": 1.0, "value": 10.2, "withdrawn": False},
                 {"station": "LP-01", "time_hour": 2.0, "value": 12.5, "withdrawn": False}]
        p1 = plan_supplement(ds, 1, items)
        ds["current_rows"] = p1["current_rows"]; ds["point_meta"] = p1["point_meta"]
        ds["current_revision"] = 1; ds["current_version"] = 1
        ds["supplement_batches"].append({"batch_id": "b1", "source_revision": 1, "status": "applied",
                                         "items": items})
        # 引擎幂等分支（服务层会优先命中持久化批次）
        p2 = plan_supplement(ds, 1, list(reversed(items)))  # 顺序不同也应识别为同内容
        self.assertEqual(p2["status"], BATCH_NOOP)

    def test_same_revision_after_noop_batch_with_different_payload_rejected(self):
        # 前一批 noop（空值不覆盖，head 不前进），同修订号换内容仍须整批拒绝
        ds = base_dataset()
        noop_items = [{"station": "LP-01", "time_hour": 2.0, "value": None, "withdrawn": False}]
        p0 = plan_supplement(ds, 1, noop_items)
        self.assertEqual(p0["status"], BATCH_NOOP)
        history = [{"batch_id": "b0", "source_revision": 1, "status": "noop", "items": noop_items}]
        p_bad = plan_supplement(ds, 1,
                                [{"station": "LP-01", "time_hour": 2.0, "value": 99.0, "withdrawn": False}],
                                batch_history=history)
        self.assertEqual(p_bad["status"], BATCH_REJECTED)
        self.assertTrue(p_bad["reject_detail"])
        # 同内容仍幂等
        p_same = plan_supplement(ds, 1, list(noop_items), batch_history=history)
        self.assertEqual(p_same["status"], BATCH_NOOP)
        # 更高修订号正常接受
        p_new = plan_supplement(ds, 2,
                                [{"station": "LP-01", "time_hour": 1.0, "value": 11.0, "withdrawn": False}],
                                batch_history=history)
        self.assertEqual(p_new["status"], BATCH_APPLIED)

    def test_unknown_station_time_rejected(self):
        ds = base_dataset()
        with self.assertRaises(ValueError):
            plan_supplement(ds, 1, [{"station": "LP-09", "time_hour": 1.0, "value": 1.0, "withdrawn": False}])
        with self.assertRaises(ValueError):
            plan_supplement(ds, 1, [{"station": "LP-01", "time_hour": 9.0, "value": 1.0, "withdrawn": False}])

    def test_duplicate_key_inside_batch_rejected(self):
        ds = base_dataset()
        with self.assertRaises(ValueError):
            plan_supplement(ds, 1, [
                {"station": "LP-01", "time_hour": 2.0, "value": 1.0, "withdrawn": False},
                {"station": "LP-01", "time_hour": 2.0, "value": 2.0, "withdrawn": False},
            ])

    def test_withdraw_cannot_carry_value(self):
        ds = base_dataset()
        with self.assertRaises(ValueError):
            plan_supplement(ds, 1, [{"station": "LP-01", "time_hour": 2.0, "value": 1.0, "withdrawn": True}])

    def test_effective_rows_legacy_dataset_without_version_fields(self):
        legacy = {"id": "old", "rows": [{"station": "S", "time_hour": 0.0, "value": 1.0}]}
        rows = effective_rows(legacy)
        self.assertEqual(rows[0]["value"], 1.0)
        meta = point_meta(legacy)
        self.assertEqual(meta[point_key("S", 0.0)]["source_revision"], 0)
        # 历史数据集首次补传也能工作
        plan = plan_supplement(legacy, 1, [{"station": "S", "time_hour": 0.0, "value": 2.0, "withdrawn": False}])
        self.assertEqual(plan["status"], BATCH_APPLIED)


if __name__ == "__main__":
    unittest.main(verbosity=2)
