# World Map Render

Straight-down world maps of a modlist, day and night, rendered from that list's own xLODGen and DynDOLOD output, on Flat World Map Framework.

- `tools/` the renderer: `rerun.py` runs it all; `render_map.py` draws, `build_maps.py` writes the mod's files, `list_worlds.py` lists worlds with map data, `worlds.json` says which lands get a map.
- `source/` the SKSE plugin: camera, view, panning, day and night. Based on GTS Land Map's GPL source; see `NOTICE.md`.

GPL-3.0-or-later.
