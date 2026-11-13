"""Offline import probe. Never initialize clients, devices, or model requests."""
import importlib
import json

missing = []
for name in ('openai', 'httpx', 'PIL.Image', 'numpy', 'cv2'):
    try:
        importlib.import_module(name)
    except Exception:
        missing.append(name)
print(json.dumps({'ok': not missing, 'missing': missing}))
raise SystemExit(1 if missing else 0)
