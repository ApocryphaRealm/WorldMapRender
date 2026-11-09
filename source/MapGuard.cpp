#include "PCH.h"

#include "MapGuard.h"

#include "utils/Logger.h"

#include <chrono>
#include <cstring>
#include <format>
#include <fstream>
#include <string>
#include <list>
#include <vector>

// Why this exists: on some launches the world map drew only its backdrop colour (2026-11-05, 2026-11-06 08:14, 2026-11-09 from
// 06:37), with the same DLL and pictures that drew it on other launches, while the log said the sheet's texture had an image and a
// view. Those two pointers only prove a texture object exists, not that the picture is in it. So the guard reads the engine's own
// copy's real size from the graphics card, logs it (the discriminating line: a wrong size there names the engine's load), and then
// puts this plugin's own copy, read straight from the file, behind the sheet's texture whatever the engine's copy was.
namespace MapGuard
{
	namespace
	{

		struct Own
		{
			std::string               path;
			ID3D11Texture2D*          texture = nullptr;
			ID3D11ShaderResourceView* view = nullptr;
			std::uint32_t             width = 0, height = 0, mips = 0, skipped = 0;
			DXGI_FORMAT               format = DXGI_FORMAT_UNKNOWN;
		};

		std::list<Own>   g_own;  // the current land's day and night pictures and the last one before; older ones are released

		RE::NiAVObject*              g_sheet = nullptr;
		RE::BSEffectShaderMaterial* g_material = nullptr;
		std::string                  g_path;
		const Own*                   g_bound = nullptr;
		int                          g_rebinds = 0;
		int                          g_tries = 0;
		int                          g_nextTry = 0;
		std::string                  g_status = "not run";

		constexpr int kMaxRebinds = 5;
		constexpr int kMaxTries = 5;

		std::string Lower(std::string a_t)
		{
			for (auto& ch : a_t) { ch = static_cast<char>(std::tolower(static_cast<unsigned char>(ch))); }
			return a_t;
		}

		std::uint32_t U32(const std::vector<std::byte>& a_b, std::size_t a_at)
		{
			std::uint32_t v = 0;
			std::memcpy(&v, a_b.data() + a_at, 4);
			return v;
		}

		// bytes per 4x4 block for the block formats, 0 for the plain 32-bit ones
		std::uint32_t BlockBytes(DXGI_FORMAT a_f)
		{
			switch (a_f)
			{
			case DXGI_FORMAT_BC1_UNORM:
			case DXGI_FORMAT_BC1_UNORM_SRGB:
			case DXGI_FORMAT_BC4_UNORM:
			case DXGI_FORMAT_BC4_SNORM:
				return 8;
			case DXGI_FORMAT_BC2_UNORM:
			case DXGI_FORMAT_BC2_UNORM_SRGB:
			case DXGI_FORMAT_BC3_UNORM:
			case DXGI_FORMAT_BC3_UNORM_SRGB:
			case DXGI_FORMAT_BC5_UNORM:
			case DXGI_FORMAT_BC5_SNORM:
			case DXGI_FORMAT_BC6H_UF16:
			case DXGI_FORMAT_BC6H_SF16:
			case DXGI_FORMAT_BC7_UNORM:
			case DXGI_FORMAT_BC7_UNORM_SRGB:
				return 16;
			default:
				return 0;
			}
		}

		bool Plain32(DXGI_FORMAT a_f) { return a_f == DXGI_FORMAT_R8G8B8A8_UNORM || a_f == DXGI_FORMAT_B8G8R8A8_UNORM || a_f == DXGI_FORMAT_B8G8R8X8_UNORM; }

		std::size_t MipBytes(DXGI_FORMAT a_f, std::uint32_t a_w, std::uint32_t a_h, std::uint32_t* a_pitch)
		{
			if (const auto bb = BlockBytes(a_f))
			{
				const std::uint32_t bw = (std::max)(1u, (a_w + 3) / 4), bh = (std::max)(1u, (a_h + 3) / 4);
				*a_pitch = bw * bb;
				return static_cast<std::size_t>(bw) * bh * bb;
			}
			*a_pitch = a_w * 4;
			return static_cast<std::size_t>(a_w) * a_h * 4;
		}

