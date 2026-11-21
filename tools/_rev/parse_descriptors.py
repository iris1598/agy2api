"""Parse embedded protobuf FileDescriptorProtos out of a Go binary.

Finds `\x0a <len> <name>.proto` occurrences (field 1 = name of
FileDescriptorProto), then decodes the message tree and pretty-prints
messages/fields/enums/services with wire field numbers.

Usage: python parse_descriptors.py <binary> <proto_name_substring> [--list]
"""
import re
import struct
import sys


def read_varint(buf, i):
    shift = 0
    val = 0
    while True:
        b = buf[i]
        i += 1
        val |= (b & 0x7F) << shift
        if not b & 0x80:
            return val, i
        shift += 7
        if shift > 63:
            raise ValueError("varint too long")


WIRE_FIXED64, WIRE_VARINT, WIRE_LEN = 1, 0, 2
WIRE_FIXED32 = 5


def parse_fields(buf, start, end):
    """Yield (field_number, wire_type, value_or_span)."""
    i = start
    while i < end:
        try:
            tag, i = read_varint(buf, i)
        except (ValueError, IndexError):
            return
        fn, wt = tag >> 3, tag & 7
        if fn == 0:
            return
        if wt == WIRE_VARINT:
            v, i = read_varint(buf, i)
            yield fn, wt, v
        elif wt == WIRE_FIXED64:
            v = struct.unpack("<Q", buf[i : i + 8])[0]
            i += 8
            yield fn, wt, v
        elif wt == WIRE_FIXED32:
            v = struct.unpack("<I", buf[i : i + 4])[0]
            i += 4
            yield fn, wt, v
        elif wt == WIRE_LEN:
            ln, i = read_varint(buf, i)
            if ln < 0 or i + ln > end:
                return
            yield fn, wt, (i, i + ln)
            i += ln
        else:
            return


def s(buf, span):
    return buf[span[0] : span[1]].decode("utf-8", "replace")


LABELS = {1: "optional", 2: "required", 3: "repeated"}
TYPES = {
    1: "double", 2: "float", 3: "int64", 4: "uint64", 5: "int32",
    6: "fixed64", 7: "fixed32", 8: "bool", 9: "string", 10: "group",
    11: "message", 12: "bytes", 13: "uint32", 14: "enum", 15: "sfixed32",
    16: "sfixed64", 17: "sint32", 18: "sint64",
}


def decode_field(buf, span, indent):
    for fn, wt, val in parse_fields(buf, span[0], span[1]):
        if fn == 1:
            print(" " * indent + f"name={s(buf, val)}")
        elif fn == 3:
            print(" " * indent + f"num={val}")
        elif fn == 4:
            print(" " * indent + f"label={LABELS.get(val, val)}")
        elif fn == 5:
            print(" " * indent + f"type={TYPES.get(val, val)}")
        elif fn == 6:
            print(" " * indent + f"type_name={s(buf, val)}")
        elif fn == 10:
            print(" " * indent + f"json_name={s(buf, val)}")
        elif fn == 7:
            print(" " * indent + f"default={s(buf, val)!r}")
        elif fn == 9:
            pass  # oneof_index
        elif fn == 17:
            print(" " * indent + f"proto3_optional")
        # ignore options etc.


def decode_enum(buf, span, indent, name):
    print(" " * indent + f"enum {name} {{")
    for fn, wt, val in parse_fields(buf, span[0], span[1]):
        if fn == 2:  # value
            vname = vnum = None
            for f2, w2, v2 in parse_fields(buf, val[0], val[1]):
                if f2 == 1:
                    vname = s(buf, v2)
                elif f2 == 2:
                    vnum = v2
            print(" " * (indent + 2) + f"{vname} = {vnum};")
    print(" " * indent + "}")


def decode_message(buf, span, indent):
    name = None
    for fn, wt, val in parse_fields(buf, span[0], span[1]):
        if fn == 1 and wt == WIRE_LEN:
            name = s(buf, val)
    print(" " * indent + f"message {name} {{")
    for fn, wt, val in parse_fields(buf, span[0], span[1]):
        if fn == 2 and wt == WIRE_LEN:  # field
            decode_field(buf, val, indent + 2)
        elif fn == 3 and wt == WIRE_LEN:  # nested_type
            decode_message(buf, val, indent + 2)
        elif fn == 4 and wt == WIRE_LEN:  # enum_type
            ename = None
            for f2, w2, v2 in parse_fields(buf, val[0], val[1]):
                if f2 == 1:
                    ename = s(buf, v2)
            decode_enum(buf, val, indent + 2, ename)
        elif fn == 8 and wt == WIRE_LEN:  # oneof_decl
            oname = None
            for f2, w2, v2 in parse_fields(buf, val[0], val[1]):
                if f2 == 1:
                    oname = s(buf, v2)
            print(" " * (indent + 2) + f"oneof {oname} {{ ... }}")
    print(" " * indent + "}")


def decode_service(buf, span, indent):
    sname = None
    for fn, wt, val in parse_fields(buf, span[0], span[1]):
        if fn == 1:
            sname = s(buf, val)
    print(" " * indent + f"service {sname} {{")
    for fn, wt, val in parse_fields(buf, span[0], span[1]):
        if fn == 2 and wt == WIRE_LEN:  # method
            mname = in_t = out_t = None
            for f2, w2, v2 in parse_fields(buf, val[0], val[1]):
                if f2 == 1:
                    mname = s(buf, v2)
                elif f2 == 2:
                    in_t = s(buf, v2)
                elif f2 == 3:
                    out_t = s(buf, v2)
            print(" " * (indent + 2) + f"rpc {mname}({in_t}) returns ({out_t});")
    print(" " * indent + "}")


def decode_file(buf, start):
    for fn, wt, val in parse_fields(buf, start, len(buf)):
        if fn == 1 and wt == WIRE_LEN:
            print(f"FILE: {s(buf, val)}")
        elif fn == 2 and wt == WIRE_LEN:
            print(f"PACKAGE: {s(buf, val)}")
        elif fn == 12:
            print(f"SYNTAX: {s(buf, val)}")
        elif fn == 4 and wt == WIRE_LEN:
            decode_message(buf, val, 2)
        elif fn == 5 and wt == WIRE_LEN:
            ename = None
            for f2, w2, v2 in parse_fields(buf, val[0], val[1]):
                if f2 == 1:
                    ename = s(buf, v2)
            decode_enum(buf, val, 2, ename)
        elif fn == 6 and wt == WIRE_LEN:
            decode_service(buf, val, 2)


def find_descriptors(buf, name):
    """Find FileDescriptorProto starting positions for `name`."""
    pat = b"\x0a" + bytes([len(name)]) + name
    out = []
    for m in re.finditer(re.escape(pat), buf):
        out.append(m.start())
    return out


def main():
    binary, needle = sys.argv[1], sys.argv[2].encode()
    list_only = "--list" in sys.argv
    data = open(binary, "rb").read()
    if list_only:
        for n in sorted(set(re.findall(rb"[a-zA-Z0-9_./]{4,150}\.proto", data))):
            if needle in n:
                print(n.decode())
        return
    starts = find_descriptors(data, needle)
    print(f"descriptor starts: {len(starts)}", file=sys.stderr)
    if not starts:
        sys.exit(1)
    decode_file(data, starts[0])


if __name__ == "__main__":
    main()
