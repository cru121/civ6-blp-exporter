"""Generic model lister/exporter for Civ6 landmark BLPs (hero_buildings / city_buildings / tilebases ...).

  python blp_models.py list   <file.blp>
  python blp_models.py export <file.blp> <name-substring> [outdir]     # one OBJ + JSON per matching model

Generalises palgum_export.py. Uses blp_reader.py. Vertex format (all 24-byte VBs, format hash 828177625):
  half3 pos @0 (Z up), snorm8 nx @6, ny @7 (nz = +sqrt(1-nx^2-ny^2)), half2 UV0 @8 (D3D v-down).
Index buffers are u16; each prim group has its own baseVertex. Decal VBs (8 B/vertex) are skipped.
"""
import json, os, re, struct, sys
import numpy as np
from blp_reader import Blp

STATE_BITS = ['Construction', 'Pillaged', 'Unbuilt', 'Unworked', 'Worked']   # order of ModelStateData_Entry.m_AssetStateNames
VB_FORMAT_24 = 828177625
VB_FORMAT_32_SKIN = 1719251312   # 32 B: half3 pos@0, snorm8 nx@6 ny@7, half2 uv@8, u8[4] bone idx@12, u8[4] weights@16 (+unknown @20-23, 8 B zero)
MAT_SLOTS = ['lean0', 'lean1', 'diffuse', 'roughness', 'metalness', 'opacity', 'ao', 'lightmap', 'emission']  # u32 at ModelMaterialData +28..+56


def safe_name(name):
    """model name -> file name: Windows-invalid characters (| / ? : ...) become '_'"""
    return re.sub(r'[<>:"/|?*\x00-\x1f]', '_', name).strip(' .') or '_'


def states(mask):
    return [n for i, n in enumerate(STATE_BITS) if mask >> i & 1]


