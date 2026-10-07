# Changelog

Versions follow [semantic versioning](https://semver.org/) (0.x: the command line and output layout may still change between minor versions). `python civ6_blp_export.py --version` prints the current one.

## [Unreleased]

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
