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
		auto        in = OpenTable();
		std::string line;
		while (std::getline(in, line))
		{
			if (line.empty() || line[0] == '#') { continue; }
			std::istringstream s(line);
			std::string        name;
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
			if (form->parentWorld && form->parentUseFlags.any(RE::TESWorldSpace::ParentUseFlag::kUseMapData))
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
		if (Lower(path).find(kSheetKey) == std::string::npos || !a_object->name.c_str() || std::strcmp(a_object->name.c_str(), "map sheet") != 0) { return 0; }
		g_sheetZ = a_object->world.translate.z;
		g_sheet = a_object;
		if (a_night) { path.insert(path.size() - 4, "_n"); }
		RE::NiPointer<RE::NiTexture> texture;
		RE::BSShaderManager::GetTexture(path.c_str(), true, texture, false);
		auto source = netimmerse_cast<RE::NiSourceTexture*>(texture.get());
		if (!source)
		{
			logger::error("Could not load {}", path);
			return 0;
		}
		material->sourceTexture = RE::NiPointer<RE::NiSourceTexture>(source);
		logger::info("Sheet {} now shows {}", a_object->name.c_str(), path);
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
		bool  tintSet = false;
		bool  framed[2] = { false, false };
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
		if (!world || !root || !player || !g.open) { return; }
		++g.frame;
		HoldLook();

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
		const float width = world->wnear * std::pow(world->wfar / world->wnear, t);
		const float half = (std::min)(width / (2.0f * d), 1.0f);
		const float fov = 2.0f * std::atan(half) * 57.29578f;

		// Keep the whole screen on the sheet, so the view never leaves the map.
		if (world->sheet[1] > world->sheet[0])
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
				fit(flight->currentPosition);
				fit(flight->zoomDestination);
				fit(flight->zoomOrigin);
			}
		}
		if (settled && world->sheet[1] > world->sheet[0])
		{
			const auto& fr = camera ? camera->GetRuntimeData2().viewFrustum : RE::NiFrustum{};
			const float aspect = fr.fRight != 0.0f ? fr.fTop / fr.fRight : 0.5625f;
			auto&       offset = settled->currentPositionScrollOffset;
			auto&       base = settled->currentPosition;
			if (std::fabs(g_stickX) > 0.15f || std::fabs(g_stickY) > 0.15f)
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

		player->GetRuntimeData2().worldFOV = fov;
		player->GetRuntimeData2().firstPersonFOV = fov;

		// The game normally follows worldFOV by itself. If it doesn't, set the view directly.
		if (camera)
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

		if (camera && g_sheet && settled)
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
				if (found) { Tiles::Open(Lower(data.worldSpace->editorID.c_str()), g_sheet, night); }
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
			}
			else if (!a_event->opening && g.open)
			{
				g.open = false;
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
				logger::info("Map closed, FOV back to {} {}", g.savedWorld, g.savedFirst);
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
