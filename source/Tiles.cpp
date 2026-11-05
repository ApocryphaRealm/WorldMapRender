// World Map Render - GPL-3.0-or-later. Sharp pictures over a land's sheet.
//
// Based on tiles.cpp of GTSLandMap.dll (GTS Land Map 1.0, Copyright (C) 2026 mxdie, GPL-3.0-or-later,
// https://github.com/7mxdie/GTSLandMap, plugin_tiles/src/tiles.cpp at commit ab4f13e). Changed from it, 2026-11-05: tiles are BC1 DDS with
// their own mips instead of WebP, so there is no decoder and each one costs a sixth of the video memory; one level of tile; the
// slots are quads already in the land's mesh, named "wmr slot NN", rather than clones of the sheet made at run time (CommonLibSSE-NG
// 3.7 has no clone call); built for Skyrim 1.5.97.
//
// A shape can only show one texture, and the game's own small slot files give each quad a texture object of its own, whose view is
// pointed at the tile on show.
//
// Tiles: Data/textures/terrain/worldmaprender/t/<edid>/<tiles|tiles_n>/tI_J.dds, I and J counted from the land's top left, four cells each.
// Places: Data/SKSE/Plugins/WorldMapRenderTiles.txt, one line a land: edid x0 y1 (cells).
#include "PCH.h"

#include "Tiles.h"

#include "utils/Logger.h"

#include <algorithm>
#include <atomic>
#include <cmath>
#include <condition_variable>
#include <cstring>
#include <deque>
#include <format>
#include <fstream>
#include <mutex>
#include <sstream>
#include <thread>
#include <unordered_map>
#include <vector>

namespace Tiles
{
	namespace
	{
		constexpr int   kKeep = 96;        // tiles kept in video memory: 96 x 0.7 MB
		constexpr float kUnits = 4096.0f;
		constexpr int   kTileCells = 4;
		constexpr float kWide = 90000.0f;  // a view wider than this is the sheet alone
		constexpr float kLift = 16.0f;     // above the sheet, in world units: more than the depth buffer's step at this distance

		using View = REX::W32::ID3D11ShaderResourceView;

		struct Place
		{
			int x0 = 0, y1 = 0;
		};

		struct Slot
		{
			RE::NiAVObject*      node = nullptr;
			RE::NiSourceTexture* texture = nullptr;
			ID3D11ShaderResourceView* own = nullptr;
			std::uint64_t        showing = 0;
		};

		struct Tile
		{
			View*         view = nullptr;
			std::uint64_t used = 0;
		};

		struct Done
		{
			std::uint64_t key;
			View*         view;
		};

		std::unordered_map<std::string, Place> g_places;
		bool g_placesRead = false;

		std::vector<Slot> g_slots;
		RE::NiAVObject*   g_sheet = nullptr;
		RE::NiNode*       g_parent = nullptr;
		Place             g_place;
		std::string       g_folder;
		bool              g_on = false;
		std::uint64_t     g_clock = 0;
		int               g_showing = 0;

		// worker side
		std::mutex                 g_lock;
		std::condition_variable    g_wake;
		std::deque<std::uint64_t>  g_wanted;
		std::vector<Done>          g_ready;
		std::atomic<std::uint64_t> g_session{ 0 };
		std::atomic<int>           g_decoded{ 0 }, g_missing{ 0 }, g_failed{ 0 };
		std::vector<std::thread>   g_workers;

		// main thread side
		std::unordered_map<std::uint64_t, Tile> g_tiles;
		std::unordered_map<std::uint64_t, int>  g_asked;  // 1 asked, 2 no such file

		std::uint64_t Key(int a_i, int a_j)
		{
			return (static_cast<std::uint64_t>(static_cast<std::uint32_t>(a_i) & 0xFFFFF) << 20) | (static_cast<std::uint32_t>(a_j) & 0xFFFFF) | (1ull << 40);
		}

		void ReadPlaces()
		{
			g_placesRead = true;
			std::ifstream in("Data/SKSE/Plugins/WorldMapRenderTiles.txt");
			std::string   line;
			while (std::getline(in, line))
			{
				if (line.empty() || line[0] == '#') { continue; }
				std::istringstream s(line);
				std::string        name;
				Place              p;
				s >> name >> p.x0 >> p.y1;
				if (s.fail())
				{
					logger::error("Bad tile line: {}", line);
					continue;
				}
				std::transform(name.begin(), name.end(), name.begin(), [](unsigned char c) { return static_cast<char>(std::tolower(c)); });
				g_places[name] = p;
			}
			logger::info("Tile places: {}", g_places.size());
		}

