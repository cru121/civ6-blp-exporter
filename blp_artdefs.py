"""Civ6 .artdef reader and the dependency / catalog report.

An artdef is XML. Roots are collections (UnitMemberTypes, UnitAttachmentBins, Buildings ...), each holding named elements with
fields (m_Values) and child collections. Base ArtDefs/ and every DLC's ArtDefs/ contribute: DLC elements are merged into
the base ones by name (new ones appended, same-named ones overwrite fields / extend child collections).

Units are built from parts (Units.artdef): Units -> Members -> UnitMemberTypes -> Cultures -> Variations -> Attachments
(Armor / Body / Head / Weapon ...; each has a Point on the root skeleton, a Tint and Bins) -> Unit_Bins.artdef
UnitAttachmentBins -> Groups -> Cultures -> Assets -> BLPEntryValue (entry name + BLP package such as 'units/units').

report_dependencies() writes, under <out>/dependency_report/:
  units/<UNIT>.md + units.json   each unit's parts and where every part lives (exported / in an unreadable BLP / not examined)
  catalog.md + catalog.json      every exported model with triangles, bones, textures, files and the artdef entries that reference it
  index.md                       overview
"""
import functools, glob, json, os, re
import xml.etree.ElementTree as ET


# ---------------------------------------------------------------- reading
def _val(e):
    d = dict(cls=(e.get('class') or '').replace('AssetObjects..', ''))
    for c in e:
        d[c.tag] = c.get('text') if c.get('text') is not None else (c.text or '').strip()
    return d


def _element(e):
    node = dict(name=None, fields=[], children={})
    for c in e:
        if c.tag == 'm_Name':
            node['name'] = c.get('text')
        elif c.tag == 'm_Fields':
            vs = c.find('m_Values')
            node['fields'] = [_val(v) for v in vs] if vs is not None else []
        elif c.tag == 'm_ChildCollections':
            for coll in c:
                node['children'][coll.find('m_CollectionName').get('text')] = [_element(x) for x in coll if x.tag == 'Element']
    return node


def load(path):
    """-> {root collection name: [element nodes]}"""
    r = ET.parse(path).getroot()
    out = {}
    for rc in r.find('m_RootCollections'):
        out[rc.find('m_CollectionName').get('text')] = [_element(x) for x in rc if x.tag == 'Element']
    return out


def _index(nodes):
    d = {}
    for i, n in enumerate(nodes):
        d[n['name'] if n['name'] is not None else '#%d' % i] = n
    return d


def _tag(node, src):
    node.setdefault('src', [])
    if src not in node['src']:
        node['src'].append(src)
    for els in node['children'].values():
        for e in els:
            _tag(e, src)


def _merge(a, b):
    """merge element b (later package) into a (earlier)"""
    fields = {f.get('m_ParamName'): f for f in a['fields']}
    for f in b['fields']:
        fields[f.get('m_ParamName')] = f
    a['fields'] = list(fields.values())
    for src in b.get('src', []):
        if src not in a.setdefault('src', []): a['src'].append(src)
    for cname, els in b['children'].items():
        mine = _index(a['children'].setdefault(cname, []))
        for e in els:
            if e['name'] in mine: _merge(mine[e['name']], e)
            else: a['children'][cname].append(e)


def load_merged(game, filename):
    """Base then each DLC (alphabetical): {root collection: {element name: node}}; node['src'] lists the packages that contributed."""
    paths = [('Base', os.path.join(game, 'Base', 'ArtDefs', filename))] + \
            [(os.path.basename(os.path.dirname(os.path.dirname(p))), p) for p in sorted(glob.glob(os.path.join(game, 'DLC', '*', 'ArtDefs', filename)))]
    merged = {}
    for pkg, p in paths:
        if not os.path.exists(p):
            continue
        try:
            roots = load(p)
        except ET.ParseError:
            continue
        for rc, nodes in roots.items():
            tgt = merged.setdefault(rc, {})
            for n in nodes:
                _tag(n, pkg)
                if n['name'] in tgt: _merge(tgt[n['name']], n)
                else: tgt[n['name']] = n
    return merged


def _f(node, param):
    for f in node['fields']:
        if f.get('m_ParamName') == param:
            return f.get('m_ElementName') or f.get('m_Value') or f.get('m_EntryName') or f.get('m_fValue') or f.get('m_bValue')
    return None


def blp_refs(node, path=()):
    """Every BLPEntryValue below node: (path of element names, entry name, BLP package, XLP class)."""
    here = path + ((node['name'],) if node['name'] else ())
    for f in node['fields']:
        if f.get('cls') == 'BLPEntryValue' and f.get('m_EntryName'):
            yield here, f['m_EntryName'], f.get('m_BLPPackage') or '', f.get('m_XLPClass') or ''
    for cname, els in node['children'].items():
        for e in els:
            yield from blp_refs(e, here)


