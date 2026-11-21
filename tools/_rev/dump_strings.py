"""Dump printable strings from a binary (Go PE here) to a text file.

Reads the untrusted binary only; writes plain-text output for grepping.
Usage: python dump_strings.py <binary> <out.txt> [min_len]
"""
import mmap
import re
import sys


def main():
    binary, out_path = sys.argv[1], sys.argv[2]
    min_len = int(sys.argv[3]) if len(sys.argv) > 3 else 6
    pat = re.compile(rb"[\x20-\x7e\r\n\t]{%d,}" % min_len)
    with open(binary, "rb") as f:
        mm = mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ)
        with open(out_path, "w", encoding="utf-8", errors="replace", newline="\n") as w:
            pos = 0
            for m in pat.finditer(mm):
                w.write(m.group(0).decode("utf-8", "replace"))
                w.write("\n")
        mm.close()
    print("done", out_path)


if __name__ == "__main__":
    main()
