// World Map Render - GPL-3.0-or-later. Runs the rendered maps' camera, view and panning while the world map is open, and
// swaps the day and night sheets.
//
// Based on GTSLandMap.dll (GTS Land Map 1.0, Copyright (C) 2026 mxdie, GPL-3.0-or-later,
// https://github.com/7mxdie/GTSLandMap, plugin/src/main.cpp at commit ab4f13e). Changed from it, 2026-11-04: rebuilt for
// Skyrim 1.5.97 on CommonLibSSE-NG 3.7; the table's format and folder are this mod's own; the view is held strictly
// straight down (the map's look controls are zeroed while it is open); the land limit column is gone, the sheet limit
// keeps the view on the map; a DevBench state tool and null checks were added. The pictures and meshes are not from it.
//
// Why the view narrows instead of the camera dropping: Community Shaders' Screen Space GI fades out by 50000 units and has
// no map check, and it brightens everything nearer than that. So the camera stays past 50000 from the sheet and the view
// narrows to show closer ground.
#include "PCH.h"

#include "DevBenchTool.h"
#include "MapState.h"
#include "Tiles.h"

#include "utils/AddressLibraryGuard.h"
#include "utils/Logger.h"

// CommonLibSSE-NG 3.7's Main.h only forward-declares its misspelt "Scenegraph"; the object it returns is this class.
#include "RE/S/SceneGraph.h"

#include <algorithm>
#include <charconv>
#include <cmath>
#include <cstring>
#include <filesystem>
#include <format>
#include <fstream>
#include <sstream>
#include <string>
#include <vector>

namespace
{
	constexpr const char* kTablePath = "Data/SKSE/Plugins/WorldMapRender.txt";
	constexpr const char* kSheetFolder = "textures\\terrain\\worldmaprender\\";
	constexpr const char* kSheetKey = "worldmaprender";

	struct World
	{
		float z, hmin, hmax, wfar, wnear;
		bool  night;
		int   cells[6];     // usable width, usable height, NW cell x y, SE cell x y
		float onam[4];      // map scale, offset x y z
		float sheet[4];     // the sheet's edges: x0 x1 y0 y1
	};

	std::vector<std::pair<std::string, World>> g_worlds;

	std::string Lower(std::string a_text)
	{
		std::transform(a_text.begin(), a_text.end(), a_text.begin(), [](unsigned char c) { return static_cast<char>(std::tolower(c)); });
		return a_text;
	}

	const World* Find(RE::TESWorldSpace* a_world)
	{
		if (!a_world) { return nullptr; }
		const auto id = Lower(a_world->editorID.c_str());
		for (auto& [name, w] : g_worlds)
		{
			if (name == id) { return &w; }
		}
		return nullptr;
	}

	// Speeds and zoom, in the table so they can change while the game runs.
	struct Tuning
	{
		float closest = 56000.0f;   // camera distance at the closest view: keep over 50000 for Screen Space GI
		float panNear = 20000.0f;   // pan speed at the closest view
		float panRange = 60000.0f;  // added by the widest view
		float keys = 0.00714f;      // keyboard speed, as a share of the view's width
	} g_tuning;

	// Test switches: a table line "! noswap noclamp ..." turns those parts off. The table is read at every map open, and a copy in
	// the log folder wins, so a part can be switched between two map opens with no relaunch. Own-map is read at data load only.
	struct Off
	{
		bool swap = false, clamp = false, ownMap = false, stick = false, look = false, fov = false, tiles = false, frame = false, lift = false, close = false;
	} g_off;

	// A copy in the log folder wins over the installed one and is read every time the map opens: changes need no restart.
	std::ifstream OpenTable()
	{
		if (auto folder = SKSE::log::log_directory())
		{
			std::ifstream live(*folder / "WorldMapRender.txt");
			if (live.is_open()) { return live; }
		}
		return std::ifstream(kTablePath);
	}

	void LoadTable(bool a_log)
	{
		g_worlds.clear();
		g_tuning = Tuning{};
		g_off = Off{};
		auto        in = OpenTable();
		std::string line;
		while (std::getline(in, line))
		{
			if (line.empty() || line[0] == '#') { continue; }
			std::istringstream s(line);
			std::string        name;
			if (line[0] == '!')
			{
				std::string word;
				s >> word;
				while (s >> word)
				{
					if (word == "noswap") { g_off.swap = true; }
					else if (word == "noclamp") { g_off.clamp = true; }
					else if (word == "noownmap") { g_off.ownMap = true; }
					else if (word == "nostick") { g_off.stick = true; }
					else if (word == "nolook") { g_off.look = true; }
					else if (word == "nofov") { g_off.fov = true; }
					else if (word == "notiles") { g_off.tiles = true; }
					else if (word == "noframe") { g_off.frame = true; }
					else if (word == "nolift") { g_off.lift = true; }
					else if (word == "noclosehold") { g_off.close = true; }
				}
				logger::info("Test switches: {}", line);
				continue;
			}
			if (line[0] == '@')
			{
				s >> name >> g_tuning.closest >> g_tuning.panNear >> g_tuning.panRange >> g_tuning.keys;
				continue;
			}
			World w{};
			int   night = 0;
			s >> name >> w.z >> w.hmin >> w.hmax >> w.wfar >> w.wnear >> night;
			for (auto& c : w.cells) { s >> c; }
			for (auto& o : w.onam) { s >> o; }
			for (auto& e : w.sheet) { s >> e; }
			if (s.fail())
			{
				logger::error("Bad table line: {}", line);
				continue;
			}
			w.night = night != 0;
			// A land whose picture is not installed keeps whatever map it has, untouched.
			const auto file = std::string(kSheetFolder) + Lower(name) + ".dds";
			if (!std::filesystem::exists("Data/" + file) && !RE::BSResourceNiBinaryStream(file).good())
			{
				if (a_log) { logger::info("{} not installed, left alone", name); }
				continue;
			}
			g_worlds.emplace_back(Lower(name), w);
		}
		if (a_log) { logger::info("Worlds in table: {}", g_worlds.size()); }
	}

