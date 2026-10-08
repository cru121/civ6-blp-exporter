# Changelog

Versions follow [semantic versioning](https://semver.org/) (0.x: the command line and output layout may still change between minor versions). `python civ6_blp_export.py --version` prints the current one.

## 0.4.1 - 2026-10-08

- **Fixed invalid skinned glTF:** when several meshes of a model share one vertex buffer but have different bone lists (e.g. `Maui_ArmorA`: 2 and 12 joints), every mesh exported the whole buffer, so vertices of the other meshes carried joint indices outside the mesh's joint list. Strict loaders (three.js bounding boxes) crashed on 38 of the 0.4.0 glTFs, and the glTF validator would reject them. Each primitive now gets only its own vertices.
- The validator checks joint indices against the skin's joint list.

## 0.4.0 - 2026-10-08

Closer to how modders work: the asset-level data of the BLPs (attachment points, animation slots, timelines) is decoded, and there are views by source file and by whole unit. Checked against the shipped `.ast`/`.fgx` files of the SDK Development Assets wherever they exist (Base game, Rise and Fall, Gathering Storm); later DLC has no sources to check against.

- **`--raw`:** each model as its source `.fgx` files in `<blp>/raw/` (every mesh once, one material; Pillaged-only meshes as `<name>_PIL`; Ice Rink: 26 + 24 meshes, as in the SDK samples). Plus `<name>.asset.json` (all structure data) and `<name>.approx.ast`, a reconstructed Asset Editor `.ast` (attachment points, animation and timeline bindings and all triggers match the real Catapult `.ast`; material and group names are not stored and are generated).
- **`--units [NAME...]`:** one glTF per unit member and variation, assembled from body, head, armor and weapons as the artdefs define them: parts at `Root` skinned onto one shared skeleton (missing bones added by name), other parts hung on their attachment point. First asset of each bin, tints not applied. `.json` next to each file lists parts and notes.
- **Attachment points** (operators, projectiles, FX): in the model JSON (name, bone, matrix) and as `attach_<name>` glTF nodes. Names are matched to their data by FNV-1a hash; checked against the shipped `.ast` (positions, scales and Euler angles equal for 549 of 586 points, the rest are the same rotation written the other way).
- **Animations:** `animations` and `animationSlots` per model, with slot names from the game's state graphs (`blp_dsgs.py`; the graph is identified by its number of timeline slots). For the 1630 models with an `.ast`, the (slot, animation) sets are identical. `--anim own` adds exactly a model's own animations to its glTF, no more guessing by bone names.
- **Timelines and triggers** (sounds, effects, transfers, actions with start time and attachment point) in the model JSON; effect and sound names are stored only as hashes and are looked up in `trigger_names.json` (12k names read from the SDK assets by `tools/build_trigger_names.py`; 99.9% of the Base game's triggers resolve, fewer for later DLC).
- The validator also checks `raw/` and `units/`. Texture paths stay correct when a model is exported from a different folder than the first one.
- **Fixed:** static meshes in the glTF (buildings, props, most tile models) were not rotated from the game's Z-up into glTF's Y-up and lay on their side (since 0.2.0). Attachment points were skipped for 114 models (e.g. the Hat / WeaponPrimary points that armors take from their behaviour files) whenever several points shared a bone and matrix; now 292 of 692 Base unit models have them. Texture file names with spaces (`TEXTURE_grey gloss`) broke `.mtl` files; spaces become `_`.
- Known gaps: tints, LEAN second normal map, burn/snow maps and lightmap weights are not converted; `roughness = 1 - gloss` is still an assumption; the transfer-trigger value is not understood; mesh names of unit models and material names are not stored in the game files.

## 0.3.1 - 2026-10-07

- **Multi-skeleton models:** a model can hold several skeletons (e.g. an elephant resource with a grass skeleton and an 18-bone elephant); the exporter only read the first, so the glTF of 24 such models failed (cranes, kurgans, mills, resource animals, wonder cameras...). Each mesh binding names its skeleton, and the glTF now carries all of them.
- **File names:** models whose names contain characters Windows does not allow (`|`, `/`) are written with `_` instead of failing (4 tilebases).
- Unit reports: a bin that lists the same asset once per culture (e.g. `BaseMale_Bodies/Hands`, 12 cultures) is now one row, with the tints grouped by culture, instead of looking like duplicate lines. The "also shipped by" list is capped at five packages.

## 0.3.0 - 2026-10-07

Dependency report, and the reader now handles the Base game's big BLPs.

- **`--report`:** reads the game's artdefs (Base + every DLC, merged) and writes `dependency_report/`. `units/<UNIT>.md` shows how each unit is composed (attachment, skeleton point, tint, bin, asset entry, BLP) and whether each part is exported, in an unreadable BLP, or in a BLP you did not export. `catalog.md/json` lists every exported model with triangles, bones, textures, files and the artdef entries that reference it.
- **Reader fix:** the container locator missed large packages, so 149 of 775 BLPs read as empty, among them Base `units`, `city_buildings`, `tilebases` and `hero_buildings`, `WonderMovie`, `VFX` and many DLC `tilebases`. They now load (e.g. 692 Base unit models, including the shared heads that were missing from the Anansi merge). Checked against the whole install: nothing that loaded before changed.
- Skeleton-only assembly nodes whose parts live elsewhere (a unit's `Root`) are skipped instead of reported as errors.
- New tests for the artdef merge/resolution/report and for the Base units BLP; README documents the report.

## 0.2.0 - 2026-10-07

Output quality, from review by a 3D artist: the exports now open in Blender with materials and PBR.

- **OBJ fix:** per-model OBJs now declare `mtllib` and select `usemtl matN` per group (only assembly OBJs did before), so materials import.
- **glTF for every model**, static ones too (previously skinned only), with materials; tile-state variants stay as primitives with `extras.states`.
- **PBR conversion:** diffuse to base colour (RGBA when there is an opacity map), LEAN0 to a normal map (green is already +Y up), AO/roughness/metalness packed into one `_ORM.png`. The "roughness" slot is a gloss map in sRGB, so it is linearised and inverted. MTL files use `map_Pr`, `map_Pm`, `norm` (they used `map_Ns`/`map_Bump`). Unconverted slots (`lean1`, lightmap, burn) stay in the glTF material `extras`.
- **`--states` now also applies to models:** `--states Worked` writes one clean `<name>__Worked.obj/.gltf` per model. Without it, models still contain all states together.
- **`--validate`** checks an export for missing textures/materials, bad indices, count mismatches and broken glTF references.
- Meshes without a bone name are called `meshN__g..` instead of `None__g..`; `.gitattributes` added; regression tests (`python -m unittest discover tests`).

## 0.1.0 - 2026-10-06

First public release: exports Civ6 `.blp` models (static and skinned) to OBJ/MTL with textures, tile-state groups, assembled tiles, skinned glTF with animations; see the README for usage and limits. Verified on the Babylon DLC (Windows, Steam).
