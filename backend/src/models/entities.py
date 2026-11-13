from dataclasses import dataclass,field
from typing import Any
@dataclass
class ObservationDataset:
 id:str
 name:str
 source:str
 rows:list[dict]
 created_at:str=''
@dataclass
class GapMask:
 id:str
 name:str
 dataset_id:str
 station:str
 start_hour:float
 end_hour:float
 target_times:list[float]
 created_at:str=''
@dataclass
class ReconstructionStudy:
 id:str
 name:str
 dataset_id:str
 mask_id:str
 q_per_hour:float=.03
 observation_r:float=.09
 initial_variance:float=10000
 initial_level:float|None=None
 created_at:str=''
@dataclass
class ReconstructionRun:
 id:str
 study_id:str
 status:str='queued'
 result:dict[str,Any]=field(default_factory=dict)
 error:str=''
 created_at:str=''
@dataclass
class GapReview:
 id:str
 run_id:str
 comment:str
 status:str='pending'
 reviewer:str=''
 created_at:str=''
@dataclass
class Report:
 id:str
 run_id:str
 review_id:str
 title:str
 created_at:str=''
@dataclass
class SupplementBatch:
 id:str
 dataset_id:str
 name:str
 source_revision:int
 status:str='applied'
 note:str=''
 reject_reason:str=''
 reject_detail:list[dict]=field(default_factory=list)
 item_count:int=0
 change_count:int=0
 items:list[dict]=field(default_factory=list)
 details:list[dict]=field(default_factory=list)
 data_version_before:int=0
 data_version_after:int=0
 actor:str=''
 replayed:bool=False
 created_at:str=''
