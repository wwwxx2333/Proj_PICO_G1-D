#include "../bridge/g1d_waist_guard.hpp"
#include "../bridge/g1d_lift_guard.hpp"
#include "../bridge/g1d_arm_guard.hpp"
#include <cassert>
#include <limits>

void TestYawTrajectory() {
    g1d::WaistGuard waist;
    g1d::Waist measured{0.2,0.3};
    waist.Tick(measured,0.1,true,0.01);
    assert(!waist.captured); // A target cannot take over.
    waist.Capture(measured);
    assert(waist.output==measured);
    for (int i=0;i<1000;++i) {
        measured=waist.output;
        const double before=waist.output[0], speed=waist.velocity;
        waist.Tick(measured,0.1,true,0.01);
        assert(std::abs(waist.output[0]-before)<=0.001+1e-12);
        assert(std::abs(waist.velocity)<=0.1+1e-12);
        if (!waist.limited) assert(std::abs(waist.velocity-speed)<=0.002+1e-12);
        assert(waist.output[1]==0.3);
        assert(waist.output[0]<=waist.limit_rad);
    }
    assert(std::abs(waist.output[0]-waist.limit_rad)<1e-12);
    // Inward operation works immediately after the upper software limit.
    waist.Tick(measured,-0.1,true,0.01);
    assert(waist.output[0]<waist.limit_rad);
    for (int i=0;i<100;++i) { measured=waist.output; waist.Tick(measured,-0.1,true,0.01); }
    for (int i=0;i<60;++i) { measured=waist.output; waist.Tick(measured,0,true,0.01); }
    assert(waist.velocity==0);
    const auto held=waist.output;
    waist.Tick(measured,0,true,0.01);
    assert(waist.output==held);
    waist.Tick(measured,-0.1,true,0.01);
    const auto before_stop=waist.output;
    waist.Tick(measured,0,false,0.01);
    assert(waist.output==before_stop && waist.velocity==0);
}

void TestOutsideWindowAndFollowingLead() {
    for (double sign : {-1.,1.}) {
        g1d::WaistGuard waist;
        g1d::Waist measured{sign*0.8,0};
        waist.Capture(measured);
        waist.Tick(measured,sign*0.1,true,0.01);
        assert(waist.output==measured); // No snap to +/-30 degrees, no outward move.
        waist.Tick(measured,-sign*0.1,true,0.01);
        assert(sign*waist.output[0]<sign*measured[0]);
        measured={0,0};
        waist.Capture(measured);
        for (int i=0;i<1000;++i) waist.Tick(measured,sign*0.1,true,0.01);
        assert(std::abs(waist.output[0])<=0.1+1e-12); // No unbounded target windup.
    }
    assert(!g1d::ValidWaist({3.,0}));
    assert(!g1d::ValidWaist({0,std::numeric_limits<double>::quiet_NaN()}));
    bool rejected=false;
    try { g1d::WaistGuard invalid(2.8); } catch (const std::runtime_error&) { rejected=true; }
    assert(rejected);
}

void TestWatchdogStopsYaw() {
    g1d::ArmGuard arms;
    g1d::WaistGuard waist;
    g1d::Joints q{};
    g1d::Waist measured{};
    arms.Receive("ARM 100",q,100);
    waist.Capture(measured);
    std::string command="TARGET6 100.01";
    for (int i=0;i<28;++i) command+=" 0";
    command+=" 0.1 0 0";
    arms.Receive(command,q,100.01);
    waist.Tick(measured,arms.waist_rate,arms.tracking,0.01);
    assert(waist.velocity>0);
    arms.Tick(q,100.3,0.01);
    waist.Tick(measured,arms.waist_rate,arms.tracking,0.01);
    assert(waist.velocity==0 && !arms.tracking);
    arms.Receive(command,q,100.31);
    assert(!arms.tracking); // Old input cannot resume.
}

void TestLiftWindow() {
    using g1d::FilterLift;
    using g1d::LiftStatus;
    assert(FilterLift(.15,.05,true,.02,.08).speed==.15f);
    assert(FilterLift(-.15,.05,true,.02,.08).speed==-.15f);
    assert(FilterLift(.15,.075,true,.02,.08).status==LiftStatus::UpperLimit);
    assert(FilterLift(-.15,.075,true,.02,.08).speed==-.15f);
    assert(FilterLift(-.15,.025,true,.02,.08).status==LiftStatus::LowerLimit);
    assert(FilterLift(.15,.025,true,.02,.08).speed==.15f);
    assert(FilterLift(.15,.05,false,.02,.08).status==LiftStatus::NoFeedback);
    assert(FilterLift(.15,.4,true,.02,.08).status==LiftStatus::OutsideWindow);
    assert(FilterLift(.15,std::numeric_limits<float>::quiet_NaN(),true,.02,.08).speed==0);
    assert(FilterLift(0,.05,true,.02,.08).speed==0);
}

