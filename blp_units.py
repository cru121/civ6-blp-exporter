"""Whole-unit export: one glTF per unit member and variation, built from the unit's parts (body, head, armor, weapon...).

A unit is defined in the artdefs (Units.artdef -> member types -> variations -> attachments -> bins).  Parts attached at 'Root' are skinned to the
same rig: they are merged onto one shared skeleton by bone name (bones a part adds, like a cape, are added to it).  Parts attached at another point
(Hat, WeaponPrimary...) are static meshes hung on that attachment point, which any of the rig parts may provide.  A bin may offer several assets
(the game picks one at random): the first is used.  Tints, scales and the other choices are recorded in the JSON next to each glTF.

  python civ6_blp_export.py Base Babylon --units        # after the models are exported; parts in BLPs not exported in this run are read from the install
"""
import glob, json, os

import blp_artdefs
from blp_models import Landmarks, safe_name

STATIC_VB = 828177625
ROOT = (None, '', 'Root')


class _Parts:
    """Finds the BLP a model lives in: first among the BLPs exported in this run, else straight from the game install."""

    def __init__(self, results, game, outroot):
        self.game, self.outroot = game, outroot
        self.loc = {}                                    # (blp rel, model name) -> [(pkg, path, outdir)]
        for r in results:
            if r.get('path') and r.get('modelInfo') is not None and r.get('outdir'):
                rel, pkg = blp_artdefs.blp_rel(r['path']), blp_artdefs.pkg_of(r['path'])
                for info in r['modelInfo']:
                    self.loc.setdefault((rel, info['name']), []).append((pkg, r['path'], r['outdir']))
        self.cache = {}
        self.installed = {}

    def blp(self, path, outdir):
        if path not in self.cache:
            L = Landmarks(path)
            L.blp_path = path
            L.texture_dir = os.path.join(outdir, 'textures')
            L.by_name = {}
            for md in L.models:
                if md['name'] and (md['meshes'] or md['attachments']):          # a mesh-less asset (EmptyUnitAttachment) can still carry the unit's attachment points
                    L.by_name.setdefault(md['name'], md)
            self.cache[path] = L
        return self.cache[path]

    def _installed(self, package):
        """[(pkg, path)] of the install's BLPs for a package like 'units/units'"""
        if package not in self.installed:
            rel = package.replace('/', os.sep) + '.blp'
            paths = [os.path.join(self.game, 'Base', 'Platforms', 'Windows', 'BLPs', rel)] + \
                sorted(glob.glob(os.path.join(self.game, 'DLC', '*', 'Platforms', 'Windows', 'BLPs', rel)))
            self.installed[package] = [(blp_artdefs.pkg_of(q), q) for q in paths if os.path.exists(q)]
        return self.installed[package]

    def find(self, entry, package, prefer):
        rank = lambda pkg: prefer.index(pkg) if pkg in prefer else len(prefer)
        hits = sorted(self.loc.get((package, entry), []), key=lambda h: rank(h[0]))
        for pkg, path, outdir in hits:
            md = self.blp(path, outdir).by_name.get(entry)
            if md:
                return self.cache[path], md, pkg
        done = {h[1] for h in hits}
        for pkg, path in sorted(self._installed(package), key=lambda h: rank(h[0])):
            if path in done:
                continue
            L = self.blp(path, os.path.join(self.outroot, 'units', '_textures', '%s__%s' % (pkg, package.replace('/', '_'))))
            md = L.by_name.get(entry)
            if md:
                return L, md, pkg
        return None


def _first_asset(attachment):
    for br in attachment['bins']:
        for cu in br['cultures']:
            for a in cu['assets']:
                return a, cu
    return None, None


