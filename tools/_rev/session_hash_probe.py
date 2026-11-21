"""Try candidate 64-bit hash formulas to reproduce a target int64 from an input string.

Usage: python session_hash_probe.py <input> <target_int64>
"""
import hashlib
import struct
import sys

FNV64_OFFSET = 0xCBF29CE484222325
FNV64_PRIME = 0x100000001B3
FNV64A_OFFSET = 0xCBF29CE484222325  # same offset basis
CRC64_ECMA_POLY = 0x42F0E1EBA9EA3693
CRC64_ISO_POLY = 0xD8B16E7B168DE75D


def fnv64(data, offset=FNV64_OFFSET):
    h = offset
    for b in data:
        h = ((h ^ b) * FNV64_PRIME) & 0xFFFFFFFFFFFFFFFF
    return h


def fnv64a(data):
    h = FNV64A_OFFSET
    for b in data:
        h = ((h ^ b) * FNV64_PRIME) & 0xFFFFFFFFFFFFFFFF
    return h


def _crc64(data, poly, init=0xFFFFFFFFFFFFFFFF, xorout=0xFFFFFFFFFFFFFFFF):
    crc = init
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = (crc >> 1) ^ poly if crc & 1 else crc >> 1
    return crc ^ xorout


def crc64_ecma(data):
    # std ECMA (init 0, no xorout) variant used by Go hash/crc64 ECMA table
    crc = 0xFFFFFFFFFFFFFFFF
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = (crc >> 1) ^ CRC64_ECMA_POLY if crc & 1 else crc >> 1
    return crc ^ 0xFFFFFFFFFFFFFFFF


def crc64_iso(data):
    crc = 0xFFFFFFFFFFFFFFFF
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = (crc >> 1) ^ CRC64_ISO_POLY if crc & 1 else crc >> 1
    return crc ^ 0xFFFFFFFFFFFFFFFF


def first_digest_int64(data, algo, le=False):
    d = hashlib.new(algo, data).digest()
    v = struct.unpack("<Q", d[:8])[0] if le else struct.unpack(">Q", d[:8])[0]
    return v


def to_signed(v):
    return struct.unpack("<q", struct.pack("<Q", v & 0xFFFFFFFFFFFFFFFF))[0]


def variants(s):
    s = s.strip()
    out = {
        "raw": s,
        "lower": s.lower(),
        "upper": s.upper(),
        "nodash": s.replace("-", ""),
        "nodash_lower": s.replace("-", "").lower(),
    }
    return out


def main():
    inp, target = sys.argv[1], int(sys.argv[2])
    t = target & 0xFFFFFFFFFFFFFFFF
    algos = ["sha256", "md5", "sha1", "sha512", "blake2b"]
    fns = {
        "fnv64": fnv64,
        "fnv64a": fnv64a,
        "crc64_ecma": crc64_ecma,
        "crc64_iso": crc64_iso,
    }
    for vname, v in variants(inp).items():
        data = v.encode()
        for fname, f in fns.items():
            for signed in (False, True):
                val = f(data)
                if (to_signed(val) if signed or True else val) == target:
                    print(f"MATCH variant={vname} fn={fname}")
                if to_signed(val) == target:
                    print(f"  ^ signed match variant={vname} fn={fname}")
        for algo in algos:
            for le in (False, True):
                val = first_digest_int64(data, algo, le)
                if to_signed(val) == target:
                    print(f"MATCH variant={vname} algo={algo} le={le} (first 8)")
                val2 = first_digest_int64(data[::-1], algo, le)
                if to_signed(val2) == target:
                    print(f"MATCH variant={vname} algo={algo} le={le} reversed")
    print("done; target unsigned =", hex(t))


if __name__ == "__main__":
    main()