	// The flat map's weather carries a 21% red tint, which turns snow pink. Other maps keep it.
	void SetMapTint(bool a_on)
	{
		static std::vector<std::pair<RE::TESImageSpace*, float>> saved;
		auto weather = RE::TESForm::LookupByID<RE::TESWeather>(0xA6858);
		if (!weather) { return; }
		for (auto space : weather->imageSpaces)
		{
			if (!space) { continue; }
			auto it = std::find_if(saved.begin(), saved.end(), [&](auto& a_entry) { return a_entry.first == space; });
			if (it == saved.end())
			{
				saved.emplace_back(space, space->data.tint.amount);
				it = std::prev(saved.end());
				logger::info("Map weather tint {} on {:X}", it->second, space->GetFormID());
			}
			space->data.tint.amount = a_on ? it->second : 0.0f;
		}
	}

	// In memory, so a later plugin carrying an older map record cannot put the camera back under the sheet.
	void ApplyRecords(bool a_log)
	{
		auto data = RE::TESDataHandler::GetSingleton();
		if (!data) { return; }
		int done = 0;
		for (auto form : data->GetFormArray<RE::TESWorldSpace>())
		{
			auto w = Find(form);
			if (!w) { continue; }
			auto& m = form->worldMapData;
			auto& o = form->worldMapOffsetData;
			// A land with a picture of its own is its own map, whatever the winning record says. Only the map flag goes: its land, LOD and water still come from its parent.
			if (!g_off.ownMap && form->parentWorld && form->parentUseFlags.any(RE::TESWorldSpace::ParentUseFlag::kUseMapData))
			{
				form->parentUseFlags.reset(RE::TESWorldSpace::ParentUseFlag::kUseMapData);
				if (a_log) { logger::info("{} now uses its own map, not {}", form->editorID.c_str(), form->parentWorld->editorID.c_str()); }
			}
			if (a_log)
			{
				logger::info("{} was min {} max {} pitch {} onam {} {} {} {}", form->editorID.c_str(), m.cameraData.minHeight, m.cameraData.maxHeight,
					m.cameraData.initialPitch, o.mapScale, o.mapOffsetX, o.mapOffsetY, o.mapOffsetZ);
			}
			m.usableWidth = static_cast<std::uint32_t>(w->cells[0]);
			m.usableHeight = static_cast<std::uint32_t>(w->cells[1]);
			m.nwCellX = static_cast<std::int16_t>(w->cells[2]);
			m.nwCellY = static_cast<std::int16_t>(w->cells[3]);
			m.seCellX = static_cast<std::int16_t>(w->cells[4]);
			m.seCellY = static_cast<std::int16_t>(w->cells[5]);
			o.mapScale = w->onam[0];
			o.mapOffsetX = w->onam[1];
			o.mapOffsetY = w->onam[2];
			o.mapOffsetZ = w->onam[3];
			m.cameraData.minHeight = w->hmin;
			m.cameraData.maxHeight = w->hmax;
			m.cameraData.initialPitch = 90.0f;  // straight down, whatever a later plugin's record says
			++done;
		}
		if (a_log) { logger::info("Map records set: {}", done); }
	}

	void SetSetting(RE::Setting*& a_setting, std::string_view a_name, float a_value)
	{
		if (!a_setting)
		{
			if (auto ini = RE::INISettingCollection::GetSingleton()) { a_setting = ini->GetSetting(a_name); }
		}
		if (a_setting) { a_setting->data.f = a_value; }
	}

	float           g_sheetZ = 0.0f;    // where the sheet really is in the scene, 0 until found
	RE::NiAVObject* g_sheet = nullptr;  // only used while the map is open

	// Viewing a land whose parent's LOD is still up (Dayspring from Skyrim), both chunks hang under the LOD root with their sheets
	// at one height, and they fight. The other land's whole chunk is hidden while this map is open.
	std::vector<RE::NiPointer<RE::NiAVObject>> g_hidden;
	std::string                                g_sheetFile;

