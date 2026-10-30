#include "../bridge/g1d_hand_guard.hpp"
#include <cassert>
#include <cmath>
#include <limits>

namespace {
std::string TargetLine(const dex3::Joints& q) {
    // Match HandBridge.target's decimal serialization over the actual pipe.
    std::ostringstream line;
    line << "TARGET 100.01" << std::fixed << std::setprecision(9);
    for (double joint : q) line << ' ' << joint;
    return line.str();
}

bool TargetRejected(dex3::Guard& guard, const dex3::Joints& requested,
                    const dex3::Joints& measured) {
    try {
        guard.Receive(TargetLine(requested),measured,100.02);
    } catch (const std::runtime_error&) {
        return true;
    }
    return false;
}

void TestNewestFeedback() {
    const dex3::Joints q{};
    dex3::Feedback fast{100.,q,dex3::FeedbackSource::Fast};
    dex3::Feedback low{100.24,q,dex3::FeedbackSource::LowFrequency};
    const auto selected=dex3::SelectFeedback(fast,low,100.249);
    assert(selected.source==dex3::FeedbackSource::LowFrequency);
    // A short IPC delay must not expire an older fast sample when newer LF exists.
    assert(dex3::Fresh(selected.time,100.26));
    fast.time=100.245;
    assert(dex3::SelectFeedback(fast,low,100.249).source==dex3::FeedbackSource::Fast);
    low.time=fast.time;
    assert(dex3::SelectFeedback(fast,low,100.249).source==dex3::FeedbackSource::Fast);
    low.time=101.;  // A future timestamp cannot displace a valid sample.
    assert(dex3::SelectFeedback(fast,low,100.249).source==dex3::FeedbackSource::Fast);
    fast.time=100.;
    low.time=100.1;
    assert(!dex3::Fresh(dex3::SelectFeedback(fast,low,100.351).time,100.351));
    assert(dex3::Fresh(100.,100.25));
    assert(!dex3::Fresh(100.,100.2501));
}

void TestTakeoverAtMeasurementMargin() {
    for (bool left : {true,false}) {
        const auto& lower=left ? dex3::left_lower : dex3::right_lower;
        const auto& upper=left ? dex3::left_upper : dex3::right_upper;
        for (size_t joint=0;joint<7;++joint) {
            for (double direction : {-1.,1.}) {
                const double boundary=direction<0 ? lower[joint] : upper[joint];
                dex3::Joints measured{};
                // Use the same float precision as a DDS motor q field.
                measured[joint]=static_cast<float>(boundary+direction*0.01);
                dex3::Guard guard(left);
                assert(dex3::FeedbackFault(measured,left,100.,100.).empty());
                assert(guard.Receive("ARM 100",measured,100.));
                assert(guard.output==measured);
                auto later_feedback=measured;
                later_feedback[joint]-=direction*0.002;
                guard.Receive("KEEPALIVE 100.01",later_feedback,100.02);
                guard.Tick(later_feedback,100.02,0.01);
                // Python/C++ may see different poses during takeover. No older
                // Python snapshot should overwrite the pose captured by ARM.
                assert(guard.output==measured && guard.target==measured);
                assert(TargetRejected(guard,measured,later_feedback));

                auto legal=measured;
                legal[joint]=boundary;
                assert(!TargetRejected(guard,legal,later_feedback));
                assert(dex3::Valid(guard.target,left));
                const double before=guard.output[joint];
                guard.Tick(later_feedback,100.03,0.01);
                assert(std::abs(guard.output[joint]-before)<=0.008+1e-12);
                assert(TargetRejected(guard,measured,later_feedback));

                // Measurement tolerance must not become general command tolerance.
                dex3::Guard normal(left);
                dex3::Joints zero{};
                assert(TargetRejected(normal,measured,zero));
                assert(normal.Receive("ARM 100",zero,100.));
                assert(TargetRejected(normal,measured,zero));

                measured[joint]=boundary+direction*0.051;
                assert(!dex3::FeedbackFault(measured,left,100.,100.).empty());
                dex3::Guard unsafe(left);
                bool rejected=false;
                try { unsafe.Receive("ARM 100",measured,100.); }
                catch (const std::runtime_error&) { rejected=true; }
                assert(rejected && !unsafe.owned);
            }
        }
    }
}

void TestKeepaliveOwnershipAndWatchdog() {
    dex3::Joints q{};
    dex3::Guard guard(true);
    guard.Receive("KEEPALIVE 100",q,100.);
    assert(!guard.owned && !guard.tracking);
    assert(guard.Receive("ARM 100",q,100.));
    // Thirty seconds of released triggers must neither move nor time out as
    // long as fresh PICO input continues to arrive.
    for (int tick=1;tick<=3000;++tick) {
        const double now=100.+tick*0.01;
        std::ostringstream line;
        line << std::fixed << std::setprecision(9) << "KEEPALIVE " << now;
        // Receipt follows the serialized input timestamp (1 ms pipe delay).
        guard.Receive(line.str(),q,now+0.001);
        guard.Tick(q,now+0.001,0.01);
        assert(guard.tracking && guard.output==q);
    }
    guard.Tick(q,130.251,0.01);
    assert(guard.owned && !guard.tracking);
    guard.Receive("KEEPALIVE 130.26",q,130.26);
    assert(!guard.tracking);  // A heartbeat cannot restart after a timeout.
    guard.Receive("ARM 130.27",q,130.27);
    assert(guard.tracking);
    guard.Receive("HOLD",q,130.28);
    guard.Receive("KEEPALIVE 130.29",q,130.29);
    assert(!guard.tracking);
    bool rejected=false;
    try { guard.Receive("KEEPALIVE 130.30 extra",q,130.30); }
    catch (const std::runtime_error&) { rejected=true; }
    assert(rejected);
}

void TestFeedbackGapDoesNotReleaseOwnership() {
    dex3::Joints q{};
    dex3::Guard guard(true);
    assert(guard.Receive("ARM 100",q,100.));
    assert(guard.owned && guard.tracking);
    assert(!dex3::Fresh(100.,100.251));
    assert(guard.owned && guard.tracking);
    guard.Receive("KEEPALIVE 100.26",q,100.26);
    guard.Receive("TARGET 100.26 0 0.63 1.05 -0.94 -1.05 -0.94 -1.05",q,100.26);
    assert(guard.owned);
    guard.Freeze();
    assert(guard.target==guard.output);
    guard.Tick(q,100.26,0.01);
    assert(guard.owned);
    assert(!guard.Receive("ARM 100.27",q,100.27));
    assert(guard.owned && guard.tracking);
    guard.Receive("RELEASE",q,100.3);
    assert(!guard.owned && !guard.tracking);
    bool rejected=false;
    try { guard.Receive("RELEASE extra",q,100.3); }
    catch (const std::runtime_error&) { rejected=true; }
    assert(rejected);
    assert(!dex3::CanTakeover(100.,100.01,true));
    assert(!dex3::CanTakeover(100.02,100.01,false));
    assert(dex3::CanTakeover(100.02,100.01,true));
}

std::string Request(double now, const dex3::Joints& q) {
    std::ostringstream line;
    line << "TARGET " << std::fixed << std::setprecision(9) << now;
    for (double value:q) line << ' ' << value;
    return line.str();
}

dex3::Joints Closed(bool left) {
    const double sign=left?1.:-1.;
    return {0,sign*0.63,sign*1.05,-sign*0.94,-sign*1.05,-sign*0.94,-sign*1.05};
}

void Step(dex3::Guard& guard, const dex3::Joints& q, const dex3::Joints& request,
          double now) {
    const auto before=guard.output;
    guard.Receive(Request(now,request),q,now+0.001);
    guard.Tick(q,now+0.001,0.01,now);
    for (size_t i=0;i<7;++i)
        assert(std::abs(guard.output[i]-before[i])<=0.006+1e-12);
    assert(guard.owned && guard.tracking);
}

void TestGraspThenPartialOpening(bool left) {
    dex3::Guard guard(left);
    const double sign=left?1.:-1.;
    const auto closed=Closed(left);
    dex3::Joints q{};
    q[1]=sign*0.2;
    guard.Receive("ARM 100",q,100.);
    for (int i=1;i<=300;++i) {
        // One joint is blocked by an object; all other joints can move.
        for (size_t j=2;j<7;++j) q[j]=guard.output[j];
        Step(guard,q,closed,100+i*0.01);
        assert(sign*(guard.output[1]-q[1])<=dex3::GraspSettings::max_lead+1e-9);
    }
    assert(guard.grasp.Mask(dex3::GraspMode::Blocked)==(1u<<1));
    const double held=guard.output[1];
    assert(sign*(held-q[1])>0.05); // A finite position preload remains.
    assert(sign*guard.output[2]>0.9); // Other joints were not frozen.
    // Slight measured compression must not ratchet the held reference closed.
    q[1]+=sign*0.02;
    for (int i=301;i<=400;++i) Step(guard,q,closed,100+i*0.01);
    assert(std::abs(guard.output[1]-held)<1e-9);
    auto partial=closed;
    for (auto& v:partial) v*=0.9;
    assert(sign*partial[1]>sign*held); // Absolute trigger is still beyond hold.
    for (int i=401;i<=420;++i) Step(guard,q,partial,100+i*0.01);
    assert(guard.grasp.modes[1]==dex3::GraspMode::Opening);
    assert(sign*guard.output[1]<sign*held-0.04); // Opens without full release/A.
    // The operator can close again after only a partial release.
    Step(guard,q,closed,104.21);
    assert(guard.grasp.modes[1]==dex3::GraspMode::Following);
    for (int i=422;i<=500;++i) Step(guard,q,closed,100+i*0.01);
    assert(guard.grasp.modes[1]==dex3::GraspMode::Blocked);
    // Full release reaches the true open preset, with no stale closure replay.
    dex3::Joints open{};
    for (int i=501;i<=750;++i) {
        q=guard.output; // Simulated fingers are free to open.
        Step(guard,q,open,100+i*0.01);
    }
    for (double value:guard.output) assert(std::abs(value)<1e-9);
    assert(guard.grasp.Mask(dex3::GraspMode::Blocked)==0);
}

void TestMovingLagDoesNotLatch() {
    dex3::GraspLimiter limiter(true);
    auto requested=Closed(true);
    dex3::Joints q{},sent{};
    for (int i=0;i<100;++i) {
        q[1]=0.001*i; // Moving at 0.1 rad/s, despite a 0.15 rad lag.
        sent[1]=q[1]+0.15;
        limiter.Goal(requested,sent,q,100+i*0.01);
        assert(limiter.Mask(dex3::GraspMode::Blocked)==0);
    }
}

void TestNoRepeatedTimestampContactAndFeedbackGap() {
    dex3::Guard guard(true);
    dex3::Joints q{};
    guard.Receive("ARM 100",q,100.);
    const auto requested=Closed(true);
    for (int i=1;i<=12;++i) Step(guard,q,requested,100+i*0.01);
    const double t=100.12;
    // Duplicate samples are not evidence of stationary fingers.
    for (int i=1;i<=17;++i) {
        const double now=t+i*0.01;
        guard.Receive(Request(now,requested),q,now+0.001);
        guard.Tick(q,now+0.001,0.01,t);
        assert(guard.grasp.Mask(dex3::GraspMode::Blocked)==0);
    }
    const auto before=guard.output;
    guard.Receive("KEEPALIVE 100.31",q,100.31);
    guard.Tick(q,100.31,0.01,t);
    assert(guard.output==before && guard.tracking && !guard.target_active);
    // A new state alone must not resume the old close request.
    guard.Tick(q,100.32,0.01,100.32);
    assert(guard.output==before);
}

void TestShortObstructionDoesNotLatch() {
    dex3::GraspLimiter limiter(true);
    dex3::Joints q{},sent{};
    sent[1]=0.15;
    for (int i=0;i<10;++i) limiter.Goal(Closed(true),sent,q,100+i*0.01);
    assert(limiter.Mask(dex3::GraspMode::Blocked)==0);
    q[1]=0.12;
    limiter.Goal(Closed(true),sent,q,100.1);
    for (int i=11;i<20;++i) limiter.Goal(Closed(true),sent,q,100+i*0.01);
    assert(limiter.Mask(dex3::GraspMode::Blocked)==0);
}

void TestFreeMotionWithLowFrequencyFeedback() {
    for (bool left : {true,false}) {
        dex3::Guard guard(left);
        dex3::Joints q{};
        guard.Receive("ARM 100",q,100.);
        const auto requested=Closed(left);
        double feedback_time=100.;
        for (int i=1;i<=400;++i) {
            const double now=100.+i*0.01;
            if (i%10==0) { q=guard.output; feedback_time=now; }
            guard.Receive(Request(now,requested),q,now+0.001);
            guard.Tick(q,now+0.001,0.01,feedback_time);
            assert(guard.tracking);
            assert(guard.grasp.Mask(dex3::GraspMode::Blocked)==0);
            assert(dex3::Valid(guard.output,left));
        }
        for (size_t i=0;i<7;++i) assert(std::abs(guard.output[i]-requested[i])<1e-9);
    }
}

void TestExternalDisplacementReturnsAtBoundedSpeed() {
    dex3::Guard guard(true);
    dex3::Joints q{};
    q[1]=0.5;
    guard.Receive("ARM 100",q,100.);
    q[1]=0;
    Step(guard,q,Closed(true),100.01);
    assert(guard.target[1]<=dex3::GraspSettings::max_lead);
    assert(guard.output[1]>=0.49); // Never jump by 0.3 rad to enforce the envelope.
}

}

