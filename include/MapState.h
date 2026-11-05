#pragma once

#include <string>

// What the map camera last did, for the DevBench tool (rule 31). Written on the main thread each map frame, read as a report.
namespace mapstate
{
	struct Data
	{
		bool        open = false;
		bool        ours = false;
		std::string world;
		float       cameraX = 0.0f, cameraY = 0.0f, distance = 0.0f, width = 0.0f, fov = 0.0f, pan = 0.0f, sheetZ = 0.0f;
		bool        night = false;
		int         sheets = 0;
		int         tilesShowing = 0, tilesHeld = 0;
	};
	inline Data g;
}
