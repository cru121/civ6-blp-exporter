"""Per-asset bundle for modders (written with --raw next to the raw meshes):

  <name>.asset.json   everything the package knows about the asset, in one readable file: source geometries and their meshes, which material each
                      state uses, the materials' texture slots, bones, attachment points, animation bindings, timelines and triggers
  <name>.approx.ast   the same as an Asset Editor .ast-shaped XML (geometry set, behaviour data, attachment points, bindings, timelines)

The .ast is reconstructed, not the original: material names, mesh group names and the model instance name are not stored in the package
(materials are called <name>_mat<N>), and effect/sound names are filled in from
trigger_names.json where the hash is known (else 'hash:xxxxxxxx').  Use it as a starting point, not as a drop-in file.
"""
import json, math, os
from xml.sax.saxutils import quoteattr

from blp_models import safe_name, source_sets, MAT_SLOTS


def euler_zyx(matrix):
    """XYZ angles (radians, as the .ast stores them) of a row-vector 4x4 whose rotation is Rz*Ry*Rx (checked against shipped .ast files),
    and its uniform scale."""
    r = [matrix[0:3], matrix[4:7], matrix[8:11]]                         # rows of the (transposed) rotation: R[c][r] = r[r][c]
    sc = math.sqrt(sum(x * x for x in r[0])) or 1.0
    R = [[r[j][i] / sc for j in range(3)] for i in range(3)]             # column-vector rotation
    beta = -math.asin(max(-1.0, min(1.0, R[2][0])))
    if abs(math.cos(beta)) > 1e-6:
        alpha, gamma = math.atan2(R[2][1], R[2][2]), math.atan2(R[1][0], R[0][0])
        wrap = lambda v: math.atan2(math.sin(v), math.cos(v))
        alt = (wrap(alpha + math.pi), wrap(math.pi - beta), wrap(gamma + math.pi))          # the same rotation written the other way round
        if sum(abs(v) for v in alt) < abs(alpha) + abs(beta) + abs(gamma) - 1e-9:
            alpha, beta, gamma = alt                                                         # keep the smaller angles (a half turn about Y, not X plus Z)
    else:                                                                # gimbal lock: put it all on z
        alpha, gamma = 0.0, math.atan2(-R[0][1], R[1][1])
    return [alpha, beta, gamma], sc


def _mat_name(model, k):
    return '%s_mat%d' % (model['name'], k)


