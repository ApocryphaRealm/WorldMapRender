#pragma once

// DevBench tool "wmr.state" (rule 31): the map camera's live state. The mod has no controls of its own to drive: the map's own
// zoom and pan are what a test presses (rule 64 has nothing further to expose).
namespace DevBenchTool
{
	// DevBench answers its interface request from kPostLoad on; retried at kDataLoaded (rule 17).
	void Init(bool a_lastAttempt);
}