		void Release(Own& a_own)
		{
			if (a_own.view) { a_own.view->Release(); }
			if (a_own.texture) { a_own.texture->Release(); }
			a_own.view = nullptr;
			a_own.texture = nullptr;
		}

		// Reads the DDS from the game's Data folder (MO2's virtual folder included) and makes our own texture on the game's device.
		// a_skip leaves out that many of the largest mips: the smaller picture, if the full one will not go onto the card.
		bool Load(const std::string& a_path, std::uint32_t a_skip, Own& a_out, std::string& a_why)
		{
			const auto start = std::chrono::steady_clock::now();
			const std::string rel = Lower(a_path).rfind("textures\\", 0) == 0 ? a_path : "textures\\" + a_path;
			std::ifstream in("Data\\" + rel, std::ios::binary | std::ios::ate);
			if (!in.is_open()) { a_why = "file not found under Data"; return false; }
			const auto size = static_cast<std::size_t>(in.tellg());
			if (size < 128) { a_why = std::format("file is {} bytes", size); return false; }
			std::vector<std::byte> file(size);
			in.seekg(0);
			in.read(reinterpret_cast<char*>(file.data()), static_cast<std::streamsize>(size));
			if (!in) { a_why = "file read failed"; return false; }
			if (U32(file, 0) != 0x20534444) { a_why = "not a DDS"; return false; }
			const std::uint32_t height = U32(file, 12), width = U32(file, 16);
			const std::uint32_t mips = (std::max)(1u, U32(file, 28));
			const std::uint32_t fourcc = U32(file, 84);
			std::size_t         at = 128;
			DXGI_FORMAT         format = DXGI_FORMAT_UNKNOWN;
			switch (fourcc)
			{
			case 0x31545844: format = DXGI_FORMAT_BC1_UNORM; break;  // DXT1
			case 0x33545844: format = DXGI_FORMAT_BC2_UNORM; break;  // DXT3
			case 0x35545844: format = DXGI_FORMAT_BC3_UNORM; break;  // DXT5
			case 0x32495441: format = DXGI_FORMAT_BC5_UNORM; break;  // ATI2
			case 0x30315844:                                         // DX10
				if (size < 148) { a_why = "DX10 header cut short"; return false; }
				format = static_cast<DXGI_FORMAT>(U32(file, 128));
				at = 148;
				break;
			default:
				if (fourcc == 0 && U32(file, 88) == 32) { format = U32(file, 92) == 0x00FF0000 ? DXGI_FORMAT_B8G8R8A8_UNORM : DXGI_FORMAT_R8G8B8A8_UNORM; }
				break;
			}
			if (!BlockBytes(format) && !Plain32(format)) { a_why = std::format("format {} (fourcc {:08X}) not handled", static_cast<int>(format), fourcc); return false; }
			if (a_skip >= mips) { a_why = "no smaller mip left"; return false; }

			std::vector<D3D11_SUBRESOURCE_DATA> levels;
			std::uint32_t                       w = width, h = height;
			for (std::uint32_t m = 0; m < mips; ++m)
			{
				std::uint32_t pitch = 0;
				const auto    bytes = MipBytes(format, w, h, &pitch);
				if (at + bytes > size) { a_why = std::format("file cut short at mip {}", m); return false; }
				if (m >= a_skip) { levels.push_back({ file.data() + at, pitch, static_cast<std::uint32_t>(bytes) }); }
				at += bytes;
				w = (std::max)(1u, w / 2);
				h = (std::max)(1u, h / 2);
			}

			auto* device = reinterpret_cast<::ID3D11Device*>(RE::BSGraphics::Renderer::GetDevice());
			if (!device) { a_why = "no graphics device"; return false; }
			D3D11_TEXTURE2D_DESC desc{};
			desc.Width = (std::max)(1u, width >> a_skip);
			desc.Height = (std::max)(1u, height >> a_skip);
			desc.MipLevels = mips - a_skip;
			desc.ArraySize = 1;
			desc.Format = format;
			desc.SampleDesc = { 1, 0 };
			desc.Usage = D3D11_USAGE_IMMUTABLE;
			desc.BindFlags = D3D11_BIND_SHADER_RESOURCE;
			ID3D11Texture2D* texture = nullptr;
			auto             hr = device->CreateTexture2D(&desc, levels.data(), &texture);
			if (FAILED(hr) || !texture) { a_why = std::format("CreateTexture2D {}x{} failed, {:08X}", desc.Width, desc.Height, static_cast<std::uint32_t>(hr)); return false; }
			ID3D11ShaderResourceView* view = nullptr;
			hr = device->CreateShaderResourceView(texture, nullptr, &view);
			if (FAILED(hr) || !view)
			{
				texture->Release();
				a_why = std::format("CreateShaderResourceView failed, {:08X}", static_cast<std::uint32_t>(hr));
				return false;
			}
			a_out = Own{ a_path, texture, view, desc.Width, desc.Height, desc.MipLevels, a_skip, format };
			const auto ms = std::chrono::duration_cast<std::chrono::milliseconds>(std::chrono::steady_clock::now() - start).count();
			logger::info("Map guard: own copy of {} made on device {:X}: {}x{} format {} mips {}{} in {} ms", a_path, reinterpret_cast<std::uintptr_t>(device), desc.Width,
				desc.Height, static_cast<int>(format), desc.MipLevels, a_skip ? std::format(" (largest {} mip(s) left out)", a_skip) : std::string(), ms);
			return true;
		}

