"""Consistency check of an export folder:  python blp_validate.py <export folder>   (or civ6_blp_export.py ... --validate)

For every OBJ under <folder>/*/models and */assemblies:
  - exactly one `mtllib`, the MTL exists, every `usemtl` is defined by a `newmtl`, every face group is preceded by a `usemtl`
  - every texture path in the MTL exists
  - face indices are in range, faces are triangles, v / vt / vn counts agree
  - model JSON: group material IDs exist in the material table, index counts are multiples of 3, and the OBJ has that many triangles
For every glTF: images / buffer exist, accessors fit their buffer, indices are in range, material/texture references are valid.
Returns a list of (severity, file, message); severity is 'error' or 'warn'.
"""
import glob, json, os, re, sys
import numpy as np


def _issue(out, sev, path, msg):
    out.append((sev, path, msg))


def parse_mtl(path):
    """-> (set of newmtl names, list of (line number, texture path as written))."""
    names, maps = set(), []
    with open(path, encoding='utf-8', errors='replace') as f:
        for n, line in enumerate(f, 1):
            t = line.split()
            if not t or t[0].startswith('#'):
                continue
            if t[0] == 'newmtl':
                names.add(t[1])
            elif t[0].startswith('map_') or t[0] in ('norm', 'bump', 'disp', 'decal', 'refl'):
                maps.append((n, t[-1]))
    return names, maps


def parse_obj(path):
    """Counts and structure of an OBJ: dict(mtllibs, usemtl, objects=[(name, usemtl-before-first-face or None, faces)], v, vt, vn, bad_faces, max_index)."""
    r = dict(mtllibs=[], usemtl=[], objects=[], v=0, vt=0, vn=0, bad_faces=0, out_of_range=0)
    cur = None; cur_mtl = None
    faces = []
    with open(path, encoding='utf-8', errors='replace') as f:
        lines = f.readlines()
    for line in lines:                       # first pass: vertex counts (faces may reference later data in theory, not in our files)
        if line.startswith('v '): r['v'] += 1
        elif line.startswith('vt '): r['vt'] += 1
        elif line.startswith('vn '): r['vn'] += 1
    for line in lines:
        t = line.split()
        if not t: continue
        if t[0] == 'mtllib': r['mtllibs'].append(' '.join(t[1:]))
        elif t[0] == 'o':
            cur = dict(name=' '.join(t[1:]), mtl_before_faces=None, faces=0); cur_mtl = None; r['objects'].append(cur)
        elif t[0] == 'usemtl':
            cur_mtl = t[1]; r['usemtl'].append(t[1])
        elif t[0] == 'f':
            if cur is None:
                cur = dict(name='(none)', mtl_before_faces=None, faces=0); r['objects'].append(cur)
            if cur['faces'] == 0:
                cur['mtl_before_faces'] = cur_mtl
            cur['faces'] += 1
            if len(t) != 4: r['bad_faces'] += 1
            for tok in t[1:]:
                for kind, part in zip(('v', 'vt', 'vn'), tok.split('/')):
                    if part and not 1 <= int(part) <= r[kind]:
                        r['out_of_range'] += 1
    return r


def check_obj(path, out, expect_triangles=None):
    o = parse_obj(path)
    d = os.path.dirname(path)
    if len(o['mtllibs']) != 1:
        _issue(out, 'error', path, '%d mtllib declarations (expected exactly 1)' % len(o['mtllibs']))
    defined = set()
    for lib in o['mtllibs'][:1]:
        mp = os.path.join(d, lib)
        if not os.path.exists(mp):
            _issue(out, 'error', path, 'mtllib %s does not exist' % lib)
        else:
            defined, maps = parse_mtl(mp)
            for n, rel in maps:
                if not os.path.exists(os.path.normpath(os.path.join(d, rel))):
                    _issue(out, 'error', mp, 'line %d: texture %s not found' % (n, rel))
    for name in sorted(set(o['usemtl']) - defined):
        _issue(out, 'error', path, 'usemtl %s has no newmtl in the MTL' % name)
    for ob in o['objects']:
        if ob['faces'] and ob['mtl_before_faces'] is None:
            _issue(out, 'error', path, 'object %s has faces but no usemtl before them' % ob['name'])
    if o['bad_faces']: _issue(out, 'error', path, '%d faces are not triangles' % o['bad_faces'])
    if o['out_of_range']: _issue(out, 'error', path, '%d face indices out of range' % o['out_of_range'])
    if not (o['v'] == o['vt'] == o['vn']):
        _issue(out, 'warn', path, 'v/vt/vn counts differ: %d/%d/%d' % (o['v'], o['vt'], o['vn']))
    tris = sum(ob['faces'] for ob in o['objects'])
    if expect_triangles is not None and tris != expect_triangles:
        _issue(out, 'error', path, '%d triangles in the OBJ but the model JSON lists %d' % (tris, expect_triangles))
    return o


