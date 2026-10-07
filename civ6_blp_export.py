#!/usr/bin/env python3
"""civ6_blp_export.py - export every 3D model in Civ6 landmark BLP files as textured OBJ.

  python civ6_blp_export.py Babylon                       # all landmark BLPs of the Babylon DLC
  python civ6_blp_export.py Base Babylon -o out           # several packages
  python civ6_blp_export.py path\\to\\city_buildings.blp   # one file (or a folder of .blp files)
  python civ6_blp_export.py Babylon --list                # only show what is inside

For each BLP that contains models you get   <out>/<package>__<blp>/{models,assemblies,textures}/   plus
summary.md / summary.json in <out>.  Needs: Python 3.9+, numpy, Pillow and an installed copy of Civ6
(textures are separate files in the game folder).  Output is derived from game files: for personal use / modding
reference - don't redistribute Firaxis assets.
"""
import argparse, glob, json, os, re, sys, time, traceback

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

GAME_DIRNAME = "Sid Meier's Civilization VI"


def steam_libraries():
    libs = []
    roots = []
    if sys.platform == 'win32':
        try:
            import winreg
            for hive, key in ((winreg.HKEY_CURRENT_USER, r'Software\Valve\Steam'), (winreg.HKEY_LOCAL_MACHINE, r'SOFTWARE\WOW6432Node\Valve\Steam')):
                try:
                    with winreg.OpenKey(hive, key) as k:
                        for val in ('SteamPath', 'InstallPath'):
                            try: roots.append(winreg.QueryValueEx(k, val)[0])
                            except OSError: pass
                except OSError: pass
        except ImportError:
            pass
        roots += [r'C:\Program Files (x86)\Steam', r'C:\Program Files\Steam']
    else:
        h = os.path.expanduser('~')
        roots += [h + '/Library/Application Support/Steam', h + '/.steam/steam', h + '/.local/share/Steam']
    for r in roots:
        libs.append(r)
        vdf = os.path.join(r, 'steamapps', 'libraryfolders.vdf')
        if os.path.exists(vdf):
            for m in re.finditer(r'"path"\s+"([^"]+)"', open(vdf, encoding='utf8', errors='ignore').read()):
                libs.append(m.group(1).replace('\\\\', '\\'))
    return libs


def find_game(explicit=None):
    cands = []
    if explicit: cands.append(explicit)
    if os.environ.get('CIV6_DIR'): cands.append(os.environ['CIV6_DIR'])
    cands += [os.path.join(l, 'steamapps', 'common', GAME_DIRNAME) for l in steam_libraries()]
    cands += [r'C:\Program Files\Epic Games\SidMeiersCivilizationVI', r'C:\Program Files (x86)\Epic Games\SidMeiersCivilizationVI']
    for c in cands:
        if c and (os.path.isdir(os.path.join(c, 'Base')) or os.path.isdir(os.path.join(c, 'DLC'))):
            return c
    return None


def discover(game, inputs, platform):
    """-> list of (package, path) for .blp files."""
    found = []
    def add_dir(pkg, root):
        for p in glob.glob(os.path.join(root, '**', '*.blp'), recursive=True):
            found.append((pkg, p))
    for inp in inputs:
        if os.path.isfile(inp):
            found.append((os.path.basename(os.path.dirname(inp)) or 'file', inp))
        elif os.path.isdir(inp):
            add_dir(os.path.basename(os.path.abspath(inp)), inp)
        else:
            name = inp
            roots = [os.path.join(game, 'Base', 'Platforms', platform, 'BLPs')] if name.lower() == 'base' else \
                    glob.glob(os.path.join(game, 'DLC', name, 'Platforms', platform, 'BLPs'))
            if not roots:
                print('! "%s" is not a file, folder, "Base" or a DLC folder name under %s' % (inp, os.path.join(game, 'DLC')))
            for r in roots: add_dir(name if name.lower() != 'base' else 'Base', r)
    seen, out = set(), []
    for pkg, p in found:
        if p not in seen:
            seen.add(p); out.append((pkg, p))
    return sorted(out)


_anim_cache = {}


