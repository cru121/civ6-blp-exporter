"""Decode Civ6 loose ANIMATION_* files (SHARED_DATA): CIVBIG wrapper around a Granny GR2 (v7, 64-bit) animation.

File layout (verified on Babylon DLC files):
  0..47    CIVBIG wrapper header
  48..     GR2 file info (version 7), section array at +0x48 (one section); section data at 48 + section.offset
  section data = [objects, 8-aligned..][16-bit data region from section.first16bit][8-bit region from first8bit:
                  strings, type names and the curve objects of every track]; pointer fixups = (to, from, section) triples.

Curve decoding follows the published Granny curve formats (as in open-source readers, e.g. LSLib):
  fmt ids: 0 DaKeyframes32f, 1 DaK32fC32f, 2 DaIdentity, 3 DaConstant32f, 4 D3Constant32f, 5 D4Constant32f,
  6 DaK16uC16u, 7 DaK8uC8u, 8 D4nK16uC15u, 9 D4nK8uC7u, 10 D3K16uC16u, 11 D3K8uC8u, 12 D9I1K16uC16u, 13 D9I1K8uC8u,
  14 D9I3K16uC16u, 15 D9I3K8uC8u, 16 D3I1K32fC32f, 17 D3I1K16uC16u, 18 D3I1K8uC8u

Track extraction is HEURISTIC: the object graph is not walked through the file's type definitions. Instead the
8-bit region is read sequentially: a bone-name string, then that track's curve objects (rotation, position, ...);
8-bit arrays follow their object inline, 16-bit arrays are taken in object order from the 16-bit region. Identity
curves are stored once and shared. Raw float curves (fmt 1, DaK32fC32f, ~14% of files) keep their float32 knot/control arrays in the
32-bit part of the section, in object order but with unexplained gaps; they are placed by content (see
_resolve_float_curves: knots 0..duration, unit-norm quaternions, depth-first with memo) - 25 of 30 such files resolve;
the rest keep curves flagged 'unresolved'. Not supported: curve formats 0,3,6,7,12-16 (rare in the Babylon files).
"""
import struct
import numpy as np
np.seterr(divide='ignore', invalid='ignore')        # some curves have a zero knot scale; those tracks are dropped later

SCALE_TABLE = [1.4142135, 0.70710677, 0.35355338, 0.35355338, 0.35355338, 0.17677669, 0.17677669, 0.17677669,
               -1.4142135, -0.70710677, -0.35355338, -0.35355338, -0.35355338, -0.17677669, -0.17677669, -0.17677669]
OFFSET_TABLE = [-0.70710677, -0.35355338, -0.53033006, -0.17677669, 0.17677669, -0.17677669, -0.088388346, 0.0,
                0.70710677, 0.35355338, 0.53033006, 0.17677669, -0.17677669, 0.17677669, 0.088388346, -0.0]
TOKENS = {'Format', 'Degree', 'ScaleOffsetTableEntries', 'OneOverKnotScale', 'KnotsControls', 'UInt8', 'UInt16', 'Padding',
          'Controls', 'Dimension', 'OneOverKnotScaleTrunc', 'ControlScales', 'ControlOffsets', 'Knots', 'Int16', 'Real32',
          'Int32', 'UInt32', 'String', 'Transform'}


def load(path):
    d = open(path, 'rb').read()
    assert d[:6] == b'CIVBIG'
    F = 48
    sec = struct.unpack_from('<11I', d, F + 72)
    S = F + sec[1]
    return d[S:S + sec[3]], sec[5], sec[6]            # section data, first16bit, first8bit


def _f32(b, o): return struct.unpack_from('<f', b, o)[0]
def _u32(b, o): return struct.unpack_from('<I', b, o)[0]
def _u16(b, o): return struct.unpack_from('<H', b, o)[0]


def trunc_scale(u16):
    return struct.unpack('<f', struct.pack('<I', u16 << 16))[0]


def obj_size(b, o):
    """Size (header + inline array bytes) of the curve object at o, or None if it doesn't look like one.
    16-bit arrays (fmt 8, 10, 17) are not inline: they live in the 16-bit data region, in object order."""
    fmt = b[o]
    if fmt == 2: return 4
    if fmt == 4: return 16
    if fmt == 5: return 20
    if fmt == 8:
        c = _u32(b, o + 8); return 20 if 0 < c < 100000 and c % 4 == 0 else None
    if fmt == 9:
        c = _u32(b, o + 8); return 20 + c if 0 < c < 100000 and c % 4 == 0 else None
    if fmt in (10, 17):
        c = _u32(b, o + 28); return 40 if 0 < c < 100000 else None
    if fmt in (11, 18):
        c = _u32(b, o + 28); return 40 + c if 0 < c < 100000 else None
    if fmt == 1:
        kc, cc = _u32(b, o + 4), _u32(b, o + 16)
        return 28 if 0 < kc < 100000 and 0 < cc < 400000 else None
    return None


