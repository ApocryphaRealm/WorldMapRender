World Map Render
Version 1.0.0

Straight-down world maps, day and night, rendered from this modlist's own landscapes, buildings, trees and roads. Runs on Flat
World Map Framework.

WHAT IT DOES
Opening the world map shows a flat picture of the land seen from straight above. The view stays straight down, with no angle
controls. It cannot be panned off the edge of the map. Zoom goes closer than the framework's own maps, and panning slows as you
zoom in. From 20:00 to 05:00 the night picture shows instead, with lit settlements.

REQUIREMENTS
SKSE, Address Library, Flat World Map Framework. Load this mod above Flat World Map Framework and any paper map for the same land,
so its meshes win. Lands without a rendered map keep whichever map they had.

THE PICTURES BELONG TO ONE LIST
The pictures are made from one modlist's LOD. tools/rerun.py in the source redraws them when that list's landscape changes.

LICENCE
GPL-3.0-or-later. See LICENSE and NOTICE.md. Source: the tools and source folders of the repository.