def matching_animations(model, patterns, max_anims, min_cover=0.6):
    """Loose ANIMATION_* files whose tracks (>= min_cover of them) name bones of this model's skeleton."""
    import blp_anim, blp_textures
    bones = {b for b in model['bones'] if b}
    out = []
    for name, path in sorted(blp_textures.texture_index().items()):
        if not name.startswith('ANIMATION_'):
            continue
        if patterns != ['all'] and not any(p.lower() in name.lower() for p in patterns):
            continue
        if path not in _anim_cache:
            try:
                _anim_cache[path] = blp_anim.parse_animation(path)
            except Exception:
                _anim_cache[path] = None
        a = _anim_cache[path]
        if a and a['tracks'] and sum(t['name'] in bones for t in a['tracks']) / len(a['tracks']) >= min_cover:
            out.append(a)
            if len(out) >= max_anims:
                break
    return out


def export_blp(pkg, path, outroot, states, textures=True, anims=None, max_anims=20):
    from blp_models import Landmarks
    import blp_assemble
    rec = dict(package=pkg, blp=os.path.basename(path), path=path, models=0, exported=0, assemblies=[], errors=[], textures=0)
    L = Landmarks(path)
    L.blp_path = path
    rec['models'] = len(L.models)
    if not L.models:
        rec['note'] = 'no models'
        return rec
    base = os.path.join(outroot, '%s__%s' % (pkg, os.path.splitext(os.path.basename(path))[0]))
    mdir, adir, tdir = os.path.join(base, 'models'), os.path.join(base, 'assemblies'), os.path.join(base, 'textures')
    os.makedirs(mdir, exist_ok=True)
    L.texture_dir = tdir if textures else None
    names = [md['name'] for md in L.models]
    for i, md in enumerate(L.models):
        if not md['name']:
            continue
        if not md['meshes']:
            continue
        tag = '_%02d' % i if names.count(md['name']) > 1 else ''
        try:
            L.export(md, mdir, tag)
            rec['exported'] += 1
            if True:                                                  # every model gets a glTF: static = plain mesh nodes, skinned = + skeleton, weights, animations
                import blp_gltf
                matched = matching_animations(md, anims, max_anims) if anims else []
                blp_gltf.export_gltf(L, md, mdir, tag, matched)
                rec['gltf'] = rec.get('gltf', 0) + 1
                rec['animations'] = rec.get('animations', 0) + len(matched)
        except Exception as e:
            rec['errors'].append('%s: %s' % (md['name'], e))
    for i, md in enumerate(L.models):
        if md['name'] and not md['meshes'] and len(md['bones']) > 1:     # assembly node
            for st in states:
                try:
                    os.makedirs(adir, exist_ok=True)
                    fn, rep = blp_assemble.assemble(L, md, adir, st)
                    if rep['placed']:
                        rec['assemblies'].append(dict(name=md['name'], state=st, placed=len(rep['placed']), external=len(rep['external']), missing=len(rep['missing'])))
                    else:
                        for ext in glob.glob(os.path.join(adir, md['name'] + '*_' + st + '.*')): os.remove(ext)
                except Exception as e:
                    rec['errors'].append('assembly %s/%s: %s' % (md['name'], st, e))
    if os.path.isdir(tdir):
        rec['textures'] = len(os.listdir(tdir))
    rec['outdir'] = base
    return rec


def inspect_blp(pkg, path):
    from blp_models import Landmarks
    L = Landmarks(path)
    asm = [md['name'] for md in L.models if md['name'] and not md['meshes'] and len(md['bones']) > 1]
    return dict(package=pkg, blp=os.path.basename(path), models=len(L.models), assemblyNodes=asm, vertexBuffers=len(L.vbs), textures=len(L.textures))


