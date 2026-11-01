// Offline only: optional Unix telemetry; no SDK, DDS or robot imports.
#include "../bridge/g1d_recording.hpp"
#include <chrono>
#include <iostream>
#include <array>
#include <cassert>
#include <limits>
int main() {
    std::ostringstream json;
    g1d::JsonOptionalArray(json, std::array<double,3>{1.5,std::numeric_limits<double>::quiet_NaN(),std::numeric_limits<double>::infinity()});
    assert(json.str()=="[1.5,null,null]");
    g1d::RecordingSink sink;
    const auto begin = std::chrono::steady_clock::now();
    for (int i=0; i<10000; ++i) sink.Send("fixture", "{\"owned\":false,\"q\":[1,2,3]}", 123.25);
    const auto elapsed = std::chrono::duration<double>(std::chrono::steady_clock::now()-begin).count();
    std::cout << "nonblocking_recording_sink_seconds=" << elapsed << '\n';
    return elapsed > 2.0 ? 1 : 0;
}