		// A BC1 DDS with its mips, straight to the card.
		View* Build(const std::vector<char>& a_raw)
		{
			if (a_raw.size() < 128 || std::memcmp(a_raw.data(), "DDS ", 4) != 0) { return nullptr; }
			const auto u32 = [&](std::size_t a_at) {
				std::uint32_t v = 0;
				std::memcpy(&v, a_raw.data() + a_at, 4);
				return v;
			};
			const std::uint32_t h = u32(12), w = u32(16), mips = (std::max)(u32(28), 1u);
			const bool          dx10 = std::memcmp(a_raw.data() + 84, "DX10", 4) == 0;
			if (!dx10 && std::memcmp(a_raw.data() + 84, "DXT1", 4) != 0) { return nullptr; }
			if (dx10 && u32(128) != 71) { return nullptr; }
			std::size_t at = dx10 ? 148 : 128;
			std::vector<REX::W32::D3D11_SUBRESOURCE_DATA> init;
			std::uint32_t mw = w, mh = h;
			for (std::uint32_t m = 0; m < mips; ++m)
			{
				const std::uint32_t pitch = (std::max)((mw + 3) / 4, 1u) * 8, rows = (std::max)((mh + 3) / 4, 1u);
				if (at + std::size_t(pitch) * rows > a_raw.size()) { return nullptr; }
				init.push_back({ a_raw.data() + at, pitch, 0 });
				at += std::size_t(pitch) * rows;
				mw = (std::max)(mw / 2, 1u);
				mh = (std::max)(mh / 2, 1u);
			}
			REX::W32::D3D11_TEXTURE2D_DESC d{};
			d.width = w;
			d.height = h;
			d.mipLevels = static_cast<std::uint32_t>(init.size());
			d.arraySize = 1;
			d.format = REX::W32::DXGI_FORMAT_BC1_UNORM;
			d.sampleDesc.count = 1;
			d.usage = REX::W32::D3D11_USAGE_IMMUTABLE;
			d.bindFlags = REX::W32::D3D11_BIND_SHADER_RESOURCE;
			auto device = RE::BSGraphics::Renderer::GetDevice();
			REX::W32::ID3D11Texture2D* tex = nullptr;
			if (!device || device->CreateTexture2D(&d, init.data(), &tex) < 0 || !tex) { return nullptr; }
			View* view = nullptr;
			device->CreateShaderResourceView(tex, nullptr, &view);
			tex->Release();  // the view holds it
			return view;
		}

		void Worker()
		{
			for (;;)
			{
				std::uint64_t key = 0, session = 0;
				std::string   folder;
				{
					std::unique_lock l(g_lock);
					g_wake.wait(l, [] { return !g_wanted.empty(); });
					key = g_wanted.front();
					g_wanted.pop_front();
					session = g_session.load();
					folder = g_folder;
				}
				const int i = static_cast<int>((key >> 20) & 0xFFFFF), j = static_cast<int>(key & 0xFFFFF);
				const auto path = std::format("{}/t{}_{}.dds", folder, i, j);
				View* view = nullptr;
				std::ifstream f(path, std::ios::binary);
				if (f.is_open())
				{
					std::vector<char> raw((std::istreambuf_iterator<char>(f)), std::istreambuf_iterator<char>());
					view = Build(raw);
					view ? ++g_decoded : ++g_failed;
				}
				else
				{
					++g_missing;  // a tile of plain sea is not written: the sheet shows
				}
				std::lock_guard l(g_lock);
				if (session == g_session.load()) { g_ready.push_back({ key, view }); }
				else if (view) { view->Release(); }
			}
		}

		void Hide(Slot& a_slot)
		{
			if (a_slot.showing)
			{
				a_slot.showing = 0;
				a_slot.node->SetAppCulled(true);
			}
		}

