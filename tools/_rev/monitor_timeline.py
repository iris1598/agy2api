"""重建 163547 会话的请求时间线:agent / agent_monitoring / checkpoint 交替形态。"""
import glob
import json
import os
from datetime import datetime

CAP = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "..", "antigravity_capture", "captures",
)
sess = os.path.join(CAP, "20261121-163547")

rows = []
for fp in sorted(glob.glob(os.path.join(sess, "*_streamGenerateContent.request.meta.json"))):
    meta = json.load(open(fp, encoding="utf-8"))
    num = os.path.basename(fp)[:4]
    req_file = fp.replace(".request.meta.json", ".request.json")
    req = json.load(open(req_file, encoding="utf-8"))
    rt = req.get("requestType", "?")
    model = req.get("model", "?")
    uc = len(req.get("request", {}).get("contents", []))
    ts = datetime.fromisoformat(meta["received_at"])
    rows.append((num, ts, rt, model, uc))

prev = None
for num, ts, rt, model, uc in rows:
    gap = (ts - prev).total_seconds() if prev else 0
    prev = ts
    model_short = model.replace("gemini-", "g").replace("claude-", "c")
    print(f"{num} +{gap:6.1f}s {rt:17s} {model_short:22s} uc={uc}")
