import random,math
from src.utils import repository as repo
from src.utils.identity import now
from src.utils.security import hash_password
from src.services.statistics import analyze
from src.services.supplement_merge import plan_supplement, effective_rows

def demo_rows():
 rng=random.Random(2309);rows=[]
 for station,offset in [('LP-01',0),('LP-02',2),('LP-03',-1.2)]:
  walk=0
  for t in range(480):
   walk+=rng.gauss(0,.035);level=10+offset+1.3*math.sin(2*math.pi*t/48)+.003*t+walk;truth=round(level+rng.gauss(0,.22),5)
   missing=t<5 or 60<=t<=64 or 160<=t<=184 or t>=474 or station=='LP-02' and 330<=t<=333 or station=='LP-03' and 350<=t<=357
   rows.append({'station':station,'time_hour':float(t),'value':None if missing else truth,'truth':truth})
 return rows

def seed():
 repo.init_db()
 for role,name in [('admin','缺测复核工程师'),('analyst','测站数据分析员'),('viewer','环境观察员')]:
  email=f'{role}@lupu.test';repo.put('users',{'id':email,'email':email,'name':name,'role':role,'password_hash':hash_password('Gap2026!')},insert_only=True)
 rows=demo_rows()
 ts=now()
 # 版本化数据集：rows 保持导入原样，current_rows/point_meta 承载补传合并后的当前状态
 dataset={'id':'ds-stations','name':'三测站 · 480小时观测档案','source':'station_gaps_202611.csv','rows':rows,'current_rows':[dict(r) for r in rows],'point_meta':{},'current_revision':0,'current_version':0,'supplement_batches':[],'updated_at':'','created_at':ts}
 # 演示补传：修订1=迟到读数+更正；修订2=空值不覆盖+显式撤回（均避开LP-01的100—111遮挡窗口）
 batch1_items=[{'station':'LP-01','time_hour':60.,'value':next(r['truth'] for r in rows if r['station']=='LP-01' and r['time_hour']==60.),'withdrawn':False},{'station':'LP-01','time_hour':120.,'value':11.62,'withdrawn':False}]
 batch2_items=[{'station':'LP-01','time_hour':121.,'value':None,'withdrawn':False},{'station':'LP-02','time_hour':200.,'value':None,'withdrawn':True}]
 plan1=plan_supplement(dataset,1,batch1_items)
 assert plan1['status']=='applied'
 for m in plan1['point_meta'].values():
  if m.get('source_revision')==1 and not m.get('updated_by'): m.update(updated_by='analyst@lupu.test',updated_at=ts)
 dataset.update(current_rows=plan1['current_rows'],point_meta=plan1['point_meta'],current_revision=1,current_version=1,updated_at=ts)
 dataset['supplement_batches'].append({'batch_id':'sup-demo-1','source_revision':1,'status':'applied','change_count':plan1['changes'],'created_at':ts})
 plan2=plan_supplement(dataset,2,batch2_items)
 assert plan2['status']=='applied'
 for m in plan2['point_meta'].values():
  if m.get('source_revision')==2 and not m.get('updated_by'): m.update(updated_by='analyst@lupu.test',updated_at=ts)
 dataset.update(current_rows=plan2['current_rows'],point_meta=plan2['point_meta'],current_revision=2,current_version=2,updated_at=ts)
 dataset['supplement_batches'].append({'batch_id':'sup-demo-2','source_revision':2,'status':'applied','change_count':plan2['changes'],'created_at':ts})
 repo.put('datasets',dataset,insert_only=True)
 sup1={'id':'sup-demo-1','dataset_id':'ds-stations','name':'测站迟到读数与首版更正','note':'演示：迟到读数填缺口、更正值覆盖实测','source_revision':1,'status':'applied','reject_reason':'','reject_detail':[],'item_count':len(batch1_items),'change_count':plan1['changes'],'items':batch1_items,'details':plan1['details'],'data_version_before':0,'data_version_after':1,'actor':'analyst@lupu.test','replayed':False,'created_at':ts}
 sup2={'id':'sup-demo-2','dataset_id':'ds-stations','name':'空值忽略与显式撤回','note':'演示：空值不覆盖实测，撤回才变回缺失','source_revision':2,'status':'applied','reject_reason':'','reject_detail':[],'item_count':len(batch2_items),'change_count':plan2['changes'],'items':batch2_items,'details':plan2['details'],'data_version_before':1,'data_version_after':2,'actor':'analyst@lupu.test','replayed':False,'created_at':ts}
 repo.put('supplements',sup1,insert_only=True);repo.put('supplements',sup2,insert_only=True)
 mask={'id':'mask-block12','name':'LP-01连续遮挡100—111h','dataset_id':'ds-stations','station':'LP-01','start_hour':100.,'end_hour':111.,'target_times':[float(x) for x in range(100,112)],'created_at':now()};repo.put('masks',mask,insert_only=True)
 study={'id':'study-local-level','name':'固定q/r离线缺口复核','dataset_id':'ds-stations','mask_id':'mask-block12','q_per_hour':.03,'observation_r':.09,'initial_variance':10000.,'initial_level':None,'created_at':now()};repo.put('studies',study,insert_only=True)
 merged_rows=effective_rows(dataset)
 result=analyze(merged_rows,mask,study,data_version=2,source_revision=2)
 run={'id':'run-demo','study_id':'study-local-level','status':'completed','result':result,'error':'','dataset_id':'ds-stations','input_rows':merged_rows,'data_version':2,'source_revision':2,'created_at':now()}
 repo.put('runs',run,insert_only=True)
 repo.put('reviews',{'id':'review-demo','run_id':'run-demo','comment':'RTS仅作离线重建；共用内点MAE与P+R观测覆盖已分开核验，原始观测不覆盖。','status':'approved','reviewer':'admin@lupu.test','created_at':now()},insert_only=True)
 repo.put('reports',{'id':'report-demo','run_id':'run-demo','review_id':'review-demo','title':'芦浦测站缺口与遮挡验证复核册','created_at':now()},insert_only=True)
 return {t:len(repo.all_rows(t)) for t in ('users','datasets','masks','studies','runs','reviews','reports','supplements')}