def array16_len(b, o):
    """Number of u16 elements the 16-bit-region array of object o holds (0 if none)."""
    fmt = b[o]
    if fmt == 8: return _u32(b, o + 8)
    if fmt in (10, 17): return _u32(b, o + 28)
    return 0


def decode_curve(b, o, arr16=None, arr32=None):
    """-> dict(fmt, dim, knots(np.float32[n]) or None, values(np.float32[n,dim]); constants have 1 value, identity none)."""
    fmt = b[o]
    if fmt == 2:
        return dict(fmt=2, dim=_u16(b, o + 2), knots=None, values=None)
    if fmt == 4:
        return dict(fmt=4, dim=3, knots=None, values=np.array([struct.unpack_from('<3f', b, o + 4)], np.float32))
    if fmt == 5:
        return dict(fmt=5, dim=4, knots=None, values=np.array([struct.unpack_from('<4f', b, o + 4)], np.float32))
    if fmt in (8, 9):
        sel = _u16(b, o + 2); inv = _f32(b, o + 4); cnt = _u32(b, o + 8)
        arr = arr16 if fmt == 8 else np.frombuffer(b, 'u1', count=cnt, offset=o + 20)
        n = cnt // 4
        knots = arr[:n].astype(np.float32) / inv
        sc = [SCALE_TABLE[(sel >> (4 * i)) & 15] * (0.000030518509 if fmt == 8 else 0.0078740157) for i in range(4)]
        of = [OFFSET_TABLE[(sel >> (4 * i)) & 15] for i in range(4)]
        ctl = arr[n:].astype(np.uint32).reshape(n, 3)
        q = np.zeros((n, 4), np.float32)
        a, bb, c = ctl[:, 0], ctl[:, 1], ctl[:, 2]
        if fmt == 8:
            sw1 = ((bb & 0x8000) >> 14) | (c >> 15); mask = 0x7FFF; signbit = (a & 0x8000) != 0
        else:
            sw1 = ((bb & 0x80) >> 6) | ((c & 0x80) >> 7); mask = 0x7F; signbit = (a & 0x80) != 0
        sw2, sw3, sw4 = (sw1 + 1) & 3, (sw1 + 2) & 3, (sw1 + 3) & 3
        scn, ofn = np.array(sc, np.float32), np.array(of, np.float32)
        A = (a & mask) * scn[sw2] + ofn[sw2]
        B = (bb & mask) * scn[sw3] + ofn[sw3]
        C = (c & mask) * scn[sw4] + ofn[sw4]
        D = np.sqrt(np.clip(1 - A * A - B * B - C * C, 0, None)); D = np.where(signbit, -D, D)
        idx = np.arange(n)
        q[idx, sw1] = D; q[idx, sw2] = A; q[idx, sw3] = B; q[idx, sw4] = C
        return dict(fmt=fmt, dim=4, knots=knots, values=q)
    if fmt in (10, 11, 17, 18):
        inv = trunc_scale(_u16(b, o + 2))
        scales = np.array(struct.unpack_from('<3f', b, o + 4), np.float32)
        offsets = np.array(struct.unpack_from('<3f', b, o + 16), np.float32)
        cnt = _u32(b, o + 28)
        arr = arr16 if fmt in (10, 17) else np.frombuffer(b, 'u1', count=cnt, offset=o + 40)
        if fmt in (10, 11):
            n = cnt // 4
            knots = arr[:n].astype(np.float32) / inv
            v = arr[n:].astype(np.float32).reshape(n, 3) * scales + offsets
        else:
            n = cnt // 2
            knots = arr[:n].astype(np.float32) / inv
            v = arr[n:].astype(np.float32)[:, None] * scales + offsets
        return dict(fmt=fmt, dim=3, knots=knots, values=v.astype(np.float32))
    if fmt == 1:
        kc, cc = _u32(b, o + 4), _u32(b, o + 16)
        knots, ctl = arr32
        dim = cc // kc if kc else 0
        return dict(fmt=1, dim=dim, knots=knots.copy(), values=ctl.reshape(kc, dim).copy())
    raise ValueError('unsupported curve format %d' % fmt)


