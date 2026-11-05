# World Map Render - copyright and licence

Copyright (C) 2026 ApocryphaRealm

This program is free software: you can redistribute it and/or modify it under the terms of the GNU General Public
License as published by the Free Software Foundation, either version 3 of the License, or (at your option) any later
version. It is distributed WITHOUT ANY WARRANTY; see `LICENSE`.

SPDX-License-Identifier: GPL-3.0-or-later

## Based on GTS Land Map

`source/main.cpp` is based on `plugin/src/main.cpp` of GTSLandMap.dll, GTS Land Map 1.0, Copyright (C) 2026 mxdie,
GPL-3.0-or-later, https://github.com/7mxdie/GTSLandMap, commit ab4f13e. Changed 2026-11-04: see the head of that file.

Nothing else comes from that mod. Its pictures and meshes are its author's own work with no permission stated for reuse, so
none of them is used here: the pictures are rendered from the list's LOD by `tools/render_map.py`, and the meshes are written by
`tools/nifwrite.py`. Its file was read as a format reference only; a test compared the writer's output with it.

## What the pictures are made from

The pictures are renders of the local list's xLODGen and DynDOLOD output, which draw on the textures of the mods in that list.
THIS MATTERS BEFORE ANY POSTING: a render made from other authors' textures is a derivative of them, so a public release needs a
decision on those permissions, or the mod ships the tool without the pictures so each list renders its own.
