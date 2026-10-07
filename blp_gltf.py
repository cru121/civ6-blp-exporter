"""glTF 2.0 (.gltf + .bin) export of a landmark/unit model with its skeleton and skinning.

  - nodes: one per Granny bone (local transform from the bone), under a root node that turns Civ6's Z-up into glTF's Y-up
  - skinned meshes (32-byte vertex format): a glTF skin per mesh; JOINTS_0 are the *local* indices stored in the vertex,
    skin.joints = skeleton bones listed by the mesh binding (MeshBindingBoneIDs[fromStart : fromStart+fromCount]);
    inverseBindMatrices = Granny InverseWorld4x4 (stored row-vector convention -> same floats, glTF is column-major)
  - rigid meshes (24-byte format) are plain mesh nodes (their vertices are already in model space)
  - each triangle group is one primitive; the tile/unit states in which it is visible are in primitive.extras.states
"""
import json, os, struct
import numpy as np
from blp_assemble import bone_matrix
from blp_anim import classify


class _Buf:
    def __init__(self):
        self.data = bytearray(); self.views = []; self.acc = []

    def add(self, arr, target=None, **acc):
        raw = np.ascontiguousarray(arr).tobytes()
        while len(self.data) % 4: self.data.append(0)
        self.views.append(dict(buffer=0, byteOffset=len(self.data), byteLength=len(raw), **({'target': target} if target else {})))
        self.data += raw
        self.acc.append(dict(bufferView=len(self.views) - 1, **acc))
        return len(self.acc) - 1


def _trs(xf):
    """TRS dict for a Granny bone local transform (scale = diagonal of ScaleShear when present)."""
    t = list(xf['pos']) if xf['flags'] & 1 else [0.0, 0.0, 0.0]
    r = list(xf['quat']) if xf['flags'] & 2 else [0.0, 0.0, 0.0, 1.0]
    m = xf['scaleShear']
    sc = [m[0], m[4], m[8]] if xf['flags'] & 4 else [1.0, 1.0, 1.0]
    return dict(translation=[float(x) for x in t], rotation=[float(x) for x in r], scale=[float(x) for x in sc])


