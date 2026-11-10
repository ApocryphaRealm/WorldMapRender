#pragma once

#include <string>

// Weather off the world map (the owner, 2026-11-10: "Our map mod should remove all weather from the map in the DLL", replacing the
// Clear Map Skies textures). While the map is open: the map's cloud bank (meshes\sky\skyrimworldmapcloudbank.nif, shapes
// "WorldMapCloudBank_*" drawing textures\sky\superfluffycloudmap0N.dds) is hidden, and so are the sky's cloud layers, its rain or snow
// and its aurora; the lightning flash and the sky's fog are held off. The sky's own objects are shown again when the map closes.
// Every map, not only the lands with our pictures, as Clear Map Skies did. Table switch "noweather" turns it off.
namespace MapWeather
{
	void Open();
	// Once a frame while the map is open: a handful of pointer and flag checks; the cloud bank is looked for every 30 frames until found.
	void Frame(RE::NiAVObject* a_cameraRoot);
	void Close();
	int  Bank();
	int  SkyHidden();
	int  Reshown();
}
