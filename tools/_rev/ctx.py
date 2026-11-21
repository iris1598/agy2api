"""Show printable context around every occurrence of a keyword in a binary.

Usage: python ctx.py <binary> <keyword> [--before N] [--after N] [--limit N]
Non-printable bytes are rendered as newlines so adjacent Go strings stay readable.
"""
import argparse
import re
import sys


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("binary")
    ap.add_argument("keyword")
    ap.add_argument("--before", type=int, default=400)
    ap.add_argument("--after", type=int, default=400)
    ap.add_argument("--limit", type=int, default=8)
    ap.add_argument("--ignore-case", action="store_true")
    args = ap.parse_args()

    flags = re.IGNORECASE if args.ignore_case else 0
    pat = re.compile(re.escape(args.keyword.encode()), flags)
    data = open(args.binary, "rb").read()
    hits = list(pat.finditer(data))
    print(f"total hits: {len(hits)}", file=sys.stderr)
    for i, m in enumerate(hits[: args.limit]):
        s = max(0, m.start() - args.before)
        e = min(len(data), m.end() + args.after)
        chunk = data[s:e]
        text = re.sub(rb"[^\x20-\x7e]+", b"\n", chunk).decode("utf-8", "replace")
        lines = [ln for ln in text.split("\n") if ln]
        print(f"===== hit {i} @ {m.start()} =====")
        for ln in lines:
            print(ln)


if __name__ == "__main__":
    main()
