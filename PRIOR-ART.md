# Prior art for World Map Render (searched 2026-11-04, rule 24)

Searched Nexus (skyrimspecialedition, GraphQL nameStemmed, op MATCHES): top down map, map render, satellite map, world map LOD,
flat map, real world map, paper map, world map framework, GTS land map, map from LOD, aerial map, terrain map, worldspace map.

Searched GitHub (gh search repos): skyrim world map render, skyrim top down map, skyrim lod map generator, xedit map export, tes5
worldspace render, skyrim map renderer, skyrim map, Skyrim LOD. Nothing renders a map from a list's LOD; the map repositories are
web maps or menu fixes (doodlum/skyrim-map-weather, tphilipgh/skyrim-map-texture-ref-fix, BrenoMancini/skyrim-map).

| Mod | Where | What it does | Why it is not this |
|---|---|---|---|
| GTS Land Maps | Nexus 197905, source 7mxdie/GTSLandMap | Satellite-style maps with a camera DLL, for the Gate to Sovngarde list | Pictures are that list's, with no stated permission to reuse; no renderer in its repo. Our DLL is based on its GPL source |
| Flat World Map Framework | Nexus 29932 | Flat map mesh, flat markers, settings | The base we run on; its own pictures are painted or vanilla |
| Improved Flat Satellite Maps | Nexus 173901 | Improves the framework's satellite maps | Fixed pictures, not this list's world |
| Paper maps for FWMF (many) | Nexus 153809, 165949, 160067 and others | Hand-drawn pictures per land | Paper art, which is what the owner wants replaced |
| Enhanced Worldspace Maps | Nexus 57176 | Better 3D maps for some lands | Keeps the 3D map |
| xEdit's Worldspace Browser | xEdit | From memory, not checked: shows and exports a land's map view | Not a game map and no objects from LOD |

The reason to build: a picture that follows this list when its LOD changes, and no fixed picture does that.