# ---------------------------------------------------------------- units
def resolve_bin(bins, ref):
    """'Hero_Heads/Anansi' -> dict(bin, group, found, cultures=[dict(culture, tint, assets=[dict(element, entry, package, scale)])])"""
    bname, _, gname = ref.partition('/')
    out = dict(bin=bname, group=gname, found=False, cultures=[], src=[])
    b = bins.get(bname)
    g = next((x for x in (b['children'].get('Groups', []) if b else []) if x['name'] == gname), None)
    if not g:
        return out
    out['found'], out['src'] = True, g.get('src', [])
    for cu in g['children'].get('Cultures', []):
        assets = []
        for a in cu['children'].get('Assets', []):
            ent = next((f for f in a['fields'] if f.get('cls') == 'BLPEntryValue'), None)
            if ent:
                assets.append(dict(element=a['name'], entry=ent.get('m_EntryName'), package=ent.get('m_BLPPackage'), scale=_f(a, 'Scale')))
        out['cultures'].append(dict(culture=cu['name'], tint=_f(cu, 'Tint'), assets=assets))
    return out


def unit_compositions(game):
    U = load_merged(game, 'Units.artdef')
    Bn = load_merged(game, 'Unit_Bins.artdef')
    bins = Bn.get('UnitAttachmentBins', {})
    members = U.get('UnitMemberTypes', {})
    units = {}
    for uname, u in U.get('Units', {}).items():
        rec = dict(src=u.get('src', []), formation=_f(u, 'Formation'), culture=_f(u, 'Culture'), combat=_f(u, 'UnitCombat'), members=[])
        for m in u['children'].get('Members', []):
            mt = _f(m, 'Type'); mnode = members.get(mt)
            mem = dict(type=mt, scale=_f(m, 'Scale'), count=_f(m, 'Count'), found=mnode is not None, src=mnode.get('src', []) if mnode else [], cultures=[])
            for cu in (mnode['children'].get('Cultures', []) if mnode else []):
                c = dict(culture=cu['name'], variations=[])
                for v in cu['children'].get('Variations', []):
                    var = dict(variation=v['name'], scale=_f(v, 'Scale'), attachments=[])
                    for at in v['children'].get('Attachments', []):
                        var['attachments'].append(dict(attachment=at['name'], point=_f(at, 'Point'), tint=_f(at, 'Tint'),
                                                       bins=[resolve_bin(bins, b['name']) for b in at['children'].get('Bins', [])]))
                    c['variations'].append(var)
                mem['cultures'].append(c)
            rec['members'].append(mem)
        units[uname] = rec
    return units


# ---------------------------------------------------------------- linking to the exported models
def pkg_of(path):
    """'Base' or the DLC folder name a BLP path belongs to"""
    m = re.search(r'/DLC/([^/]+)/', path.replace(chr(92), '/'))
    return m.group(1) if m else 'Base'


def blp_rel(path):
    p = path.replace('\\', '/')
    i = p.lower().rfind('/blps/')
    return os.path.splitext(p[i + 6:] if i >= 0 else os.path.basename(p))[0]


class ModelIndex:
    """What was exported (from export_blp records) and which BLPs were examined / unreadable."""

    def __init__(self, results, game=None):
        self.game = game
        self.by_blp = {}          # blp rel -> [(pkg, rec)]
        self.models = {}          # (blp rel, model name) -> [(pkg, info)]
        for r in results:
            if not r.get('path'): continue
            rel, pkg = blp_rel(r['path']), pkg_of(r['path'])
            self.by_blp.setdefault(rel, []).append((pkg, r))
            for info in r.get('modelInfo', []):
                self.models.setdefault((rel, info['name']), []).append((pkg, info))

    @functools.lru_cache(maxsize=None)
    def installed_in(self, package):
        """packages (Base / DLC folder names) that ship BLPs/<package>.blp for the Windows platform"""
        if not self.game:
            return []
        rel = package.replace('/', os.sep) + '.blp'
        paths = [('Base', os.path.join(self.game, 'Base', 'Platforms', 'Windows', 'BLPs', rel))] +                 [(pkg_of(p), p) for p in glob.glob(os.path.join(self.game, 'DLC', '*', 'Platforms', 'Windows', 'BLPs', rel))]
        return [pkg for pkg, p in paths if os.path.exists(p)]

    def find(self, entry, package):
        hits = self.models.get((package, entry))
        if hits:
            return 'exported', hits
        examined = self.by_blp.get(package, [])
        if not examined:
            return 'blp-not-examined', ['installed in: ' + (', '.join(self.installed_in(package)) or '?')]
        notes = []
        for pkg, r in examined:
            notes.append('%s/%s: %s' % (pkg, package, r.get('error') or ('%d models read, none named %s' % (r.get('models', 0), entry))))
        other = [p for p in self.installed_in(package) if p not in {pkg for pkg, _ in examined}]
        if other:
            notes.append('also shipped by (not examined): ' + ', '.join(other[:5]) + (' and %d more' % (len(other) - 5) if len(other) > 5 else ''))
        unreadable = any(not r.get('models') for _, r in examined)      # an examined copy yielded nothing: the entry may be hiding in it
        return ('blp-unreadable' if unreadable else 'not-in-examined-blps'), notes


