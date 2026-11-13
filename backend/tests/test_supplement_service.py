"""补传服务与运行快照集成测试：持久化、并发防丢失更新、排队任务不偷用新值、API权限。"""
import os
import sys
import tempfile
import threading
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
from src.services import study as study_service
from src.services import run as run_service
from src.services.supplement_merge import effective_rows
from src.schemas.entities import DatasetInput, SupplementInput, MaskInput, ReconstructionStudyInput, RunInput

repo.init_db()

CSV = "\n".join([
    "station,time_hour,value",
    "LP-01,0,10",
    "LP-01,1,",
    "LP-01,2,12",
    "LP-01,3,",
    "LP-01,4,14",
])


def make_dataset(name="补传测试数据集"):
    return dataset_service.create(DatasetInput(name=name, source="t.csv", csv_text=CSV))


class SupplementServiceTest(unittest.TestCase):
    def setUp(self):
        self.ds = make_dataset(f"数据集-{id(self)}")
        self.did = self.ds["id"]

    def test_apply_then_version_trail_and_persistence(self):
        batch = supplement_service.create_supplement(
            SupplementInput(dataset_id=self.did, source_revision=1, name="首批补传",
                            items=[{"station": "LP-01", "time_hour": 1, "value": 11.0, "withdrawn": False}],
                            csv_text=""),
            "analyst@lupu.test")
        self.assertEqual(batch["status"], "applied")
        self.assertEqual(batch["data_version_before"], 0)
        self.assertEqual(batch["data_version_after"], 1)
        ds = repo.get("datasets", self.did)
        self.assertEqual(effective_rows(ds)[1]["value"], 11.0)
        # 原始 rows 保持导入时的缺失
        self.assertIsNone(ds["rows"][1]["value"])
        trail = supplement_service.dataset_version_trail(self.did)
        self.assertEqual(trail["current_version"], 1)
        self.assertEqual(trail["current_revision"], 1)
        self.assertEqual(len(trail["batches"]), 1)
        listed = supplement_service.list_supplements(self.did)
        self.assertEqual(len(listed), 1)

    def test_idempotent_replay_does_not_create_new_batch_or_version(self):
        payload = dict(dataset_id=self.did, source_revision=1, name="补传",
                       items=[{"station": "LP-01", "time_hour": 1, "value": 11.0, "withdrawn": False}], csv_text="")
        b1 = supplement_service.create_supplement(SupplementInput(**payload), "a@x")
        b2 = supplement_service.create_supplement(SupplementInput(**payload), "a@x")
        self.assertFalse(b1.get("replayed"))
        self.assertTrue(b2.get("replayed"))
        self.assertEqual(b1["id"], b2["id"])
        self.assertEqual(len(supplement_service.list_supplements(self.did)), 1)
        self.assertEqual(repo.get("datasets", self.did)["current_version"], 1)

    def test_same_revision_different_value_persists_rejected_batch_and_409(self):
        from fastapi import HTTPException
        supplement_service.create_supplement(
            SupplementInput(dataset_id=self.did, source_revision=1, name="补传",
                            items=[{"station": "LP-01", "time_hour": 1, "value": 11.0, "withdrawn": False}],
                            csv_text=""), "a@x")
        with self.assertRaises(HTTPException) as ctx:
            supplement_service.create_supplement(
                SupplementInput(dataset_id=self.did, source_revision=1, name="补传冲突",
                                items=[{"station": "LP-01", "time_hour": 1, "value": 11.9, "withdrawn": False}],
                                csv_text=""), "a@x")
        self.assertEqual(ctx.exception.status_code, 409)
        batches = supplement_service.list_supplements(self.did)
        self.assertEqual(len(batches), 2)
        self.assertEqual(batches[1]["status"], "rejected")
        self.assertTrue(batches[1]["reject_detail"])
        # 生效值未被污染
        self.assertEqual(effective_rows(repo.get("datasets", self.did))[1]["value"], 11.0)

    def test_same_revision_different_payload_after_noop_still_rejected(self):
        from fastapi import HTTPException
        # 首批全空值 -> noop，head 不前进；同修订号换真实值必须整批拒绝
        b1 = supplement_service.create_supplement(
            SupplementInput(dataset_id=self.did, source_revision=1, name="空批",
                            items=[{"station": "LP-01", "time_hour": 2, "value": None, "withdrawn": False}],
                            csv_text=""), "a@x")
        self.assertEqual(b1["status"], "noop")
        with self.assertRaises(HTTPException) as ctx:
            supplement_service.create_supplement(
                SupplementInput(dataset_id=self.did, source_revision=1, name="同版换值",
                                items=[{"station": "LP-01", "time_hour": 2, "value": 99.0, "withdrawn": False}],
                                csv_text=""), "a@x")
        self.assertEqual(ctx.exception.status_code, 409)
        # 生效值未变，版本仍为0
        ds = repo.get("datasets", self.did)
        self.assertEqual(ds["current_version"], 0)
        self.assertEqual(effective_rows(ds)[2]["value"], 12.0)

    def test_stale_revision_rejected(self):
        from fastapi import HTTPException
        supplement_service.create_supplement(
            SupplementInput(dataset_id=self.did, source_revision=5, name="高修订",
                            items=[{"station": "LP-01", "time_hour": 1, "value": 11.0, "withdrawn": False}],
                            csv_text=""), "a@x")
        with self.assertRaises(HTTPException) as ctx:
            supplement_service.create_supplement(
                SupplementInput(dataset_id=self.did, source_revision=3, name="旧修订",
                                items=[{"station": "LP-01", "time_hour": 2, "value": 99.0, "withdrawn": False}],
                                csv_text=""), "a@x")
        self.assertEqual(ctx.exception.status_code, 409)
        self.assertEqual(effective_rows(repo.get("datasets", self.did))[2]["value"], 12.0)

    def test_null_ignored_and_withdraw_to_missing(self):
        # 空值不覆盖实测
        b = supplement_service.create_supplement(
            SupplementInput(dataset_id=self.did, source_revision=1, name="空值",
                            items=[{"station": "LP-01", "time_hour": 2, "value": None, "withdrawn": False}],
                            csv_text=""), "a@x")
        self.assertEqual(b["status"], "noop")
        self.assertEqual(effective_rows(repo.get("datasets", self.did))[2]["value"], 12.0)
        # 显式撤回变回缺失
        b2 = supplement_service.create_supplement(
            SupplementInput(dataset_id=self.did, source_revision=2, name="撤回",
                            items=[{"station": "LP-01", "time_hour": 2, "value": None, "withdrawn": True}],
                            csv_text=""), "a@x")
        self.assertEqual(b2["status"], "applied")
        self.assertIsNone(effective_rows(repo.get("datasets", self.did))[2]["value"])
        # 迟到读数重新填回
        b3 = supplement_service.create_supplement(
            SupplementInput(dataset_id=self.did, source_revision=3, name="迟到",
                            items=[{"station": "LP-01", "time_hour": 3, "value": 13.1, "withdrawn": False}],
                            csv_text=""), "a@x")
        self.assertEqual(b3["details"][0]["action"], "late_fill")

    def test_csv_parse_withdraw_and_empty(self):
        b = supplement_service.create_supplement(
            SupplementInput(dataset_id=self.did, source_revision=1, name="CSV补传", items=None,
                            csv_text="station,time_hour,value,withdrawn\nLP-01,1,11.5,0\nLP-01,2,,1\n"),
            "a@x")
        rows = effective_rows(repo.get("datasets", self.did))
        self.assertEqual(rows[1]["value"], 11.5)
        self.assertIsNone(rows[2]["value"])
        self.assertEqual(b["details"][1]["action"], "withdrawn")

    def test_concurrent_supplements_no_lost_update(self):
        # 修订号与线程启动顺序无关，调度乱序时旧修订被拒是正确仲裁；
        # 这里验证的是：每个测站时刻的成功补传都完整落地、互不覆盖（无丢失更新）。
        errors = []
        barrier = threading.Barrier(5)

        def worker(rev, target_hour, value):
            try:
                barrier.wait()
                supplement_service.create_supplement(
                    SupplementInput(dataset_id=self.did, source_revision=rev, name=f"并发{rev}",
                                    items=[{"station": "LP-01", "time_hour": target_hour,
                                            "value": value, "withdrawn": False}], csv_text=""),
                    f"u{rev}@x")
            except Exception as exc:  # 调度乱序导致的旧修订 409 属于预期
                errors.append(getattr(exc, "status_code", str(exc)))
        specs = [(1, 1, 21.0), (2, 3, 22.0), (3, 0, 23.0), (4, 4, 24.0), (5, 2, 25.0)]
        threads = [threading.Thread(target=worker, args=s) for s in specs]
        for t in threads: t.start()
        for t in threads: t.join()
        # 被拒的全部是“旧修订号”409，没有出现500或脏写
        self.assertTrue(all(code == 409 for code in errors))
        ds = repo.get("datasets", self.did)
        rows = effective_rows(ds)
        accepted = {b["source_revision"] for b in supplement_service.list_supplements(self.did)
                    if b["status"] == "applied"}
        # 每个成功修订对应的时刻值都精确落地
        for rev, hour, value in specs:
            if rev in accepted:
                self.assertEqual(rows[hour]["value"], value, f"修订{rev}的更新丢失")
        # 版本号连续且与接受批次数一致
        self.assertEqual(ds["current_version"], len(accepted))
        self.assertEqual(len(supplement_service.list_supplements(self.did)), 5)

    def test_concurrent_same_revision_one_wins_rest_rejected(self):
        outcomes = []
        barrier = threading.Barrier(5)

        def worker(value):
            try:
                barrier.wait()
                b = supplement_service.create_supplement(
                    SupplementInput(dataset_id=self.did, source_revision=1, name="同版竞争",
                                    items=[{"station": "LP-01", "time_hour": 2, "value": value,
                                            "withdrawn": False}], csv_text=""), f"u{value}@x")
                outcomes.append(("ok", b["status"]))
            except Exception as exc:
                outcomes.append(("err", getattr(exc, "status_code", str(exc))))
        threads = [threading.Thread(target=worker, args=(v,)) for v in (30.1, 30.2, 30.3, 30.4, 30.5)]
        for t in threads: t.start()
        for t in threads: t.join()
        oks = [o for o in outcomes if o[0] == "ok"]
        errs = [o for o in outcomes if o[0] == "err"]
        self.assertEqual(len(oks), 1)
        self.assertEqual(len(errs), 4)
        self.assertTrue(all(code == 409 for _, code in errs))
        # 胜出者的值唯一且完整落地
        val = effective_rows(repo.get("datasets", self.did))[2]["value"]
        self.assertIn(val, [30.1, 30.2, 30.3, 30.4, 30.5])


