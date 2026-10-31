#pragma once
// SDK-independent parsing shared by the real AGV bridge and offline tests.
#include "g1d_limits.hpp"
#include <sstream>

namespace g1d {
struct AgvCommand { float vx=0, vyaw=0, vz=0; };

inline bool ParseAgvCommand(const std::string& line, AgvCommand& out) {
    std::istringstream stream(line);
    AgvCommand parsed;
    std::string extra;
    if (!(stream >> parsed.vx >> parsed.vyaw >> parsed.vz) || (stream >> extra)) return false;
    if (!std::isfinite(parsed.vx) || !std::isfinite(parsed.vyaw) || !std::isfinite(parsed.vz)) return false;
    // Compare floats to floats: SDK2's API parameters are float as well.
    if (std::abs(parsed.vx) > static_cast<float>(limits::AgvForwardMps) ||
        std::abs(parsed.vyaw) > static_cast<float>(limits::AgvYawRps) ||
        std::abs(parsed.vz) > static_cast<float>(limits::LiftNormalized)) return false;
    out = parsed;
    return true;
}
} // namespace g1d
