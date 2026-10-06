"""Generic model lister/exporter for Civ6 landmark BLPs (hero_buildings / city_buildings / tilebases ...).

  python blp_models.py list   <file.blp>
  python blp_models.py export <file.blp> <name-substring> [outdir]     # one OBJ + JSON per matching model

Generalises palgum_export.py. Uses blp_reader.py. Vertex format (all 24-byte VBs, format hash 828177625):
  half3 pos @0 (Z up), snorm8 nx @6, ny @7 (nz = +sqrt(1-nx^2-ny^2)), half2 UV0 @8 (D3D v-down).
Index buffers are u16; each prim group has its own baseVertex. Decal VBs (8 B/vertex) are skipped.
"""
import json, os, struct, sys
import numpy as np
from blp_reader import Blp

STATE_BITS = ['Construction', 'Pillaged', 'Unbuilt', 'Unworked', 'Worked']   # order of ModelStateData_Entry.m_AssetStateNames
VB_FORMAT_24 = 828177625
VB_FORMAT_32_SKIN = 1719251312   # 32 B: half3 pos@0, snorm8 nx@6 ny@7, half2 uv@8, u8[4] bone idx@12, u8[4] weights@16 (+unknown @20-23, 8 B zero)
MAT_SLOTS = ['lean0', 'lean1', 'diffuse', 'roughness', 'metalness', 'opacity', 'ao', 'lightmap', 'emission']  # u32 at ModelMaterialData +28..+56


def states(mask):
    return [n for i, n in enumerate(STATE_BITS) if mask >> i & 1]


