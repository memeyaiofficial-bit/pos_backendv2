import sys
sys.path.insert(0, '.')
from schemas.schemas import RegisterIn
print(f"RegisterIn schema OK - fields: {list(RegisterIn.model_fields.keys())}")
from routers.auth import router
print("Auth router imported OK")
print(f"Routes on auth router: {[r.path for r in router.routes]}")
