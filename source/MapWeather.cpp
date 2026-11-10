#include "PCH.h"

#include "MapWeather.h"

#include "utils/Logger.h"

// CommonLibSSE-NG 3.7's Main.h only forward-declares its misspelt "Scenegraph"; the object it returns is this class.
#include "RE/S/SceneGraph.h"

#include <algorithm>
#include <cstring>
#include <format>
#include <string>
#include <vector>

// What the map shows of the weather, and where each part lives (read 2026-11-10):
// * The cloud bank: Clear Map Skies replaces meshes\sky\skyrimworldmapcloudbank.nif (a BSFadeNode of BSEffectShader shapes named
//   WorldMapCloudBank_Low/Mid/Mid2/High/Top/Top2/Top3) and textures\sky\superfluffycloudmap01-04.dds and superfluffycloudlut.dds,
//   which only that mesh draws (the Sky keeps them loaded in storedWorldMapCloudTextures). Where the engine hangs the mesh is not
//   known from source, so the scene is searched for it by shape name and by texture.
// * The sky: while the map is open the Sky runs the map's own weather (the log of 2026-11-10 04:09: weather A6858 on every frame,
//   fog near 0 far 0, no weather image-space modifier). Its cloud layers (Sky::clouds), its rain or snow (Sky::precip), its aurora
//   and lightning flash belong to the sky, so they are hidden only while the map is open and shown again on close.
// * The map weather's image-space tint is zeroed by SetMapTint in main.cpp.
namespace MapWeather
{
	namespace
	{
		constexpr float kFogNear = 900000.0f;
		constexpr float kFogFar = 1000000.0f;

		// The cloud bank's shapes. Hidden while the map is open and never shown again by this plugin: the engine owns when that mesh is
		// drawn, and showing it on close could put map clouds into the game world if the engine hides it by the same flag.
		std::vector<RE::NiPointer<RE::NiAVObject>> g_bank;
		// The sky's objects this plugin hid while they were showing: shown again on close.
		std::vector<RE::NiPointer<RE::NiAVObject>> g_sky;
		int   g_frame = 0;
		int   g_reshown = 0;  // times something showed a hidden object again during this open (the engine fighting the hide)
		bool  g_loggedNone = false;
		bool  g_fogSaved = false;
		float g_fog[3] = { 0.0f, 0.0f, 0.0f };

		bool StartsWithNoCase(const char* a_text, const char* a_prefix)
		{
			return a_text && _strnicmp(a_text, a_prefix, std::strlen(a_prefix)) == 0;
		}

		bool ContainsNoCase(const char* a_text, const char* a_part)
		{
			if (!a_text) { return false; }
			std::string text = a_text;
			std::transform(text.begin(), text.end(), text.begin(), [](unsigned char c) { return static_cast<char>(std::tolower(c)); });
			return text.find(a_part) != std::string::npos;
		}

		bool IsCloudBank(RE::NiAVObject* a_object)
		{
			if (StartsWithNoCase(a_object->name.c_str(), "WorldMapCloudBank")) { return true; }
			auto geometry = a_object->AsGeometry();
			if (!geometry) { return false; }
			auto effect = netimmerse_cast<RE::BSEffectShaderProperty*>(geometry->GetGeometryRuntimeData().properties[RE::BSGeometry::States::kEffect].get());
			auto material = effect ? static_cast<RE::BSEffectShaderMaterial*>(effect->GetBaseMaterial()) : nullptr;
			return material && ContainsNoCase(material->sourceTexturePath.c_str(), "sky\\superfluffycloud");
		}

		// The sky's cloud layers and rain are passed over: they are the sky's, hidden and shown again by Hide below, never kept hidden.
		void FindBank(RE::NiAVObject* a_object, const std::vector<RE::NiAVObject*>& a_skip, int a_depth)
		{
			if (!a_object || a_depth > 64 || std::find(a_skip.begin(), a_skip.end(), a_object) != a_skip.end()) { return; }
			if (IsCloudBank(a_object))
			{
				if (std::none_of(g_bank.begin(), g_bank.end(), [&](auto& a_known) { return a_known.get() == a_object; }))
				{
					g_bank.emplace_back(a_object);
				}
				return;
			}
			if (auto node = a_object->AsNode())
			{
				for (auto& child : node->GetChildren()) { FindBank(child.get(), a_skip, a_depth + 1); }
			}
		}

		void Hide(RE::NiAVObject* a_object, std::vector<RE::NiPointer<RE::NiAVObject>>& a_list)
		{
			if (!a_object || a_object->GetAppCulled()) { return; }
			a_object->SetAppCulled(true);
			if (std::any_of(a_list.begin(), a_list.end(), [&](auto& a_known) { return a_known.get() == a_object; })) { ++g_reshown; }
			else { a_list.emplace_back(a_object); }
		}

		std::string Describe(RE::NiAVObject* a_object)
		{
			if (!a_object) { return "none"; }
			return std::format("{}{}", a_object->name.c_str() ? a_object->name.c_str() : "?", a_object->GetAppCulled() ? " (hidden)" : " (showing)");
		}
	}

