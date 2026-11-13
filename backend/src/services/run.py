from src.models.entities import ReconstructionRun
from src.utils import repository as repo
from src.utils.identity import new_id, now
from src.services.statistics import analyze
from src.services.supplement_merge import effective_rows
from src.config.settings import TASK_MODE, REDIS_URL, ARTIFACT_DIR
import json
def list_runs():
    # 列表轮询不需要发起时的完整观测快照，剥离 input_rows 避免响应随观测行数翻倍
    return [{k: v for k, v in r.items() if k != "input_rows"} for r in repo.all_rows("runs")]
def execute(id):
    run=repo.get("runs",id)
    if run["status"] in ("running","completed"): return run
    run["status"]="running"; repo.put("runs",run)
    try:
        exp=repo.get("studies",run["study_id"])
        mask=repo.get("masks",exp["mask_id"])
        # 优先使用发起时冻结的观测快照；历史运行无快照时回退到数据集当前值
        if run.get("input_rows") is not None:
            rows=run["input_rows"]
            dataset_id=run.get("dataset_id", exp["dataset_id"])
            data_version=run.get("data_version", 0)
            source_revision=run.get("source_revision", 0)
            if exp["dataset_id"]!=dataset_id or mask["dataset_id"]!=dataset_id:
                raise ValueError("运行快照与方案/掩码数据集不一致，拒绝执行")
        else:
            dataset=repo.get("datasets",exp["dataset_id"])
            rows=effective_rows(dataset)
            data_version=int(dataset.get("current_version",0))
            source_revision=int(dataset.get("current_revision",0))
        run["result"]=analyze(rows,mask,exp,data_version=data_version,source_revision=source_revision)
        ARTIFACT_DIR.mkdir(parents=True,exist_ok=True)
        (ARTIFACT_DIR / (id+".json")).write_text(json.dumps(run["result"],ensure_ascii=False))
        run["status"]="completed"
    except Exception as exc:
        run["status"]="failed"; run["error"]=str(exc)
    return repo.put("runs",run)
def create(data):
    exp=repo.get("studies",data.study_id)
    mask=repo.get("masks",exp["mask_id"])
    if mask["dataset_id"]!=exp["dataset_id"]:
        raise ValueError("掩码与研究数据集必须一致")
    if sum(r["status"] in ("queued","running") for r in repo.all_rows("runs"))>=10: raise ValueError("运行队列已满，请等待已有任务完成")
    dataset=repo.get("datasets",exp["dataset_id"])
    # 重建固定使用发起那一刻的数据版本：快照随运行持久化，之后补传不影响排队/在途任务
    snapshot_rows=effective_rows(dataset)
    run=ReconstructionRun(new_id("run"),data.study_id,created_at=now())
    payload={
        **run.__dict__,
        "dataset_id":exp["dataset_id"],
        "input_rows":snapshot_rows,
        "data_version":int(dataset.get("current_version",0)),
        "source_revision":int(dataset.get("current_revision",0)),
    }
    run=repo.put("runs",payload)
    if TASK_MODE=="inline": return execute(run["id"])
    try:
        import redis
        redis.Redis.from_url(REDIS_URL).rpush("gap-reconstruction:jobs",run["id"])
    except Exception:
        run["status"]="failed"; run["error"]="队列不可用，请恢复Redis后重新提交"
        repo.put("runs",run)
        raise ValueError(run["error"])
    return run
