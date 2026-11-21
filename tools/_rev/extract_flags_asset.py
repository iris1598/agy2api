"""Extract the client's embedded default-flags JSON asset from language_server binary.

The asset is stored verbatim (pretty-printed) after a NUL-padded region:
    {\n  "base": {...}...  }\n
followed by a text-proto asset marker. Offsets shift between client versions,
so both ends are located by pattern, not byte offset.

Usage: python extract_flags_asset.py <binary> [out_json]
Output: tools/_rev/client_default_flags.json by default.
"""
import json
import os
import sys


def main():
    binary = sys.argv[1]
    default_out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "client_default_flags.json")
    out = sys.argv[2] if len(sys.argv) > 2 else default_out
    data = open(binary, "rb").read()
    start = data.find(b'{\n  "base"')
    if start < 0:
        sys.exit("asset start marker not found (client layout changed?)")
    end = data.find(b"\n}\n", start)
    if end < 0:
        sys.exit("asset end marker not found")
    blob = data[start : end + 3].decode("utf-8")
    obj = json.loads(blob)
    with open(out, "w", encoding="utf-8", newline="\n") as w:
        json.dump(obj, w, ensure_ascii=False, indent=1)
    print(f"extracted {len(blob)} bytes, top keys: {list(obj.keys())} -> {out}")


if __name__ == "__main__":
    main()