		const Own* GetOwn(const std::string& a_path)
		{
			for (auto& o : g_own)
			{
				if (o.path == a_path && o.view) { return &o; }
			}
			for (std::uint32_t skip = 0; skip < 3; ++skip)
			{
				Own         own;
				std::string why;
				if (Load(a_path, skip, own, why))
				{
					// keep at most three pictures of ours; a land's day and night and the one before
					while (g_own.size() >= 3)
					{
						if (g_bound == &g_own.front()) { g_bound = nullptr; }
						Release(g_own.front());
						g_own.erase(g_own.begin());
					}
					g_own.push_back(own);
					return &g_own.back();
				}
				logger::warn("Map guard: own copy of {} at {} mip(s) down: {}", a_path, skip, why);
				if (why.find("CreateTexture2D") == std::string::npos) { break; }  // only a card refusal is helped by a smaller picture
			}
			return nullptr;
		}

		std::string Describe(RE::BSGraphics::Texture* a_rt)
		{
			if (!a_rt) { return "no renderer texture"; }
			if (!a_rt->texture) { return "no image"; }
			D3D11_RESOURCE_DIMENSION dim{};
			a_rt->texture->GetType(&dim);
			if (dim != D3D11_RESOURCE_DIMENSION_TEXTURE2D) { return std::format("resource of kind {}", static_cast<int>(dim)); }
			D3D11_TEXTURE2D_DESC desc{};
			a_rt->texture->GetDesc(&desc);
			return std::format("{}x{} format {} mips {}, view {}", desc.Width, desc.Height, static_cast<int>(desc.Format), desc.MipLevels, a_rt->resourceView ? "yes" : "NONE");
		}

		// The sheet and everything above it drawn, and the sheet not faded out.
		void ShowSheet()
		{
			for (RE::NiAVObject* p = g_sheet; p; p = p->parent)
			{
				if (p->GetAppCulled())
				{
					p->SetAppCulled(false);
					logger::warn("Map guard: {} was hidden, shown again", p->name.c_str() ? p->name.c_str() : "?");
				}
			}
		}