		// The slot is a unit quad. Lifted towards the camera and drawn in by as much, so from the camera it sits exactly where it belongs on the sheet.
		void PlaceSlot(Slot& a_slot, float a_x0, float a_x1, float a_y0, const RE::NiPoint3& a_cam, float a_d)
		{
			const float r = (a_d - kLift) / a_d;
			const float x0 = a_cam.x + (a_x0 - a_cam.x) * r, x1 = a_cam.x + (a_x1 - a_cam.x) * r;
			const float y0 = a_cam.y + (a_y0 - a_cam.y) * r;
			RE::NiTransform want;
			want.scale = x1 - x0;
			want.translate = { x0, y0, g_sheet->world.translate.z + kLift };
			a_slot.node->local = g_parent->world.Invert() * want;
			a_slot.node->SetAppCulled(false);
			RE::NiUpdateData update{};
			a_slot.node->Update(update);
		}

		bool FindSlots()
		{
			g_parent = g_sheet->parent;
			if (!g_parent)
			{
				logger::error("Tiles: sheet has no parent");
				return false;
			}
			for (auto& child : g_parent->GetChildren())
			{
				auto object = child.get();
				if (!object || !object->name.c_str() || std::strncmp(object->name.c_str(), "wmr slot", 8) != 0) { continue; }
				auto geometry = object->AsGeometry();
				auto effect = geometry ? netimmerse_cast<RE::BSEffectShaderProperty*>(geometry->GetGeometryRuntimeData().properties[RE::BSGeometry::States::kEffect].get()) : nullptr;
				auto material = effect ? static_cast<RE::BSEffectShaderMaterial*>(effect->GetBaseMaterial()) : nullptr;
				if (!material || !material->sourceTexturePath.c_str())
				{
					logger::error("Tiles: slot {} has no material", object->name.c_str());
					return false;
				}
				RE::NiPointer<RE::NiTexture> texture;
				RE::BSShaderManager::GetTexture(material->sourceTexturePath.c_str(), true, texture, false);
				auto source = netimmerse_cast<RE::NiSourceTexture*>(texture.get());
				if (!source || !source->rendererTexture)
				{
					logger::error("Tiles: no slot file {}", material->sourceTexturePath.c_str());
					return false;
				}
				material->sourceTexture = RE::NiPointer<RE::NiSourceTexture>(source);
				Slot s;
				s.node = object;
				s.texture = source;
				s.own = source->rendererTexture->resourceView;
				object->SetAppCulled(true);
				g_slots.push_back(s);
			}
			if (g_slots.empty())
			{
				logger::error("Tiles: this land's mesh has no slots");
				return false;
			}
			return true;
		}

		void Clear()
		{
			for (auto& s : g_slots)
			{
				if (s.texture && s.texture->rendererTexture) { s.texture->rendererTexture->resourceView = s.own; }
				if (s.node) { s.node->SetAppCulled(true); }
			}
			g_slots.clear();
			{
				std::lock_guard l(g_lock);
				++g_session;
				g_wanted.clear();
				for (auto& d : g_ready)
				{
					if (d.view) { d.view->Release(); }
				}
				g_ready.clear();
			}
			for (auto& [k, t] : g_tiles)
			{
				if (t.view) { t.view->Release(); }
			}
			g_tiles.clear();
			g_asked.clear();
			g_sheet = nullptr;
			g_parent = nullptr;
			g_on = false;
			g_showing = 0;
		}
	}

	void Open(const std::string& a_edid, RE::NiAVObject* a_sheet, bool a_night)
	{
		if (g_on) { Clear(); }
		if (!g_placesRead) { ReadPlaces(); }
		auto it = g_places.find(a_edid);
		if (it == g_places.end() || !a_sheet) { return; }
		g_sheet = a_sheet;
		g_place = it->second;
		{
			std::lock_guard l(g_lock);
			g_folder = std::format("Data/textures/terrain/worldmaprender/t/{}/{}", a_edid, a_night ? "tiles_n" : "tiles");
		}
		if (!FindSlots())
		{
			Clear();
			return;
		}
		if (g_workers.empty())
		{
			for (int n = 0; n < 2; ++n) { g_workers.emplace_back(Worker).detach(); }
		}
		g_decoded = g_missing = g_failed = 0;
		g_on = true;
		logger::info("Tiles on for {}: {} slots, folder {}", a_edid, g_slots.size(), g_folder);
	}

