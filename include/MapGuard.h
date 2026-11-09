#pragma once

#include <string>

// The map guard (the owner, 2026-11-09: "something that can force load the map ... that can guarantee that it always loads", and "our
// DLL will ignore any conflicts and just load the map regardless"). The sheet's picture is read from the DDS by this plugin, made
// into a texture on the game's own graphics device, and put behind the sheet's texture, so the engine's texture manager can no longer
// leave the sheet with a texture that has no picture in it. Each open is checked once; the first frames only compare a pointer.
namespace MapGuard
{
	// a_path as the material holds it, e.g. "textures\\terrain\\worldmaprender\\tamriel.dds". True once our own copy is bound.
	bool Bind(RE::NiAVObject* a_sheet, RE::BSEffectShaderMaterial* a_material, const std::string& a_path);
	// Once a frame while the map is open: a pointer compare; rebinds (a bounded number of times) if something replaced our picture,
	// and retries a failed bind every 30 frames, a bounded number of times.
	void Frame(int a_frame);
	void Close();
	std::string Status();
}
