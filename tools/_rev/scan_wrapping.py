"""Scan captured requests for <USER_REQUEST> wrapping / metadata section rules."""
import glob
import json
import os

CAPTURES = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "..", "antigravity_capture", "captures",
)

SECTIONS = [
    "<USER_REQUEST>",
    "<ADDITIONAL_METADATA>",
    "<USER_SETTINGS_CHANGE>",
    "<ENVIRONMENT>",
    "<COMPACTED_HISTORY>",
    "<INITIAL_USER_REQUEST>",
    "<AGENT_GENERATED_SUMMARY>",
    "<PLAN>",
]

for sess in sorted(os.listdir(CAPTURES)):
    sess_dir = os.path.join(CAPTURES, sess)
    if not os.path.isdir(sess_dir):
        continue
    files = sorted(glob.glob(os.path.join(sess_dir, "*streamGenerateContent.request.json")))
    print(f"===== {sess} ({len(files)} requests) =====")
    for fp in files:
        try:
            d = json.load(open(fp, encoding="utf-8"))
        except Exception as e:  # noqa: BLE001
            print("  load fail", fp, e)
            continue
        req = d.get("request", {})
        rid = d.get("requestId", "?")
        contents = req.get("contents", [])
        users = [c for c in contents if c.get("role") == "user"]
        n_user_turns = len(users)
        flags = []
        # analyze the LAST user turn (the one driving this request)
        last_user = users[-1]["parts"][0].get("text", "") if users else ""
        for s in SECTIONS:
            if s == "<USER_REQUEST>":
                if last_user.startswith(s) or s in last_user:
                    flags.append("USER_REQ")
            elif s in last_user:
                flags.append(s.strip("<>"))
        tail = last_user[:50].replace("\n", "\\n")
        print(f"{os.path.basename(fp)[:4]} rid={rid[-30:]} uc={len(contents):3d} ut={n_user_turns:2d} | {','.join(flags) or '-':40} | {tail!r}")
