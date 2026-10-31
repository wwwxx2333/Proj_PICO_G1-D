#pragma once
// Official SDK command bounds and G1-D URDF position bounds.
// Sources: deploy/OFFICIAL_LIMITS.md. Mirror of g1d_pico/limits.py.
#include <cmath>
#include <iomanip>
#include <ostream>
#include <string>

namespace g1d::limits {
inline constexpr double AgvForwardMps = 1.5;
inline constexpr double AgvYawRps = 0.6;
inline constexpr double LiftNormalized = 1.0;
inline constexpr double ColumnTravelM = 0.42;
inline constexpr double WaistYawRad = 2.7053;
inline constexpr double WaistPitchMinRad = -0.04363;
inline constexpr double WaistPitchMaxRad = 2.3562;

inline bool ValidHeightWindow(double lower, double upper) {
    return std::isfinite(lower) && std::isfinite(upper) &&
           0 <= lower && lower < upper && upper-lower <= ColumnTravelM+1e-7;
}

// Four argv entries deliberately reject on older AGV AND arm executables
// before they initialize DDS. This query never opens a robot connection.
inline bool PrintIfRequested(int argc, char** argv, std::ostream& out) {
    if (argc != 4 || std::string(argv[1]) != "--print-limits" ||
        std::string(argv[2]) != "--offline" || std::string(argv[3]) != "1") return false;
    out << std::setprecision(12)
        << "{\"revision\":1,\"agv_forward_mps\":" << AgvForwardMps
        << ",\"agv_yaw_rps\":" << AgvYawRps
        << ",\"lift_normalized\":" << LiftNormalized
        << ",\"column_travel_m\":" << ColumnTravelM
        << ",\"waist_yaw_rad\":" << WaistYawRad
        << ",\"waist_pitch_min_rad\":" << WaistPitchMinRad
        << ",\"waist_pitch_max_rad\":" << WaistPitchMaxRad << "}\n";
    return true;
}
} // namespace g1d::limits
