"""Regression tests.  python -m unittest discover tests     (the game-backed tests are skipped when Civ6 is not found)"""
import glob, json, os, shutil, sys, tempfile, unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
import blp_validate
import civ6_blp_export as ex


class ValidatorOnSyntheticFiles(unittest.TestCase):
    """The validator must flag the original defect (no mtllib / usemtl) and other inconsistencies, without needing the game."""

    def setUp(self):
        self.d = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.d, ignore_errors=True)

    def write(self, name, text):
        p = os.path.join(self.d, name)
        with open(p, 'w') as f:
            f.write(text)
        return p

    GOOD = 'mtllib m.mtl\nv 0 0 0\nv 1 0 0\nv 0 1 0\nvt 0 0\nvt 1 0\nvt 0 1\nvn 0 0 1\nvn 0 0 1\nvn 0 0 1\no a__g0_Worked_mat1\nusemtl mat1\nf 1/1/1 2/2/2 3/3/3\n'

    def check(self, obj):
        out = []
        blp_validate.check_obj(self.write('x.obj', obj), out)
        return [m for sev, _, m in out if sev == 'error']

    def test_good(self):
        self.write('m.mtl', 'newmtl mat1\nKd 1 1 1\n')
        self.assertEqual(self.check(self.GOOD), [])

    def test_missing_mtllib_and_usemtl(self):
        self.write('m.mtl', 'newmtl mat1\n')
        errs = self.check(self.GOOD.replace('mtllib m.mtl\n', '').replace('usemtl mat1\n', ''))
        self.assertTrue(any('mtllib' in e for e in errs) and any('no usemtl' in e for e in errs), errs)

    def test_undefined_material_missing_texture_bad_index(self):
        self.write('m.mtl', 'newmtl mat2\nmap_Kd nothere.png\n')
        errs = self.check(self.GOOD.replace('f 1/1/1 2/2/2 3/3/3', 'f 1/1/1 2/2/2 9/3/3'))
        self.assertTrue(any('no newmtl' in e for e in errs), errs)
        self.assertTrue(any('out of range' in e for e in errs), errs)