def _resolve_float_curves(b, f32curves, duration, limit):
    """DaK32fC32f arrays (float32 knots then controls, per object) sit in the 32-bit part of the section, in object order,
    each followed by a little zero padding. The first array is found by scanning; each next one is searched in a short window
    after the previous array's end and accepted by content: knots non-decreasing within 0..duration, controls finite and,
    for rotations, unit quaternions (positions: smoothest candidate). If any object can't be placed all stay 'unresolved'."""
    if not f32curves:
        return
    fl = np.frombuffer(b[:limit // 4 * 4], '<f4')
    spec = [(_u32(b, o + 4), _u32(b, o + 16)) for _, _, o in f32curves]

    def score(p, kc, cc):
        """None if (p) can't start this array, else a badness number (lower = better)."""
        if p + kc + cc > len(fl):
            return None
        k, c = fl[p:p + kc], fl[p + kc:p + kc + cc]
        if not (np.isfinite(k).all() and np.isfinite(c).all()):
            return None
        if k[0] < -1e-4 or k[0] > duration or (np.diff(k) < -1e-6).any() or k[-1] > duration + 0.02 or k[-1] <= 0:
            return None
        dim = cc // kc if kc else 0
        c = c.reshape(kc, dim)
        if dim == 4:
            bad = 1 - (abs(np.linalg.norm(c, axis=1) - 1) < 0.15).mean()
            return bad if bad < 0.35 else None
        rng = np.ptp(c, axis=0).max() + 1e-3
        return float(np.abs(np.diff(c, axis=0)).mean() / rng) if kc > 1 else 0.0

    def candidates(n, cursor):
        kc, cc = spec[n]
        rng = range(0, len(fl) - kc - cc) if cursor is None else range(cursor, min(cursor + 256, len(fl) - kc - cc))
        out = []
        for p in rng:
            if cursor is None and fl[p] != 0.0:        # a curve's knot vector starts at time 0 (possibly repeated)
                continue
            sc = score(p, kc, cc)
            if sc is not None:
                out.append((sc, p))
        out.sort(key=lambda t: (round(t[0], 3), t[1]))
        return out[:3]

    memo = {}

    def place(n, cursor):                       # depth-first with memo: a wrong early guess shows up as a dead end later
        if n == len(spec):
            return []
        key = (n, cursor)
        if key not in memo:
            memo[key] = None
            for sc, p in candidates(n, cursor):
                rest = place(n + 1, p + spec[n][0] + spec[n][1])
                if rest is not None:
                    memo[key] = [p] + rest
                    break
        return memo[key]

    placed = place(0, None)
    if placed is None:
        for lst, idx, o in f32curves:
            kc, cc = _u32(b, o + 4), _u32(b, o + 16)
            lst[idx] = dict(fmt=1, dim=cc // kc if kc else 0, knots=None, values=None, unresolved=True)
        return
    for (lst, idx, o), p, (kc, cc) in zip(f32curves, placed, spec):
        lst[idx] = decode_curve(b, o, None, (fl[p:p + kc].copy(), fl[p + kc:p + kc + cc].copy()))


def parse_animation(path):
    b, first16, first8 = load(path)
    duration, timestep = _f32(b, 132), _f32(b, 136)
    end = b.find(b'FxsArtToolInfo')
    o = (first8 - 4) & ~3                                 # the 8-bit region starts with the first track name
    tracks, cur, cursor16 = [], None, first16 - 32      # the 16-bit arrays start 32 bytes before the section's first16bit marker
    f32curves = []                                        # (curve slot, offset): float arrays live at the end of the 32-bit region
    while o < end:
        e = b.find(b'\0', o)
        s = b[o:e]
        if len(s) >= 3 and all(32 <= c < 127 for c in s) and s[:1].isalpha():
            txt = s.decode('latin1')
            o = (e + 4) & ~3
            if txt.startswith('CurveDataHeader_') or txt in TOKENS:
                continue
            cur = dict(name=txt, curves=[])
            tracks.append(cur)
            continue
        sz = obj_size(b, o) if b[o] <= 18 and b[o + 1] <= 4 else None
        if sz and cur is not None:
            n16 = array16_len(b, o)
            a16 = None
            if n16:
                a16 = np.frombuffer(b, '<u2', count=n16, offset=cursor16); cursor16 += n16 * 2
            if b[o] == 1:
                cur['curves'].append(None); f32curves.append((cur['curves'], len(cur['curves']) - 1, o))
            else:
                cur['curves'].append(decode_curve(b, o, a16))
            o = (o + sz + 3) & ~3
            continue
        o += 4
    _resolve_float_curves(b, f32curves, duration, first16 - 32)
    return dict(name=path.replace('\\', '/').split('/')[-1], duration=duration, timeStep=timestep, tracks=tracks,
                used16=cursor16 - (first16 - 32), size16=first8 - first16)


def classify(track):
    """-> dict(rot=curve|None, pos=curve|None, scale=curve|None) from the track's curve objects by dimension."""
    out = dict(rot=None, pos=None, scale=None)
    for c in track['curves']:
        dim = c['dim']
        key = 'rot' if dim == 4 else 'pos' if dim == 3 else 'scale' if dim == 9 else None
        if key and out[key] is None:
            out[key] = c
    return out


if __name__ == '__main__':
    import sys
    a = parse_animation(sys.argv[1])
    print(a['name'], 'duration', a['duration'], 'step', a['timeStep'], 'tracks', len(a['tracks']), '16-bit bytes used/avail', a['used16'], a['size16'])
    for t in a['tracks'][:80]:
        k = classify(t)
        print('%-22s' % t['name'], ' '.join('%s:%s' % (n, ('fmt%d/%d' % (c['fmt'], 0 if c['knots'] is None else len(c['knots']))) if c else '-') for n, c in k.items()), '(%d curves)' % len(t['curves']))