	bool IsOtherSheet(RE::NiAVObject* a_object)
	{
		auto geometry = a_object->AsGeometry();
		if (!geometry || !a_object->name.c_str() || std::strcmp(a_object->name.c_str(), "map sheet") != 0) { return false; }
		auto effect = netimmerse_cast<RE::BSEffectShaderProperty*>(geometry->GetGeometryRuntimeData().properties[RE::BSGeometry::States::kEffect].get());
		auto material = effect ? static_cast<RE::BSEffectShaderMaterial*>(effect->GetBaseMaterial()) : nullptr;
		if (!material || !material->sourceTexturePath.c_str()) { return false; }
		return Lower(material->sourceTexturePath.c_str()).find(g_sheetFile) == std::string::npos;
	}

	void HideOtherSheets(RE::NiAVObject* a_object)
	{
		if (!a_object) { return; }
		if (auto node = a_object->AsNode())
		{
			for (auto& child : node->GetChildren()) { HideOtherSheets(child.get()); }
			return;
		}
		if (!IsOtherSheet(a_object) || !a_object->parent || a_object->parent->GetAppCulled()) { return; }
		a_object->parent->SetAppCulled(true);
		g_hidden.emplace_back(a_object->parent);
		logger::info("Hid another land's chunk under this map: {}", a_object->parent->name.c_str());
	}

	void ShowHidden()
	{
		for (auto& node : g_hidden)
		{
			if (node) { node->SetAppCulled(false); }
		}
		g_hidden.clear();
	}

	// Where a sheet hangs and how it is drawn, once per sheet at each map open: which chunk, whether anything above it is culled,
	// and its alpha and shader flags. Two copies of one land's sheet were found on 2026-11-06 (sheets 4, not 2) when nothing drew.
	void DescribeSheet(RE::NiAVObject* a_object, RE::BSEffectShaderProperty* a_effect, RE::BSEffectShaderMaterial* a_material)
	{
		std::string chain;
		for (RE::NiAVObject* p = a_object->parent; p; p = p->parent)
		{
			chain += std::format(" < {}{}", p->name.c_str() ? p->name.c_str() : "?", p->GetAppCulled() ? " (culled)" : "");
		}
		logger::info("Sheet at {} {} {} (local z {}), culled {}, alpha {} colour alpha {}, flags {:X}{}", a_object->world.translate.x, a_object->world.translate.y,
			a_object->world.translate.z, a_object->local.translate.z, a_object->GetAppCulled(), a_effect->alpha, a_material->baseColor.alpha,
			static_cast<std::uint64_t>(a_effect->flags.underlying()), chain);
	}

	// Whether a sheet's picture really reached the graphics card: a texture can be handed back with no image behind it.
	std::string DescribeTexture(RE::NiSourceTexture* a_source)
	{
		if (!a_source) { return "no texture"; }
		auto data = reinterpret_cast<RE::NiTexture::RendererData*>(a_source->rendererTexture);
		if (!data) { return std::format("{} with no renderer data", a_source->name.c_str() ? a_source->name.c_str() : "?"); }
		return std::format("{} {}x{}, image {}, view {}", a_source->name.c_str() ? a_source->name.c_str() : "?", data->width, data->height,
			data->texture ? "yes" : "NONE", data->resourceView ? "yes" : "NONE");
	}

	// What the frame is drawn through, read once the view has settled: the sky's fog, the camera's near and far planes, and the sheet again.
	void DescribeView(RE::NiCamera* a_camera)
	{
		if (auto sky = RE::Sky::GetSingleton())
		{
			logger::info("Sky mode {}, fog near {} far {} power {} clamp {}, weather {:X}", static_cast<std::uint32_t>(sky->mode.get()), sky->fogNear, sky->fogFar, sky->fogPower,
				sky->fogClamp, sky->currentWeather ? sky->currentWeather->GetFormID() : 0);
		}
		if (a_camera)
		{
			const auto& fr = a_camera->GetRuntimeData2().viewFrustum;
			logger::info("View near {} far {}, camera at {} {} {}", fr.fNear, fr.fFar, a_camera->world.translate.x, a_camera->world.translate.y, a_camera->world.translate.z);
		}
		auto geometry = g_sheet ? g_sheet->AsGeometry() : nullptr;
		auto effect = geometry ? netimmerse_cast<RE::BSEffectShaderProperty*>(geometry->GetGeometryRuntimeData().properties[RE::BSGeometry::States::kEffect].get()) : nullptr;
		auto material = effect ? static_cast<RE::BSEffectShaderMaterial*>(effect->GetBaseMaterial()) : nullptr;
		if (material)
		{
			DescribeSheet(g_sheet, effect, material);
			logger::info("Sheet picture now: {}", DescribeTexture(material->sourceTexture.get()));
		}
	}