class RunSnapshotTest(unittest.TestCase):
    def setUp(self):
        # 运行/掩码测试需要连续有值的遮挡窗口，使用独立全有值数据集
        full_csv = "\n".join([
            "station,time_hour,value",
            "LP-01,0,10", "LP-01,1,11", "LP-01,2,12", "LP-01,3,13", "LP-01,4,14",
        ])
        self.ds = dataset_service.create(
            DatasetInput(name=f"运行数据集-{id(self)}", source="t.csv", csv_text=full_csv))
        self.did = self.ds["id"]
        mask = mask_service.create(MaskInput(name="遮挡", dataset_id=self.did, station="LP-01",
                                             start_hour=2.0, end_hour=4.0))
        study = study_service.create(ReconstructionStudyInput(
            name="方案", dataset_id=self.did, mask_id=mask["id"],
            q_per_hour=0.03, observation_r=0.09, initial_variance=10000.0, initial_level=None))
        self.study_id = study["id"]

    def test_run_freezes_snapshot_and_keeps_using_it_after_supplement(self):
        run = run_service.create(RunInput(study_id=self.study_id))
        self.assertEqual(run["data_version"], 0)
        self.assertIsNotNone(run["input_rows"])
        # 运行完成后再补传：版本推进，但旧 run 结果仍属于旧版本
        supplement_service.create_supplement(
            SupplementInput(dataset_id=self.did, source_revision=1, name="更正值",
                            items=[{"station": "LP-01", "time_hour": 1, "value": 77.7, "withdrawn": False}],
                            csv_text=""), "a@x")
        old = repo.get("runs", run["id"])
        self.assertEqual(old["data_version"], 0)
        self.assertEqual(old["result"]["data_version"], 0)
        # 快照里时刻1仍是发起时的旧值
        self.assertEqual(old["input_rows"][1]["value"], 11.0)
        # 新运行使用新版本数据
        run2 = run_service.create(RunInput(study_id=self.study_id))
        self.assertEqual(run2["data_version"], 1)
        self.assertEqual(run2["input_rows"][1]["value"], 77.7)
        self.assertEqual(run2["result"]["data_version"], 1)
        # 校验和随版本不同（旧结果可核查且不可被新值改写）
        self.assertNotEqual(old["result"]["checksum"], run2["result"]["checksum"])
        self.assertEqual(old["result"]["observed_count"], run2["result"]["observed_count"])

    def test_queued_run_uses_snapshot_not_live_rows(self):
        # 手动构造一个排队中的 run（模拟 Redis 任务尚未被 worker 取走）
        created = run_service.create(RunInput(study_id=self.study_id))
        queued = repo.get("runs", created["id"])
        # inline 模式已完成；重新置为排队并清空结果，模拟入队后、执行前发生补传
        queued["status"] = "queued"; queued["result"] = {}
        repo.put("runs", queued)
        # 排队期间撤回时刻3的观测：当前生效数据少一个实测
        supplement_service.create_supplement(
            SupplementInput(dataset_id=self.did, source_revision=1, name="排队期间撤回",
                            items=[{"station": "LP-01", "time_hour": 3, "value": None, "withdrawn": True}],
                            csv_text=""), "a@x")
        # worker 此时才执行：必须吃发起时快照，不能偷用新值
        executed = run_service.execute(queued["id"])
        self.assertEqual(executed["status"], "completed")
        self.assertEqual(executed["result"]["data_version"], 0)
        self.assertEqual(executed["input_rows"][3]["value"], 13.0)
        self.assertEqual(executed["result"]["observed_count"], 5)
        # 对照：数据集当前版本已推进，时刻3确为缺失
        self.assertIsNone(effective_rows(repo.get("datasets", self.did))[3]["value"])


class ApiEndToEndTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from fastapi.testclient import TestClient
        from src.main import app
        cls.client = TestClient(app)
        r = cls.client.post("/auth/login", json={"email": "analyst@lupu.test", "password": "Gap2026!"})
        # 演示账号未必存在（SEED_DEMO=false），手动建一个
        if r.status_code != 200:
            from src.utils.security import hash_password
            repo.put("users", {"id": "analyst@lupu.test", "email": "analyst@lupu.test",
                               "name": "分析员", "role": "analyst",
                               "password_hash": hash_password("Gap2026!")}, insert_only=True)
            repo.put("users", {"id": "viewer@lupu.test", "email": "viewer@lupu.test",
                               "name": "观察员", "role": "viewer",
                               "password_hash": hash_password("Gap2026!")}, insert_only=True)
            r = cls.client.post("/auth/login", json={"email": "analyst@lupu.test", "password": "Gap2026!"})
        cls.token = r.json()["access_token"]
        cls.h = {"Authorization": f"Bearer {cls.token}"}
        rv = cls.client.post("/auth/login", json={"email": "viewer@lupu.test", "password": "Gap2026!"})
        cls.viewer_h = {"Authorization": f"Bearer {rv.json()['access_token']}"}

    def test_01_anonymous_401_viewer_403_analyst_201(self):
        self.assertEqual(self.client.post("/supplements", json={}).status_code, 401)
        ds = make_dataset("API数据集")
        body = {"dataset_id": ds["id"], "source_revision": 1, "name": "API补传",
                "items": [{"station": "LP-01", "time_hour": 1, "value": 11.0, "withdrawn": False}]}
        self.assertEqual(self.client.post("/supplements", json=body, headers=self.viewer_h).status_code, 403)
        res = self.client.post("/supplements", json=body, headers=self.h)
        self.assertEqual(res.status_code, 201)
        self.assertEqual(res.json()["status"], "applied")
        # 重播返回 200
        res2 = self.client.post("/supplements", json=body, headers=self.h)
        self.assertEqual(res2.status_code, 200)
        self.assertTrue(res2.json()["replayed"])
        # 同版异值 409
        body["items"][0]["value"] = 12.3
        self.assertEqual(self.client.post("/supplements", json=body, headers=self.h).status_code, 409)
        # 版本轨迹与明细可读
        trail = self.client.get(f"/datasets/{ds['id']}/version-trail", headers=self.h)
        self.assertEqual(trail.status_code, 200)
        self.assertEqual(trail.json()["current_version"], 1)
        listing = self.client.get("/supplements", params={"dataset_id": ds["id"]}, headers=self.h)
        self.assertEqual(listing.status_code, 200)
        self.assertTrue(all("details" in b for b in listing.json()))

    def test_02_validation_422(self):
        ds = make_dataset("校验数据集")
        res = self.client.post("/supplements", headers=self.h, json={
            "dataset_id": ds["id"], "source_revision": 1, "name": "坏数据",
            "items": [{"station": "LP-09", "time_hour": 1, "value": 1.0, "withdrawn": False}]})
        self.assertEqual(res.status_code, 422)


if __name__ == "__main__":
    unittest.main(verbosity=2)
