# WorldMapRender - changelog

Written as changes happen, not reconstructed afterwards (rule 61). Each version carries its
**version-ledger status**, so this file cannot quietly claim more than the ledger does:

* **working** - observed running in game
* **untested** - built and packaged, not yet confirmed
* **failed** - built but crashed or malfunctioned; the number was reclaimed
* **scratch** - a hypothesis-test build that never held a real number

## 1.0.0 - 2026-11-04 - untested

### Added
- Structures and rock drawn from full models on every land but Skyrim and Sovngarde, roads and paths from the land's paint, lantern specks at night, building outlines
- tools/rerun.py redraws the maps when the list's landscape changes
- SKSE plugin: straight-down view with no angle controls, stays on the map, closer zoom, panning that slows as you zoom in, day and night sheets (based on GTS Land Map's GPL-3.0 source)
- Straight-down maps for 13 lands rendered from the list's own LOD, day and night

### Fixed
- The player's own field of view comes back after the map, whatever other mods did to it at the map's open