	void Frame(const RE::NiPoint3& a_cam, float a_width, float a_aspect, float a_d)
	{
		if (!g_on) { return; }
		++g_clock;
		// what the workers have finished
		{
			std::lock_guard l(g_lock);
			for (auto& d : g_ready)
			{
				if (d.view) { g_tiles[d.key] = { d.view, g_clock }; }
				else { g_asked[d.key] = 2; }
			}
			g_ready.clear();
		}
		struct Want
		{
			std::uint64_t key;
			float         x0, x1, y0;
			float         dist;
		};
		std::vector<Want> wants;
		if (a_width < kWide)
		{
			const float h = a_width * a_aspect;
			const float size = kTileCells * kUnits, top = g_place.y1 * kUnits, left = g_place.x0 * kUnits, m = size * 0.25f;
			const int   i0 = static_cast<int>(std::floor((a_cam.x - a_width / 2 - m - left) / size)), i1 = static_cast<int>(std::floor((a_cam.x + a_width / 2 + m - left) / size));
			const int   j0 = static_cast<int>(std::floor((top - (a_cam.y + h / 2 + m)) / size)), j1 = static_cast<int>(std::floor((top - (a_cam.y - h / 2 - m)) / size));
			for (int j = (std::max)(j0, 0); j <= j1; ++j)
			{
				for (int i = (std::max)(i0, 0); i <= i1; ++i)
				{
					const float x0 = left + i * size, y1 = top - j * size;
					const float cx = x0 + size / 2 - a_cam.x, cy = y1 - size / 2 - a_cam.y;
					wants.push_back({ Key(i, j), x0, x0 + size, y1 - size, cx * cx + cy * cy });
				}
			}
		}
		// nearest the middle first
		std::stable_sort(wants.begin(), wants.end(), [](const Want& a, const Want& b) { return a.dist < b.dist; });
		std::vector<std::uint64_t> ask;
		std::size_t next = 0;
		for (auto& w : wants)
		{
			auto found = g_tiles.find(w.key);
			if (found == g_tiles.end())
			{
				if (!g_asked.contains(w.key))
				{
					g_asked[w.key] = 1;
					ask.push_back(w.key);
				}
				continue;
			}
			found->second.used = g_clock;
			if (next >= g_slots.size()) { continue; }
			auto& s = g_slots[next++];
			if (s.showing != w.key)
			{
				s.texture->rendererTexture->resourceView = reinterpret_cast<ID3D11ShaderResourceView*>(found->second.view);
				s.showing = w.key;
			}
			PlaceSlot(s, w.x0, w.x1, w.y0, a_cam, a_d);
		}
		g_showing = static_cast<int>(next);
		for (; next < g_slots.size(); ++next) { Hide(g_slots[next]); }
		if (!ask.empty())
		{
			std::lock_guard l(g_lock);
			// the view moves on: what it asked for before and still hasn't got waits behind this
			for (auto it = ask.rbegin(); it != ask.rend(); ++it) { g_wanted.push_front(*it); }
			g_wake.notify_all();
		}
		// oldest out, never one on show
		if (g_tiles.size() > kKeep)
		{
			std::vector<std::pair<std::uint64_t, std::uint64_t>> order;
			for (auto& [k, t] : g_tiles)
			{
				if (t.used != g_clock) { order.emplace_back(t.used, k); }
			}
			std::sort(order.begin(), order.end());
			for (std::size_t n = 0; n < order.size() && g_tiles.size() > kKeep; ++n)
			{
				auto it = g_tiles.find(order[n].second);
				for (auto& s : g_slots)
				{
					if (s.showing == order[n].second) { Hide(s); }
				}
				it->second.view->Release();
				g_tiles.erase(it);
				g_asked.erase(order[n].second);
			}
		}
		if (g_clock % 600 == 1)
		{
			logger::info("Tiles: {} wanted, {} held, read {} missing {} failed {}", wants.size(), g_tiles.size(), g_decoded.load(), g_missing.load(), g_failed.load());
		}
	}

	void Close()
	{
		if (g_on)
		{
			logger::info("Tiles off: read {} missing {} failed {}", g_decoded.load(), g_missing.load(), g_failed.load());
			Clear();
		}
	}

	int Showing() { return g_showing; }
	int Held() { return static_cast<int>(g_tiles.size()); }
}
