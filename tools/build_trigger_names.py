"""Builds trigger_names.json: FNV-1a hash -> name for the effect / sound / attachment-point names used by timeline triggers.

The BLPs store these names only as 32-bit hashes.  This reads the names from the SDK's Development Assets (the .ast files) and writes the lookup
the exporter uses to print trigger names.  Run it once if you have the assets:

  python tools/build_trigger_names.py "<...>/Sid Meier's Civilization VI SDK Assets" [trigger_names.json]
"""
import glob, json, os, re, sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
from blp_models import _fnv1a

root = sys.argv[1]
out = sys.argv[2] if len(sys.argv) > 2 else os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'trigger_names.json')
names = set()
pat = re.compile(r'<m_(?:FXName|AttachmentPointName|CollectionName) text="([^"]+)"')
for ext in ('ast', 'bhv'):                                    # .bhv: behaviours shared by several units (horse sounds, impacts...)
    for p in glob.glob(os.path.join(root, '**', '*.' + ext), recursive=True):
        if ext == 'ast':
            names.add(os.path.basename(p)[:-4])                    # FX assets are referenced by asset name
        try:
            names.update(pat.findall(open(p, encoding='utf8', errors='replace').read()))
        except OSError:
            pass
table, clash = {}, 0
for n in sorted(names):
    h = '%08x' % _fnv1a(n)
    if h in table and table[h] != n:
        clash += 1
    table[h] = n
json.dump(table, open(out, 'w'), indent=0, sort_keys=True)
print('%d names -> %s (%d hash clashes)' % (len(table), out, clash))
