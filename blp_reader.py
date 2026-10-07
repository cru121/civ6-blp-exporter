"""Minimal reader for Civ6 .blp files (2026 install, header version 2).

Built on the Fandom wiki spec (see civ6-blp-spec-fandom.md) plus reverse-engineering of hero_buildings.blp:

  file = 32-byte header | 512-byte signature (offset 72..583) | zero padding |
         [package A: type info]  [package B: main]  | bigData (VB/IB/...) at header.dataOffset

  Each package = stripe0 (block data) + stripe1 (temp data: root obj, strings) + PackageAllocation table
  (40-byte records, see wiki).  Pointers are ptr64 = allocation index + 1.
  Package A (type info) : stripe0 starts right after the root-type-name string that follows the package header
                          (hero_buildings.blp: 1228), stripe1 follows stripe0, table follows stripe1.
  Package B (main)      : stripe0 starts where A's table ends, stripe1 follows, table at end of table region.

Locating the stripes is heuristic (scan for chains of plausible PackageAllocation records, then find the
stripe1 base by matching NUL-terminated char[] allocations). Works on hero_buildings.blp; untested elsewhere.
"""
import struct, sys


class Pkg:
    def __init__(self, data, table, count, s0, s1):
        self.d = data
        self.al = []
        for i in range(count):
            q = table + 40 * i
            st, ty = data[q], data[q + 1]
            par, = struct.unpack_from('<H', data, q + 6)
            off, sz, cnt = struct.unpack_from('<III', data, q + 8)
            ud, tn = struct.unpack_from('<QQ', data, q + 24)
            self.al.append(dict(i=i, stripe=st, ty=ty, par=par, off=off, size=sz, cnt=cnt, ud=ud, tn=tn))
        self.base = {0: s0, 1: s1}
        for a in self.al:   # resolve type names (String::Global -> char[] alloc in stripe 1)
            a['tname'] = None
            if a['tn'] and a['tn'] - 1 < count:
                t = self.al[a['tn'] - 1]
                if t['stripe'] == 1:
                    o = s1 + t['off']
                    a['tname'] = data[o:o + t['size'] - 1].decode('latin1')

    def addr(self, ptr):
        if not ptr:
            return None
        a = self.al[ptr - 1]
        return self.base[a['stripe']] + a['off']

    def u64(self, o):
        return struct.unpack_from('<Q', self.d, o)[0]

    def cstr(self, ptr):
        if not ptr:
            return None
        o = self.addr(ptr)
        return self.d[o:self.d.find(b'\0', o)].decode('latin1')

    def vec(self, o):
        """Types::Vector / BLP::BLPVector: ptr64, u32 count (24 bytes). Returns (address, count)."""
        p, n = struct.unpack_from('<QI', self.d, o)
        return (self.addr(p) if p else None), n

    def bstr(self, o):
        """String::BasicT at address o: ptr64 -> Storage{u32 cap,u32 len,chars}."""
        p = self.u64(o)
        if not p:
            return None
        a = self.addr(p)
        ln, = struct.unpack_from('<I', self.d, a + 4)
        return self.d[a + 8:a + 8 + ln].decode('latin1')


def _plausible(d, q, need_size=True):
    if q + 40 > len(d) or d[q] > 1 or d[q + 1] > 8 or d[q + 2:q + 6] != b'\0\0\0\0':
        return False
    off, sz, cnt = struct.unpack_from('<III', d, q + 8)
    ud, tn = struct.unpack_from('<QQ', d, q + 24)
    return d[q + 20:q + 24] == b'\0\0\0\0' and tn < (1 << 22) and off < 0x10000000 and sz < 0x10000000 and ud < (1 << 40) and (sz > 0 or not need_size)


def _candidates(d, start, end):
    """Cheap numpy prefilter: offsets q that could start a PackageAllocation record (byte-pattern test only)."""
    import numpy as np
    cnt = min(end - start + 40, len(d) - start)
    a = np.frombuffer(d, np.uint8, count=cnt, offset=start)
    n = max(cnt - 40, 0)
    m = (a[0:n] <= 1) & (a[1:n + 1] <= 8)
    for k in (2, 3, 4, 5, 20, 21, 22, 23, 29, 30, 31, 35, 36, 37, 38, 39):
        m &= a[k:k + n] == 0
    m &= a[34:34 + n] < 0x40            # type-name index tn < 1<<22 (Base units.blp has > 32k allocations)
    return np.nonzero(m)[0] + start


def _tn_in_range(d, q, n):
    """every record's type-name allocation index must point inside the table"""
    return all(struct.unpack_from('<Q', d, q + 40 * i + 32)[0] <= n for i in range(n))


def find_tables(d, min_n=20, start=1024):
    """Return [(start, count)] of PackageAllocation chains (>= min_n records) in the package area."""
    hdr = struct.unpack_from('<6sHIIIII', d, 0)
    end = hdr[4]
    runs, skip_to = [], 0
    for q in _candidates(d, start, max(start, end - 40)):
        q = int(q)
        if q < skip_to or not _plausible(d, q):
            continue
        n, p = 0, q
        while _plausible(d, p):
            n += 1
            p += 40
        while p + 40 <= end and _plausible(d, p, False) and d[p + 24:p + 40] == bytes(16):
            n += 1      # trailing sentinel / empty records
            p += 40
        if n >= min_n and _tn_in_range(d, q, n):      # a chain shifted by a byte or two also looks plausible; its type-name indices are garbage
            runs.append((q, n))
            skip_to = p
    return runs


