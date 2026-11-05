#include "PCH.h"

#include "DevBenchTool.h"

#include "DevBench/DevBenchAPI.h"
#include "MapState.h"
#include "utils/Logger.h"

#include <format>
#include <string>

namespace DevBenchTool
{
	namespace
	{
		void StateTool(void*, const char*, void* a_sink, DevBenchAPI::WriteFn a_write)
		{
			if (!a_write) { return; }
			const auto& s = mapstate::g;
			const std::string out = std::format(
				"{{\"open\":{},\"ours\":{},\"world\":\"{}\",\"camera\":[{},{}],\"distance\":{},\"viewWidth\":{},\"fov\":{},\"pan\":{},\"sheetZ\":{},\"night\":{},\"sheets\":{}}}",
				s.open, s.ours, s.world, s.cameraX, s.cameraY, s.distance, s.width, s.fov, s.pan, s.sheetZ, s.night, s.sheets);
			a_write(a_sink, out.c_str());
		}
	}

	void Init(bool a_lastAttempt)
	{
		static bool registered = false;
		if (registered) { return; }
		auto* devBench = DevBenchAPI::GetDevBenchInterface001();
		if (!devBench)
		{
			if (a_lastAttempt) { logger::info("DevBench not detected; skipping the \"wmr.state\" tool"); }
			return;
		}
		constexpr const char* descriptor =
			"{\"description\":\"World Map Render: the map camera's live state: open, world, whether it is ours, camera, distance to the sheet, view width, field of view, pan speed, sheet height, night, sheets swapped.\","
			"\"inputSchema\":{\"type\":\"object\",\"properties\":{}},\"readOnly\":true}";
		if (devBench->RegisterTool("wmr.state", descriptor, &StateTool, nullptr))
		{
			logger::info("registered \"wmr.state\" with DevBench (build {})", devBench->GetBuildNumber());
			registered = true;
		}
	}
}
