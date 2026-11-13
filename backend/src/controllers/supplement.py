from src.services import supplement as service
def list_items(dataset_id=None): return service.list_supplements(dataset_id)
def detail(id): return service.detail(id)
def create(data,user): return service.create_supplement(data,user["email"])
def version_trail(dataset_id): return service.dataset_version_trail(dataset_id)
