// G1-D: arms/yaw on lowcmd/lowstate; pitch on waistpitchcmd/waistpitchstate.
// No DDS writes before an explicit ARM command with fresh state/input.
#include "g1d_upper_body_io.hpp"
#include "g1d_recording.hpp"
#include <atomic>
#include <cerrno>
#include <csignal>
#include <cstring>
#include <fcntl.h>
#include <iomanip>
#include <iostream>
#include <mutex>
#include <poll.h>
#include <thread>
#include <time.h>
#include <unistd.h>
#include <unitree/robot/channel/channel_publisher.hpp>
#include <unitree/robot/channel/channel_subscriber.hpp>
#include <unitree/idl/hg/LowCmd_.hpp>
#include <unitree/idl/hg/LowState_.hpp>

using namespace unitree::robot;
using namespace unitree_hg::msg::dds_;
namespace {
volatile std::sig_atomic_t running = 1;
void Stop(int) { running = 0; }
double Now() {
    timespec t{};
    clock_gettime(CLOCK_MONOTONIC, &t);
    return t.tv_sec + t.tv_nsec * 1e-9;
}
std::mutex state_mutex;
g1d::UpperBodyFeedback latest;
void OnState(const void* message) {
    g1d::ArmFeedback next;
    if (!g1d::DecodeArmState(*static_cast<const LowState_*>(message), Now(), next)) return;
    std::lock_guard<std::mutex> lock(state_mutex);
    latest.arm = next;
}
void OnPitchState(const void* message) {
    g1d::PitchFeedback next;
    if (!g1d::DecodePitchState(*static_cast<const MotorState_*>(message), Now(), next)) return;
    std::lock_guard<std::mutex> lock(state_mutex);
    latest.pitch = next;
}
g1d::UpperBodyFeedback ReadState() {
    std::lock_guard<std::mutex> lock(state_mutex);
    return latest;
}
void PublishArms(ChannelPublisher<LowCmd_>& publisher, const g1d::ArmGuard& guard,
                 double yaw, uint8_t mode) {
    const auto command = g1d::MakeArmCommand(guard, yaw, mode);
    if (!publisher.Write(command)) throw std::runtime_error("arm DDS Write failed");
}
void PublishPitch(ChannelPublisher<MotorCmd_>& publisher, double pitch) {
    const auto command = g1d::MakePitchCommand(pitch);
    if (!publisher.Write(command)) throw std::runtime_error("rt/waistpitchcmd DDS Write failed");
}
}