def _files(info):
    return ', '.join('%s.obj / .gltf' % f for f in info['files'])


def _part_rows(index, attachment, binref):
    rows = []
    for cu in binref['cultures']:
        for a in cu['assets']:
            status, hits = index.find(a['entry'], a['package'])
            rows.append(dict(attachment=attachment['attachment'], point=attachment['point'], tint=attachment['tint'] or cu['tint'], bin='%s/%s' % (binref['bin'], binref['group']),
                             culture=cu['culture'], element=a['element'], entry=a['entry'], blp=a['package'], status=status, hits=hits))
    return rows


STATUS_TEXT = {
    'exported': 'exported',
    'blp-unreadable': 'in a BLP the exporter could not read (0 models)',
    'blp-not-examined': 'BLP not examined (export that package too)',
    'not-in-examined-blps': 'not found in the examined BLPs',
}


def _merge_cultures(rows):
    """A bin lists the same asset once per culture (usually only the skin tint differs): one row per asset, tints grouped by culture."""
    out = {}
    for r in rows:
        out.setdefault(r['entry'], []).append(r)
    merged = []
    for entry, rs in out.items():
        by_tint = {}
        for r in rs:
            by_tint.setdefault(r['tint'] or '', []).append(r['culture'])
        if len(by_tint) == 1:
            tint = next(iter(by_tint))
        else:
            tint = '; '.join('%s (%s)' % (t or 'none', ', '.join(cs)) for t, cs in by_tint.items())
        merged.append(dict(rs[0], tint=tint))
    return merged


def _unit_md(name, u, index):
    L = ['# %s' % name, '',
         'Formation `%s`, culture `%s`, combat `%s`. Defined by: %s.' % (u['formation'], u['culture'], u['combat'], ', '.join(u['src']) or '?'), '']
    missing = []
    for mem in u['members']:
        L += ['## Member `%s` (count %s, scale %s)' % (mem['type'], mem['count'] or '1', mem['scale'] or '1'), '']
        if not mem['found']:
            L += ['_UnitMemberType not found in the artdefs._', '']; continue
        for cu in mem['cultures']:
            for var in cu['variations']:
                L += ['### Culture `%s`, variation `%s` (scale %s)' % (cu['culture'], var['variation'], var['scale']), '',
                      'All parts attach to the same skeleton at the listed point; a unit uses one asset per attachment (bins with several assets are a random choice).', '',
                      '| Attachment | Point | Tint | Bin / group | Asset entry | BLP | Status | Files / notes |', '|---|---|---|---|---|---|---|---|']
                for at in var['attachments']:
                    if not at['bins']:
                        L.append('| %s | %s | %s | - | - | - | no bin | |' % (at['attachment'], at['point'], at['tint'] or ''))
                    for br in at['bins']:
                        rows = _part_rows(index, at, br)
                        if not br['found']:
                            L.append('| %s | %s | %s | %s/%s | - | - | bin/group not found | |' % (at['attachment'], at['point'], at['tint'] or '', br['bin'], br['group']))
                        for r in _merge_cultures(rows):
                            note = ''
                            if r['status'] == 'exported':
                                pkg, info = r['hits'][0]
                                note = '%s (%s, %d tris, %d bones)' % (_files(info), pkg, info['tris'], info['bones'])
                            else:
                                note = '; '.join(r['hits'][:2]) if r['hits'] else ''
                                missing.append('%s (%s) - %s' % (r['entry'], r['blp'], STATUS_TEXT[r['status']]))
                            L.append('| %s | %s | %s | %s | `%s` | %s | %s | %s |' % (r['attachment'], r['point'], r['tint'] or '', r['bin'], r['entry'], r['blp'], STATUS_TEXT[r['status']], note))
                L.append('')
    if missing:
        L += ['## Parts that are not in the export', ''] + ['- %s' % m for m in sorted(set(missing))] + ['']
    L += ['_Animations and per-unit timelines are not resolved by this report._', '']
    return '\n'.join(L)


def _unit_part_entries(u):
    for mem in u['members']:
        for cu in mem['cultures']:
            for var in cu['variations']:
                for at in var['attachments']:
                    for br in at['bins']:
                        for c in br['cultures']:
                            for a in c['assets']:
                                yield a['entry'], a['package']