def model_for_state(model, state):
    """Copy of `model` keeping only the groups visible in `state` (None if there are none). Group numbers and state lists are kept."""
    meshes = [dict(me, groups=[g for g in me['groups'] if state in g['states']]) for me in model['meshes']]
    if not any(me['groups'] for me in meshes):
        return None
    return dict(model, meshes=meshes, exportState=state)


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
        skeletons = []                                   # a model can hold several Granny models (skeletons); mesh bindings say which one they use
        for k in range(gn):
            gm = m.addr(m.u64(ga + 8 * k))
            sk_name = m.bstr(gm + 208)
            na, nn = m.vec(gm + 112 + 72)
            sk_bones = [m.bstr(na + i * 8) for i in range(nn)]
            ba, bn = m.vec(gm + 112 + 48)            # granny_bone (164 B): parent@8, LocalTransform@12 {flags, pos[3]@16, quat xyzw@28, scaleShear 3x3@44}
            sk_xforms = []
            for i in range(bn):
                o = ba + i * 164
                par, flags = struct.unpack_from('<iI', d, o + 8)
                sk_xforms.append(dict(parent=par, flags=flags, pos=list(struct.unpack_from('<3f', d, o + 16)),
                                      quat=list(struct.unpack_from('<4f', d, o + 28)), scaleShear=list(struct.unpack_from('<9f', d, o + 44)),
                                      invWorld=list(struct.unpack_from('<16f', d, o + 80))))     # Granny InverseWorld4x4 (row-vector convention)
            skeletons.append(dict(name=sk_name, bones=sk_bones, xforms=sk_xforms))
        name, bones, xforms = (skeletons[0]['name'], skeletons[0]['bones'], skeletons[0]['xforms']) if skeletons else (None, [], [])      # skeleton 0 = the model's own
        ia, inn = m.vec(e + 160 + 136)
        bone_ids = [struct.unpack_from('<i', d, ia + i * 4)[0] for i in range(inn)] if ia else []     # MeshBindingBoneIDs: local bone index -> skeleton bone
        ba2, nb2 = m.vec(e + 160 + 184)                                                              # MeshBindings, one per mesh
        bindings = []
        for i in range(nb2):
            fs, fc = struct.unpack_from('<IH', d, ba2 + i * 20 + 8)
            from_sk = min(d[ba2 + i * 20], max(gn - 1, 0))                                          # nFromSkeleton: which skeleton the bone IDs index
            bindings.append(dict(fromStart=fs, fromCount=fc, fromSkeleton=from_sk))
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
            sk_bones = skeletons[bindings[mi]['fromSkeleton']]['bones'] if mi < len(bindings) and skeletons else bones
            bone = sk_bones[mesh_bone[mi]] if mi < len(mesh_bone) and 0 <= mesh_bone[mi] < len(sk_bones) else None
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
        return dict(name=name, className=m.bstr(e + 8), bones=bones, meshes=meshes, materials=materials, boneXforms=xforms, boneIds=bone_ids, meshBindings=bindings, skeletons=skeletons)

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

    def texture_array(self, name):
        """Decoded top mip of a loose texture as (uint8 array h,w,c, DXGI format), or None."""
        import blp_textures as bt
        tex = next((t for t in self.textures if t['name'] == name), None)
        path = bt.texture_index().get(name)
        if not tex or not path:
            return None
        try:
            return bt.decode(open(path, 'rb').read(), tex['fmt'], tex['w'], tex['h']), tex['fmt']
        except Exception as e:
            print('  texture %s: %s' % (name, e)); return None

    def _save_png(self, arr, stem, outdir):
        from PIL import Image
        tdir = getattr(self, 'texture_dir', None) or os.path.join(outdir, 'textures')
        os.makedirs(tdir, exist_ok=True)
        full = os.path.join(tdir, stem + '.png')
        Image.fromarray(arr[:, :, 0] if arr.shape[2] == 1 else arr).save(full)
        return os.path.relpath(full, outdir).replace(os.sep, '/')

    def pbr_maps(self, mat, outdir):
        """Convert one Civ6 material to glTF metallic-roughness semantics. Returns {key: relative png path}:
          baseColor  diffuse (sRGB); with the opacity map folded into alpha when there is one (+ 'alpha': True)
          normal     lean0 (BC5 x,y -> RGB with z rebuilt); Civ6's green channel is already +Y-up (OpenGL / glTF convention)
          orm        R = AO, G = roughness, B = metalness (glTF packing; same image serves occlusion and metallicRoughness)
          roughness / metallic / occlusion   the same three as separate grayscale images (for MTL)
          emissive   emission (sRGB)
        Roughness = 1 - gloss: the 'roughness' slot is a gloss map stored as RGB with the same signal in all three channels,
        so the green channel is used (best precision in BC1), and since it is a *_SRGB format it is linearised first.
        AO and metalness are BC4 UNORM (already linear). Missing AO = 1, missing metalness = 0."""
        import zlib
        from PIL import Image
        cache = self.__dict__.setdefault('_pbr_cache', {})
        key = tuple(mat.get(s) for s in ('diffuse', 'opacity', 'lean0', 'roughness', 'metalness', 'ao', 'emission'))
        if key in cache:
            return cache[key]
        out = {}

        def resized(arr, w, h):
            return arr if arr.shape[1] == w and arr.shape[0] == h else np.asarray(Image.fromarray(arr[:, :, 0]).resize((w, h), Image.BILINEAR))[:, :, None]

        d = self.texture_array(mat['diffuse']) if mat.get('diffuse') else None
        if d is not None:
            rgb = d[0][:, :, :3]
            op = self.texture_array(mat['opacity']) if mat.get('opacity') else None
            if op is not None:
                a = resized(op[0][:, :, :1], rgb.shape[1], rgb.shape[0])
                out['baseColor'] = self._save_png(np.concatenate([rgb, a], 2), mat['diffuse'] + '_RGBA', outdir); out['alpha'] = True
            else:
                out['baseColor'] = self.texture_png(mat['diffuse'], outdir)
        if mat.get('lean0'):
            out['normal'] = self.texture_png(mat['lean0'], outdir)
        if mat.get('emission'):
            out['emissive'] = self.texture_png(mat['emission'], outdir)
        gloss = self.texture_array(mat['roughness']) if mat.get('roughness') else None
        metal = self.texture_array(mat['metalness']) if mat.get('metalness') else None
        ao = self.texture_array(mat['ao']) if mat.get('ao') else None
        parts = [p for p in (ao, gloss, metal) if p is not None]
        if parts:
            w, h = max(p[0].shape[1] for p in parts), max(p[0].shape[0] for p in parts)
            R = resized(ao[0][:, :, :1], w, h)[:, :, 0] if ao else np.full((h, w), 255, np.uint8)
            B = resized(metal[0][:, :, :1], w, h)[:, :, 0] if metal else np.zeros((h, w), np.uint8)
            if gloss:
                g = resized(gloss[0][:, :, 1:2] if gloss[0].shape[2] > 1 else gloss[0], w, h)[:, :, 0].astype(float) / 255
                if gloss[1] in (72, 78):                                      # BC1/BC3 *_SRGB -> linear
                    g = np.where(g <= 0.04045, g / 12.92, ((g + 0.055) / 1.055) ** 2.4)
                G = np.round((1 - g) * 255).astype(np.uint8)
            else:
                G = np.full((h, w), 255, np.uint8)
            stem = (mat.get('roughness') or mat.get('ao') or mat.get('metalness')) + '_%08x' % (zlib.crc32(repr(key[3:6]).encode()))
            out['orm'] = self._save_png(np.stack([R, G, B], 2), stem + '_ORM', outdir)
            if gloss: out['roughness'] = self._save_png(G[:, :, None], stem + '_roughness', outdir)
            if metal: out['metallic'] = self._save_png(B[:, :, None], stem + '_metallic', outdir)
            if ao: out['occlusion'] = self._save_png(R[:, :, None], stem + '_occlusion', outdir)
            out['has'] = dict(ao=ao is not None, roughness=gloss is not None, metal=metal is not None)
        cache[key] = out
        return out

    def write_materials(self, model, groups, outdir, fname, prefix='', mode='w'):
        used = sorted({g['materialID'] for _, g in groups})
        with open(os.path.join(outdir, fname), mode) as f:
            f.write('# PBR MTL: map_Kd=base colour (RGBA if the material has an opacity map) map_Ke=emission map_Pr=roughness (=1-gloss, linear) map_Pm=metalness norm=normal map (+Y up) map_Ka=ambient occlusion\n')
            for k in used:
                mat = model['materials'][k]
                f.write('newmtl %smat%d\nKd 1 1 1\nKa 1 1 1\nPr 1\nPm 0\n' % (prefix, k))
                maps = self.pbr_maps(mat, outdir)
                for slot, key in (('baseColor', 'map_Kd'), ('emissive', 'map_Ke'), ('roughness', 'map_Pr'), ('metallic', 'map_Pm'), ('normal', 'norm'), ('occlusion', 'map_Ka')):
                    if maps.get(slot):
                        f.write('%s %s\n' % (key, maps[slot]))
                if maps.get('emissive'):
                    f.write('Ke 1 1 1\n')
                for slot in ('lean1', 'lightmap', 'burnMap'):
                    if mat.get(slot):
                        f.write('# %s: %s (not converted)\n' % (slot, mat[slot]))
                f.write('\n')
        return fname

    def export(self, model, outdir, tag=''):
        P, UV, N = None, None, None
        vbi = None
        groups = [(me['bone'] or 'mesh%d' % me['mesh'], g) for me in model['meshes'] for g in me['groups']]
        if not groups:
            raise AssertionError('no meshes (skeleton-only assembly node, %d bones)' % len(model['bones']))
        used_vb = sorted({g['vb'] for _, g in groups})
        Ps, UVs, Ns, voff = [], [], [], {}
        for vbi in used_vb:                    # a model may draw from several vertex buffers: concatenate them
            voff[vbi] = sum(len(x) for x in Ps)
            p, u, n = self.vertices(vbi); Ps.append(p); UVs.append(u); Ns.append(n)
        P, UV, N = np.concatenate(Ps), np.concatenate(UVs), np.concatenate(Ns)
        fn = os.path.join(outdir, safe_name(model['name']) + tag + '.obj')
        mtl = self.write_materials(model, groups, outdir, safe_name(model['name']) + tag + '.mtl')
        with open(fn, 'w') as f:
            f.write('# %s from %s. Z-up. objects = bone__group_states_material; vertex buffers: %s\n' % (model['name'], os.path.basename(getattr(self, 'blp_path', '')), ', '.join(self.vbs[v]['name'] for v in used_vb)))
            for v in P: f.write('v %.5f %.5f %.5f\n' % tuple(v))
            for v in UV: f.write('vt %.5f %.5f\n' % (v[0], 1 - v[1]))
            for v in N: f.write('vn %.5f %.5f %.5f\n' % tuple(v))
            f.write('mtllib %s\n' % os.path.basename(mtl))
            for bone, g in groups:
                IDX = self.indices(g['ib'])
                f.write('o %s__g%d_%s_mat%d\n' % (bone, g['group'], '+'.join(g['states']) or 'none', g['materialID']))
                f.write('usemtl mat%d\n' % g['materialID'])
                for t in (IDX[g['firstIndex']:g['firstIndex'] + g['indexCount']] + g['baseVertex'] + voff[g['vb']]).reshape(-1, 3) + 1:
                    f.write('f ' + ' '.join('%d/%d/%d' % (x, x, x) for x in t) + '\n')
        json.dump(dict({k: v for k, v in model.items() if k != 'skeletons'}, skeletonNames=[sk['name'] for sk in model.get('skeletons', [])], vertexBuffers=[self.vbs[v]['name'] for v in used_vb], stateBits=STATE_BITS), open(os.path.join(outdir, safe_name(model['name']) + tag + '.json'), 'w'), indent=1)
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