	// Night and day: every sheet carrying one of our textures gets the picture for the hour.
	int SwapSheets(RE::NiAVObject* a_object, bool a_night)
	{
		if (!a_object) { return 0; }
		int found = 0;
		if (auto node = a_object->AsNode())
		{
			for (auto& child : node->GetChildren()) { found += SwapSheets(child.get(), a_night); }
			return found;
		}
		auto geometry = a_object->AsGeometry();
		if (!geometry) { return 0; }
		auto effect = netimmerse_cast<RE::BSEffectShaderProperty*>(geometry->GetGeometryRuntimeData().properties[RE::BSGeometry::States::kEffect].get());
		if (!effect) { return 0; }
		auto material = static_cast<RE::BSEffectShaderMaterial*>(effect->GetBaseMaterial());
		if (!material || !material->sourceTexturePath.c_str()) { return 0; }
		std::string path = material->sourceTexturePath.c_str();
		if (Lower(path).find(kSheetKey) == std::string::npos) { return 0; }
		if (g_off.swap)
		{
			if (a_object->name.c_str() && std::strcmp(a_object->name.c_str(), "map sheet") == 0 && !IsOtherSheet(a_object))
			{
				g_sheetZ = a_object->world.translate.z;
				g_sheet = a_object;
				DescribeSheet(a_object, effect, material);
			}
			return 1;
		}
		// Every shape of ours has its picture set, as d34d519 did, but only the sheet itself gives the height and takes the night picture.
		const bool isSheet = a_object->name.c_str() && std::strcmp(a_object->name.c_str(), "map sheet") == 0 && !IsOtherSheet(a_object);
		if (isSheet)
		{
			g_sheetZ = a_object->world.translate.z;
			g_sheet = a_object;
			DescribeSheet(a_object, effect, material);
			if (a_night) { path.insert(path.size() - 4, "_n"); }
		}
		RE::NiPointer<RE::NiTexture> texture;
		RE::BSShaderManager::GetTexture(path.c_str(), true, texture, false);
		auto source = netimmerse_cast<RE::NiSourceTexture*>(texture.get());
		if (!source)
		{
			logger::error("Could not load {}", path);
			return 0;
		}
		const auto before = DescribeTexture(material->sourceTexture.get());
		material->sourceTexture = RE::NiPointer<RE::NiSourceTexture>(source);
		logger::info("Sheet {} now shows {} (was {}; now {})", a_object->name.c_str(), path, before, DescribeTexture(source));
		return 1;
	}

	struct State
	{
		bool  open = false;
		float savedWorld = 90.0f, savedFirst = 90.0f;
		int   frame = 0;
		bool  sheetsDone = false;
		bool  frustumFallback = false;
		int   mismatch = 0;
		float lastD = 0.0f;
		int   still = 0;
		float lastLoggedD = 0.0f;
		float zoomLow = 0.0f;
		float zoomHigh = 0.0f;
		int   heldHeight = 0;
		bool  loggedRange = false;
		bool  tintSet = false;
		bool  framed[2] = { false, false };
		bool  closeHeld = false;
	} g;

	// The player's own field of view. Other plugins handle the map's open event too (FWMF's BakaWorldMapFOV raises it to 110
	// and puts it back on close), and may run first, so what we read at the map's open is not trustworthy. It is sampled at
	// every other menu event while the map is shut, at data loaded, and on each game load.
	float g_realWorldFOV = 0.0f;
	float g_realFirstFOV = 0.0f;

	void SampleRealFOV()
	{
		if (g.open) { return; }
		auto player = RE::PlayerCamera::GetSingleton();
		if (!player) { return; }
		auto& fov = player->GetRuntimeData2();
		if (fov.worldFOV > 0.0f) { g_realWorldFOV = fov.worldFOV; }
		if (fov.firstPersonFOV > 0.0f) { g_realFirstFOV = fov.firstPersonFOV; }
	}

	std::vector<RE::TESWorldSpace*> g_raised;  // lands whose limit has been lifted since the map opened: once each, so it can't creep

	// Worlds with no sky (Blackreach) don't get the map's fog-free weather, so their own fog can swallow a sheet this far away.
	// CommonLibSSE-NG 3.7 has no definition of the object fog property, so only the sky's fog is cleared on this build line.
	void ClearFog()
	{
		if (auto sky = RE::Sky::GetSingleton())
		{
			sky->fogNear = 900000.0f;
			sky->fogFar = 1000000.0f;
			sky->fogClamp = 0.0f;
		}
		for (RE::NiAVObject* p = g_sheet; p; p = p->parent)
		{
			if (p->GetAppCulled()) { p->SetAppCulled(false); }
		}
	}

	// The owner's ask: straight down, no angle moves. Zero while any of our maps is showing; only the map reads these.
	void HoldLook()
	{
		static RE::Setting* lookMouse = nullptr;
		static RE::Setting* lookPad = nullptr;
		static RE::Setting* yaw = nullptr;
		static RE::Setting* maxPitch = nullptr;
		static RE::Setting* minPitch = nullptr;
		SetSetting(lookMouse, "fMapLookMouseSpeed:MapMenu", 0.0f);
		SetSetting(lookPad, "fMapLookGamepadSpeed:MapMenu", 0.0f);
		SetSetting(yaw, "fMapWorldYawRange:MapMenu", 0.0f);
		SetSetting(maxPitch, "fMapWorldMaxPitch:MapMenu", 0.0f);
		SetSetting(minPitch, "fMapWorldMinPitch:MapMenu", 0.0f);
	}

	float g_stickX = 0.0f, g_stickY = 0.0f;  // the right stick, read from input events; it pans now that it no longer tilts

