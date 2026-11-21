"""Extract an Electron .asar archive (read-only reverse-engineering helper).

Usage: python extract_asar.py <archive.asar> <out_dir>

Only reads the archive; never executes anything from it.
"""
import json
import os
import struct
import sys


def read_header(f):
    f.seek(4)
    header_size = struct.unpack("<I", f.read(4))[0]
    f.seek(12)
    json_size = struct.unpack("<I", f.read(4))[0]
    raw = f.read(json_size)
    header = json.loads(raw.decode("utf-8", "replace"))
    data_offset = 8 + header_size
    return header, data_offset


def walk(files, base, out_dir, f, data_offset):
    os.makedirs(out_dir, exist_ok=True)
    for name, node in files.items():
        path = os.path.join(out_dir, name)
        if "files" in node:
            walk(node["files"], base, path, f, data_offset)
            continue
        if node.get("unpacked"):
            # real file lives next to the asar (app.asar.unpacked mirror)
            src = os.path.join(base, "..", name + ".unpacked")
            continue
        off = data_offset + int(node.get("offset", 0))
        size = int(node.get("size", 0))
        f.seek(off)
        data = f.read(size)
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "wb") as w:
            w.write(data)


def main():
    archive, out_dir = sys.argv[1], sys.argv[2]
    base = os.path.dirname(os.path.abspath(archive))
    with open(archive, "rb") as f:
        header, data_offset = read_header(f)
        walk(header.get("files", {}), base, out_dir, f, data_offset)
    print("extracted to", out_dir)


if __name__ == "__main__":
    main()
