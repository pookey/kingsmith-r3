"""Parse an Apple PacketLogger .pklg capture and print the ATT (GATT) traffic.

Rebuilds the handle -> UUID map from the GATT discovery in the capture itself,
falling back to our own gatt dump (captures/gatt-*.json) for handles not seen.

Usage: uv run tools/pklg_parse.py <capture.pklg> [--all-conns] [--no-td] [--jsonl out.jsonl]
"""

import argparse
import glob
import json
import struct
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from sniff import decode, label  # noqa: E402

PKT_CMD, PKT_EVT, ACL_TX, ACL_RX = 0x00, 0x01, 0x02, 0x03

ATT_OPS = {
    0x01: "ErrorRsp", 0x02: "MtuReq", 0x03: "MtuRsp", 0x04: "FindInfoReq", 0x05: "FindInfoRsp",
    0x08: "ReadByTypeReq", 0x09: "ReadByTypeRsp", 0x0A: "ReadReq", 0x0B: "ReadRsp",
    0x0C: "ReadBlobReq", 0x0D: "ReadBlobRsp", 0x10: "ReadByGroupReq", 0x11: "ReadByGroupRsp",
    0x12: "WriteReq", 0x13: "WriteRsp", 0x52: "WriteCmd", 0x1B: "Notify", 0x1D: "Indicate",
    0x1E: "Confirm",
}
BASE = "-0000-1000-8000-00805f9b34fb"


def uuid_str(b: bytes) -> str:
    if len(b) == 2:
        return f"0000{struct.unpack('<H', b)[0]:04x}{BASE}"
    if len(b) == 16:
        h = b[::-1].hex()
        return f"{h[:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:]}"
    return b.hex()


def read_records(path):
    data = Path(path).read_bytes()
    # Detect endianness from the first record length.
    be = struct.unpack_from(">I", data)[0]
    endian = ">" if 9 <= be < 0x10000 else "<"
    off = 0
    while off + 4 <= len(data):
        (ln,) = struct.unpack_from(endian + "I", data, off)
        if ln < 9 or off + 4 + ln > len(data):
            break
        secs, usecs = struct.unpack_from(endian + "II", data, off + 4)
        typ = data[off + 12]
        payload = data[off + 13: off + 4 + ln]
        yield secs + usecs / 1e6, typ, payload
        off += 4 + ln


def fallback_map():
    m = {}
    files = sorted(glob.glob("captures/gatt-*.json"))
    if files:
        for s in json.loads(Path(files[-1]).read_text())["services"]:
            for c in s["characteristics"]:
                # bleak/CoreBluetooth reports value handle - 1 (verified against an iOS
                # capture: Treadmill Data is 0x20 on the wire, bleak says 31).
                m[c["handle"] + 1] = c["uuid"]
    return m


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("pklg")
    ap.add_argument("--no-td", action="store_true", help="hide repeated Treadmill Data notifications")
    ap.add_argument("--jsonl")
    args = ap.parse_args()

    handles = {}                     # value handle -> uuid (from capture)
    fb = fallback_map()
    pending_type = {}                # (conn) -> last ReadByType UUID
    reasm = {}                       # (conn, dir) -> [expected_len, bytes]
    out = open(args.jsonl, "w") if args.jsonl else None
    last_td = None
    t0 = None

    def name_of(h):
        u = handles.get(h) or fb.get(h)
        return label(u) if u else f"h{h:#06x}"

    def on_att(ts, conn, direction, pdu):
        nonlocal last_td
        op = pdu[0]
        opn = ATT_OPS.get(op, f"op{op:#04x}")
        rel = ts - t0
        rec = {"t": round(rel, 3), "wall": datetime.fromtimestamp(ts).isoformat(), "conn": conn,
               "dir": direction, "op": opn}

        # Discovery bookkeeping
        if op == 0x08 and len(pdu) >= 7:
            pending_type[conn] = uuid_str(pdu[5:])
        elif op == 0x09 and len(pdu) >= 2 and pending_type.get(conn, "").startswith("00002803"):
            n = pdu[1]
            for i in range(2, len(pdu) - n + 1, n):
                e = pdu[i:i + n]
                vh = struct.unpack_from("<H", e, 3)[0]
                handles[vh] = uuid_str(e[5:])
                handles.setdefault(vh + 1, handles[vh] + "#cccd?")

        if op in (0x12, 0x52, 0x1B, 0x1D, 0x0A, 0x0B) or op == 0x01:
            if op == 0x0B:
                h, val = rec.get("h"), pdu[1:]
            elif op == 0x01:
                h, val = struct.unpack_from("<H", pdu, 2)[0], pdu[4:]
                rec["error"] = f"req={pdu[1]:#04x} code={pdu[4]:#04x}"
            else:
                h, val = struct.unpack_from("<H", pdu, 1)[0], pdu[3:]
            if op == 0x0A:
                val = b""
            rec.update({"handle": h, "char": name_of(h) if h is not None else None, "hex": val.hex()})
            u = handles.get(h) or fb.get(h) if h is not None else None
            if u and op in (0x1B, 0x1D, 0x12, 0x52, 0x0B):
                rec["decoded"] = decode(u.split("#")[0], val) if "#" not in u else None
            if args.no_td and u and u.startswith("00002acd") and op == 0x1B:
                if val.hex() == last_td:
                    if out:
                        out.write(json.dumps(rec) + "\n")
                    return
                last_td = val.hex()
            arrow = "->" if direction == "tx" else "<-"
            hs = f"{h:#06x}" if h is not None else "?"
            print(f"{rel:9.3f} c{conn:03x} {arrow} {opn:<9} {hs} {rec['char'] or '':<16} "
                  f"{val.hex(' ')}  {rec.get('decoded') or rec.get('error') or ''}")
        elif op in (0x02, 0x03):
            print(f"{rel:9.3f} c{conn:03x} {'->' if direction == 'tx' else '<-'} {opn} mtu={struct.unpack_from('<H', pdu, 1)[0]}")
        if out:
            out.write(json.dumps(rec, default=str) + "\n")

    for ts, typ, p in read_records(args.pklg):
        if t0 is None:
            t0 = ts
        if typ not in (ACL_TX, ACL_RX) or len(p) < 4:
            continue
        direction = "tx" if typ == ACL_TX else "rx"
        hdr, ln = struct.unpack_from("<HH", p)
        conn, pb = hdr & 0x0FFF, (hdr >> 12) & 0x3
        frag = p[4:4 + ln]
        key = (conn, direction)
        if pb == 0x01 and key in reasm:          # continuation
            reasm[key][1] += frag
        else:
            if len(frag) < 4:
                continue
            l2len = struct.unpack_from("<H", frag)[0]
            reasm[key] = [l2len, bytearray(frag)]
        need, buf = reasm[key]
        if len(buf) - 4 < need:
            continue
        del reasm[key]
        cid = struct.unpack_from("<H", buf, 2)[0]
        if cid == 0x0004 and need > 0:
            on_att(ts, conn, direction, bytes(buf[4:4 + need]))

    print("\nHandle map from capture:", file=sys.stderr)
    for h in sorted(handles):
        print(f"  {h:#06x} {handles[h]}", file=sys.stderr)


if __name__ == "__main__":
    main()
