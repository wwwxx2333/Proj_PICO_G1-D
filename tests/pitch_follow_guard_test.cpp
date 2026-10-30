// Pure trajectory/state-machine tests. No DDS, sockets or robot operations.
#include "../bridge/g1d_waist_guard.hpp"
#include <cassert>
#include <limits>

using g1d::PitchFollowing;

struct Rig {
    g1d::WaistGuard guard;
    g1d::Waist measured{0,0.4};
    double now=100;
    Rig() { guard.Capture(measured); }
    void tick(double request, bool enabled=true, double yaw=0, double dt=.005) {
        now+=dt;
        guard.Tick(measured,yaw,true,dt,request,enabled,now);
    }
    void stall(double direction=-1) {
        for(int i=0;i<1500 && guard.pitch_following==PitchFollowing::Following;++i)
            tick(direction*.08);
        assert(guard.pitch_following==PitchFollowing::WaitCenter);
    }
};

void TestLoggedAnglesCanPassOldCapThenCancel() {
    for(double degrees : {16.5,17.1,22.4,30.8}) {
        Rig r;
        r.measured[1]=degrees*3.141592653589793/180;
        r.guard.Capture(r.measured);
        bool passed_old_cap=false, reached_new_cap=false;
        for(int i=0;i<1500 && r.guard.pitch_following==PitchFollowing::Following;++i) {
            r.tick(-.08);
            const double lead=r.measured[1]-r.guard.output[1];
            assert(lead<=.1+1e-12);
            passed_old_cap|=lead>.05+1e-4;
            reached_new_cap|=(r.guard.pitch_limit & g1d::PitchFollowError)!=0;
        }
        assert(passed_old_cap && reached_new_cap);
        assert(r.guard.pitch_following==PitchFollowing::WaitCenter);
        assert(std::abs(r.guard.pitch_stall_error_rad+.1)<1e-10);
        assert(r.guard.pitch_stall_elapsed_s>=1.5-1e-9);
        assert(r.guard.output[1]==r.measured[1] && r.guard.pitch_velocity==0);
        const double held=r.guard.output[1];
        for(int i=0;i<1000;++i) r.tick(-.08);
        assert(r.guard.output[1]==held); // A held stick cannot cause retries.
        assert(r.guard.pitch_following==PitchFollowing::WaitCenter);
    }
}

void TestRecoveryRequiresFreshCenterThenNewPush() {
    Rig r;
    r.stall();
    const double failed_stamp=r.now, held=r.guard.output[1];
    r.guard.Tick(r.measured,0,true,.005,0,true,failed_stamp);
    assert(r.guard.pitch_following==PitchFollowing::WaitCenter);
    for(int i=0;i<10;++i) r.tick(0,false); // X/LIFT or other disabled zeros.
    for(int i=0;i<10;++i) r.tick(0,true,.1); // Y centred, X not centred.
    assert(r.guard.pitch_following==PitchFollowing::WaitCenter);
    r.tick(0);
    assert(r.guard.pitch_following==PitchFollowing::WaitPush);
    r.guard.Tick(r.measured,0,true,.005,-.08,true,r.now); // Not a newer sample.
    assert(r.guard.pitch_following==PitchFollowing::WaitPush);
    r.measured[1]+=.01;
    r.tick(0);
    assert(r.guard.output[1]==held); // A fixed hold, not continuous recapture/drift.
    r.tick(-.08);
    assert(r.guard.pitch_following==PitchFollowing::Following);
    assert(r.guard.output[1]<r.measured[1]);
    assert(r.measured[1]-r.guard.output[1]<.001);
    assert(r.guard.pitch_stall_elapsed_s==0);
    // Mode toggles alone cannot clear a latch; A/takeover does not clear it either.
    r.stall();
    r.guard.Stop();
    r.guard.Capture(r.measured);
    r.tick(-.08);
    assert(r.guard.pitch_following==PitchFollowing::WaitCenter);
}

void TestProgressCountsDirectionNotMessagesOrNoise() {
    for(double direction : {-1.,1.}) {
        Rig r;
        r.measured[1]=.25; r.guard.Capture(r.measured);
        bool reached_limit=false;
        for(int i=0;i<500;++i) {
            r.measured[1]+=direction*.01*.005;
            r.tick(direction*.08);
            reached_limit|=(r.guard.pitch_limit & g1d::PitchFollowError)!=0;
            assert(r.guard.pitch_following==PitchFollowing::Following);
        }
        assert(reached_limit);
    }
    Rig noisy;
    for(int i=0;i<1500 && noisy.guard.pitch_following==PitchFollowing::Following;++i) {
        noisy.measured[1]=.4+(i%2 ? .0002 : -.0002);
        noisy.tick(-.08);
    }
    assert(noisy.guard.pitch_following==PitchFollowing::WaitCenter);
    Rig wrong_way;
    for(int i=0;i<1500 && wrong_way.guard.pitch_following==PitchFollowing::Following;++i) {
        wrong_way.measured[1]+=.00001;
        wrong_way.tick(-.08);
    }
    assert(wrong_way.guard.pitch_following==PitchFollowing::WaitCenter);
}