		bool Apply(const char* a_when)
		{
			if (!g_material) { return false; }
			auto* source = g_material->sourceTexture.get();
			if (!source)
			{
				RE::NiPointer<RE::NiTexture> texture;
				RE::BSShaderManager::GetTexture(g_path.c_str(), true, texture, false);
				if (auto* s = netimmerse_cast<RE::NiSourceTexture*>(texture.get())) { g_material->sourceTexture = RE::NiPointer<RE::NiSourceTexture>(s); }
				source = g_material->sourceTexture.get();
				if (!source)
				{
					g_status = "no texture object on the sheet";
					logger::warn("Map guard ({}): {}: the sheet has no texture object and none could be made", a_when, g_path);
					return false;
				}
			}
			auto*       rt = source->rendererTexture;
			const auto  engine = Describe(rt);
			const auto* own = GetOwn(g_path);
			if (!own)
			{
				g_status = "own copy failed; engine copy " + engine;
				logger::error("Map guard ({}): {}: engine copy {}; own copy failed, the engine's is left", a_when, g_path, engine);
				return false;
			}
			if (!rt)
			{
				// the engine's own load has not given it one yet: one of ours, released by the engine with the texture as usual
				// 0x28 bytes in the game (this CommonLib names only the first 0x18); the count at 0x20 starts at 1
				auto* raw = static_cast<std::byte*>(RE::malloc(0x28));
				if (!raw) { return false; }
				std::memset(raw, 0, 0x28);
				*reinterpret_cast<std::uint32_t*>(raw + 0x20) = 1;
				rt = reinterpret_cast<RE::BSGraphics::Texture*>(raw);
				source->rendererTexture = rt;
			}
			if (rt->resourceView != own->view)
			{
				if (rt->resourceView) { rt->resourceView->Release(); }
				if (rt->texture) { rt->texture->Release(); }
				own->texture->AddRef();
				own->view->AddRef();
				rt->texture = own->texture;
				rt->resourceView = own->view;
			}
			g_bound = own;
			ShowSheet();
			const auto now = Describe(rt);
			const bool ok = rt->resourceView == own->view;
			g_status = std::format("{}: engine copy {}; now {}{}", g_path, engine, now, ok ? "" : " (NOT bound)");
			logger::info("Map guard ({}): {}: engine copy {} -> own copy bound: {} -> {}", a_when, g_path, engine, now, ok ? "ok" : "failed");
			return ok;
		}
	}

	bool Bind(RE::NiAVObject* a_sheet, RE::BSEffectShaderMaterial* a_material, const std::string& a_path)
	{
		g_sheet = a_sheet;
		g_material = a_material;
		g_path = a_path;
		g_bound = nullptr;
		g_rebinds = 0;
		g_tries = 1;
		g_nextTry = 0;
		return Apply("map open");
	}

	void Frame(int a_frame)
	{
		if (!g_material) { return; }
		if (!g_bound)
		{
			if (g_tries >= kMaxTries) { return; }
			if (g_nextTry == 0) { g_nextTry = a_frame + 30; }
			if (a_frame < g_nextTry) { return; }
			++g_tries;
			g_nextTry = a_frame + 30;
			Apply(std::format("try {}", g_tries).c_str());
			return;
		}
		auto* source = g_material->sourceTexture.get();
		auto* rt = source ? source->rendererTexture : nullptr;
		if (rt && rt->resourceView == g_bound->view) { return; }
		if (g_rebinds >= kMaxRebinds) { return; }
		++g_rebinds;
		logger::warn("Map guard: the sheet's picture was replaced at frame {} ({}), binding ours again ({} of {})", a_frame, source ? Describe(rt) : "no texture object", g_rebinds, kMaxRebinds);
		Apply("rebind");
	}

	void Close()
	{
		g_sheet = nullptr;
		g_material = nullptr;
		g_bound = nullptr;
	}

	std::string Status() { return g_status; }
}