def check_model_json(path, out):
    with open(path, encoding='utf-8') as f:
        j = json.load(f)
    nmat = len(j.get('materials', []))
    tris = 0
    for me in j.get('meshes', []):
        for g in me['groups']:
            if g['materialID'] >= nmat:
                _issue(out, 'error', path, 'group %d: material ID %d outside the material table (%d)' % (g['group'], g['materialID'], nmat))
            if g['indexCount'] % 3:
                _issue(out, 'error', path, 'group %d: index count %d is not a multiple of 3' % (g['group'], g['indexCount']))
            tris += g['indexCount'] // 3
    return tris


def check_gltf(path, out):
    d = os.path.dirname(path)
    with open(path, encoding='utf-8') as f:
        j = json.load(f)
    for im in j.get('images', []):
        if not os.path.exists(os.path.normpath(os.path.join(d, im['uri']))):
            _issue(out, 'error', path, 'image %s not found' % im['uri'])
    buf = None
    for b in j.get('buffers', []):
        bp = os.path.join(d, b['uri'])
        if not os.path.exists(bp):
            _issue(out, 'error', path, 'buffer %s not found' % b['uri'])
        else:
            with open(bp, 'rb') as f:
                buf = f.read()
            if len(buf) != b['byteLength']:
                _issue(out, 'error', path, 'buffer %s is %d bytes, expected %d' % (b['uri'], len(buf), b['byteLength']))
    comp = {5121: ('u1', 1), 5123: ('<u2', 2), 5125: ('<u4', 4), 5126: ('<f4', 4)}
    ncomp = dict(SCALAR=1, VEC2=2, VEC3=3, VEC4=4, MAT4=16)
    acc = j.get('accessors', [])
    for i, a in enumerate(acc):
        bv = j['bufferViews'][a['bufferView']]
        need = a['count'] * ncomp[a['type']] * comp[a['componentType']][1]
        if a.get('byteOffset', 0) + need > bv['byteLength']:
            _issue(out, 'error', path, 'accessor %d runs past its bufferView' % i)
    nmat, ntex, nimg = len(j.get('materials', [])), len(j.get('textures', [])), len(j.get('images', []))
    for t in j.get('textures', []):
        if t['source'] >= nimg: _issue(out, 'error', path, 'texture refers to missing image %d' % t['source'])
    for m in j.get('materials', []):
        refs = [m.get(k) for k in ('normalTexture', 'occlusionTexture', 'emissiveTexture')] + [m.get('pbrMetallicRoughness', {}).get(k) for k in ('baseColorTexture', 'metallicRoughnessTexture')]
        for r in refs:
            if r and r['index'] >= ntex: _issue(out, 'error', path, 'material %s refers to missing texture %d' % (m.get('name'), r['index']))
    for me in j.get('meshes', []):
        for p in me['primitives']:
            if p.get('material', 0) >= nmat and nmat:
                _issue(out, 'error', path, 'primitive refers to missing material %d' % p['material'])
            nv = acc[p['attributes']['POSITION']]['count']
            ia = acc[p['indices']]
            if ia['count'] % 3: _issue(out, 'error', path, 'primitive index count %d is not a multiple of 3' % ia['count'])
            if buf is not None:
                bv = j['bufferViews'][ia['bufferView']]
                idx = np.frombuffer(buf, comp[ia['componentType']][0], ia['count'], bv['byteOffset'] + ia.get('byteOffset', 0))
                if len(idx) and idx.max() >= nv:
                    _issue(out, 'error', path, 'primitive index %d >= %d vertices' % (idx.max(), nv))


def validate_tree(root):
    out = []
    objs = sorted(glob.glob(os.path.join(root, '*', 'models', '*.obj')) + glob.glob(os.path.join(root, '*', 'assemblies', '*.obj')))
    for p in objs:
        jp = os.path.splitext(p)[0] + '.json'
        tris = None
        if os.path.exists(jp) and os.sep + 'models' + os.sep in p:
            tris = check_model_json(jp, out)
        check_obj(p, out, tris)
    for p in sorted(glob.glob(os.path.join(root, '*', 'models', '*.gltf'))):
        check_gltf(p, out)
    return out, len(objs)


def report(root):
    issues, n = validate_tree(root)
    errs = [i for i in issues if i[0] == 'error']
    print('Validated %d OBJ files (+ glTF) under %s: %d error(s), %d warning(s)' % (n, root, len(errs), len(issues) - len(errs)))
    for sev, path, msg in issues[:200]:
        print('  %-5s %s: %s' % (sev.upper(), os.path.relpath(path, root), msg))
    if len(issues) > 200: print('  ... %d more' % (len(issues) - 200))
    return len(errs)


if __name__ == '__main__':
    sys.exit(1 if report(sys.argv[1] if len(sys.argv) > 1 else 'civ6_export') else 0)