def find_s1(d, table, count, lo, hi):
    """Find the stripe1 base: the one that makes every stripe-1 char[] allocation a NUL-terminated ASCII string."""
    al = []
    for i in range(count):
        q = table + 40 * i
        if d[q] == 1:
            off, sz, cnt = struct.unpack_from('<III', d, q + 8)
            if sz == cnt and sz > 3:
                al.append((off, sz))
    al = al[:40]

    def score(T):
        ok = 0
        for off, sz in al:
            seg = d[T + off:T + off + sz]
            if len(seg) == sz and seg[-1] == 0 and all(32 <= c < 127 for c in seg[:-1]):
                ok += 1
        return ok
    # fast path: stripe1 ends exactly where the allocation table starts (true for every file checked)
    end1 = 0
    for i in range(count):
        q = table + 40 * i
        if d[q] == 1:
            off, sz = struct.unpack_from('<II', d, q + 8)
            end1 = max(end1, off + sz)
    if lo <= table - end1 < hi and score(table - end1) >= len(al) - 2:
        return table - end1
    for T in range(lo, hi):                  # fallback: brute-force scan
        if score(T) >= len(al) - 2:
            return T
    raise ValueError('stripe1 base not found')


def load_types(d, ti):
    """Parse the TypeInfoStripe: {typename: {size, flags, fields:[(name,type,(version,offset))]}}."""
    root = ti.base[1]
    ta, nt = ti.vec(root)
    types = {}
    for i in range(nt):
        o = ta + i * 56
        name = ti.cstr(ti.u64(o))
        fa, fn = ti.vec(o + 16)
        ver, size, flags = struct.unpack_from('<III', d, o + 40)
        fields = [(ti.cstr(ti.u64(fa + k * 24)), ti.cstr(ti.u64(fa + k * 24 + 8)), struct.unpack_from('<II', d, fa + k * 24 + 16))
                  for k in range(fn)]
        types[name] = dict(size=size, flags=flags, fields=fields)
    return types


class Blp:
    def __init__(self, path):
        import mmap
        self._f = open(path, 'rb')
        d = self.d = mmap.mmap(self._f.fileno(), 0, access=mmap.ACCESS_READ)      # big BLPs (leaders/units) are 10-80 MB; avoid reading them whole
        magic, ver, self.pkg_off, self.pkg_size, self.big_off, self.big_count, self.size = struct.unpack_from('<6sHIIIII', d, 0)
        assert magic == b'CIVBLP', magic
        tabs = find_tables(d)
        if len(tabs) == 1:      # small main package: rescan after the type-info table with a lower threshold
            more = find_tables(d, 3, tabs[0][0] + 40 * tabs[0][1])
            tabs = tabs + more[-1:]
        if len(tabs) == 1 and self.big_count == 0:
            tabs = tabs + [None]      # type-info only: container without entries
        elif len(tabs) < 2:
            raise ValueError('expected type-info and main allocation tables, got %r' % tabs)
        (t_ti, n_ti), main_tab = tabs[0], tabs[-1]
        # type-info package: root-type-name string precedes stripe0
        rn = d.find(b'TypeInfoStripe\0', 1024)
        s0_ti = rn + 15
        s1_ti = find_s1(d, t_ti, n_ti, s0_ti, t_ti)
        self.ti = Pkg(d, t_ti, n_ti, s0_ti, s1_ti)
        self.types = load_types(d, self.ti)
        if main_tab is None:
            self.pkg = Pkg(d, t_ti, 0, 0, 0)
            return
        t_main, n_main = main_tab
        s1 = find_s1(d, t_main, n_main, t_ti, t_main)
        # stripe0 ends where stripe1 begins and its size = end of its last allocation  ->  s0 = s1 - maxEnd0
        end0 = 0
        for i in range(n_main):
            q = t_main + 40 * i
            if d[q] == 0:
                off, sz = struct.unpack_from('<II', d, q + 8)
                end0 = max(end0, off + sz)
        s0 = s1 - end0
        self.pkg = Pkg(d, t_main, n_main - 1 if self.pkg_is_sentinel(t_main, n_main) else n_main, s0, s1)

    def pkg_is_sentinel(self, t, n):
        q = t + 40 * (n - 1)
        return self.d[q + 8:q + 20] == b'\0' * 12

    def layout(self, typename):
        t = self.types[typename]
        return t['size'], sorted((f[2][1], f[0], f[1]) for f in t['fields'])

    def allocs(self, typename):
        return [a for a in self.pkg.al if a['tname'] == typename]


if __name__ == '__main__':
    b = Blp(sys.argv[1])
    print('types', len(b.types), 'main allocs', len(b.pkg.al))
    from collections import Counter
    for k, v in Counter(a['tname'] for a in b.pkg.al).most_common(15):
        print(v, k)
