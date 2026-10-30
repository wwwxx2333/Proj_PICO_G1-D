// Independent Dex3-1 DDS process, one instance per side; no publish before ARM.
// Topics, 7 motors, mode bits and gains follow SDK2 g1_dex3_example.cpp.
#include "g1d_hand_guard.hpp"
#include "g1d_dds_channel.hpp"
#include <atomic>
#include <cerrno>
#include <csignal>
#include <fcntl.h>
#include <iomanip>
#include <iostream>
#include <mutex>
#include <poll.h>
#include <sstream>
#include <thread>
#include <time.h>
#include <unistd.h>
#include <unitree/robot/channel/channel_publisher.hpp>
#include <unitree/robot/channel/channel_subscriber.hpp>
#include <unitree/idl/hg/HandCmd_.hpp>
#include <unitree/idl/hg/HandState_.hpp>

using namespace unitree::robot;
using namespace unitree_hg::msg::dds_;
namespace {
volatile std::sig_atomic_t running=1;
void Stop(int) { running=0; }
double Now() {
    timespec t{};
    clock_gettime(CLOCK_MONOTONIC,&t);
    return t.tv_sec+t.tv_nsec*1e-9;
}
std::mutex state_mutex;
dex3::Feedback latest_fast,latest_low_frequency;
// Protected by state_mutex along with the corresponding feedback timestamps.
unsigned long fast_received=0,low_frequency_received=0;
constexpr auto command_period=std::chrono::milliseconds(10); // XR Dex3 default: 100 Hz
constexpr double feedback_freeze_delay=dex3::GraspSettings::feedback_gap;
std::atomic<unsigned long> wrong_motor_count{0};
std::atomic<unsigned long> nonfinite_joint{0};
void OnState(const void* raw,dex3::FeedbackSource source) {
    const auto& state=*static_cast<const HandState_*>(raw);
    if (state.motor_state().size() != 7) { ++wrong_motor_count; return; }
    dex3::Feedback next;
    next.time=Now();
    next.source=source;
    for (size_t i=0;i<7;++i) {
        next.q[i]=state.motor_state()[i].q();
        if (!std::isfinite(next.q[i])) { ++nonfinite_joint; return; }
    }
    std::lock_guard<std::mutex> lock(state_mutex);
    if (source==dex3::FeedbackSource::Fast) {
        latest_fast=next;
        ++fast_received;
    } else {
        latest_low_frequency=next;
        ++low_frequency_received;
    }
}
void OnFastState(const void* raw) { OnState(raw,dex3::FeedbackSource::Fast); }
void OnLowFrequencyState(const void* raw) { OnState(raw,dex3::FeedbackSource::LowFrequency); }
struct FeedbackPair {
    dex3::Feedback fast,low_frequency;
    unsigned long fast_count,low_frequency_count;
};
FeedbackPair ReadStates() {
    std::lock_guard<std::mutex> lock(state_mutex);
    return {latest_fast,latest_low_frequency,fast_received,low_frequency_received};
}
const char* SourceName(dex3::FeedbackSource source) {
    switch (source) {
        case dex3::FeedbackSource::Fast: return "rt/dex3/*/state";
        case dex3::FeedbackSource::LowFrequency: return "rt/lf/dex3/*/state";
        default: return "none";
    }
}
std::string Age(double timestamp,double now) {
    if (timestamp<=0) return "never";
    std::ostringstream out;
    out<<std::fixed<<std::setprecision(0)<<(now-timestamp)*1000<<" ms";
    return out.str();
}
// Observation only: these measurements never refresh a feedback timestamp or
// change a watchdog decision. Print on exit even if Python stopped us first.
struct TakeoverDiagnostics {
    double armed_at=0,max_loop_gap=0,max_write_time=0;
    unsigned long fast_at_arm=0,lf_at_arm=0,writes=0;
    void Arm(const FeedbackPair& states,double now) {
        armed_at=now;
        fast_at_arm=states.fast_count;
        lf_at_arm=states.low_frequency_count;
    }
    std::string Summary(const std::string& side,const FeedbackPair& states,double now) const {
        std::ostringstream out;
        out<<side<<" Dex3 diagnostics: "<<std::fixed<<std::setprecision(1);
        if (armed_at>0) {
            out<<"since_first_ARM_ms="<<(now-armed_at)*1000
               <<", rx_since_ARM(fast/lf)="<<(states.fast_count-fast_at_arm)
               <<'/'<<(states.low_frequency_count-lf_at_arm);
        } else {
            out<<"not_armed";
        }
        out<<", rx_total(fast/lf)="<<states.fast_count<<'/'<<states.low_frequency_count
           <<", fast_age="<<Age(states.fast.time,now)
           <<", lf_age="<<Age(states.low_frequency.time,now)
           <<", cmd_writes="<<writes
           <<", max_write_ms="<<max_write_time*1000
           <<", max_loop_gap_ms="<<max_loop_gap*1000<<'\n';
        return out.str();
    }
};
dex3::Joints Publish(g1d::ClosingChannel<HandCmd_>& publisher,const dex3::Guard& guard) {
    HandCmd_ cmd{};
    dex3::Joints sent{};
    cmd.motor_cmd().resize(7);
    for (size_t i=0;i<7;++i) {
        auto& motor=cmd.motor_cmd()[i];
        motor.mode(static_cast<uint8_t>(0x10|i)); // id[3:0], status=1, timeout=0
        motor.q(guard.output[i]);
        motor.dq(0);
        motor.tau(0);
        motor.kp(0.5); // SDK example's low position gain
        motor.kd(0.1);
        sent[i]=motor.q(); // Report the float value actually given to DDS.
    }
    if (!publisher.Write(cmd)) throw std::runtime_error("hand DDS Write failed");
    return sent;
}
}
int main(int argc,char** argv) {
    if (argc!=3 || (std::string(argv[2])!="left" && std::string(argv[2])!="right")) {
        std::cerr << "Usage: g1d_hand_bridge eth0 left|right\n"; return 2;
    }
    const bool left=std::string(argv[2])=="left";
    const std::string side=argv[2];
    std::signal(SIGINT,Stop);
    std::signal(SIGTERM,Stop);
    std::signal(SIGPIPE,SIG_IGN);
    fcntl(STDOUT_FILENO,F_SETFL,fcntl(STDOUT_FILENO,F_GETFL)|O_NONBLOCK);
    ChannelFactory::Instance()->Init(0,argv[1]);
    // The official XR controller uses the normal-rate state topic. SDK2's
    // standalone example uses the lf topic; select the newest snapshot.
    g1d::ClosingChannel<HandState_> fast_subscriber,low_frequency_subscriber;
    fast_subscriber.Subscribe("rt/dex3/"+side+"/state",OnFastState);
    low_frequency_subscriber.Subscribe("rt/lf/dex3/"+side+"/state",OnLowFrequencyState);
    g1d::ClosingChannel<HandCmd_> publisher;
    publisher.Advertise("rt/dex3/"+side+"/cmd");
    dex3::Guard guard(left);
    TakeoverDiagnostics diagnostics;
    std::string pending;
    double previous=Now(),last_report=0,last_source_report=0;
    double last_feedback_time=0,last_sent_at=0;
    dex3::Joints last_sent{};
    std::array<dex3::GraspMode,7> reported_modes{};
    double pending_arm_since=0;
    std::string pending_arm_line;
    double last_loop=previous;
    auto last_source=dex3::FeedbackSource::None;
    bool failed=false;
    std::cerr<<side<<" Dex3 bridge: protocol=HANDSTATE2, bounded grasp enabled, max_cmd_hz=100; "
             <<"max_lead_rad="<<dex3::GraspSettings::max_lead
             <<", operator input watchdog=250 ms\n";
    try {
        while (running) {
            const auto states=ReadStates();
            const double now=Now();
            if (guard.owned)
                diagnostics.max_loop_gap=std::max(diagnostics.max_loop_gap,now-last_loop);
            last_loop=now;
            const auto state=dex3::SelectFeedback(states.fast,states.low_frequency,now);
            // Newest-sample selection can switch at every LF packet. Limit
            // console messages, not feedback processing.
            if (state.source!=dex3::FeedbackSource::None && state.source!=last_source &&
                (last_source==dex3::FeedbackSource::None || now-last_source_report>=1.0)) {
                std::cerr<<side<<" Dex3 feedback source: "<<SourceName(state.source)<<'\n';
                last_source=state.source;
                last_source_report=now;
            }
            const bool new_feedback=state.time>last_feedback_time;
            if (new_feedback) last_feedback_time=state.time;
            const auto fault=dex3::FeedbackFault(state.q,left,state.time,now);
            if (guard.owned && !fault.empty())
                throw std::runtime_error(fault + "; discarded states: wrong_motor_count="
                    + std::to_string(wrong_motor_count.load()) + ", nonfinite_joint="
                    + std::to_string(nonfinite_joint.load()) + "; fast_age="
                    + Age(states.fast.time,now) + ", lf_age="
                    + Age(states.low_frequency.time,now));
            pollfd input{STDIN_FILENO,POLLIN,0};
            const int result=poll(&input,1,0);
            if (result<0 && errno!=EINTR) throw std::runtime_error("hand pipe poll failed");
            if (input.revents&(POLLERR|POLLNVAL)) throw std::runtime_error("hand pipe failed");
            if (input.revents&(POLLIN|POLLHUP)) {
                char buffer[1024];
                const ssize_t n=read(STDIN_FILENO,buffer,sizeof(buffer));
                if (n<=0) break;
                pending.append(buffer,static_cast<size_t>(n));
                if (pending.size()>2048) throw std::runtime_error("hand input backlog");
                size_t end;
                while ((end=pending.find('\n'))!=std::string::npos) {
                    const auto line=pending.substr(0,end);
                    pending.erase(0,end+1);
                    // Takeover must use feedback received since the operator's
                    // ARM event. Retain the first request across a feedback
                    // gap; repeated ARM messages refresh the PICO timestamp.
                    if (line.rfind("ARM ",0)==0 && !guard.tracking) {
                        std::istringstream command(line);
                        std::string verb;
                        double arm_time=0;
                        command>>verb>>arm_time;
                        if (!dex3::Fresh(arm_time,Now())) continue;
                        if (pending_arm_since==0) pending_arm_since=arm_time;
                        pending_arm_line=line;
                        continue;
                    }
                    if (line=="HOLD" || line=="RELEASE") {
                        pending_arm_since=0;
                        pending_arm_line.clear();
                    }
                    // Freeze a missing-feedback side at the last output. Do
                    // not accumulate targets while its measurements are absent.
                    if (!new_feedback && line.rfind("TARGET ",0)==0)
                        continue;
                    if (!fault.empty() && (guard.owned || (line != "HOLD" && line != "RELEASE")))
                        throw std::runtime_error(fault);
                    if (guard.Receive(line,new_feedback?state.q:guard.output,Now())) {
                        const auto at_arm=ReadStates();
                        const double arm_time=Now();
                        diagnostics.Arm(at_arm,arm_time);
                        std::cerr<<diagnostics.Summary(side,at_arm,arm_time);
                    }
                }
            }
            const double control_now=Now();
            if (pending_arm_since>0 &&
                dex3::CanTakeover(state.time,pending_arm_since,new_feedback)) {
                std::istringstream command(pending_arm_line);
                std::string verb;
                double latest_input_time=0;
                command>>verb>>latest_input_time;
                if (dex3::Fresh(latest_input_time,control_now) &&
                    guard.Receive(pending_arm_line,state.q,control_now)) {
                    const auto at_arm=ReadStates();
                    diagnostics.Arm(at_arm,control_now);
                    std::cerr<<diagnostics.Summary(side,at_arm,control_now);
                }
                pending_arm_since=0;
                pending_arm_line.clear();
            }
            if (state.time>0 && control_now-state.time>=feedback_freeze_delay)
                guard.Freeze();
            guard.Tick(state.q,control_now,control_now-previous,state.time);
            previous=control_now;
            if (guard.owned) {
                const double write_start=Now();
                last_sent=Publish(publisher,guard);
                last_sent_at=Now(); // A successful Write, not a motor execution ACK.
                diagnostics.max_write_time=std::max(diagnostics.max_write_time,Now()-write_start);
                ++diagnostics.writes;
            }
            for (size_t i=1;i<7;++i) {
                const auto mode=guard.grasp.modes[i];
                if (mode==reported_modes[i]) continue;
                const char* name=mode==dex3::GraspMode::Blocked ? "BLOCKED" :
                                 mode==dex3::GraspMode::Opening ? "OPENING" : "FOLLOW";
                std::ostringstream event;
                event<<std::fixed<<std::setprecision(4)<<"Dex3 grasp t="<<control_now
                     <<" side="<<side<<" joint="<<i<<" mode="<<name
                     <<" requested="<<guard.requested[i]<<" sent="<<last_sent[i]
                     <<" measured="<<state.q[i]<<" error="<<last_sent[i]-state.q[i]
                     <<" velocity="<<guard.grasp.velocity[i]
                     <<" feedback_age_ms="<<(control_now-state.time)*1000<<'\n';
                std::cerr<<event.str();
                reported_modes[i]=mode;
            }
            if (state.time>0 && now-last_report>=0.02) {
                std::ostringstream line;
                // Send command telemetry even during a feedback pause. Keep
                // the original feedback timestamp, never manufacture freshness.
                line<<std::setprecision(12)<<"HANDSTATE2 "<<state.time<<' '
                    <<(guard.tracking?1:guard.owned?2:0);
                for (double q:state.q) line<<' '<<q;
                line<<' '<<last_sent_at;
                for (double q:last_sent) line<<' '<<q;
                for (double q:guard.requested) line<<' '<<q;
                line<<' '<<guard.grasp.Mask(dex3::GraspMode::Blocked)
                    <<' '<<guard.grasp.Mask(dex3::GraspMode::Opening)
                    <<' '<<guard.grasp.limited_mask;
                line<<'\n';
                const auto text=line.str();
                const auto written=write(STDOUT_FILENO,text.data(),text.size());
                if (written==static_cast<ssize_t>(text.size())) last_report=now;
                else if (!(written<0 && errno==EAGAIN))
                    throw std::runtime_error("hand state reader disconnected");
            }
            // No catch-up bursts after a delayed iteration.
            std::this_thread::sleep_for(command_period);
        }
    } catch (const std::exception& exc) {
        std::cerr<<"Hand bridge stopped: "<<exc.what()<<'\n';
        failed=true;
    }
    {
        const auto final_states=ReadStates();
        std::cerr<<diagnostics.Summary(side,final_states,Now());
    }
    // Briefly hold the last commanded pose, then stop publishing.
    if (guard.owned) {
        guard.Hold(guard.output);
        try {
            const double deadline=Now()+0.2;
            while (Now()<deadline) {
                Publish(publisher,guard);
                std::this_thread::sleep_for(command_period);
            }
        } catch (...) { failed=true; }
    }
    std::cerr<<side<<" Dex3 shutdown: closing DDS readers and writer on main thread\n";
    // Try every close even if an earlier one reports an error. The wrapper's
    // destructor is also a fallback for exceptional paths during startup.
    const auto close=[&](auto& channel) {
        try { channel.Close(); }
        catch (const std::exception& exc) {
            std::cerr<<side<<" Dex3 shutdown error: "<<exc.what()<<'\n';
            failed=true;
        }
    };
    close(fast_subscriber);
    close(low_frequency_subscriber);
    close(publisher);
    std::cerr<<side<<" Dex3 shutdown: done, exit="<<(failed?1:0)<<'\n';
    return failed?1:0;
}