class Landmarks:
    def __init__(self, path):
        self.blp = b = Blp(path)
        self.m, self.d = b.pkg, b.d
        m, d = self.m, self.d
        self.vbs, self.ibs = [], []
        for a in b.allocs('BLP::VertexBufferEntry'):
            for i in range(a['cnt']):
                o = m.base[0] + a['off'] + i * (a['size'] // a['cnt'])
                off, = struct.unpack_from('<Q', d, o + 32); size, = struct.unpack_from('<I', d, o + 40)
                fmt, nv = struct.unpack_from('<II', d, o + 64)
                self.vbs.append(dict(name=m.bstr(o + 8), off=off, size=size, fmt=fmt, count=nv))
        for a in b.allocs('BLP::IndexBufferEntry'):
            for i in range(a['cnt']):
                o = m.base[0] + a['off'] + i * (a['size'] // a['cnt'])
                off, = struct.unpack_from('<Q', d, o + 32); size, = struct.unpack_from('<I', d, o + 40)
                bpi, n = struct.unpack_from('<II', d, o + 64)
                self.ibs.append(dict(name=m.bstr(o + 8), off=off, size=size, bpi=bpi, count=n))
        self.textures = []
        for a in b.allocs('BLP::TextureEntry'):
            for i in range(a['cnt']):
                o = m.base[0] + a['off'] + i * (a['size'] // a['cnt'])
                fmt, w, h = struct.unpack_from('<3H', d, o + 88)
                try:
                    self.textures.append(dict(name=m.bstr(o + 8), cls=m.bstr(o + 64), fmt=fmt, w=w, h=h, mips=d[o + 98]))
                except IndexError:        # entry layout differs in some sprite/UI packages: keep list index-aligned
                    self.textures.append(dict(name=None, cls=None, fmt=fmt, w=w, h=h, mips=0))
        self.models = [self._model(m.base[0] + a['off']) for a in b.allocs('ModelPackageEntry::BaseModelData_Entry')]
        for md in self.models:        # unit models are called 'Root'/'skin_root' in the file: use the vertex-buffer name instead
            if md['name'] in (None, 'Root', 'skin_root') and md['meshes']:
                vb = md['meshes'][0]['groups'][0]['vb'] if md['meshes'][0]['groups'] else None
                if vb is not None and vb < len(self.vbs) and self.vbs[vb]['name']:
                    md['internalName'], md['name'] = md['name'], self.vbs[vb]['name']

    def _model(self, e):
        m, d = self.m, self.d
        ga, gn = m.vec(e + 160 + 256)
        if gn:
            gm = m.addr(m.u64(ga))
            name = m.bstr(gm + 208)
            na, nn = m.vec(gm + 112 + 72)
            bones = [m.bstr(na + i * 8) for i in range(nn)]
            ba, bn = m.vec(gm + 112 + 48)            # granny_bone (164 B): parent@8, LocalTransform@12 {flags, pos[3]@16, quat xyzw@28, scaleShear 3x3@44}
            xforms = []
            for i in range(bn):
                o = ba + i * 164
                par, flags = struct.unpack_from('<iI', d, o + 8)
                xforms.append(dict(parent=par, flags=flags, pos=list(struct.unpack_from('<3f', d, o + 16)),
                                   quat=list(struct.unpack_from('<4f', d, o + 28)), scaleShear=list(struct.unpack_from('<9f', d, o + 44)),
                                   invWorld=list(struct.unpack_from('<16f', d, o + 80))))     # Granny InverseWorld4x4 (row-vector convention)
        else:
            name, bones, xforms = None, [], []
        ia, inn = m.vec(e + 160 + 136)
        bone_ids = [struct.unpack_from('<i', d, ia + i * 4)[0] for i in range(inn)] if ia else []     # MeshBindingBoneIDs: local bone index -> skeleton bone
        ba2, nb2 = m.vec(e + 160 + 184)                                                              # MeshBindings, one per mesh
        bindings = []
        for i in range(nb2):
            fs, fc = struct.unpack_from('<IH', d, ba2 + i * 20 + 8)
            bindings.append(dict(fromStart=fs, fromCount=fc))
        mesh_bone = []
        for i in range(max(len(bindings), 0)):
            b_ = bindings[i]
            mesh_bone.append(bone_ids[b_['fromStart']] if b_['fromCount'] == 1 and b_['fromStart'] < len(bone_ids) else -1)   # -1 = skinned over several bones
        mesh_a, nmesh = m.vec(e + 8 + 32)
        pg_a, _ = m.vec(e + 8 + 56)
        meshes = []
        for mi in range(nmesh):
            gs, gc, _ = struct.unpack_from('<III', d, mesh_a + mi * 40 + 24)
            groups = []
            for g in range(gs, gs + gc):
                sm, mat = struct.unpack_from('<II', d, pg_a + g * 32)
                ud, _ = m.vec(pg_a + g * 32 + 8)
                vb, ib, first, cnt, base, vc = struct.unpack_from('<6I', d, m.addr(m.u64(ud)) + 8)
                groups.append(dict(group=g, stateMask=sm, states=states(sm), materialID=mat, vb=vb, ib=ib, firstIndex=first, indexCount=cnt, baseVertex=base, vertCount=vc))
            bone = bones[mesh_bone[mi]] if mi < len(mesh_bone) and 0 <= mesh_bone[mi] < len(bones) else None
            meshes.append(dict(mesh=mi, bone=bone, groups=groups))
        materials = []
        ma, mn = m.vec(e + 8 + 80)
        for k in range(mn):
            ud, un = m.vec(ma + k * 24)
            mat = dict(index=k)
            for j in range(un):
                p = m.u64(ud + j * 8); a = m.al[p - 1]; o = m.addr(p)
                if a['tname'] == 'ModelMaterialData':
                    vals = struct.unpack_from('<9I', d, o + 28)
                    for slot, v in zip(MAT_SLOTS, vals):
                        mat[slot] = self.textures[v]['name'] if v < len(self.textures) else None
                    mat['uvScroll'] = list(struct.unpack_from('<2f', d, o + 8))
                elif a['tname'] == 'ModelBurnMaterialData':
                    v, = struct.unpack_from('<I', d, o + 8)
                    mat['burnMap'] = self.textures[v]['name'] if v < len(self.textures) else None
            materials.append(mat)
        return dict(name=name, className=m.bstr(e + 8), bones=bones, meshes=meshes, materials=materials, boneXforms=xforms, boneIds=bone_ids, meshBindings=bindings)

    def vertices(self, vbi):
        v, b = self.vbs[vbi], self.blp
        stride = {VB_FORMAT_24: 24, VB_FORMAT_32_SKIN: 32}.get(v['fmt'])
        assert stride and v['size'] == v['count'] * stride, 'unsupported vertex format'
        V = np.frombuffer(self.d[b.big_off + v['off']:b.big_off + v['off'] + v['size']], np.uint8).reshape(-1, stride)
        P = np.frombuffer(V[:, 0:6].tobytes(), '<f2').reshape(-1, 3).astype(float)
        UV = np.frombuffer(V[:, 8:12].tobytes(), '<f2').reshape(-1, 2).astype(float)
        sb = V.astype(np.int8).astype(float)
        N = np.zeros((len(V), 3)); N[:, 0] = np.clip(sb[:, 6] / 127, -1, 1); N[:, 1] = np.clip(sb[:, 7] / 127, -1, 1)
        N[:, 2] = np.sqrt(np.clip(1 - N[:, 0] ** 2 - N[:, 1] ** 2, 0, 1))
        return P, UV, N

    def skin(self, vbi):
        """(joints uint8[n,4] = local indices into the mesh's bone-binding list, weights float[n,4]) for skinned VBs, else None."""
        v, b = self.vbs[vbi], self.blp
        if v['fmt'] != VB_FORMAT_32_SKIN:
            return None
        V = np.frombuffer(self.d[b.big_off + v['off']:b.big_off + v['off'] + v['size']], np.uint8).reshape(-1, 32)
        w = V[:, 16:20].astype(float)
        s = w.sum(1, keepdims=True); s[s == 0] = 1
        return V[:, 12:16].copy(), w / s

    def indices(self, ibi):
        i, b = self.ibs[ibi], self.blp
        assert i['bpi'] == 2
        return np.frombuffer(self.d[b.big_off + i['off']:b.big_off + i['off'] + i['size']], '<u2').astype(int)

    def texture_png(self, name, outdir):
        """Decode a loose texture to outdir/textures/<name>.png; returns relative path or None."""
        import blp_textures as bt
        tex = next((t for t in self.textures if t['name'] == name), None)
        path = bt.texture_index().get(name)
        if not tex or not path:
            return None
        tdir = getattr(self, 'texture_dir', None) or os.path.join(outdir, 'textures')
        full = os.path.join(tdir, name + '.png')
        rel = os.path.relpath(full, outdir).replace(os.sep, '/')
        if not os.path.exists(full):
            os.makedirs(tdir, exist_ok=True)
            try:
                bt.to_png(bt.decode(open(path, 'rb').read(), tex['fmt'], tex['w'], tex['h']), full)
            except Exception as e:                       # unsupported format etc.
                print('  texture %s: %s' % (name, e)); return None
        return rel

    def write_materials(self, model, groups, outdir, fname, prefix='', mode='w'):
        used = sorted({g['materialID'] for _, g in groups})
        with open(os.path.join(outdir, fname), mode) as f:
            f.write('# slots: diffuse=map_Kd emission=map_Ke ao=map_Ka lean0=map_Bump(normal, BC5 xy) roughness(gloss)=map_Ns opacity=map_d ; others listed as comments\n')
            for k in used:
                mat = model['materials'][k]
                f.write('newmtl %smat%d\nKd 1 1 1\nKa 1 1 1\n' % (prefix, k))
                for slot, key in (('diffuse', 'map_Kd'), ('emission', 'map_Ke'), ('ao', 'map_Ka'), ('lean0', 'map_Bump'), ('roughness', 'map_Ns'), ('opacity', 'map_d')):
                    if mat.get(slot):
                        rel = self.texture_png(mat[slot], outdir)
                        f.write(('%s %s\n' % (key, rel)) if rel else '# %s: %s (file not found/unsupported)\n' % (slot, mat[slot]))
                for slot in ('lean1', 'metalness', 'lightmap', 'burnMap'):
                    if mat.get(slot):
                        f.write('# %s: %s\n' % (slot, mat[slot]))
                f.write('\n')
        return fname

    def export(self, model, outdir, tag=''):
        P, UV, N = None, None, None
        vbi = None
        groups = [(me['bone'], g) for me in model['meshes'] for g in me['groups']]
        if not groups:
            raise AssertionError('no meshes (skeleton-only assembly node, %d bones)' % len(model['bones']))
        used_vb = sorted({g['vb'] for _, g in groups})
        Ps, UVs, Ns, voff = [], [], [], {}
        for vbi in used_vb:                    # a model may draw from several vertex buffers: concatenate them
            voff[vbi] = sum(len(x) for x in Ps)
            p, u, n = self.vertices(vbi); Ps.append(p); UVs.append(u); Ns.append(n)
        P, UV, N = np.concatenate(Ps), np.concatenate(UVs), np.concatenate(Ns)
        fn = os.path.join(outdir, model['name'] + tag + '.obj')
        mtl = self.write_materials(model, groups, outdir, model['name'] + tag + '.mtl')
        with open(fn, 'w') as f:
            f.write('# %s from %s. Z-up. objects = bone__group_states_material; vertex buffers: %s\n' % (model['name'], os.path.basename(getattr(self, 'blp_path', '')), ', '.join(self.vbs[v]['name'] for v in used_vb)))
            for v in P: f.write('v %.5f %.5f %.5f\n' % tuple(v))
            for v in UV: f.write('vt %.5f %.5f\n' % (v[0], 1 - v[1]))
            for v in N: f.write('vn %.5f %.5f %.5f\n' % tuple(v))
            for bone, g in groups:
                IDX = self.indices(g['ib'])
                f.write('o %s__g%d_%s_mat%d\n' % (bone, g['group'], '+'.join(g['states']) or 'none', g['materialID']))
                for t in (IDX[g['firstIndex']:g['firstIndex'] + g['indexCount']] + g['baseVertex'] + voff[g['vb']]).reshape(-1, 3) + 1:
                    f.write('f ' + ' '.join('%d/%d/%d' % (x, x, x) for x in t) + '\n')
        json.dump(dict(model, vertexBuffers=[self.vbs[v]['name'] for v in used_vb], stateBits=STATE_BITS), open(os.path.join(outdir, model['name'] + tag + '.json'), 'w'), indent=1)
        return fn


def main(argv):
    if len(argv) < 3 or argv[1] not in ('list', 'export'):
        print(__doc__); return
    L = Landmarks(argv[2]); L.blp_path = argv[2]
    if argv[1] == 'list':
        for i, md in enumerate(L.models):
            ng = sum(len(me['groups']) for me in md['meshes'])
            tris = sum(g['indexCount'] for me in md['meshes'] for g in me['groups']) // 3
            print('%2d %-34s class=%-10s meshes=%d groups=%d tris(all states)=%d bones=%s' % (i, md['name'], md['className'], len(md['meshes']), ng, tris, ','.join(b for b in md['bones'] if b)[:70]))
    else:
        sub = argv[3]; out = argv[4] if len(argv) > 4 else '.'
        os.makedirs(out, exist_ok=True)
        for i, md in enumerate(L.models):
            if md['name'] and sub.lower() in md['name'].lower():
                dup = sum(1 for x in L.models if x['name'] == md['name']) > 1
                try:
                    print('wrote', L.export(md, out, '_%02d' % i if dup else ''))
                except AssertionError as e:
                    print('skip', md['name'], e)


if __name__ == '__main__':
    main(sys.argv)