def main(argv=None):
    ap = argparse.ArgumentParser(description='Export Civ6 BLP models (OBJ + textures + assemblies).')
    ap.add_argument('inputs', nargs='+', help='.blp file, folder, "Base", or a DLC folder name (e.g. Babylon)')
    ap.add_argument('-o', '--out', default='civ6_export', help='output folder (default ./civ6_export)')
    ap.add_argument('--game', help="Civ6 install folder (auto-detected from Steam/Epic if omitted; or set CIV6_DIR)")
    ap.add_argument('--states', default='Worked,Pillaged', help='assembly states to build (Construction,Pillaged,Unbuilt,Unworked,Worked)')
    ap.add_argument('--platform', default='Windows', help='Platforms/<name> to read when given Base/DLC names (default Windows)')
    ap.add_argument('--list', action='store_true', help='only list the models inside, export nothing')
    ap.add_argument('--anim', nargs='+', metavar='NAME', help="add loose ANIMATION_* files whose name contains NAME (or 'all') to the glTF of skinned models, if their bone tracks fit the skeleton")
    ap.add_argument('--max-anims', type=int, default=20, help='max animations per model (default 20)')
    ap.add_argument('--no-textures', action='store_true', help='skip texture decoding')
    a = ap.parse_args(argv)

    game = find_game(a.game)
    if not game:
        print('Could not find Civ6. Pass --game "<folder containing Base and DLC>" or set CIV6_DIR.'); return 2
    print('Game folder:', game)
    import blp_textures
    blp_textures.set_game(game)
    files = discover(game, a.inputs, a.platform)
    if not files:
        print('No .blp files found.'); return 1
    print('%d BLP file(s) to look at' % len(files))
    states = [s.strip() for s in a.states.split(',') if s.strip()]
    results, t0 = [], time.time()
    for k, (pkg, path) in enumerate(files, 1):
        label = '%s/%s' % (pkg, os.path.basename(path))
        try:
            if a.list:
                r = inspect_blp(pkg, path)
                if r['models']:
                    print('%-48s models=%-3d vb=%-3d textures=%-3d assembly nodes=%s' % (label, r['models'], r['vertexBuffers'], r['textures'], ','.join(r['assemblyNodes']) or '-'))
                results.append(r); continue
            r = export_blp(pkg, path, a.out, states, not a.no_textures, a.anim, a.max_anims)
            if r['models']:
                print('[%d/%d] %-44s models %d/%d exported, assemblies %d, textures %d%s' % (k, len(files), label, r['exported'], r['models'], len(r['assemblies']), r['textures'], ', %d error(s)' % len(r['errors']) if r['errors'] else ''))
            results.append(r)
        except Exception as e:
            results.append(dict(package=pkg, blp=os.path.basename(path), path=path, error='%s: %s' % (type(e).__name__, e)))
            if os.environ.get('CIV6_DEBUG'): traceback.print_exc()
    if a.list:
        return 0
    os.makedirs(a.out, exist_ok=True)
    json.dump(results, open(os.path.join(a.out, 'summary.json'), 'w'), indent=1)
    with_models = [r for r in results if r.get('models')]
    failed = [r for r in results if r.get('error')]
    with open(os.path.join(a.out, 'summary.md'), 'w', encoding='utf8') as f:
        f.write('# Civ6 BLP export\n\n- game: %s\n- BLP files examined: %d, with models: %d, unreadable: %d\n- models exported: %d\n\n' % (
            game, len(results), len(with_models), len(failed), sum(r['exported'] for r in with_models)))
        for r in with_models:
            f.write('## %s / %s\n- models: %d (exported %d), textures: %d\n' % (r['package'], r['blp'], r['models'], r['exported'], r['textures']))
            for asm in r['assemblies']:
                f.write('- assembly %s [%s]: %d parts placed, %d external props, %d unresolved\n' % (asm['name'], asm['state'], asm['placed'], asm['external'], asm['missing']))
            for e in r['errors']: f.write('- ERROR %s\n' % e)
            f.write('\n')
        if failed:
            f.write('## Unreadable BLPs\n')
            for r in failed: f.write('- %s/%s: %s\n' % (r['package'], r['blp'], r['error']))
    print('\nDone in %.0fs: %d BLP(s) with models, %d unreadable/skipped. Output: %s' % (time.time() - t0, len(with_models), len(failed), os.path.abspath(a.out)))
    return 0


if __name__ == '__main__':
    sys.exit(main())