void TestConfirmedColumnTravel() {
    using g1d::FilterLift;
    using g1d::LiftStatus;
    // Keep 10 mm away from each end in normal motion, but do not strand a
    // column that starts fully retracted or extended inside the end margin.
    for (float height : {0.f, .005f, .01f}) {
        assert(FilterLift(.15f, height, true, 0.f, .42f).speed == .15f);
        assert(FilterLift(-.15f, height, true, 0.f, .42f).status == LiftStatus::LowerLimit);
    }
    for (float height : {.415f, .42f}) {
        assert(FilterLift(-.15f, height, true, 0.f, .42f).speed == -.15f);
        assert(FilterLift(.15f, height, true, 0.f, .42f).status == LiftStatus::UpperLimit);
    }
    assert(FilterLift(.15f, .40f, true, 0.f, .42f).speed == .15f);
    assert(FilterLift(-.15f, .02f, true, 0.f, .42f).speed == -.15f);
    for (float height : {-.001f, .421f}) {
        for (float speed : {-.15f, 0.f, .15f}) {
            assert(FilterLift(speed, height, true, 0.f, .42f).status == LiftStatus::OutsideWindow);
            assert(FilterLift(speed, height, true, 0.f, .42f).speed == 0);
        }
    }
    assert(FilterLift(.15f, .2f, false, 0.f, .42f).speed == 0);
}

void TestPitchTrajectoryAndModeSwitch() {
    g1d::WaistGuard waist;
    g1d::Waist measured{0,0};
    waist.Capture(measured);
    for (int i=0;i<1000;++i) {
        measured=waist.output;
        const double angle=waist.output[1], speed=waist.pitch_velocity;
        waist.Tick(measured,0.1,true,0.01,0.08,true);
        assert(waist.output[1]>=angle && waist.output[1]<=waist.pitch_max_rad);
        assert(waist.output[1]-angle<=0.0008+1e-12);
        assert(std::abs(waist.pitch_velocity)<=0.08+1e-12);
        if (!waist.pitch_limited) assert(std::abs(waist.pitch_velocity-speed)<=0.0016+1e-12);
    }
    assert(std::abs(waist.output[1]-waist.pitch_max_rad)<1e-12);
    waist.Tick(measured,0,true,0.01,-0.08,true);
    assert(waist.output[1]<waist.pitch_max_rad);
    const auto held=waist.output[1];
    waist.Tick(waist.output,0.1,true,0.01,0,false); // X switches to lift, no deceleration drift.
    assert(waist.output[1]==held && waist.pitch_velocity==0);
    for (int i=0;i<1000;++i) {
        measured=waist.output;
        waist.Tick(measured,0,true,0.01,-0.08,true);
        assert(waist.output[1]>=0);
    }
    assert(waist.output[1]==0); // Back stick returns to upright, no hyperextension.
    for (int i=0;i<100;++i) waist.Tick(waist.output,0,true,0.01,0.08,true);
    for (int i=0;i<60;++i) waist.Tick(waist.output,0,true,0.01,0,true);
    assert(waist.pitch_velocity==0); // Normal release decelerates to hold.
    waist.Tick(waist.output,0.1,true,0.01,0.08,true);
    const auto before=waist.output;
    waist.Tick(measured,0,false,0.01,0,false);
    assert(waist.output==before && waist.velocity==0 && waist.pitch_velocity==0);
}

void TestPitchLimitsAndFollowingLead() {
    for (double start : {-0.03,0.8}) {
        g1d::WaistGuard waist;
        g1d::Waist measured{0,start};
        const double outward=start<0 ? -0.08 : 0.08;
        waist.Capture(measured);
        waist.Tick(measured,0,true,0.01,outward,true);
        assert(waist.output==measured); // No snap to zero/boundary, no outward command.
        waist.Tick(measured,0,true,0.01,-outward,true);
        assert((waist.output[1]-start)*outward<0); // Inward recovery is permitted.
    }
    g1d::WaistGuard waist;
    const g1d::Waist measured{0,0.2};
    waist.Capture(measured);
    bool reached_cap=false;
    for (int i=0;i<1000;++i) {
        waist.Tick(measured,0,true,0.01,0.08,true);
        assert(waist.output[1]<=0.30+1e-12);
        reached_cap|=(waist.pitch_limit & g1d::PitchFollowError)!=0;
    }
    assert(reached_cap && waist.pitch_following==g1d::PitchFollowing::WaitCenter);
    assert(waist.output[1]==measured[1]); // Stalled target is cancelled after timeout.
    for (double minimum : {-0.05, 0.5, std::numeric_limits<double>::quiet_NaN()}) {
        bool rejected=false;
        try { g1d::WaistGuard invalid(.5,.1,.2,minimum,.5,.08,.16); }
        catch (const std::runtime_error&) { rejected=true; }
        assert(rejected);
    }
    bool rejected=false;
    try { waist.Tick(measured,0,true,.01,.08,false); }
    catch (const std::runtime_error&) { rejected=true; }
    assert(rejected);
}

int main() {
    TestYawTrajectory();
    TestOutsideWindowAndFollowingLead();
    TestWatchdogStopsYaw();
    TestLiftWindow();
    TestConfirmedColumnTravel();
    TestPitchTrajectoryAndModeSwitch();
    TestPitchLimitsAndFollowingLead();
}