def asset_json(model, source=None):
    """dict describing the asset (see module doc)."""
    geoms = []
    for suf, meshes in source_sets(model).items():
        if not meshes:
            continue
        ms = []
        for me in meshes:
            per_state = {}
            for g in me['groups']:
                for st in g['states']:
                    per_state.setdefault(st, []).append(dict(material=_mat_name(model, g['materialID']), triangles=g['indexCount'] // 3, vertices=g['vertCount']))
            seen = set()
            for g in me['groups']:
                key = (g['vb'], g['ib'], g['firstIndex'], g['indexCount'], g['baseVertex'])
                if key not in seen:
                    seen.add(key)
            ms.append(dict(name=me['bone'], skinned=me['bone'] is None, states=per_state, uniqueGeometries=len(seen)))
        geoms.append(dict(name=model['name'] + suf, meshes=ms))
    mats = []
    for k, m in enumerate(model['materials']):
        mats.append(dict(name=_mat_name(model, k), uvScroll=m.get('uvScroll'), textures={s: m.get(s) for s in list(MAT_SLOTS) + ['burnMap'] if m.get(s)}))
    atts = []
    for a in model.get('attachments', []):
        eul, sc = euler_zyx(a['matrix'])
        atts.append(dict(name=a['name'], bone=a['bone'], position=[round(x, 6) for x in a['matrix'][12:15]], orientationRadiansXYZ=[round(x, 6) for x in eul], scale=round(sc, 6)))
    notes = ['Reconstructed from the package: material, mesh-group and instance names are not stored; see blp_bundle.py.']
    if len(model.get('stateGraph', [])) != 1:
        notes.append('State graph (DSG) is ambiguous or unknown: candidates %s' % (model.get('stateGraph') or 'none'))
    return dict(name=model['name'], className=model.get('className'), source=source, stateGraph=model.get('stateGraph', []), geometries=geoms, materials=mats,
                bones=[b for b in model['bones'] if b], attachmentPoints=atts,
                animationBindings=[dict(slot=x['slot'], slotId=x['slotId'], animation=x['animation']) for x in model.get('animationSlots', [])],
                timelines=model.get('timelines', []), notes=notes)


# ------------------------------------------------------------------ approximate .ast
class _X:
    def __init__(self):
        self.lines, self.depth = [], 0

    def open(self, tag, attrs=''):
        self.lines.append('\t' * self.depth + '<%s%s>' % (tag, attrs)); self.depth += 1

    def close(self, tag):
        self.depth -= 1; self.lines.append('\t' * self.depth + '</%s>' % tag)

    def text(self, tag, value):
        self.lines.append('\t' * self.depth + '<%s text=%s/>' % (tag, quoteattr(str(value))))

    def val(self, tag, value):
        self.lines.append('\t' * self.depth + '<%s>%s</%s>' % (tag, value, tag))

    def empty(self, tag):
        self.lines.append('\t' * self.depth + '<%s/>' % tag)


def approx_ast(model):
    x = _X()
    x.lines.append('<?xml version="1.0" encoding="UTF-8" ?>')
    x.open('AssetObjects..AssetInstance')
    x.open('m_BehaviorData'); x.open('m_behaviorDataSets')
    binds = [b for b in model.get('animationSlots', []) if b['slot']]
    x.open('m_animationBindings')
    if binds:
        x.open('m_Bindings')
        for b in binds:
            x.open('Element'); x.text('m_SlotName', b['slot']); x.text('m_AnimationName', (b['animation'] or '').replace('ANIMATION_', '')); x.close('Element')
        x.close('m_Bindings')
    else:
        x.empty('m_Bindings')
    x.close('m_animationBindings')
    tls = [t for t in model.get('timelines', []) if t['slot'] and (t['triggers'] or t['duration'])]
    anim_of = {b['slot']: (b['animation'] or '').replace('ANIMATION_', '') for b in binds}
    x.open('m_timelineBindings')
    if tls:
        x.open('m_Bindings')
        for t in tls:
            x.open('Element'); x.text('m_SlotName', t['slot']); x.text('m_TimelineName', t['slot']); x.close('Element')
        x.close('m_Bindings')
    else:
        x.empty('m_Bindings')
    x.close('m_timelineBindings')
    x.open('m_timelines')
    if tls:
        x.open('m_Timelines')
        for t in tls:
            x.open('Element'); x.text('m_Name', t['slot']); x.text('m_Description', ''); x.text('m_AnimationName', anim_of.get(t['slot'], ''))
            x.val('m_fDuration', '%.6f' % 0.0)
            if t['triggers']:
                x.open('m_Triggers')
                for i, tr in enumerate(t['triggers']):
                    x.open('Element'); x.val('m_eType', tr['type']); x.text('m_Name', i); x.text('m_Description', '')
                    x.text('m_FXName', tr.get('name') or 'hash:' + tr['nameHash']); x.text('m_CollectionName', '')
                    x.text('m_AttachmentPointName', tr.get('attachment') or ''); x.val('m_fStartTime', '%.6f' % tr['start'])
                    x.val('m_fDuration', '%.6f' % tr['duration']); x.val('m_nTrackIndex', 0); x.close('Element')
                x.close('m_Triggers')
            else:
                x.empty('m_Triggers')
            x.close('Element')
        x.close('m_Timelines')
    else:
        x.empty('m_Timelines')
    x.close('m_timelines')
    x.open('m_attachmentPoints')
    if model.get('attachments'):
        x.open('m_Points')
        for a in model['attachments']:
            eul, sc = euler_zyx(a['matrix'])
            x.open('Element'); x.open('m_CookParams'); x.empty('m_Values'); x.close('m_CookParams')
            x.open('m_position'); [x.val(tag, '%.6f' % v) for tag, v in zip('xyz', a['matrix'][12:15])]; x.close('m_position')
            x.open('m_orientation'); [x.val(tag, '%.6f' % v) for tag, v in zip('xyz', eul)]; x.close('m_orientation')
            x.text('m_Name', a['name']); x.text('m_BoneName', a['bone'] or ''); x.text('m_ModelInstanceName', 'Root'); x.val('m_scale', '%.6f' % sc)
            x.close('Element')
        x.close('m_Points')
    else:
        x.empty('m_Points')
    x.close('m_attachmentPoints')
    x.empty('m_stateSet')
    x.close('m_behaviorDataSets')
    x.empty('m_behaviorInstances')
    x.text('m_dsgName', model['stateGraph'][0] if model.get('stateGraph') else '')
    x.empty('m_referenceGeometryNames')
    x.close('m_BehaviorData')
    x.open('m_GeometrySet'); x.open('m_ModelInstances')
    for suf, meshes in source_sets(model).items():
        if not meshes:
            continue
        x.open('Element'); x.text('m_Name', 'Root' if not suf else 'Root' + suf); x.text('m_GeoName', model['name'] + suf)
        x.open('m_GroupStates')
        for me in meshes:
            for g in me['groups']:
                for st in g['states']:
                    x.open('Element'); x.open('m_Values'); x.open('m_Values')
                    x.open('Element', ' class="AssetObjects..ObjectValue"'); x.text('m_ObjectName', _mat_name(model, g['materialID']))
                    x.val('m_eObjectType', 'MATERIAL'); x.text('m_ParamName', 'Material'); x.close('Element')
                    x.close('m_Values'); x.close('m_Values')
                    x.text('m_GroupName', _mat_name(model, g['materialID'])); x.text('m_MeshName', me['bone'] or 'skinned'); x.text('m_StateName', st)
                    x.close('Element')
        x.close('m_GroupStates'); x.close('Element')
    x.close('m_ModelInstances'); x.close('m_GeometrySet')
    x.text('m_ClassName', model.get('className') or ''); x.text('m_Name', model['name'])
    x.text('m_Description', 'reconstructed from a BLP by civ6-blp-exporter')
    x.close('AssetObjects..AssetInstance')
    return '\n'.join(x.lines) + '\n'


def write_bundle(model, outdir, source=None, tag=''):
    base = os.path.join(outdir, safe_name(model['name']) + tag)
    json.dump(asset_json(model, source), open(base + '.asset.json', 'w'), indent=1)
    open(base + '.approx.ast', 'w', encoding='utf8').write(approx_ast(model))