	class StickSink : public RE::BSTEventSink<RE::InputEvent*>
	{
	public:
		RE::BSEventNotifyControl ProcessEvent(RE::InputEvent* const* a_event, RE::BSTEventSource<RE::InputEvent*>*) override
		{
			if (!a_event || !g.open) { return RE::BSEventNotifyControl::kContinue; }
			for (auto* e = *a_event; e; e = e->next)
			{
				if (e->GetEventType() != RE::INPUT_EVENT_TYPE::kThumbstick) { continue; }
				auto* stick = static_cast<RE::ThumbstickEvent*>(e);
				if (stick->idCode == RE::ThumbstickEvent::InputTypes::kRightThumbstick)
				{
					g_stickX = stick->xValue;
					g_stickY = stick->yValue;
				}
			}
			return RE::BSEventNotifyControl::kContinue;
		}
	};

	void Frame(RE::MapMenu* a_menu, float a_interval)
	{
		if (!a_menu) { return; }
		auto& data = *a_menu->GetRuntimeData2();
		auto  world = Find(data.worldSpace);
		auto  root = data.camera.cameraRoot.get();
		auto  player = RE::PlayerCamera::GetSingleton();
		if (g.open && !g.tintSet)
		{
			g.tintSet = true;
			SetMapTint(world == nullptr);
		}
		mapstate::g.world = data.worldSpace ? data.worldSpace->editorID.c_str() : "";
		mapstate::g.ours = world != nullptr;
		if (!world || !root || !player || !g.open || g_off.frame) { return; }
		++g.frame;
		if (!g_off.look) { HoldLook(); }

		// Real distance to the sheet once it's found. Record height and camera height may differ by the land's map offset.
		auto        camera = RE::Main::WorldRootCamera();
		const float camZ = camera ? camera->world.translate.z : root->world.translate.z;
		const float d0 = g_tuning.closest;
		const float d1 = d0 + (world->hmax - world->hmin);
		const float d = g_sheetZ != 0.0f ? (std::max)(1000.0f, camZ - g_sheetZ) : (std::max)(1000.0f, root->world.translate.z - world->z - 6000.0f);

		// The opening and closing flights pass close to the sheet, so only the settled view counts.
		RE::MapCameraStates::World* settled = nullptr;
		for (auto& state : data.camera.unk68)
		{
			if (state && static_cast<RE::TESCameraState*>(state.get()) == data.camera.currentState.get()) { settled = state.get(); }
		}
		g.still = settled && std::fabs(d - g.lastD) < 1.0f ? g.still + 1 : 0;
		g.lastD = d;

		if (g_sheetZ != 0.0f && d < 52000.0f && g.still > 20 && std::find(g_raised.begin(), g_raised.end(), data.worldSpace) == g_raised.end())
		{
			// Too close for Screen Space GI's fade: lift this land's limit by the shortfall, once.
			g_raised.push_back(data.worldSpace);
			auto& limits = data.worldSpace->worldMapData.cameraData;
			limits.minHeight += 54000.0f - d;
			limits.maxHeight = (std::max)(limits.maxHeight, limits.minHeight + 40000.0f);
			logger::warn("Settled {} from the sheet, minimum height now {}", d, limits.minHeight);
		}

		if (data.worldSpace->flags.any(RE::TESWorldSpace::Flag::kNoSky)) { ClearFog(); }

		const float t = std::clamp(std::log(d / d0) / std::log(d1 / d0), 0.0f, 1.0f);
		const auto& zoomFrustum = camera ? camera->GetRuntimeData2().viewFrustum : RE::NiFrustum{};
		const float screen = zoomFrustum.fRight != 0.0f ? zoomFrustum.fTop / zoomFrustum.fRight : 0.5625f;
		// On any screen shape, full zoom-out never asks for more than the limits hold: the picture always fills the view.
		const float widest = world->sheet[1] > world->sheet[0] ? (std::min)(world->sheet[1] - world->sheet[0], (world->sheet[3] - world->sheet[2]) / screen) : world->wfar;
		const float width = (std::min)(world->wnear * std::pow(world->wfar / world->wnear, t), widest);
		const float half = (std::min)(width / (2.0f * d), 1.0f);
		const float fov = 2.0f * std::atan(half) * 57.29578f;

		// Keep the whole screen on the sheet, so the view never leaves the map.
		if (!g_off.clamp && world->sheet[1] > world->sheet[0])
		{
			const float cx = (world->sheet[0] + world->sheet[1]) / 2.0f, cy = (world->sheet[2] + world->sheet[3]) / 2.0f;
			const auto  fit = [&](RE::NiPoint3& a_p) {
				a_p.x = std::clamp(a_p.x, world->sheet[0], world->sheet[1]);
				a_p.y = std::clamp(a_p.y, world->sheet[2], world->sheet[3]);
			};
			// The camera opens over the player's own coordinates, which for another land can be far off its sheet. It looks at the state's
			// position while the scroll offset moves it, so a clamp through the offset alone turned the whole view (seen as a skewed corner
			// of the map, whenever the player stood outside that land's bounds). So the position itself goes onto the sheet, centred at first.
			for (int i = 0; i < 2; ++i)
			{
				auto* state = data.camera.unk68[i].get();
				if (!state) { continue; }
				auto& p = state->currentPosition;
				if (!g.framed[i])
				{
					g.framed[i] = true;
					if (p.x < world->sheet[0] || p.x > world->sheet[1] || p.y < world->sheet[2] || p.y > world->sheet[3])
					{
						logger::info("Camera opened off this land at {} {}, centring it", p.x, p.y);
						p.x = cx;
						p.y = cy;
						state->currentPositionScrollOffset.x = 0.0f;
						state->currentPositionScrollOffset.y = 0.0f;
					}
				}
				fit(p);
			}
			if (auto* flight = data.camera.unk80.get())
			{
				// A land that shows this map (Windhelm's city shows Skyrim's) starts the opening flight at its own height, far under the
				// sheet, and the view rose through the real land for half a second (log of 06:37, Windhelm). GTS Land Map leaves those
				// lands' records alone, and so does this plugin again: 222c0a2 copied the parent's map offset into them, and the
				// picture stopped drawing. The flight is started above the sheet here instead; only a flight rising to the sheet, never the closing one.
				const float floor = world->z + 10000.0f;
				if (!g_off.lift && flight->zoomDestination.z > floor && (flight->currentPosition.z < floor || flight->zoomOrigin.z < floor))
				{
					logger::info("Flight started under the sheet at {} (from {}), lifted to {}", flight->currentPosition.z, flight->zoomOrigin.z, floor);
					flight->currentPosition.z = (std::max)(flight->currentPosition.z, floor);
					flight->zoomOrigin.z = (std::max)(flight->zoomOrigin.z, floor);
				}
				// The closing flight went back to the player and down to the sheet, so the view panned home and zoomed in before the map shut
				// (the owner, 2026-11-06). It now stays where the view is.
				const bool flying = static_cast<RE::TESCameraState*>(flight) == data.camera.currentState.get();
				if (!g_off.close && flying && flight->zoomDestination.z < flight->zoomOrigin.z - 1.0f)
				{
					if (!g.closeHeld) { logger::info("Closing flight held at {} {} {} (was going to {} {} {})", flight->currentPosition.x, flight->currentPosition.y, flight->currentPosition.z,
						flight->zoomDestination.x, flight->zoomDestination.y, flight->zoomDestination.z); }
					g.closeHeld = true;
					flight->zoomDestination = flight->currentPosition;
					flight->zoomOrigin = flight->currentPosition;
				}
				fit(flight->currentPosition);
				fit(flight->zoomDestination);
				fit(flight->zoomOrigin);
				if (flight->zoomDestination.z > world->hmax)
				{
					logger::info("Flight was going to height {}, above this land's {}", flight->zoomDestination.z, world->hmax);
					flight->zoomDestination.z = world->hmax;
				}
			}
		}
		if (!g_off.clamp && settled && world->sheet[1] > world->sheet[0])
		{
			const auto& fr = camera ? camera->GetRuntimeData2().viewFrustum : RE::NiFrustum{};
			const float aspect = fr.fRight != 0.0f ? fr.fTop / fr.fRight : 0.5625f;
			auto&       offset = settled->currentPositionScrollOffset;
			auto&       base = settled->currentPosition;
			// Some lands' opening flight ends far above their limit, and the wheel then does nothing (log of 06:36: Skuldafn and five others at
			// height 392000 against a limit of 282000). Why those lands is not known yet. Bring the camera back under its limit.
			if (!g.loggedRange)
			{
				g.loggedRange = true;
				logger::info("Zoom range {}: limits {} to {}, width {} to {}; camera at {}, state height {} offset {}, limits held {} to {}", data.worldSpace->editorID.c_str(), world->hmin, world->hmax,
					world->wnear, world->wfar, camZ, base.z, offset.z, data.worldSpace->worldMapData.cameraData.minHeight, data.worldSpace->worldMapData.cameraData.maxHeight);
			}
			if (camZ > world->hmax + 1000.0f && base.z + offset.z > world->hmax)
			{
				if (++g.heldHeight == 1) { logger::warn("Camera at {} above the limit {}: state height {} offset {}, bringing it down", camZ, world->hmax, base.z, offset.z); }
				base.z = world->hmax - offset.z;
			}
			else if (camZ > world->hmax + 1000.0f && g.heldHeight == 0)
			{
				g.heldHeight = -1;
				logger::warn("Camera at {} above the limit {}, but state height {} offset {} do not say so", camZ, world->hmax, base.z, offset.z);
			}
			if (g.zoomLow == 0.0f || d < g.zoomLow) { g.zoomLow = d; }
			if (d > g.zoomHigh) { g.zoomHigh = d; }
			if (!g_off.stick && (std::fabs(g_stickX) > 0.15f || std::fabs(g_stickY) > 0.15f))
			{
				const float step = 0.7f * width * std::clamp(a_interval, 0.0f, 0.05f);
				base.x += g_stickX * step;
				base.y += g_stickY * step;
			}
			const auto  inside = [](float a_at, float a_lo, float a_hi, float a_half) {
				return a_hi - a_lo <= a_half * 2.0f ? (a_lo + a_hi) / 2.0f : std::clamp(a_at, a_lo + a_half, a_hi - a_half);
			};
			// the position first, so the offset stays small and the view stays straight down
			base.x = inside(base.x, world->sheet[0], world->sheet[1], width / 2.0f);
			base.y = inside(base.y, world->sheet[2], world->sheet[3], width / 2.0f * aspect);
			offset.x = inside(base.x + offset.x, world->sheet[0], world->sheet[1], width / 2.0f) - base.x;
			offset.y = inside(base.y + offset.y, world->sheet[2], world->sheet[3], width / 2.0f * aspect) - base.y;
		}

		if (!g_off.fov)
		{
			player->GetRuntimeData2().worldFOV = fov;
			player->GetRuntimeData2().firstPersonFOV = fov;
		}

		// The game normally follows worldFOV by itself. If it doesn't, set the view directly.
		if (camera && !g_off.fov)
		{
			auto& fr = camera->GetRuntimeData2().viewFrustum;
			g.mismatch = std::fabs(fr.fRight - half) > half * 0.05f ? g.mismatch + 1 : 0;
			if (g.mismatch > 30)
			{
				if (!g.frustumFallback)
				{
					g.frustumFallback = true;
					logger::warn("View didn't follow worldFOV: right {} wanted {}, setting it directly", fr.fRight, half);
				}
				const float aspect = fr.fRight != 0.0f ? fr.fTop / fr.fRight : 0.5625f;
				fr.fRight = half;
				fr.fLeft = -half;
				fr.fTop = half * aspect;
				fr.fBottom = -half * aspect;
			}
		}

		// Mouse panning uses the pan speed directly; keys are pan speed times the keyboard value, so they stay a share of the view.
		const float pan = (std::min)(width / world->wnear, 1.0f) * g_tuning.panNear + g_tuning.panRange * std::clamp(std::log(width / world->wnear) / std::log(world->wfar / world->wnear), 0.0f, 1.0f);
		static RE::Setting* minPan = nullptr;
		static RE::Setting* maxPan = nullptr;
		static RE::Setting* keys = nullptr;
		SetSetting(minPan, "fMapWorldMinPanSpeed:MapMenu", pan);
		SetSetting(maxPan, "fMapWorldMaxPanSpeed:MapMenu", pan);
		SetSetting(keys, "fMapMoveKeyboardSpeed:MapMenu", g_tuning.keys * width / pan);

		if (camera && g_sheet && settled && !g_off.tiles)
		{
			const auto& tf = camera->GetRuntimeData2().viewFrustum;
			Tiles::Frame(camera->world.translate, width, tf.fRight != 0.0f ? tf.fTop / tf.fRight : 0.5625f, d);
		}
		mapstate::g.tilesShowing = Tiles::Showing();
		mapstate::g.tilesHeld = Tiles::Held();

		mapstate::g.cameraX = root->world.translate.x;
		mapstate::g.cameraY = root->world.translate.y;
		mapstate::g.distance = d;
		mapstate::g.width = width;
		mapstate::g.fov = fov;
		mapstate::g.pan = pan;
		mapstate::g.sheetZ = g_sheetZ;

		if (std::fabs(d - g.lastLoggedD) > d * 0.1f)
		{
			g.lastLoggedD = d;
			logger::info("Camera {} {} z {} distance {} width {} fov {} right {} pan {}", root->world.translate.x, root->world.translate.y, root->world.translate.z, d, width, fov,
				camera ? camera->GetRuntimeData2().viewFrustum.fRight : 0.0f, pan);
		}

		if (g.frame == 90) { DescribeView(camera); }

		// the other land's chunk can arrive a little after the map opens
		if (g.frame % 30 == 1 && g.frame < 1800)
		{
			g_sheetFile = kSheetKey + std::string("\\") + Lower(data.worldSpace->editorID.c_str()) + ".dds";
			if (auto tes = RE::TES::GetSingleton()) { HideOtherSheets(tes->lodLandRoot); }
		}

		if (!g.sheetsDone && g.frame % 30 == 1)
		{
			auto       calendar = RE::Calendar::GetSingleton();
			const auto hour = calendar ? calendar->GetHour() : 12.0f;
			const bool night = world->night && (hour >= 20.0f || hour < 5.0f);
			auto       tes = RE::TES::GetSingleton();
			int        found = tes ? SwapSheets(tes->lodLandRoot, night) : 0;
			if (!found) { found = SwapSheets(reinterpret_cast<RE::SceneGraph*>(RE::Main::WorldRootNode()), night); }
			if (found || g.frame > 600)
			{
				g.sheetsDone = true;
				if (found && !g_off.tiles) { Tiles::Open(Lower(data.worldSpace->editorID.c_str()), g_sheet, night); }
				mapstate::g.night = night;
				mapstate::g.sheets = found;
				logger::info("Hour {} night {} sheets {} frame {} sheet z {} camera z {}", hour, night, found, g.frame, g_sheetZ, camZ);
			}
		}
	}

