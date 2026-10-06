"""Assemble 'skeleton-only' landmark models (e.g. IMP_Trading_Dome, IMP_Mahavihara in tilebases.blp).

Each bone of such a model is an *instance placement*: bone name = <part model name><3-digit instance number>,
LocalTransform = Granny transform (flags: 1 position, 2 orientation (quat xyzw), 4 scale/shear 3x3).
world = pos + R(q) * (ScaleShear * v)         (all bones here are children of the root bone at the origin)

Bones whose base name is not a model in this BLP (trees, shrubs, fruit, RoadCP control points) are listed as
external/placeholders in the JSON (their meshes live in other BLPs).

  python blp_assemble.py <file.blp> <assembly-model-name> [outdir] [state]      # state default 'Worked'
"""
import json, os, re, sys
import numpy as np
from blp_models import Landmarks


def quat_matrix(q):
    x, y, z, w = q
    n = (x * x + y * y + z * z + w * w) ** 0.5 or 1.0
    x, y, z, w = x / n, y / n, z / n, w / n
    return np.array([[1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
                     [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
                     [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)]])


def bone_matrix(xf):
    R = quat_matrix(xf['quat']) if xf['flags'] & 2 else np.eye(3)
    S = np.array(xf['scaleShear']).reshape(3, 3) if xf['flags'] & 4 else np.eye(3)
    M = np.eye(4); M[:3, :3] = R @ S
    if xf['flags'] & 1: M[:3, 3] = xf['pos']
    return M


def resolve_part(L, base):
    """Model for an instance base name: exact name, or the same name with a '_PIL' suffix (models are named after one of their bones)."""
    for md in L.models:
        if md['name'] == base or md['name'] == base + '_PIL':
            if md['meshes']:
                return md
    return None


def assemble(L, asm, outdir, state='Worked'):
    os.makedirs(outdir, exist_ok=True)
    world = {}
    def wm(i):                                   # parent chain
        if i not in world:
            xf = asm['boneXforms'][i]
            world[i] = (wm(xf['parent']) if xf['parent'] >= 0 else np.eye(4)) @ bone_matrix(xf)
        return world[i]
    mtl = asm['name'] + '_' + state + '.mtl'
    open(os.path.join(outdir, mtl), 'w').close()
    parts_done = {}                              # id(part) -> prefix
    verts, uvs, norms, faces = [], [], [], []    # faces: (objname, usemtl, [idx...])
    vcount = 0
    report = dict(assembly=asm['name'], state=state, placed=[], external=[], missing=[])
    for i, bname in enumerate(asm['bones']):
        if asm['boneXforms'][i]['parent'] < 0:
            continue
        base = re.sub(r'\d{3}$', '', bname)
        part = resolve_part(L, base)
        if not part:
            (report['external'] if not base.startswith('IMP_') and not base.startswith('DIS_CTY') else report['missing']).append(bname)
            continue
        groups = [(me['bone'], g) for me in part['meshes'] for g in me['groups'] if state in g['states']]
        if not groups:
            report['missing'].append(bname + ' (no %s groups in %s)' % (state, part['name']))
            continue
        pi = L.models.index(part)
        if pi not in parts_done:
            parts_done[pi] = 'p%d_' % pi
            L.write_materials(part, groups_all(part), outdir, mtl, prefix=parts_done[pi], mode='a')
        M = wm(i)
        used_vb = sorted({g['vb'] for _, g in groups})
        local = {}
        for vb in used_vb:
            P, UV, N = L.vertices(vb)
            local[vb] = (vcount, len(P))
            verts.append(P @ M[:3, :3].T + M[:3, 3]); uvs.append(UV); norms.append(N @ quat_matrix(asm['boneXforms'][i]['quat']).T if asm['boneXforms'][i]['flags'] & 2 else N)
            vcount += len(P)
        for _, g in groups:
            I = L.indices(g['ib'])[g['firstIndex']:g['firstIndex'] + g['indexCount']] + g['baseVertex'] + local[g['vb']][0]
            faces.append(('%s__%s_g%d' % (bname, part['name'], g['group']), '%smat%d' % (parts_done[pi], g['materialID']), I.reshape(-1, 3) + 1))
        report['placed'].append(dict(bone=bname, part=part['name'], pos=[round(x, 3) for x in asm['boneXforms'][i]['pos']], flags=asm['boneXforms'][i]['flags']))
    fn = os.path.join(outdir, asm['name'] + '_assembled_' + state + '.obj')
    V, UV, N = np.concatenate(verts), np.concatenate(uvs), np.concatenate(norms)
    with open(fn, 'w') as f:
        f.write('# %s assembled (state %s), Z-up; %d placed parts\n' % (asm['name'], state, len(report['placed'])))
        f.write('mtllib %s\n' % mtl)
        for v in V: f.write('v %.5f %.5f %.5f\n' % tuple(v))
        for v in UV: f.write('vt %.5f %.5f\n' % (v[0], 1 - v[1]))
        for v in N: f.write('vn %.5f %.5f %.5f\n' % tuple(v))
        for name, mat, I in faces:
            f.write('o %s\nusemtl %s\n' % (name, mat))
            for t in I: f.write('f ' + ' '.join('%d/%d/%d' % (x, x, x) for x in t) + '\n')
    json.dump(report, open(os.path.join(outdir, asm['name'] + '_assembly_' + state + '.json'), 'w'), indent=1)
    return fn, report


def groups_all(part):
    return [(me['bone'], g) for me in part['meshes'] for g in me['groups']]


if __name__ == '__main__':
    L = Landmarks(sys.argv[1]); name = sys.argv[2]
    out = sys.argv[3] if len(sys.argv) > 3 else '.'; state = sys.argv[4] if len(sys.argv) > 4 else 'Worked'
    asm = next(md for md in L.models if md['name'] == name)
    fn, rep = assemble(L, asm, out, state)
    print('wrote', fn, '| placed', len(rep['placed']), 'external', len(rep['external']), 'missing', len(rep['missing']))
    if rep['missing']: print('missing:', rep['missing'])