int main() {
    TestGraspThenPartialOpening(true);
    TestGraspThenPartialOpening(false);
    TestMovingLagDoesNotLatch();
    TestFreeMotionWithLowFrequencyFeedback();
    TestNoRepeatedTimestampContactAndFeedbackGap();
    TestShortObstructionDoesNotLatch();
    TestExternalDisplacementReturnsAtBoundedSpeed();
    TestNewestFeedback();
    TestTakeoverAtMeasurementMargin();
    TestKeepaliveOwnershipAndWatchdog();
    TestFeedbackGapDoesNotReleaseOwnership();
    dex3::Joints q{};
    dex3::Guard left(true),right(false);
    left.Tick(q,10,0.005);
    assert(!left.owned);
    left.Receive("TARGET 10 0 0 0 0 0 0 0",q,10);
    assert(!left.owned); // Never take over on a target.
    left.Receive("ARM 9",q,10);
    assert(!left.owned); // Stale input cannot take over.
    assert(left.Receive("ARM 10",q,10));
    assert(left.owned && left.tracking && left.output==q);
    left.Receive("TARGET 10.01 0 0.63 1.05 -0.94 -1.05 -0.94 -1.05",q,10.01);
    left.Tick(q,10.02,0.005);
    assert(std::abs(left.output[1]-0.003)<1e-9);
    assert(std::abs(left.output[3]+0.003)<1e-9);
    assert(right.Receive("ARM 10",q,10));
    right.Receive("TARGET 10.01 0 -0.63 -1.05 0.94 1.05 0.94 1.05",q,10.01);
    right.Tick(q,10.02,0.005);
    assert(right.output[1]<0 && right.output[3]>0);
    q[1]=0.002;
    left.Tick(q,10.5,0.005);
    assert(left.owned && !left.tracking && left.output!=q);
    left.Receive("TARGET 10.51 0 0.63 1.05 -0.94 -1.05 -0.94 -1.05",q,10.51);
    assert(!left.tracking);
    left.Receive("ARM 10.52",q,10.52);
    left.Receive("HOLD",q,10.53);
    assert(!left.tracking);
    bool rejected=false;
    try { left.Receive("TARGET 10.54 0 0 9 0 0 0 0",q,10.54); }
    catch (const std::runtime_error&) { rejected=true; }
    assert(rejected);
    q[0]=std::numeric_limits<double>::quiet_NaN();
    assert(!dex3::Valid(q,true));
    assert(dex3::FeedbackFault(q,true,10,10).find("motor[0]")!=std::string::npos);
    q.fill(0);
    assert(dex3::FeedbackFault(q,true,10,10).empty());
    assert(dex3::FeedbackFault(q,true,10,10.3).empty());
    assert(dex3::FeedbackFault(q,true,0,10).find("unavailable")!=std::string::npos);
    q[3]=-1.7;
    assert(dex3::FeedbackFault(q,true,10,10).find("motor[3]")!=std::string::npos);
    dex3::Feedback fast{10,q,dex3::FeedbackSource::Fast};
    dex3::Feedback low{10.2,q,dex3::FeedbackSource::LowFrequency};
    assert(dex3::SelectFeedback(fast,low,10.21).source==dex3::FeedbackSource::LowFrequency);
    assert(dex3::SelectFeedback(fast,low,10.3).source==dex3::FeedbackSource::LowFrequency);
    assert(dex3::SelectFeedback(fast,low,10.5).source==dex3::FeedbackSource::LowFrequency);
    assert(!dex3::Fresh(dex3::SelectFeedback(fast,low,10.5).time,10.5));
    dex3::Feedback missing;
    assert(dex3::SelectFeedback(missing,missing,10).source==dex3::FeedbackSource::None);
}