	struct AdvanceHook
	{
		static void Thunk(RE::MapMenu* a_menu, float a_interval, std::uint32_t a_time)
		{
			Frame(a_menu, a_interval);
			Original(a_menu, a_interval, a_time);
		}
		static inline REL::Relocation<decltype(Thunk)> Original;
	};

	class MenuSink : public RE::BSTEventSink<RE::MenuOpenCloseEvent>
	{
	public:
		RE::BSEventNotifyControl ProcessEvent(const RE::MenuOpenCloseEvent* a_event, RE::BSTEventSource<RE::MenuOpenCloseEvent>*) override
		{
			if (!a_event) { return RE::BSEventNotifyControl::kContinue; }
			if (a_event->menuName != RE::MapMenu::MENU_NAME)
			{
				SampleRealFOV();
				return RE::BSEventNotifyControl::kContinue;
			}
			auto player = RE::PlayerCamera::GetSingleton();
			if (!player) { return RE::BSEventNotifyControl::kContinue; }
			auto& fov = player->GetRuntimeData2();
			if (a_event->opening && !g.open)
			{
				g = State{};
				g.open = true;
				mapstate::g = {};
				mapstate::g.open = true;
				LoadTable(false);
				ApplyRecords(false);
				g_raised.clear();
				g_sheetZ = 0.0f;
				g_sheet = nullptr;
				g.savedWorld = g_realWorldFOV > 0.0f ? g_realWorldFOV : fov.worldFOV;
				g.savedFirst = g_realFirstFOV > 0.0f ? g_realFirstFOV : fov.firstPersonFOV;
				logger::info("Map opened, FOV now {} {}, the player's own {} {}", fov.worldFOV, fov.firstPersonFOV, g.savedWorld, g.savedFirst);
				if (auto pc = RE::PlayerCharacter::GetSingleton())
				{
					auto ws = pc->GetWorldspace();
					auto cell = pc->GetParentCell();
					if (ws)
					{
						const auto& o = ws->worldMapOffsetData;
						logger::info("Player in {} at {} {} {}, parent {} (its map {}), onam {} {} {} {}", ws->editorID.c_str(), pc->GetPositionX(), pc->GetPositionY(), pc->GetPositionZ(),
							ws->parentWorld ? ws->parentWorld->editorID.c_str() : "none", ws->parentUseFlags.any(RE::TESWorldSpace::ParentUseFlag::kUseMapData),
							o.mapScale, o.mapOffsetX, o.mapOffsetY, o.mapOffsetZ);
					}
					else { logger::info("Player in an interior: {}", cell && cell->GetFormEditorID() ? cell->GetFormEditorID() : "?"); }
				}
			}
			else if (!a_event->opening && g.open)
			{
				g.open = false;
				ShowHidden();
				g_stickX = g_stickY = 0.0f;
				mapstate::g.open = false;
				Tiles::Close();
				g_sheet = nullptr;
				if (g.frustumFallback)
				{
					// The game only rebuilds the view when the value differs from its last one.
					if (auto scene = RE::Main::WorldRootNode()) { static_cast<RE::BSSceneGraph*>(reinterpret_cast<RE::SceneGraph*>(scene))->GetRuntimeData().cameraFOV = 0.0f; }
				}
				fov.worldFOV = g.savedWorld;
				fov.firstPersonFOV = g.savedFirst;
				g_realWorldFOV = g.savedWorld;
				g_realFirstFOV = g.savedFirst;
				logger::info("Map closed, FOV back to {} {}; settled distance from the sheet ran {} to {}", g.savedWorld, g.savedFirst, g.zoomLow, g.zoomHigh);
				g.zoomLow = 0.0f;
				g.zoomHigh = 0.0f;
				g.heldHeight = 0;
				g.loggedRange = false;
			}
			return RE::BSEventNotifyControl::kContinue;
		}
	};