# ---------------------------------------------------------------- generic reverse references (buildings, districts, landmarks ...)
def reverse_references(game):
    """(entry, package) -> ['Buildings.artdef: Buildings > BUILDING_X > ...']"""
    refs = {}
    files = {}
    for p in [os.path.join(game, 'Base', 'ArtDefs')] + sorted(glob.glob(os.path.join(game, 'DLC', '*', 'ArtDefs'))):
        pkg = 'Base' if p.startswith(os.path.join(game, 'Base')) else os.path.basename(os.path.dirname(p))
        for f in glob.glob(os.path.join(p, '*.artdef')):
            files.setdefault(os.path.basename(f), []).append((pkg, f))
    for fname, lst in files.items():
        if fname in ('Units.artdef',):
            continue
        for pkg, f in lst:
            try:
                roots = load(f)
            except ET.ParseError:
                continue
            for rc, nodes in roots.items():
                for n in nodes:
                    for path, entry, package, cls in blp_refs(n):
                        refs.setdefault((entry, package), set()).add('%s [%s] %s > %s' % (fname[:-7], pkg, rc, ' > '.join(path[:4])))
    return refs


# ---------------------------------------------------------------- the report
def report_dependencies(game, results, outroot):
    out = os.path.join(outroot, 'dependency_report')
    os.makedirs(os.path.join(out, 'units'), exist_ok=True)
    index = ModelIndex(results, game)
    comps = unit_compositions(game)
    revrefs = reverse_references(game)
    used_by = {}                      # (entry, package) -> [unit parts]
    summary = []
    unit_json = {}
    for name, u in sorted(comps.items()):
        entries = set(_unit_part_entries(u))
        for e in entries:
            used_by.setdefault(e, []).append(name)
        # report only units with at least one part in an examined BLP set (follows what was exported)
        if not any(index.find(e, p)[0] == 'exported' for e, p in entries):
            continue
        with open(os.path.join(out, 'units', name + '.md'), 'w', encoding='utf-8') as f:
            f.write(_unit_md(name, u, index))
        parts = {e: index.find(e, p)[0] for e, p in sorted(entries)}
        miss = [e for e, s in parts.items() if s != 'exported']
        summary.append((name, len(parts), miss))
        unit_json[name] = dict(u, partStatus=parts)
    with open(os.path.join(out, 'units.json'), 'w', encoding='utf-8') as f:
        json.dump(unit_json, f, indent=1)

    # catalog
    cat = []
    lines = ['# Model catalog', '', 'Every exported model: size, textures, files, and the artdef entries that reference it (units: parts of UNIT_x; others: artdef > collection > element).', '']
    for r in sorted((r for r in results if r.get('modelInfo')), key=lambda r: (pkg_of(r['path']), blp_rel(r['path']))):
        rel = blp_rel(r['path'])
        lines += ['## %s / %s' % (pkg_of(r['path']), rel), '', '| Model | Class | Tris | Bones | Textures | Files | Referenced by |', '|---|---|---|---|---|---|---|']
        for info in sorted(r['modelInfo'], key=lambda i: i['name']):
            ub = ['%s (unit part)' % n for n in sorted(used_by.get((info['name'], rel), []))]
            ub += sorted(revrefs.get((info['name'], rel), []))
            cat.append(dict(package=pkg_of(r['path']), blp=rel, referencedBy=ub, **info))
            lines.append('| `%s` | %s | %d | %d | %s | %s | %s |' % (info['name'], info['cls'], info['tris'], info['bones'], ', '.join(info['textures']) or '-', _files(info), '<br>'.join(ub[:6]) + (' (+%d more)' % (len(ub) - 6) if len(ub) > 6 else '') if ub else '_none found in artdefs_'))
        lines.append('')
    with open(os.path.join(out, 'catalog.md'), 'w', encoding='utf-8') as f:
        f.write('\n'.join(lines))
    with open(os.path.join(out, 'catalog.json'), 'w', encoding='utf-8') as f:
        json.dump(cat, f, indent=1)

    idx = ['# Dependency report', '', '- `catalog.md` / `catalog.json`: every exported model with its textures and referencing artdef entries',
           '- `units/<UNIT>.md` / `units.json`: how each unit is composed and where each part lives', '', '## Units with parts in this export', '',
           '| Unit | Parts | Parts not in the export |', '|---|---|---|']
    for name, n, miss in summary:
        idx.append('| [%s](units/%s.md) | %d | %s |' % (name, name, n, ', '.join('`%s`' % m for m in miss) or '-'))
    with open(os.path.join(out, 'index.md'), 'w', encoding='utf-8') as f:
        f.write('\n'.join(idx) + '\n')
    return out, len(summary), len(cat)
