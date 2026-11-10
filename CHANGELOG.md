# WorldMapRender - changelog

Written as changes happen, not reconstructed afterwards (rule 61). Each version carries its
**version-ledger status**, so this file cannot quietly claim more than the ledger does:

* **working** - observed running in game
* **untested** - built and packaged, not yet confirmed
* **failed** - built but crashed or malfunctioned; the number was reclaimed
* **scratch** - a hypothesis-test build that never held a real number

## 1.0.0 - 2026-11-04 - untested

### Added
- The weather comes off the world map from the plugin itself, so Clear Map Skies is no longer needed: while the map is open the map's cloud bank (the WorldMapCloudBank shapes of skyrimworldmapcloudbank.nif, found in the scene by name and by their superfluffycloud textures) is hidden, and so are the sky's cloud layers, its rain or snow and its aurora; lightning flashes and the sky's fog are held off, and the map weather's tint is taken off every map, not only ours. The sky's own parts are shown again when the map closes. Always on (the mod has no settings page); table switch noweather leaves the weather on the map. Log lines name what was found and hidden on each open and what the sky holds one frame after the map closes; the DevBench state tool reports it too
- Once a second while the map is open, a diagnosis line in the log: the sky's fog and weather image space, the view, and for the sheet and every node above it whether it is hidden, its flags, fade, bound and alpha; table switches fogclear and forceshow (always drawn, no fade, full alpha up the sheet's chain, put back on close) to test fog and culling live
- Map guard log lines: the size, format and mip count the game's own copy of the picture really has on the graphics card, our copy's, the device, and each bind
- Structures and rock drawn from full models on every land but Skyrim and Sovngarde, roads and paths from the land's paint, lantern specks at night, building outlines
- tools/rerun.py redraws the maps when the list's landscape changes
- SKSE plugin: straight-down view with no angle controls, stays on the map, closer zoom, panning that slows as you zoom in, day and night sheets (based on GTS Land Map's GPL-3.0 source)
- Straight-down maps for 13 lands rendered from the list's own LOD, day and night

### Fixed
- The world map no longer comes up as a bare backdrop colour on some launches: the map guard reads each sheet's picture from its file itself, puts it on the graphics card and behind the sheet's texture, whatever the game's texture manager did with its own copy; it checks again over the first frames and binds it again if anything replaces it, and falls back to the picture one or two sizes smaller if the card refuses the full one (table switch noguard turns it off)
- Closing the map no longer pans back to the player and zooms in first: the view stays where it is and the map shuts (table switch noclosehold turns this off)
- Opening the map inside a city no longer shows a close view of the land below for half a second: the opening flight starts above the picture (table switch nolift turns this off). Those lands keep their own map offset, as in GTS Land Map
- The player's own field of view comes back after the map, whatever other mods did to it at the map's open