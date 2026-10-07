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
