// No SDK, network, or robot. Exercise the same parser/guards as real bridges.
#include "../bridge/g1d_agv_command.hpp"
#include "../bridge/g1d_waist_guard.hpp"
#include <cassert>
#include <iostream>
#include <limits>

int main(int argc, char** argv) {
    if (g1d::limits::PrintIfRequested(argc, argv, std::cout)) return 0;
    if (argc == 5 && std::string(argv[1]) == "--command") {
        g1d::AgvCommand command;
        if (!g1d::ParseAgvCommand(std::string(argv[2])+" "+argv[3]+" "+argv[4], command)) return 2;
        std::cout << command.vx << ' ' << command.vyaw << ' ' << command.vz << '\n';
        return 0;
    }
    g1d::AgvCommand command;
    for (const char* value : {"1.5 0.6 1", "-1.5 -0.6 -1", "0 0 0", "0.5 -0.4 0.8"}) {
        assert(g1d::ParseAgvCommand(value, command));
    }
    const auto previous=command;
    for (const char* value : {"1.50001 0 0", "0 0.60001 0", "0 0 1.00001", "0 -0.60001 0",
                              "nan 0 0", "inf 0 0", "1 0", "0 0 0 extra"}) {
        assert(!g1d::ParseAgvCommand(value, command));
        assert(command.vx==previous.vx && command.vyaw==previous.vyaw && command.vz==previous.vz);
    }
    assert(g1d::limits::ValidHeightWindow(0,.42));
    assert(g1d::limits::ValidHeightWindow(.05,.47));
    assert(!g1d::limits::ValidHeightWindow(0,.421));
    assert(!g1d::limits::ValidHeightWindow(.1,.1));
    assert(!g1d::limits::ValidHeightWindow(0,std::numeric_limits<double>::infinity()));
    g1d::WaistGuard full(2.7053,.1,.2,-.04363,2.3562,.08,.16);
    // Newly valid angles above the previous 60-degree cap are actually sent
    // by trajectory generation; capture does not snap to a preset.
    g1d::Waist measured{1.2,1.3};
    full.Capture(measured);
    for (int i=0;i<100;++i) { measured=full.output; full.Tick(measured,.1,true,.01,.08,true); }
    assert(full.output[0]>1.2 && full.output[1]>1.3);
    g1d::WaistGuard small(.01,.1,.2,.10,.11,.08,.16); // Positive-only pitch window is legal.
    small.Capture({0,.105});
    for (int i=0;i<100;++i) small.Tick(small.output,.1,true,.01,.08,true);
    assert(small.output[0]<=.01 && small.output[1]<=.11);
    for (const auto ends : {g1d::Waist{-.04364,2.3562}, g1d::Waist{0,2.35621},
                           g1d::Waist{.1,.1}, g1d::Waist{.2,.1}}) {
        bool rejected=false;
        try { g1d::WaistGuard invalid(.5,.1,.2,ends[0],ends[1]); }
        catch (const std::runtime_error&) { rejected=true; }
        assert(rejected);
    }
    return 0;
}