int main(int argc, char** argv) {
    if (g1d::limits::PrintIfRequested(argc, argv, std::cout)) return 0;
    if (argc != 2 && argc != 3 && argc != 6 && argc != 10 && argc != 13) {
        std::cerr << "Usage: g1d_arm_bridge eth0 [arm_speed [yaw_limit yaw_speed yaw_acceleration"
                     " [pitch_min pitch_max pitch_speed pitch_acceleration"
                     " [pitch_max_error pitch_stall_timeout pitch_min_progress]]]]\n";
        return 2;
    }
    double joint_speed = 0.30;
    double yaw_limit=0.5235987756, yaw_speed=0.10, yaw_acceleration=0.20;
    double pitch_min=0.0, pitch_max=0.5235987756, pitch_speed=0.08, pitch_acceleration=0.16;
    double pitch_max_error=0.10, pitch_stall_timeout=1.5, pitch_min_progress=0.005;
    try {
        auto number=[](const char* text) {
            size_t consumed=0;
            const double value=std::stod(text,&consumed);
            if (consumed!=std::strlen(text)) throw std::runtime_error("invalid numeric parameter");
            return value;
        };
        if (argc >= 3) {
            size_t consumed = 0;
            joint_speed = std::stod(argv[2], &consumed);
            if (consumed != std::strlen(argv[2])) throw std::runtime_error("invalid speed");
        }
        if (argc >= 6) {
            yaw_limit=number(argv[3]); yaw_speed=number(argv[4]); yaw_acceleration=number(argv[5]);
        }
        if (argc >= 10) {
            pitch_min=number(argv[6]); pitch_max=number(argv[7]);
            pitch_speed=number(argv[8]); pitch_acceleration=number(argv[9]);
        }
        if (argc == 13) {
            pitch_max_error=number(argv[10]); pitch_stall_timeout=number(argv[11]);
            pitch_min_progress=number(argv[12]);
        }
        const g1d::ArmGuard validation(joint_speed);
        const g1d::WaistGuard waist_validation(yaw_limit,yaw_speed,yaw_acceleration,
                                              pitch_min,pitch_max,pitch_speed,pitch_acceleration,
                                              pitch_max_error,pitch_stall_timeout,pitch_min_progress);
    } catch (const std::exception& e) {
        std::cerr << "Invalid arm/waist configuration: " << e.what() << '\n';
        return 2;
    }
    std::signal(SIGINT, Stop);
    std::signal(SIGTERM, Stop);
    std::signal(SIGPIPE, SIG_IGN);
    // State output must never block the DDS loop if the Python reader stalls.
    fcntl(STDOUT_FILENO, F_SETFL, fcntl(STDOUT_FILENO, F_GETFL) | O_NONBLOCK);
    ChannelFactory::Instance()->Init(0, argv[1]);
    ChannelSubscriber<LowState_> subscriber("rt/lowstate");
    subscriber.InitChannel(OnState, 1);
    ChannelPublisher<LowCmd_> publisher("rt/lowcmd");
    publisher.InitChannel();
    ChannelSubscriber<MotorState_> pitch_subscriber(g1d::kPitchStateTopic);
    pitch_subscriber.InitChannel(OnPitchState, 1);
    ChannelPublisher<MotorCmd_> pitch_publisher(g1d::kPitchCommandTopic);
    pitch_publisher.InitChannel();
    g1d::RecordingSink recording;
    double last_write_at = 0.0;
    g1d::ArmGuard guard(joint_speed);
    uint8_t mode = 0;
    g1d::WaistGuard waist(yaw_limit,yaw_speed,yaw_acceleration,
                         pitch_min,pitch_max,pitch_speed,pitch_acceleration,
                         pitch_max_error,pitch_stall_timeout,pitch_min_progress);
    std::string pending;
    double previous = Now(), last_report = 0;
    bool failed = false;
    bool reported_pitch_ready = false;
    std::cerr << "G1-D upper-body bridge: protocol=STATE6, torso-relative arms; yaw limit="
              <<yaw_limit<<" rad, speed="<<yaw_speed<<" rad/s, accel="<<yaw_acceleration<<" rad/s^2; "
              <<"pitch window=["<<pitch_min<<','<<pitch_max<<"] rad, speed="<<pitch_speed
              <<" rad/s, accel="<<pitch_acceleration<<" rad/s^2; max_error="<<pitch_max_error
              <<" rad, stall_timeout="<<pitch_stall_timeout<<" s, min_progress="<<pitch_min_progress<<" rad\n";
    std::cerr << "Pitch interface: cmd=rt/waistpitchcmd, state=rt/waistpitchstate, kp=500, kd=12; "
                 "lowcmd[14] unused. Waiting for dedicated pitch feedback before takeover.\n";
    try {
        while (running) {
            const auto state = ReadState();
            const double now = Now();
            const bool arm_fresh = g1d::Fresh(state.arm.received_at, now);
            const bool pitch_fresh = g1d::Fresh(state.pitch.received_at, now);
            if (pitch_fresh && !reported_pitch_ready) {
                std::cerr << "Pitch feedback READY: source=rt/waistpitchstate, q="
                          << state.pitch.q << " rad, age_ms=" << 1000*(now-state.pitch.received_at) << '\n';
                reported_pitch_ready = true;
            }
            if (guard.owned && (!arm_fresh || state.arm.mode != mode || !g1d::Valid(state.arm.q)))
                throw std::runtime_error("arm feedback lost, mode changed, or joint limit exceeded");
            if (guard.owned && !pitch_fresh)
                throw std::runtime_error("rt/waistpitchstate feedback stale: age=" +
                                         std::to_string(1000*(now-state.pitch.received_at)) + " ms (limit 250 ms)");
            if (guard.owned && !g1d::ValidWaist(state.waist()))
                throw std::runtime_error("waist yaw/pitch feedback outside model limits");
            pollfd input{STDIN_FILENO, POLLIN, 0};
            const int result = poll(&input, 1, 0);
            if (result < 0 && errno != EINTR) throw std::runtime_error("arm input poll failed");
            if (input.revents & (POLLERR | POLLNVAL)) throw std::runtime_error("arm input stream failed");
            if (input.revents & (POLLIN | POLLHUP)) {
                char buffer[2048];
                const ssize_t size = read(STDIN_FILENO, buffer, sizeof(buffer));
                if (size <= 0) break;
                pending.append(buffer, static_cast<size_t>(size));
                if (pending.size() > 4096) throw std::runtime_error("arm input backlog");
                size_t end;
                while ((end = pending.find('\n')) != std::string::npos) {
                    const std::string line = pending.substr(0, end);
                    pending.erase(0, end + 1);
                    // Stop-only messages need no feedback and never take over.
                    if (line != "HOLD" && line != "PITCH_HOLD" && !state.fresh(Now()))
                        throw std::runtime_error("cannot take over/control upper body without fresh "
                                                 "rt/lowstate AND rt/waistpitchstate");
                    if (line != "HOLD" && line != "PITCH_HOLD" &&
                        (!g1d::Valid(state.arm.q) || !g1d::ValidWaist(state.waist())))
                        throw std::runtime_error("cannot take over/control upper body: feedback outside model limits");
                    const bool was_tracking=guard.tracking;
                    if (guard.Receive(line, state.arm.q, Now())) mode=state.arm.mode;
                    if (!was_tracking && guard.tracking) {
                        waist.Capture(state.waist());
                        std::cerr << "G1-D upper body taken over; yaw/pitch start at measured angles; pitch source=rt/waistpitchstate.\n";
                    }
                    if (!guard.tracking) waist.Stop();
                    else if (!guard.pitch_enabled) waist.StopPitch();
                }
            }
            const double control_now = Now();
            guard.Tick(state.arm.q, control_now, control_now - previous);
            const auto before_pitch=waist.pitch_following;
            waist.Tick(state.waist(),guard.waist_rate,guard.tracking,control_now-previous,
                       guard.pitch_rate,guard.pitch_enabled,guard.last_input);
            if (waist.pitch_following!=before_pitch) {
                if (waist.pitch_following==g1d::PitchFollowing::WaitCenter) {
                    std::cerr << "PITCH_NOT_FOLLOWING: request="<<waist.pitch_requested_rate
                              <<" rad/s, measured="<<state.pitch.q<<" rad, previous_target="
                              <<state.pitch.q+waist.pitch_stall_error_rad<<" rad, error="
                              <<waist.pitch_stall_error_rad<<" rad, elapsed="<<waist.pitch_stall_elapsed_s
                              <<" s; cancelled catch-up target, holding measured pitch. "
                                "Centre both right-stick axes in PITCH mode, then push again; A remains enabled.\n";
                } else if (waist.pitch_following==g1d::PitchFollowing::WaitPush) {
                    std::cerr << "PITCH_RECOVERY: centred; waiting for a new pitch stick push.\n";
                } else {
                    std::cerr << "PITCH_RECOVERY: resumed from measured angle "<<state.pitch.q<<" rad.\n";
                }
            }
            previous = control_now;
            if (guard.owned) {
                // Both command streams share ownership, A/B and input watchdog.
                PublishArms(publisher, guard, waist.output[0], mode);
                PublishPitch(pitch_publisher, waist.output[1]);
                last_write_at = Now();
            }
            if (arm_fresh && now - last_report >= 0.02) {
                const std::string text = g1d::FormatUpperBodyState(state, guard, waist);
                // A state line is < PIPE_BUF; EAGAIN drops a sample, never a partial line.
                if (!text.empty() && write(STDOUT_FILENO, text.data(), text.size()) < 0 && errno != EAGAIN)
                    throw std::runtime_error("arm state reader disconnected");
                if (recording.enabled()) {
                    try {
                        std::ostringstream row;
                        row << std::setprecision(12) << "{\"owned\":" << (guard.owned ? "true" : "false")
                            << ",\"last_write_monotonic_s\":" << last_write_at
                            << ",\"requested_q\":";
                        g1d::JsonArray(row, guard.target);
                        row << ",\"output_q\":"; g1d::JsonArray(row, guard.output);
                        row << ",\"output_tau_ff\":"; g1d::JsonArray(row, guard.torque);
                        row << ",\"waist_output_q\":"; g1d::JsonArray(row, waist.output);
                        row << '}';
                        recording.Send("arm_output", row.str(), Now());
                        std::ostringstream feedback;
                        feedback << std::setprecision(12) << "{\"received_at\":" << state.arm.received_at
                                 << ",\"q\":";
                        g1d::JsonOptionalArray(feedback, state.arm.q);
                        feedback << ",\"dq\":"; g1d::JsonOptionalArray(feedback, state.arm.dq);
                        feedback << ",\"tau_est\":"; g1d::JsonOptionalArray(feedback, state.arm.tau_est);
                        feedback << ",\"waist\":[";
                        for (int i=0; i<2; ++i) {
                            if (i) feedback << ',';
                            feedback << "{\"joint\":\"" << (i ? "pitch" : "yaw") << "\",\"received_at\":"
                                     << (i ? state.pitch.received_at : state.arm.received_at) << ",\"q\":";
                            g1d::JsonNumber(feedback, i ? state.pitch.q : state.arm.yaw);
                            feedback << ",\"dq\":";
                            g1d::JsonNumber(feedback, i ? state.pitch.dq : state.arm.yaw_dq);
                            feedback << ",\"tau_est\":";
                            g1d::JsonNumber(feedback, i ? state.pitch.tau_est : state.arm.yaw_tau_est);
                            feedback << '}';
                        }
                        feedback << "]}";
                        recording.Send("arm_dynamics", feedback.str(), Now());
                    } catch (...) { /* Optional telemetry never faults robot control. */ }
                }
                last_report = now;
            }
            std::this_thread::sleep_for(std::chrono::milliseconds(5));
        }
    } catch (const std::exception& error) {
        std::cerr << "Arm bridge stopped: " << error.what() << '\n';
        failed = true;
    }
    // Brief measured-pose hold, never command zero joint angles on exit.
    auto state = ReadState();
    if (guard.owned && waist.captured && g1d::Fresh(state.arm.received_at, Now()) &&
        state.arm.mode == mode && g1d::Valid(state.arm.q)) {
        guard.Hold(state.arm.q);
        waist.Stop();
        const double deadline = Now() + 0.2;
        try {
            while (Now() < deadline) {
                state = ReadState();
                if (!g1d::Fresh(state.arm.received_at, Now()) || state.arm.mode != mode) break;
                PublishArms(publisher, guard, waist.output[0], mode);
                // Never publish pitch based on missing/stale dedicated feedback.
                if (g1d::Fresh(state.pitch.received_at, Now()) && g1d::ValidWaist(state.waist()))
                    PublishPitch(pitch_publisher, waist.output[1]);
                std::this_thread::sleep_for(std::chrono::milliseconds(5));
            }
        } catch (...) { failed = true; }
    }
    return failed ? 1 : 0;
}
