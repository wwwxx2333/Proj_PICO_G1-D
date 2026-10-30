// Offline guard tests: no SDK, sockets or robot access.
#include "../bridge/g1d_arm_guard.hpp"
#include <cassert>
#include <limits>

int main() {
    bool bad_speed = false;
    try { const g1d::ArmGuard invalid(2.0); }
    catch (const std::runtime_error&) { bad_speed = true; }
    assert(bad_speed);
    g1d::Joints measured{};
    g1d::ArmGuard faster(0.6);
    faster.Receive("ARM 8", measured, 8);
    faster.target[0] = 0.1;
    faster.Tick(measured, 8.01, 0.005);
    assert(std::abs(faster.output[0] - 0.003) < 1e-9);
    g1d::ArmGuard guard;
    guard.Tick(measured, 10, 0.005);
    assert(!guard.owned);
    guard.Receive("TARGET6 10 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0", measured, 10);
    assert(!guard.owned); // Targets must never implicitly take over.
    guard.Receive("ARM 9", measured, 10);
    assert(!guard.owned); // Expired activation cannot take over.
    assert(guard.Receive("ARM 10", measured, 10));
    assert(guard.tracking && guard.output == measured);
    guard.Receive("TARGET6 10.01 0.1 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0", measured, 10.01);
    guard.Tick(measured, 10.02, 0.005);
    assert(guard.output[0] > 0 && guard.output[0] <= 0.0015 + 1e-9);
    measured[0] = 0.0005;
    guard.Tick(measured, 10.5, 0.005);
    assert(!guard.tracking && guard.owned && guard.output == measured);
    guard.Receive("TARGET6 10.5 0.1 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0", measured, 10.5);
    assert(!guard.tracking && guard.target == measured); // Cannot auto-resume after watchdog.
    guard.Receive("ARM 10.6", measured, 10.6);
    guard.Receive("HOLD", measured, 10.61);
    assert(!guard.tracking && guard.output == measured);
    bool rejected = false;
    try { guard.Receive("TARGET6 10.7 99 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0", measured, 10.7); }
    catch (const std::runtime_error&) { rejected = true; }
    assert(rejected);
    guard.Receive("ARM 11", measured, 11);
    guard.Receive("TARGET6 11.01 0 0 0 0 0 0 0 0 0 0 0 0 0 0 2 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0", measured, 11.01);
    guard.Tick(measured, 11.02, 0.005);
    assert(guard.torque[0] > 0 && guard.torque[0] <= 0.100001);
    guard.Receive("HOLD", measured, 11.03);
    const double support = guard.torque[0];
    guard.Tick(measured, 11.04, 0.005);
    assert(guard.torque[0] == support);
    rejected = false;
    try { guard.Receive("TARGET6 11.05 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 6 0 0 0 0 0 0 0 0 0 0 0", measured, 11.05); }
    catch (const std::runtime_error&) { rejected = true; }
    assert(rejected); // Wrist torque must respect its 5 Nm model limit.
    guard.Receive("ARM 12", measured, 12);
    guard.Receive("TARGET6 12.01 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0.1 0 0", measured, 12.01);
    assert(guard.waist_rate == 0.1);
    guard.Tick(measured, 12.3, 0.005);
    assert(!guard.tracking && guard.waist_rate == 0);
    // Pitch shares the arm watchdog and cannot be activated by a mode switch.
    g1d::ArmGuard pitch;
    pitch.Receive("PITCH_HOLD",measured,20);
    assert(!pitch.owned);
    pitch.Receive("ARM 20",measured,20);
    std::string target="TARGET6 20.01";
    for (int i=0;i<28;++i) target+=" 0";
    pitch.Receive(target+" 0.1 0.08 1",measured,20.01);
    assert(pitch.pitch_enabled && pitch.pitch_rate==.08 && pitch.waist_rate==.1);
    pitch.Receive("PITCH_HOLD",measured,20.02);
    assert(pitch.tracking && !pitch.pitch_enabled && pitch.pitch_rate==0 && pitch.waist_rate==.1);
    assert(pitch.last_input==20.01);
    // Entire packet is validated before any new target/velocity is applied.
    for (const auto& tail : {" 0.1 0.08 0", " 0.1 0.2 1", " 0.1 0.08 2", " 0.1 nan 1", " 0.1 0.08 1 extra"}) {
        rejected=false;
        try { pitch.Receive(target+tail,measured,20.03); }
        catch (const std::runtime_error&) { rejected=true; }
        assert(rejected && !pitch.pitch_enabled && pitch.pitch_rate==0);
    }
    pitch.Receive(target+" 0.1 0.08 1",measured,20.04);
    pitch.Tick(measured,20.3,0.005);
    assert(!pitch.tracking && !pitch.pitch_enabled && pitch.pitch_rate==0 && pitch.waist_rate==0);
    pitch.Receive("ARM 20.4",measured,20.4);
    assert(!pitch.pitch_enabled && pitch.pitch_rate==0);
    measured[0] = std::numeric_limits<double>::quiet_NaN();
    assert(!g1d::Valid(measured));
    return 0;
}