@unittest.skipUnless(ex.find_game(None), 'Civ6 install not found')
class ExportedBabylon(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import blp_textures
        from blp_models import Landmarks
        cls.game = ex.find_game(None)
        blp_textures.set_game(cls.game)
        cls.out = tempfile.mkdtemp()
        cls.landmarks = os.path.join(cls.game, 'DLC', 'Babylon', 'Platforms', 'Windows', 'BLPs', 'landmarks')
        cls.units = os.path.join(cls.game, 'DLC', 'Babylon', 'Platforms', 'Windows', 'BLPs', 'units')
        cls.blps = [('Babylon', p) for p in sorted(glob.glob(os.path.join(cls.landmarks, '*.blp')) + glob.glob(os.path.join(cls.units, '*.blp')))]
        if not cls.blps:
            raise unittest.SkipTest('Babylon DLC not installed')
        for pkg, p in cls.blps:
            ex.export_blp(pkg, p, cls.out, ['Worked'])
        cls.L = {os.path.basename(p): Landmarks(p) for _, p in cls.blps}

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.out, ignore_errors=True)

    def models(self):
        for obj in sorted(glob.glob(os.path.join(self.out, '*', 'models', '*.obj'))):
            blp = os.path.basename(os.path.dirname(os.path.dirname(obj))).split('__', 1)[1] + '.blp'
            yield obj, json.load(open(os.path.splitext(obj)[0] + '.json')), self.L[blp]

    def test_whole_export_validates(self):
        issues, n = blp_validate.validate_tree(self.out)
        self.assertGreater(n, 50)
        self.assertEqual([i for i in issues if i[0] == 'error'], [])

    def test_mtllib_usemtl_and_counts(self):
        multi_vb = repeated_mat = checked = 0
        for obj, j, L in self.models():
            o = blp_validate.parse_obj(obj)
            self.assertEqual(len(o['mtllibs']), 1, obj)
            self.assertEqual(o['mtllibs'][0], os.path.splitext(os.path.basename(obj))[0] + '.mtl', obj)
            newmtl, _ = blp_validate.parse_mtl(os.path.join(os.path.dirname(obj), o['mtllibs'][0]))
            groups = [g for me in j['meshes'] for g in me['groups']]
            for ob in o['objects']:
                self.assertIsNotNone(ob['mtl_before_faces'], (obj, ob['name']))
                self.assertIn(ob['mtl_before_faces'], newmtl, obj)
            # every group's usemtl equals its materialID (not parsed from the name)
            self.assertEqual([ob['mtl_before_faces'] for ob in o['objects']], ['mat%d' % g['materialID'] for g in groups], obj)
            used_vb = {g['vb'] for g in groups}
            nv = sum(L.vbs[v]['count'] for v in used_vb)
            self.assertEqual((o['v'], o['vt'], o['vn']), (nv, nv, nv), obj)
            self.assertEqual(sum(ob['faces'] for ob in o['objects']), sum(g['indexCount'] for g in groups) // 3, obj)
            multi_vb += len(used_vb) > 1
            mats = [g['materialID'] for g in groups]
            repeated_mat += len(mats) != len(set(mats))
            checked += 1
        self.assertGreater(checked, 50)
        self.assertGreater(repeated_mat, 0, 'sample has no model with repeated material IDs')
        print('\n  checked %d OBJs; %d use several vertex buffers, %d repeat a material ID' % (checked, multi_vb, repeated_mat))

    def test_no_none_names(self):
        for obj, _, _ in self.models():
            self.assertNotRegex(open(obj).read(), r'(?m)^o None__', obj)

    def test_every_model_has_gltf_with_pbr_materials(self):
        n = 0
        for obj, j, _ in self.models():
            g = os.path.splitext(obj)[0] + '.gltf'
            self.assertTrue(os.path.exists(g), g)
            d = json.load(open(g))
            for m in d.get('materials', []):
                self.assertIn('extras', m)
                if 'metallicRoughnessTexture' in m['pbrMetallicRoughness']:
                    n += 1
        self.assertGreater(n, 0)

    def test_states_option_exports_only_that_state(self):
        out = tempfile.mkdtemp()
        try:
            pkg, p = next(b for b in self.blps if b[1].endswith('city_buildings.blp'))
            ex.export_blp(pkg, p, out, ['Worked'], model_states=['Worked'])
            gl = glob.glob(os.path.join(out, '*', 'models', '*__Worked.gltf'))
            self.assertTrue(gl)
            for g in gl:
                for me in json.load(open(g))['meshes']:
                    for pr in me['primitives']:
                        self.assertIn('Worked', pr['extras']['states'], g)
            for o in glob.glob(os.path.join(out, '*', 'models', '*__Worked.obj')):
                for line in open(o):
                    if line.startswith('o '):
                        self.assertIn('Worked', line, o)
        finally:
            shutil.rmtree(out, ignore_errors=True)


if __name__ == '__main__':
    unittest.main()


# --------------------------------------------------------------------------- artdefs / dependency report
import blp_artdefs


def _el(name, fields='', children=''):
    return '<Element><m_Fields><m_Values>%s</m_Values></m_Fields><m_ChildCollections>%s</m_ChildCollections><m_Name text="%s"/></Element>' % (fields, children, name)


def _coll(name, *els):
    return '<Element><m_CollectionName text="%s"/>%s</Element>' % (name, ''.join(els))


def _str(param, v):
    return '<Element class="AssetObjects..StringValue"><m_Value text="%s"/><m_ParamName text="%s"/></Element>' % (v, param)


def _blp(entry, pkg):
    return '<Element class="AssetObjects..BLPEntryValue"><m_EntryName text="%s"/><m_BLPPackage text="%s"/><m_ParamName text="Asset"/></Element>' % (entry, pkg)


def _artdef(path, *roots):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w') as f:
        f.write('<AssetObjects..ArtDefSet><m_Version>1</m_Version><m_TemplateName text="Units"/><m_RootCollections>%s</m_RootCollections></AssetObjects..ArtDefSet>' % ''.join(roots))


class ArtdefsOnSyntheticFiles(unittest.TestCase):
    """Base defines a unit + a head bin, a DLC extends the bin: merging, bin resolution and part statuses."""

    def setUp(self):
        self.g = tempfile.mkdtemp()
        unit = _el('UNIT_X', _str('Culture', 'Any'), _coll('Members', _el('M1', _str('Type', 'Hero'))))
        atts = _coll('Attachments',
                     _el('Body', _str('Point', 'Root'), _coll('Bins', _el('Bodies/Hero'))),
                     _el('Head', _str('Point', 'Root'), _coll('Bins', _el('Heads/Hero'))))
        member = _el('Hero', '', _coll('Cultures', _el('Any', '', _coll('Variations', _el('A', '', atts)))))
        _artdef(os.path.join(self.g, 'Base', 'ArtDefs', 'Units.artdef'), _coll('Units', unit), _coll('UnitMemberTypes', member))
        bins = lambda *groups: _coll('UnitAttachmentBins', *groups)
        grp = lambda b, g, entry: _el(b, '', _coll('Groups', _el(g, '', _coll('Cultures', _el('Any', '', _coll('Assets', _el('a', _blp(entry, 'units/units'))))))))
        _artdef(os.path.join(self.g, 'Base', 'ArtDefs', 'Unit_Bins.artdef'), bins(grp('Bodies', 'Hero', 'Hero_Body')))
        _artdef(os.path.join(self.g, 'DLC', 'Pack', 'ArtDefs', 'Unit_Bins.artdef'), bins(grp('Heads', 'Hero', 'Hero_Head')))

    def tearDown(self):
        shutil.rmtree(self.g, ignore_errors=True)

    def test_composition_and_statuses(self):
        u = blp_artdefs.unit_compositions(self.g)['UNIT_X']
        at = u['members'][0]['cultures'][0]['variations'][0]['attachments']
        self.assertEqual([(a['attachment'], a['point']) for a in at], [('Body', 'Root'), ('Head', 'Root')])
        self.assertEqual(at[0]['bins'][0]['cultures'][0]['assets'][0]['entry'], 'Hero_Body')
        self.assertEqual(at[1]['bins'][0]['cultures'][0]['assets'][0]['entry'], 'Hero_Head')     # the DLC's bin is merged in
        self.assertEqual(at[1]['bins'][0]['src'], ['Pack'])
        res = [dict(package='Pack', path=self.g + '/DLC/Pack/Platforms/Windows/BLPs/units/units.blp', models=1,
                    modelInfo=[dict(name='Hero_Body', cls='Unit', files=['Hero_Body'], bones=3, tris=10, textures=[], skinned=True)])]
        idx = blp_artdefs.ModelIndex(res, self.g)
        self.assertEqual(idx.find('Hero_Body', 'units/units')[0], 'exported')
        self.assertEqual(idx.find('Hero_Head', 'units/units')[0], 'not-in-examined-blps')
        res.append(dict(package='Base', path=self.g + '/Base/Platforms/Windows/BLPs/units/units.blp', models=0))
        self.assertEqual(blp_artdefs.ModelIndex(res, self.g).find('Hero_Head', 'units/units')[0], 'blp-unreadable')
        self.assertEqual(blp_artdefs.ModelIndex([], self.g).find('Hero_Head', 'units/units')[0], 'blp-not-examined')

    def test_cultures_of_one_asset_collapse_into_one_row(self):
        rows = [dict(entry='Hands', culture=c, tint=t) for c, t in (('Any', 'Pale'), ('Asian', 'Tan'), ('African', 'Tan'))] + [dict(entry='Other', culture='Any', tint='Pale')]
        merged = {r['entry']: r['tint'] for r in blp_artdefs._merge_cultures(rows)}
        self.assertEqual(merged, {'Hands': 'Pale (Any); Tan (Asian, African)', 'Other': 'Pale'})
        same = blp_artdefs._merge_cultures([dict(entry='H', culture=c, tint='Tan') for c in ('Any', 'Asian')])
        self.assertEqual([r['tint'] for r in same], ['Tan'])

    def test_report_files(self):
        res = [dict(package='Pack', path=self.g + '/DLC/Pack/Platforms/Windows/BLPs/units/units.blp', models=1,
                    modelInfo=[dict(name='Hero_Body', cls='Unit', files=['Hero_Body'], bones=3, tris=10, textures=['T'], skinned=True)])]
        out, nunits, nmodels = blp_artdefs.report_dependencies(self.g, res, self.g)
        self.assertEqual((nunits, nmodels), (1, 1))
        md = open(os.path.join(out, 'units', 'UNIT_X.md'), encoding='utf-8').read()
        self.assertIn('Hero_Head', md)
        self.assertIn('Parts that are not in the export', md)
        self.assertIn('UNIT_X (unit part)', open(os.path.join(out, 'catalog.md'), encoding='utf-8').read())


@unittest.skipUnless(ex.find_game(None), 'Civ6 install not found')
class AnansiReport(unittest.TestCase):
    def test_anansi_parts(self):
        comps = blp_artdefs.unit_compositions(ex.find_game(None))
        u = comps['UNIT_HERO_ANANSI']
        parts = {a['attachment']: [x['entry'] for br in a['bins'] for c in br['cultures'] for x in c['assets']]
                 for a in u['members'][0]['cultures'][0]['variations'][0]['attachments']}
        self.assertEqual(parts, {'Armor': ['Anansi_ArmorA'], 'Body': ['Anansi_Body'], 'Head': ['Male_African_Head_01']})

    def test_base_units_blp_is_readable(self):
        """Regression: Base units/units.blp (> 32k allocations, type-name indices > 10000) was read as 0 models."""
        from blp_models import Landmarks
        g = ex.find_game(None)
        base = os.path.join(g, 'Base', 'Platforms', 'Windows', 'BLPs', 'units', 'units.blp')
        babylon = os.path.join(g, 'DLC', 'Babylon', 'Platforms', 'Windows', 'BLPs', 'units', 'units.blp')
        if not (os.path.exists(base) and os.path.exists(babylon)):
            self.skipTest('units BLPs not found')
        names = {m['name'] for m in Landmarks(base).models}
        self.assertGreater(len(names), 500)
        self.assertIn('Male_African_Head_01', names)
        self.assertEqual(len([m for m in Landmarks(babylon).models if m['name']]), 36)       # the shifted-by-one table must not be picked


@unittest.skipUnless(ex.find_game(None), 'Civ6 install not found')
class AttachmentPoints(unittest.TestCase):
    def test_catapult_points_match_its_ast(self):
        """CatapultA: 14 points, names/bones/positions as in the .ast (slots are sorted by bone; names are found by FNV-1a hash)."""
        from blp_models import Landmarks, _fnv1a
        p = os.path.join(ex.find_game(None), 'Base', 'Platforms', 'Windows', 'BLPs', 'units', 'units.blp')
        if not os.path.exists(p):
            self.skipTest('Base units.blp not found')
        md = next(m for m in Landmarks(p).models if m['name'] == 'CatapultA')
        pts = {a['name']: a for a in md['attachments']}
        self.assertEqual(len(pts), 14)
        self.assertEqual(pts['Operator']['bone'], 'CatapultOperator')
        self.assertEqual(pts['FX_Hitloc_01']['bone'], 'Catapult_Body')
        self.assertEqual([round(x, 2) for x in pts['FX_Hitloc_01']['matrix'][12:15]], [13.5, 4.0, 0.75])
        self.assertEqual(pts['FX_Boulder_Loc_01']['scale'], 3.0)
        self.assertEqual(_fnv1a('SpawnProjectile'), 0x3bba96bf)


@unittest.skipUnless(ex.find_game(None), 'Civ6 install not found')
class AnimationBindings(unittest.TestCase):
    def test_catapult_bindings_match_its_ast(self):
        """CatapultA.ast has 47 animation bindings (slot -> animation); the BLP stores the same, with slot ids in place of names."""
        from blp_models import Landmarks
        p = os.path.join(ex.find_game(None), 'Base', 'Platforms', 'Windows', 'BLPs', 'units', 'units.blp')
        if not os.path.exists(p):
            self.skipTest('Base units.blp not found')
        md = next(m for m in Landmarks(p).models if m['name'] == 'CatapultA')
        slots = {x['slot']: x['animation'] for x in md['animationSlots']}
        self.assertEqual(len(md['animationSlots']), 47)
        self.assertEqual(len(md['animations']), 14)
        self.assertEqual(slots['ATTACK_A'], 'ANIMATION_Catapult_AttackA')
        self.assertEqual(slots['RUN_COMBAT'], 'ANIMATION_Catapult_RunFwdA')
        self.assertEqual(slots['BREATHING_A'], 'ANIMATION_RedcoatGun_Idle')
        self.assertEqual(slots['REACT_RANGED_C'], 'ANIMATION_Catapult_BraceC')

        self.assertEqual(md['stateGraph'], ['potential_any_graph'])
        tl = {t['slot']: t for t in md['timelines']}                       # timelines and their triggers, as in the .ast
        self.assertEqual(len(tl['ATTACK_A']['triggers']), 6)
        self.assertEqual([t['type'] for t in tl['RUN_A']['triggers']], ['ASSET_FX', 'SOUND'])
        self.assertEqual(tl['RUN_A']['triggers'][0]['attachment'], 'FX_Dust_01')
        self.assertAlmostEqual(tl['PERSISTENT']['duration'], 2.4583, places=3)
        self.assertEqual(sum(len(t['triggers']) for t in md['timelines']), 20)


@unittest.skipUnless(ex.find_game(None), 'Civ6 install not found')
class StaticGltfIsYUp(unittest.TestCase):
    def test_static_meshes_hang_under_the_z_up_to_y_up_node(self):
        """Regression (0.2.0 - 0.3.1): static meshes were scene roots beside the rotation node, so buildings lay on their side."""
        import json, tempfile, blp_gltf, blp_textures
        from blp_models import Landmarks
        g = ex.find_game(None)
        p = os.path.join(g, 'Base', 'Platforms', 'Windows', 'BLPs', 'landmarks', 'tilebases.blp')
        if not os.path.exists(p):
            self.skipTest('Base tilebases.blp not found')
        blp_textures.set_game(g)
        L = Landmarks(p)
        md = next(m for m in L.models if m['name'] == 'IMP_Farm_AN_Bld_A')
        with tempfile.TemporaryDirectory() as d:
            gl = json.load(open(blp_gltf.export_gltf(L, md, d, '')))
        parent = {c: i for i, n in enumerate(gl['nodes']) for c in n.get('children', [])}
        meshes = [i for i, n in enumerate(gl['nodes']) if 'mesh' in n and 'skin' not in n]
        self.assertTrue(meshes)
        for i in meshes:
            while i in parent:
                i = parent[i]
            self.assertEqual(i, 0)                                                 # root of the chain is the Z-up -> Y-up node
        self.assertEqual(gl['scenes'][0]['nodes'], [0])


class CombineUnitParts(unittest.TestCase):
    """blp_units.combine: parts that share a rig are merged by bone name; static parts hang on attachment points."""
    def test_merge(self):
        import blp_units
        xf = lambda parent: dict(parent=parent, flags=0, pos=[0, 0, 0], quat=[0, 0, 0, 1], scaleShear=[1, 0, 0, 0, 1, 0, 0, 0, 1], invWorld=list(range(16)))
        class FakeL:                                                                  # just what combine needs: vertex-buffer formats
            vbs = [dict(fmt=1719251312), dict(fmt=828177625)]
        g = lambda vb, mat: dict(group=0, states=['Construction'], materialID=mat, vb=vb, ib=0, firstIndex=0, indexCount=3, baseVertex=0, vertCount=3)
        body = dict(name='Body', bones=['Root', 'Spine'], boneXforms=[xf(-1), xf(0)], skeletons=[dict(name='Root', bones=['Root', 'Spine'], xforms=[xf(-1), xf(0)])],
                    meshes=[dict(mesh=0, bone=None, groups=[g(0, 0)])], meshBindings=[dict(fromStart=0, fromCount=2, fromSkeleton=0)], boneIds=[0, 1],
                    materials=[dict(index=0, diffuse='A')], attachments=[dict(name='Hat', bone='Spine', boneIndex=1, matrix=list(range(16)), scale=1.0)], animations=[])
        armor = dict(name='Armor', bones=['Root', 'Spine', 'Cape'], boneXforms=[xf(-1), xf(0), xf(1)], skeletons=[dict(name='Root', bones=['Root', 'Spine', 'Cape'], xforms=[xf(-1), xf(0), xf(1)])],
                     meshes=[dict(mesh=0, bone=None, groups=[g(0, 0)])], meshBindings=[dict(fromStart=0, fromCount=2, fromSkeleton=0)], boneIds=[2, 0],
                     materials=[dict(index=0, diffuse='B')], attachments=[], animations=[])
        helm = dict(name='Helm', bones=['Helm'], boneXforms=[xf(-1)], skeletons=[dict(name='Helm', bones=['Helm'], xforms=[xf(-1)])],
                    meshes=[dict(mesh=0, bone='Helm', groups=[g(1, 0)])], meshBindings=[dict(fromStart=0, fromCount=1, fromSkeleton=0)], boneIds=[0],
                    materials=[dict(index=0, diffuse='C')], attachments=[], animations=[])
        parts = [dict(entry='Body', point='Root', model=body, L=FakeL()), dict(entry='Armor', point='Root', model=armor, L=FakeL()),
                 dict(entry='Helm', point='Hat', model=helm, L=FakeL())]
        m = blp_units.combine(parts, 0, 'X')
        self.assertEqual(m['bones'], ['Root', 'Spine', 'Cape'])                      # the cape was added to the shared skeleton
        self.assertEqual(m['skeletons'][0]['xforms'][2]['parent'], 1)
        self.assertEqual([x['materialID'] for me in m['meshes'] for x in me['groups']], [0, 1, 2])
        self.assertEqual(m['boneIds'], [0, 1, 2, 0])                                  # armor's local ids remapped to the shared skeleton
        self.assertEqual(m['meshParent'], {2: 'Hat'})                                  # the static helmet hangs on the body's Hat point
        self.assertEqual(m['_warnings'], [])


class Bundle(unittest.TestCase):
    def test_euler_matches_the_ast_convention(self):
        """Checked against shipped .ast files: TankA Gun (0,-90deg,0) and SiegeTower dust points (0,180deg,0) scale 0.4."""
        import math, blp_bundle
        e, sc = blp_bundle.euler_zyx([0, 0, 1, 0, 0, 1, 0, 0, -1, 0, 0, 0, 0, 0, 2, 1])
        self.assertEqual([round(x, 6) for x in e], [0.0, round(-math.pi / 2, 6), 0.0]); self.assertAlmostEqual(sc, 1.0)
        e, sc = blp_bundle.euler_zyx([-0.4, 0, 0, 0, 0, 0.4, 0, 0, 0, 0, -0.4, 0, 6.6, 8.25, 0, 1])
        self.assertAlmostEqual(abs(e[1]), math.pi, places=5); self.assertAlmostEqual(sc, 0.4)

    def test_trigger_names_resolve(self):
        from blp_models import trigger_name, _fnv1a
        self.assertEqual(trigger_name('%08x' % _fnv1a('FX_Dust_BatteringRam_01')), 'FX_Dust_BatteringRam_01')

    @unittest.skipUnless(ex.find_game(None), 'Civ6 install not found')
    def test_catapult_bundle(self):
        import json, tempfile, xml.etree.ElementTree as ET, blp_bundle
        from blp_models import Landmarks
        p = os.path.join(ex.find_game(None), 'Base', 'Platforms', 'Windows', 'BLPs', 'units', 'units.blp')
        if not os.path.exists(p):
            self.skipTest('Base units.blp not found')
        md = next(m for m in Landmarks(p).models if m['name'] == 'CatapultA')
        with tempfile.TemporaryDirectory() as d:
            blp_bundle.write_bundle(md, d, dict(package='Base', blp='units.blp'))
            j = json.load(open(os.path.join(d, 'CatapultA.asset.json')))
            root = ET.parse(os.path.join(d, 'CatapultA.approx.ast')).getroot()               # well-formed XML
        self.assertEqual(len(j['attachmentPoints']), 14)
        self.assertEqual(len(j['animationBindings']), 47)
        self.assertEqual(root.find('m_BehaviorData/m_dsgName').get('text'), 'potential_any_graph')
        self.assertEqual(len(root.findall('m_BehaviorData/m_behaviorDataSets/m_attachmentPoints/m_Points/Element')), 14)
        fx = [t.find('m_FXName').get('text') for t in root.iter('Element') if t.find('m_FXName') is not None]
        self.assertIn('FX_Dust_BatteringRam_01', fx)
        self.assertIn('Catapult_Move_Loop', fx)


class RawVariants(unittest.TestCase):
    def test_state_copies_collapse_and_pillaged_meshes_split_off(self):
        from blp_models import raw_variants
        g = lambda sts, mat, vb: dict(group=0, states=sts, materialID=mat, vb=vb, ib=vb, firstIndex=0, indexCount=3, baseVertex=0, vertCount=3)
        md = dict(name='X', meshes=[
            dict(mesh=0, bone='a', groups=[g(['Unworked'], 1, 5), g(['Worked'], 2, 5), g(['Construction', 'Unbuilt'], 3, 5)]),
            dict(mesh=1, bone='b', groups=[g(['Pillaged'], 4, 6)]),
            dict(mesh=2, bone='c', groups=[g(['Pillaged', 'Unworked'], 5, 7)])])
        v = dict(raw_variants(md))
        self.assertEqual(set(v), {'', '_PIL'})
        self.assertEqual([(me['bone'], [(x['materialID'], x['states']) for x in me['groups']]) for me in v['']['meshes']], [('a', [(2, ['Default'])])])
        self.assertEqual([me['bone'] for me in v['_PIL']['meshes']], ['b', 'c'])


@unittest.skipUnless(ex.find_game(None), 'Civ6 install not found')
class RawIceRink(unittest.TestCase):
    def test_ice_rink_splits_like_its_fgx_files(self):
        """IMP_Hockey_IceRink_MOD_PSC: 26 meshes in the .fgx, 24 in the _PIL .fgx (counts from the SDK samples' .geo files)."""
        from blp_models import Landmarks, raw_variants
        p = os.path.join(ex.find_game(None), 'DLC', 'Expansion2', 'Platforms', 'Windows', 'BLPs', 'landmarks', 'tilebases.blp')
        if not os.path.exists(p):
            self.skipTest('Expansion2 tilebases.blp not found')
        md = next(m for m in Landmarks(p).models if m['name'] == 'IMP_Hockey_IceRink_MOD_PSC')
        self.assertEqual({suf: len(v['meshes']) for suf, v in raw_variants(md)}, {'': 26, '_PIL': 24})


class FileNames(unittest.TestCase):
    def test_safe_name(self):
        from blp_models import safe_name
        self.assertEqual(safe_name('|IMP_Chateau_Wall_PIL'), '_IMP_Chateau_Wall_PIL')
        self.assertEqual(safe_name('DIS_HBR_Classical_Shack w/Door'), 'DIS_HBR_Classical_Shack w_Door')
        self.assertEqual(safe_name('plain_Name-01'), 'plain_Name-01')


@unittest.skipUnless(ex.find_game(None), 'Civ6 install not found')
class MultiSkeletonModels(unittest.TestCase):
    def test_skin_uses_the_skeleton_named_by_its_binding(self):
        """RES_Ele_Grass04 holds a 1-bone grass skeleton and an 18-bone elephant; the skinned mesh binds to the second."""
        import blp_gltf, blp_textures
        from blp_models import Landmarks
        g = ex.find_game(None)
        p = os.path.join(g, 'Base', 'Platforms', 'Windows', 'BLPs', 'environment', 'clutter.blp')
        if not os.path.exists(p):
            self.skipTest('Base clutter.blp not found')
        blp_textures.set_game(g)
        L = Landmarks(p)
        md = next(m for m in L.models if m['name'] == 'RES_Ele_Grass04')
        self.assertEqual([len(s['bones']) for s in md['skeletons']], [1, 18])
        self.assertEqual([b['fromSkeleton'] for b in md['meshBindings']], [0, 1])
        out = tempfile.mkdtemp()
        try:
            fn = blp_gltf.export_gltf(L, md, out)
            with open(fn) as f:
                j = json.load(f)
            self.assertEqual(len(j['skins']), 1)
            self.assertEqual(len(j['skins'][0]['joints']), 17)
            issues = []
            blp_validate.check_gltf(fn, issues)
            self.assertEqual(issues, [])
        finally:
            shutil.rmtree(out, ignore_errors=True)