def export_gltf(L, model, outdir, tag='', animations=None):
    d = model
    groups = [(mi, me, g) for mi, me in enumerate(d['meshes']) for g in me['groups']]
    if not groups:
        raise AssertionError('no meshes')
    nb = len(d['bones'])
    B = _Buf()
    nodes = []
    # node 0: Z-up -> Y-up root
    s = 2 ** -0.5
    nodes.append(dict(name='Z_up_to_Y_up', rotation=[-s, 0, 0, s], children=[]))
    for i, name in enumerate(d['bones']):
        xf = d['boneXforms'][i]
        nodes.append(dict(name=name or 'bone%d' % i, **_trs(xf)))
    for i in range(nb):
        p = d['boneXforms'][i]['parent']
        (nodes[0] if p < 0 else nodes[1 + p]).setdefault('children', []).append(1 + i)
    bone_node = lambda i: 1 + i

    meshes, skins, materials, textures, images = [], [], [], [], []
    mat_index, tex_index, acc_cache, skin_of_vb = {}, {}, {}, {}

    def texture(rel):
        if not rel: return None
        if rel not in tex_index:
            images.append(dict(uri=rel)); textures.append(dict(source=len(images) - 1)); tex_index[rel] = len(textures) - 1
        return tex_index[rel]

    def material(k):
        if k not in mat_index:
            mt = d['materials'][k] if k < len(d['materials']) else {}
            maps = L.pbr_maps(mt, outdir)
            m = dict(name='mat%d' % k, doubleSided=True, pbrMetallicRoughness=dict(metallicFactor=0.0, roughnessFactor=1.0))
            t = texture(maps.get('baseColor'))
            if t is not None: m['pbrMetallicRoughness']['baseColorTexture'] = dict(index=t)
            if maps.get('alpha'): m['alphaMode'] = 'BLEND'
            n = texture(maps.get('normal'))
            if n is not None: m['normalTexture'] = dict(index=n)
            o = texture(maps.get('orm'))                  # R = AO, G = roughness, B = metalness
            if o is not None:
                has = maps['has']
                if has['ao']: m['occlusionTexture'] = dict(index=o)
                m['pbrMetallicRoughness']['metallicRoughnessTexture'] = dict(index=o)
                m['pbrMetallicRoughness']['metallicFactor'] = 1.0 if has['metal'] else 0.0       # B is 0 without a metalness map anyway
                m['pbrMetallicRoughness']['roughnessFactor'] = 1.0
            e = texture(maps.get('emissive'))
            if e is not None: m['emissiveTexture'] = dict(index=e); m['emissiveFactor'] = [1.0, 1.0, 1.0]
            m['extras'] = {k_: v for k_, v in mt.items() if k_ != 'index'}
            materials.append(m); mat_index[k] = len(materials) - 1
        return mat_index[k]

    def vertex_accessors(vb, skinned):
        if vb not in acc_cache:
            P, UV, N = L.vertices(vb)
            a = dict(POSITION=B.add(P.astype('<f4'), 34962, componentType=5126, count=len(P), type='VEC3', min=[float(x) for x in P.min(0)], max=[float(x) for x in P.max(0)]),
                     NORMAL=B.add(N.astype('<f4'), 34962, componentType=5126, count=len(N), type='VEC3'),
                     TEXCOORD_0=B.add(UV.astype('<f4'), 34962, componentType=5126, count=len(UV), type='VEC2'))
            sk = L.skin(vb)
            if sk is not None:
                J, W = sk
                a['JOINTS_0'] = B.add(J.astype('u1'), 34962, componentType=5121, count=len(J), type='VEC4')
                a['WEIGHTS_0'] = B.add(W.astype('<f4'), 34962, componentType=5126, count=len(W), type='VEC4')
            acc_cache[vb] = a
        return acc_cache[vb]

    mesh_prims = {}
    for mi, me, g in groups:
        skinned = L.vbs[g['vb']]['fmt'] != 828177625
        acc = vertex_accessors(g['vb'], skinned)
        I = (L.indices(g['ib'])[g['firstIndex']:g['firstIndex'] + g['indexCount']] + g['baseVertex']).astype('<u4')
        prim = dict(attributes=dict(acc), indices=B.add(I, 34963, componentType=5125, count=len(I), type='SCALAR'), mode=4,
                    material=material(g['materialID']), extras=dict(states=g['states'], group=g['group']))
        mesh_prims.setdefault(mi, []).append((prim, skinned, g['vb']))

    scene_children = [0]
    for mi, prims in mesh_prims.items():
        me = d['meshes'][mi]
        meshes.append(dict(name='mesh%d_%s' % (mi, me['bone'] or 'skinned'), primitives=[p for p, _, _ in prims]))
        node = dict(name=meshes[-1]['name'], mesh=len(meshes) - 1)
        if prims[0][1]:                                   # skinned mesh
            bd = d['meshBindings'][mi]
            jids = d['boneIds'][bd['fromStart']:bd['fromStart'] + bd['fromCount']]
            ibm = np.array([d['boneXforms'][j]['invWorld'] for j in jids], '<f4')
            skins.append(dict(joints=[bone_node(j) for j in jids], inverseBindMatrices=B.add(ibm, None, componentType=5126, count=len(jids), type='MAT4'),
                              skeleton=bone_node(0) if nb else None))
            if skins[-1]['skeleton'] is None: del skins[-1]['skeleton']
            node['skin'] = len(skins) - 1
        nodes.append(node); scene_children.append(len(nodes) - 1)
    gl_anims = []
    bone_index = {n: i for i, n in enumerate(d['bones']) if n}
    for anim in animations or []:
        channels, samplers = [], []
        for tr in anim['tracks']:
            bi = bone_index.get(tr['name'])
            if bi is None:
                continue
            k = classify(tr)
            for key, path, dim, ident in (('rot', 'rotation', 4, [0, 0, 0, 1]), ('pos', 'translation', 3, [0, 0, 0])):
                c = k[key]
                if c is None or c.get('unresolved'):
                    continue
                if c['values'] is None:                    # DaIdentity: constant identity value
                    times, vals = np.array([0.0], '<f4'), np.array([ident], '<f4')
                else:
                    times = np.asarray(c['knots'], '<f4') if c['knots'] is not None else np.array([0.0], '<f4')
                    vals = np.asarray(c['values'], '<f4')
                    order = np.argsort(times, kind='stable'); times, vals = times[order], vals[order]
                    keep = np.concatenate([[True], np.diff(times) > 1e-6]); times, vals = times[keep], vals[keep]
                if key == 'rot':                           # keep quaternions on the same hemisphere so LINEAR interpolation takes the short way
                    vals = vals.copy()
                    for j in range(1, len(vals)):
                        if np.dot(vals[j], vals[j - 1]) < 0: vals[j] = -vals[j]
                    vals /= np.maximum(np.linalg.norm(vals, axis=1, keepdims=True), 1e-9)
                ti = B.add(times, None, componentType=5126, count=len(times), type='SCALAR', min=[float(times.min())], max=[float(times.max())])
                vi = B.add(vals.astype('<f4'), None, componentType=5126, count=len(vals), type='VEC%d' % dim)
                samplers.append(dict(input=ti, output=vi, interpolation='LINEAR'))
                channels.append(dict(sampler=len(samplers) - 1, target=dict(node=bone_node(bi), path=path)))
        if channels:
            gl_anims.append(dict(name=anim['name'].replace('ANIMATION_', ''), channels=channels, samplers=samplers))
    gltf = dict(asset=dict(version='2.0', generator='civ6-blp-exporter'), scene=0, scenes=[dict(nodes=[0] + [c for c in scene_children[1:]])],
                nodes=nodes, meshes=meshes, accessors=B.acc, bufferViews=B.views, buffers=[dict(uri=(d['name'] + tag + '.bin'), byteLength=len(B.data))])
    if skins: gltf['skins'] = skins
    if gl_anims: gltf['animations'] = gl_anims
    if materials: gltf['materials'] = materials
    if textures: gltf['textures'] = textures; gltf['images'] = images
    fn = os.path.join(outdir, d['name'] + tag + '.gltf')
    open(fn, 'w').write(json.dumps(gltf))
    open(os.path.join(outdir, d['name'] + tag + '.bin'), 'wb').write(bytes(B.data))
    return fn