	void Open()
	{
		g_bank.clear();
		g_sky.clear();
		g_frame = 0;
		g_reshown = 0;
		g_loggedNone = false;
		g_fogSaved = false;
	}

	void Frame(RE::NiAVObject* a_cameraRoot)
	{
		++g_frame;
		auto sky = RE::Sky::GetSingleton();
		if (sky)
		{
			Hide(sky->clouds ? sky->clouds->root.get() : nullptr, g_sky);
			if (auto precip = sky->precip)
			{
				Hide(precip->currentPrecip.get(), g_sky);
				Hide(precip->lastPrecip.get(), g_sky);
			}
			Hide(sky->auroraRoot.get(), g_sky);
			sky->flash = 0.0f;
			if (!g_fogSaved)
			{
				g_fogSaved = true;
				g_fog[0] = sky->fogNear;
				g_fog[1] = sky->fogFar;
				g_fog[2] = sky->fogClamp;
				logger::info("Weather off the map: sky weather {:X}, fog near {} far {} clamp {}, cloud layers {}, rain or snow {} / {}, aurora {}",
					sky->currentWeather ? sky->currentWeather->GetFormID() : 0, g_fog[0], g_fog[1], g_fog[2], sky->clouds ? sky->clouds->numLayers : 0,
					sky->precip ? Describe(sky->precip->currentPrecip.get()) : "none", sky->precip ? Describe(sky->precip->lastPrecip.get()) : "none",
					Describe(sky->auroraRoot.get()));
			}
			sky->fogNear = kFogNear;
			sky->fogFar = kFogFar;
			sky->fogClamp = 0.0f;
		}

		// The cloud bank, every frame once found: one flag check per shape.
		for (auto& shape : g_bank) { Hide(shape.get(), g_bank); }
		if (g_bank.empty() && g_frame % 30 == 1 && g_frame < 600)
		{
			std::vector<RE::NiAVObject*> skip;
			if (sky && sky->clouds) { skip.push_back(sky->clouds->root.get()); }
			if (sky && sky->precip)
			{
				skip.push_back(sky->precip->currentPrecip.get());
				skip.push_back(sky->precip->lastPrecip.get());
			}
			FindBank(reinterpret_cast<RE::SceneGraph*>(RE::Main::WorldRootNode()), skip, 0);
			if (sky) { FindBank(sky->root.get(), skip, 0); }
			FindBank(a_cameraRoot, skip, 0);
			if (!g_bank.empty())
			{
				std::string names;
				for (auto& shape : g_bank)
				{
					names += std::format(" {}{}", Describe(shape.get()), shape->parent && shape->parent->name.c_str() ? std::format(" under {}", shape->parent->name.c_str()) : "");
					shape->SetAppCulled(true);
				}
				logger::info("Map cloud bank found at map frame {}: {} shapes hidden:{}", g_frame, g_bank.size(), names);
			}
		}
		else if (g_bank.empty() && g_frame >= 600 && !g_loggedNone)
		{
			g_loggedNone = true;
			logger::warn("No map cloud bank found in the scene by map frame 600 (no shape named WorldMapCloudBank* or drawing sky\\superfluffycloud*)");
		}
	}

	void Close()
	{
		for (auto& object : g_sky)
		{
			if (object) { object->SetAppCulled(false); }
		}
		auto sky = RE::Sky::GetSingleton();
		// The sky works its fog out again from the weather; this only puts back what was there if nothing has done so yet.
		if (sky && g_fogSaved && sky->fogNear == kFogNear && sky->fogFar == kFogFar && sky->fogClamp == 0.0f)
		{
			sky->fogNear = g_fog[0];
			sky->fogFar = g_fog[1];
			sky->fogClamp = g_fog[2];
		}
		logger::info("Weather back on map close: cloud bank shapes kept hidden {}, sky objects shown again {}, hidden again during the open {} times", g_bank.size(),
			g_sky.size(), g_reshown);
		g_sky.clear();
		g_bank.clear();
		// One frame on, what the game world has: the falsifier for "closing the map brings the weather back unchanged".
		if (auto tasks = SKSE::GetTaskInterface())
		{
			tasks->AddTask([]() {
				auto s = RE::Sky::GetSingleton();
				if (!s) { return; }
				logger::info("After the map: sky weather {:X}, fog near {} far {} clamp {}, cloud layers {}, rain or snow {} / {}, aurora {}",
					s->currentWeather ? s->currentWeather->GetFormID() : 0, s->fogNear, s->fogFar, s->fogClamp, Describe(s->clouds ? s->clouds->root.get() : nullptr),
					s->precip ? Describe(s->precip->currentPrecip.get()) : "none", s->precip ? Describe(s->precip->lastPrecip.get()) : "none", Describe(s->auroraRoot.get()));
			});
		}
	}

	int Bank() { return static_cast<int>(g_bank.size()); }
	int SkyHidden() { return static_cast<int>(g_sky.size()); }
	int Reshown() { return g_reshown; }
}
