"""补传特性的回归测试：报告版本戳、掩码随当前值生效、历史数据集兼容、仓储行锁。"""
import os
import sys
import tempfile
import unittest

DB_FD, DB_PATH = tempfile.mkstemp(suffix=".sqlite")
os.close(DB_FD)
os.environ["DATABASE_URL"] = f"sqlite:///{DB_PATH}"
os.environ["TASK_MODE"] = "inline"
os.environ["SEED_DEMO"] = "false"

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.utils import repository as repo
from src.services import dataset as dataset_service
from src.services import supplement as supplement_service
from src.services import mask as mask_service
from src.services import report as report_service
from src.services.supplement_merge import effective_rows
from src.schemas.entities import (
    DatasetInput, SupplementInput, MaskInput, ReconstructionStudyInput, RunInput,
)

repo.init_db()

FULL_CSV = "\n".join([
    "station,time_hour,value",
    "LP-01,0,10", "LP-01,1,11", "LP-01,2,12", "LP-01,3,13", "LP-01,4,14",
])


class RegressionTest(unittest.TestCase):
    def setUp(self):
        self.ds = dataset_service.create(DatasetInput(
            name=f"回归数据集-{id(self)}", source="t.csv", csv_text=FULL_CSV))
        self.did = self.ds["id"]

    def test_mask_rejects_withdrawn_point_and_accepts_after_relate_fill(self):
        # 撤回时刻3后，包含时刻3的遮挡窗口必须被拒（当前值已缺失）
        supplement_service.create_supplement(
            SupplementInput(dataset_id=self.did, source_revision=1, name="撤回3",
                            items=[{"station": "LP-01", "time_hour": 3, "value": None, "withdrawn": True}],
                            csv_text=""), "a@x")
        with self.assertRaises(ValueError):
            mask_service.create(MaskInput(name="坏窗口", dataset_id=self.did, station="LP-01",
                                          start_hour=2.0, end_hour=4.0))
        # 更高修订号把时刻3补回后，窗口又可用
        supplement_service.create_supplement(
            SupplementInput(dataset_id=self.did, source_revision=2, name="补回3",
                            items=[{"station": "LP-01", "time_hour": 3, "value": 13.2, "withdrawn": False}],
                            csv_text=""), "a@x")
        mask = mask_service.create(MaskInput(name="好窗口", dataset_id=self.did, station="LP-01",
                                             start_hour=2.0, end_hour=4.0))
        self.assertEqual(len(mask["target_times"]), 3)

    def test_report_csv_carries_data_version(self):
        from src.services import study as study_service
        from src.services import run as run_service
        supplement_service.create_supplement(
            SupplementInput(dataset_id=self.did, source_revision=1, name="更正",
                            items=[{"station": "LP-01", "time_hour": 1, "value": 11.7, "withdrawn": False}],
                            csv_text=""), "a@x")
        mask = mask_service.create(MaskInput(name="窗口", dataset_id=self.did, station="LP-01",
                                             start_hour=2.0, end_hour=4.0))
        study = study_service.create(ReconstructionStudyInput(
            name="方案", dataset_id=self.did, mask_id=mask["id"],
            q_per_hour=0.03, observation_r=0.09, initial_variance=10000.0, initial_level=None))
        sid = study["id"]
        run = run_service.create(RunInput(study_id=sid))
        repo.put("reviews", {"id": "rev-" + run["id"], "run_id": run["id"],
                             "comment": "版本戳回归测试复核意见足够长", "status": "approved",
                             "reviewer": "admin@lupu.test", "created_at": "2026-11-13T00:00:00+00:00"})
        rpt = report_service.create(type("R", (), {
            "run_id": run["id"], "review_id": "rev-" + run["id"], "title": "版本戳报告"})())
        csv_text = report_service.download(rpt["id"])
        lines = csv_text.splitlines()
        self.assertIn("data_version", lines[0])  # 首行带 BOM 的汇总表头
        self.assertIn("source_revision", lines[0])
        self.assertIn(",1,1,", lines[1])  # 汇总数据行：data_version=1, source_revision=1

    def test_legacy_dataset_without_version_fields_still_runs(self):
        # 手工构造历史形态数据集（无 current_rows/版本字段）
        legacy_id = "ds-legacy-" + str(id(self))
        repo.put("datasets", {"id": legacy_id, "name": "历史档案", "source": "old.csv",
                              "rows": [{"station": "LP-01", "time_hour": float(t),
                                        "value": 10.0 + t, "truth": None} for t in range(6)],
                              "created_at": "2026-11-01T00:00:00+00:00"})
        # 历史数据首次补传可正常工作并惰性建立版本字段
        batch = supplement_service.create_supplement(
            SupplementInput(dataset_id=legacy_id, source_revision=1, name="历史首次补传",
                            items=[{"station": "LP-01", "time_hour": 2, "value": 99.0, "withdrawn": False}],
                            csv_text=""), "a@x")
        self.assertEqual(batch["status"], "applied")
        ds = repo.get("datasets", legacy_id)
        self.assertEqual(ds["current_version"], 1)
        self.assertEqual(effective_rows(ds)[2]["value"], 99.0)
        # 原始 rows 仍保持历史导入值
        self.assertEqual(ds["rows"][2]["value"], 12.0)

    def test_locked_row_serializes_and_rolls_back_on_error(self):
        import threading
        order = []

        def tx(mark, fail, gate):
            gate.wait()  # 先同步再竞争写锁，避免在锁内等待导致屏障死锁
            with repo.locked_row("datasets", self.did) as (conn, item):
                order.append(mark + "-in")
                item["name"] = mark
                repo.upsert_on(conn, "datasets", item)
                if fail:
                    raise RuntimeError("boom")

        gate = threading.Barrier(2)
        threads = [threading.Thread(target=tx, args=("A", False, gate)),
                   threading.Thread(target=tx, args=("B", True, gate))]
        for t in threads: t.start()
        for t in threads: t.join()
        # 写锁下两事务串行（均完整进入临界区）；失败事务回滚，成功事务 A 的提交始终保留
        self.assertEqual(sorted(order), ["A-in", "B-in"])
        self.assertEqual(repo.get("datasets", self.did)["name"], "A")
        # 异常确实向上抛出
        with self.assertRaises(RuntimeError):
            with repo.locked_row("datasets", self.did) as (conn, item):
                item["name"] = "C"
                repo.upsert_on(conn, "datasets", item)
                raise RuntimeError("rollback please")
        self.assertNotEqual(repo.get("datasets", self.did)["name"], "C")


if __name__ == "__main__":
    unittest.main(verbosity=2)