void TestLocalFaultLeavesYawMoving() {
    Rig r; r.stall();
    const double pitch=r.guard.output[1], yaw=r.guard.output[0];
    for(int i=0;i<50;++i) {
        r.measured[0]=r.guard.output[0];
        r.tick(-.08,true,.1);
    }
    assert(r.guard.output[0]>yaw && r.guard.output[1]==pitch);
    assert(r.guard.pitch_following==PitchFollowing::WaitCenter);
}

void TestNormalReturnAndAngleLimitsAreNotStalls() {
    Rig r;
    for(int i=0;i<2000;++i) {
        r.measured=r.guard.output;
        r.tick(-.08);
        assert(r.guard.pitch_following==PitchFollowing::Following);
        assert(r.guard.output[1]>=0);
    }
    assert(r.guard.output[1]==0 && r.guard.pitch_limit==g1d::PitchAngleMin);
    for(int i=0;i<2000;++i) {
        r.measured=r.guard.output;
        r.tick(.08);
        assert(r.guard.pitch_following==PitchFollowing::Following);
    }
    assert(r.guard.output[1]==r.guard.pitch_max_rad);
    assert(r.guard.pitch_limit==g1d::PitchAngleMax);
    r.measured=r.guard.output; r.tick(-.08);
    assert(r.guard.output[1]<r.guard.pitch_max_rad);
    // At the absolute boundary a larger tracking error cannot cause false stalls.
    Rig bound; bound.measured[1]=.05; bound.guard.Capture(bound.measured);
    for(int i=0;i<1500;++i) bound.tick(-.08);
    assert(bound.guard.pitch_limit==g1d::PitchAngleMin);
    assert(bound.guard.pitch_following==PitchFollowing::Following);
}

void TestReleaseReverseModeAndTimingResetWindow() {
    Rig r;
    while(r.guard.pitch_stall_elapsed_s<1.0) r.tick(-.08);
    r.tick(0);
    assert(r.guard.pitch_stall_elapsed_s==0);
    r.tick(-.08);
    assert(r.guard.pitch_stall_elapsed_s==0);
    for(int i=0;i<100;++i) r.tick(-.08);
    r.tick(.08);
    assert(r.guard.pitch_stall_elapsed_s==0);
    while(r.guard.pitch_stall_elapsed_s<1.0) r.tick(-.08);
    r.tick(0,false);
    assert(r.guard.pitch_stall_elapsed_s==0);
    r.tick(-.08);
    r.tick(-.08,true,0,.3); // Gap cannot be counted as evidence of no motion.
    assert(r.guard.pitch_stall_elapsed_s==0);
    // Progress timeout uses elapsed time, even if trajectory dt is capped at .02.
    while(!(r.guard.pitch_limit & g1d::PitchFollowError)) r.tick(-.08);
    r.tick(-.08);
    const double before=r.guard.pitch_stall_elapsed_s;
    r.tick(-.08,true,0,.05);
    assert(std::abs(r.guard.pitch_stall_elapsed_s-before-.05)<1e-9);
}

void TestNewParametersAndBothErrorDirections() {
    for(int field=0;field<3;++field) for(double bad : {0.,100.,std::numeric_limits<double>::quiet_NaN()}) {
        double args[]={.1,1.5,.005}; args[field]=bad;
        bool rejected=false;
        try { g1d::WaistGuard guard(.5235987756,.1,.2,0,.5235987756,.08,.16,args[0],args[1],args[2]); }
        catch(const std::runtime_error&) { rejected=true; }
        assert(rejected);
    }
    for(double sign : {-1.,1.}) {
        Rig r; r.measured[1]=.25; r.guard.Capture(r.measured); r.stall(sign);
        assert(std::abs(r.guard.pitch_stall_error_rad-sign*.1)<1e-10);
    }
}

int main() {
    TestLoggedAnglesCanPassOldCapThenCancel();
    TestRecoveryRequiresFreshCenterThenNewPush();
    TestProgressCountsDirectionNotMessagesOrNoise();
    TestLocalFaultLeavesYawMoving();
    TestNormalReturnAndAngleLimitsAreNotStalls();
    TestReleaseReverseModeAndTimingResetWindow();
    TestNewParametersAndBothErrorDirections();
}