	void OnMessage(SKSE::MessagingInterface::Message* a_msg)
	{
		if (!a_msg) { return; }
		if (a_msg->type == SKSE::MessagingInterface::kPostLoad) { DevBenchTool::Init(false); }
		if (a_msg->type == SKSE::MessagingInterface::kDataLoaded)
		{
			DevBenchTool::Init(true);
			LoadTable(true);
			ApplyRecords(true);
			if (!g_worlds.empty()) { SetMapTint(false); }
			SampleRealFOV();
			static MenuSink sink;
			if (auto ui = RE::UI::GetSingleton()) { ui->AddEventSink<RE::MenuOpenCloseEvent>(&sink); }
			else { logger::error("no UI singleton at data loaded; the map camera will not run"); }
			static StickSink stick;
			if (auto* input = RE::BSInputDeviceManager::GetSingleton()) { input->AddEventSink<RE::InputEvent*>(&stick); }
			REL::Relocation<std::uintptr_t> vtable{ RE::VTABLE_MapMenu[0] };
			AdvanceHook::Original = vtable.write_vfunc(0x5, AdvanceHook::Thunk);
			logger::info("Ready");
		}
	}
}

SKSEPluginLoad(const SKSE::LoadInterface* a_skse)
{
	SKSE::log::init("WorldMapRender");
	if (!AddressLibraryGuard::Guard("World Map Render")) { return true; }
	SKSE::Init(a_skse);
	logger::info("World Map Render {} loading", SKSE::PluginDeclaration::GetSingleton()->GetVersion().string("."));
	auto* messaging = SKSE::GetMessagingInterface();
	if (!messaging || !messaging->RegisterListener(OnMessage))
	{
		logger::error("could not register the SKSE message listener; the map camera will not run");
	}
	return true;
}