def combine(parts, host_ix, label):
    """One model dict out of several parts (see module doc); each group/material remembers its BLP in '_L'."""
    host = parts[host_ix]['model']
    sk0 = host['skeletons'][0] if host.get('skeletons') else dict(name=host['name'], bones=host['bones'], xforms=host['boneXforms'])
    sks = [dict(sk0, bones=list(sk0['bones']), xforms=list(sk0['xforms']))]
    hidx = {n: i for i, n in enumerate(sks[0]['bones']) if n}
    meshes, binds, bids, mats, pending, warnings = [], [], [], [], [], []
    skmaps = {}

    def map_skeleton(pi, k):
        """how bone ids of skeleton k of part pi map into the shared skeleton: ('host', None) | ('map', {id: id}) | ('own', skeleton index)"""
        if (pi, k) in skmaps:
            return skmaps[(pi, k)]
        p = parts[pi]
        part_sks = p['model'].get('skeletons') or [dict(name=p['model']['name'], bones=p['model']['bones'], xforms=p['model']['boneXforms'])]
        sk = part_sks[k]
        if pi == host_ix and k == 0:
            res = ('host', None)
        elif any(n in hidx for n in sk['bones'] if n):
            for i, n in enumerate(sk['bones']):                  # the parts share one rig: bones the shared skeleton lacks (a cape, a strap) are added to it
                if n and n not in hidx:
                    par = sk['xforms'][i]['parent']
                    sks[0]['bones'].append(n)
                    sks[0]['xforms'].append(dict(sk['xforms'][i], parent=hidx.get(sk['bones'][par], 0) if par >= 0 else -1))
                    hidx[n] = len(sks[0]['bones']) - 1
            res = ('map', {i: hidx[n] for i, n in enumerate(sk['bones']) if n})
        else:
            sks.append(dict(sk))
            res = ('own', len(sks) - 1)
            warnings.append('%s: skeleton %r shares no bones with the main skeleton; kept as its own' % (p['entry'], sk.get('name')))
        skmaps[(pi, k)] = res
        return res

    for pi, p in enumerate(parts):                              # rig parts first, so all their bones are in the shared skeleton before attachment points are placed
        if p['point'] in ROOT:
            map_skeleton(pi, 0)
    for pi, p in enumerate(parts):
        mm, L = p['model'], p['L']
        off = len(mats)
        mats += [dict(m, _L=L) for m in mm['materials']]
        n_sk = len(mm.get('skeletons') or [None])
        for mi, me in enumerate(mm['meshes']):
            groups = [dict(g, materialID=g['materialID'] + off, _L=L) for g in me['groups']]
            if not groups:
                continue
            if L.vbs[groups[0]['vb']]['fmt'] != STATIC_VB:       # skinned
                bd = mm['meshBindings'][mi]
                kind, val = map_skeleton(pi, min(bd.get('fromSkeleton', 0), n_sk - 1))
                ids = mm['boneIds'][bd['fromStart']:bd['fromStart'] + bd['fromCount']]
                if kind == 'map':
                    ids = [val.get(i, 0) for i in ids]
                binds.append(dict(fromStart=len(bids), fromCount=len(ids), fromSkeleton=val if kind == 'own' else 0))
                bids += list(ids)
            else:
                binds.append(dict(fromStart=len(bids), fromCount=0, fromSkeleton=0))
                if p['point'] not in ROOT:
                    pending.append((len(meshes), p))
            meshes.append(dict(mesh=len(meshes), bone=me['bone'], groups=groups, part=p['entry']))
    attachments, seen = [], set()                              # attachment points of all rig parts, on the shared skeleton by bone name
    for p in parts:
        if p['point'] not in ROOT:
            continue
        for a in p['model'].get('attachments', []):
            if a['name'] not in seen and a['bone'] in hidx:
                seen.add(a['name'])
                attachments.append(dict(a, boneIndex=hidx[a['bone']]))
    mparent = {}
    for mesh_i, p in pending:
        if p['point'] in seen:
            mparent[mesh_i] = p['point']
        else:
            warnings.append('%s: attachment point %r not found on the unit; placed at the origin' % (p['entry'], p['point']))
    return dict(name=label, className='Unit', bones=sks[0]['bones'], boneXforms=sks[0]['xforms'], skeletons=sks, boneIds=bids, meshBindings=binds,
                meshes=meshes, materials=mats, attachments=attachments, meshParent=mparent, animations=host.get('animations', []), _warnings=warnings)


def export_units(game, results, outroot, anims=None, only=None, log=print):
    """Writes <outroot>/units/<UNIT>/<member>__<culture>_<variation>.gltf (+ .json). Returns a summary dict."""
    import blp_gltf
    comps = blp_artdefs.unit_compositions(game)
    parts_db = _Parts(results, game, outroot)
    summary = dict(units=0, files=0, skipped=[], warnings=0)
    for uname, u in sorted(comps.items()):
        if only and not any(o.lower() in uname.lower() for o in only):
            continue
        prefer = list(u['src']) + ['Base']
        wrote = 0
        used = set()
        for mem in u['members']:
            for cu in mem['cultures'][:1]:                          # first culture only (usually 'Any'); cultures differ mostly in tints
                for var in cu['variations']:
                    parts, notes = [], []
                    for at in var['attachments']:
                        a, c = _first_asset(at)
                        if not a:
                            continue
                        got = parts_db.find(a['entry'], a['package'], prefer + list(mem['src']))
                        if not got and a['entry'] == 'EmptyUnitAttachment':        # deliberately empty slot
                            continue
                        if not got:
                            notes.append('%s (%s): not found in the installed BLPs' % (a['entry'], a['package']))
                            continue
                        L, md, pkg = got
                        parts.append(dict(attachment=at['attachment'], point=at['point'], tint=at['tint'] or c['tint'], entry=a['entry'], package=pkg, L=L, model=md))
                    if not parts:
                        continue
                    roots = [i for i, p in enumerate(parts) if p['point'] in ROOT] or list(range(len(parts)))
                    host_ix = max(roots, key=lambda i: len(parts[i]['model']['bones']))
                    label = '%s__%s_%s' % (mem['type'], cu['culture'], var['variation'])
                    while label in used:                                  # the same member type can be listed twice in a unit
                        label += '_2'
                    used.add(label)
                    combined = combine(parts, host_ix, label)
                    outdir = os.path.join(outroot, 'units', safe_name(uname))
                    os.makedirs(outdir, exist_ok=True)
                    try:
                        matched = []
                        if anims:
                            import civ6_blp_export as ex
                            matched = ex.matching_animations(combined, ['own'], 999)
                        fn = blp_gltf.export_gltf(parts[host_ix]['L'], combined, outdir, '', matched)
                    except Exception as e:                           # keep going: one broken unit should not stop the rest
                        summary['skipped'].append('%s/%s: %s' % (uname, label, e))
                        continue
                    info = dict(unit=uname, member=mem['type'], culture=cu['culture'], variation=var['variation'], variationScale=var['scale'],
                                memberScale=mem['scale'], formation=u['formation'], mainPart=parts[host_ix]['entry'], animations=len(matched),
                                parts=[dict(attachment=p['attachment'], point=p['point'], tint=p['tint'], entry=p['entry'], package=p['package'],
                                            placed=('skinned on the shared skeleton' if p['point'] in ROOT else 'on attachment point ' + str(p['point'])))
                                       for p in parts],
                                notes=notes + combined['_warnings'])
                    json.dump(info, open(os.path.splitext(fn)[0] + '.json', 'w'), indent=1)
                    summary['files'] += 1
                    wrote += 1
                    summary['warnings'] += len(info['notes'])
        if wrote:
            summary['units'] += 1
    return summary
