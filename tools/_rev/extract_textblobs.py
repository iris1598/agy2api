"""Extract long human-text blobs (>min_len printable chars containing spaces)
from a binary, longest first, to help locate embedded prompt templates.

Usage: python extract_textblobs.py <binary> <out.txt> [min_len=400]
"""
import re
import sys


def main():
    binary, out_path = sys.argv[1], sys.argv[2]
    min_len = int(sys.argv[3]) if len(sys.argv) > 3 else 400
    data = open(binary, "rb").read()
    pat = re.compile(rb"[\x20-\x7e\r\n]{%d,}" % min_len)
    blobs = []
    for m in pat.finditer(data):
        t = m.group(0)
        # require real prose: spaces + lowercase + letters, not pure base64/ident
        if t.count(b" ") < 8:
            continue
        if not re.search(rb"[a-z]{3,} [a-z]{3,}", t):
            continue
        blobs.append((m.start(), t))
    blobs.sort(key=lambda x: -len(x[1]))
    with open(out_path, "w", encoding="utf-8", errors="replace", newline="\n") as w:
        for off, t in blobs:
            w.write(f"\n===== @ {off} len={len(t)} =====\n")
            w.write(t.decode("utf-8", "replace"))
            w.write("\n")
    print("blobs:", len(blobs), "->", out_path)


if __name__ == "__main__":
    main()
