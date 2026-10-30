// Read "vx vyaw vz" from stdin and control G1-D through official SDK2.
// A command expires after 300 ms; missing height feedback stops column lift.
#include "g1d_lift_guard.hpp"
#include "g1d_recording.hpp"

#include <algorithm>
#include <atomic>
#include <cerrno>
#include <chrono>
#include <cmath>
#include <csignal>
#include <cstdlib>
#include <iostream>
#include <poll.h>
#include <sstream>
#include <string>
#include <unistd.h>

#include <unitree/idl/ros2/Point32_.hpp>
#include <unitree/robot/channel/channel_subscriber.hpp>
#include <unitree/robot/g1/agv/g1_agv_client.hpp>

using Clock = std::chrono::steady_clock;
using geometry_msgs::msg::dds_::Point32_;
using unitree::robot::ChannelSubscriber;
using unitree::robot::ChannelSubscriberPtr;

namespace {
volatile std::sig_atomic_t running = 1;
std::atomic<float> height_m{0.0f};
std::atomic<int64_t> height_time_ms{0};

int64_t NowMs() {
    return std::chrono::duration_cast<std::chrono::milliseconds>(Clock::now().time_since_epoch()).count();
}

void OnSignal(int) { running = 0; }

void OnHeight(const void* message) {
    const auto* state = static_cast<const Point32_*>(message);
    const float value = state->y();  // Official G1-D example: y is column height in metres.
    if (std::isfinite(value)) {
        height_m.store(value, std::memory_order_relaxed);
        height_time_ms.store(NowMs(), std::memory_order_release);
    }
}

struct Command {
    float vx = 0.0f;
    float vyaw = 0.0f;
    float vz = 0.0f;
};

enum class Mode { Base, Lift, Both };

bool ParseCommand(const std::string& line, Command& out) {
    std::istringstream stream(line);
    Command parsed;
    std::string extra;
    if (!(stream >> parsed.vx >> parsed.vyaw >> parsed.vz) || (stream >> extra)) return false;
    if (!std::isfinite(parsed.vx) || !std::isfinite(parsed.vyaw) || !std::isfinite(parsed.vz)) return false;
    // Independent caps protect against an accidental Python-side configuration change.
    if (std::fabs(parsed.vx) > 0.10f || std::fabs(parsed.vyaw) > 0.15f || std::fabs(parsed.vz) > 0.15f) return false;
    out = parsed;
    return true;
}

bool ParseHeight(const char* text, float& out) {
    char* end = nullptr;
    errno = 0;
    const float value = std::strtof(text, &end);
    if (end == text || *end != '\0' || errno == ERANGE || !std::isfinite(value)) return false;
    out = value;
    return true;
}
}  // namespace

int main(int argc, char* argv[]) {
    if (argc != 5) {
        std::cerr << "Usage: g1d_agv_bridge NETWORK_INTERFACE HEIGHT_MIN_M HEIGHT_MAX_M base|lift|both\n";
        return 2;
    }
    const std::string mode_text = argv[4];
    Mode mode;
    if (mode_text == "base") mode = Mode::Base;
    else if (mode_text == "lift") mode = Mode::Lift;
    else if (mode_text == "both") mode = Mode::Both;
    else {
        std::cerr << "Mode must be base, lift or both.\n";
        return 2;
    }
    float height_min = 0.0f, height_max = 0.0f;
    if (!ParseHeight(argv[2], height_min) || !ParseHeight(argv[3], height_max) ||
        height_min < 0.0f || height_min >= height_max || height_max > 3.0f) {
        std::cerr << "Invalid verified height limits.\n";
        return 2;
    }
    std::signal(SIGINT, OnSignal);
    std::signal(SIGTERM, OnSignal);

    unitree::robot::ChannelFactory::Instance()->Init(0, argv[1]);
    unitree::robot::g1::AgvClient client;
    client.SetTimeout(0.2f);
    client.Init();

    ChannelSubscriberPtr<Point32_> height_subscriber(
        new ChannelSubscriber<Point32_>("rt/hispeed_state"));
    height_subscriber->InitChannel(OnHeight, 1);

    g1d::RecordingSink recording;
    Command latest;
    int64_t last_input_ms = 0;
    int64_t last_send_ms = 0;
    std::string pending;
    bool stdin_open = true;
    bool failed = false;
    const char* trace_setting = std::getenv("G1D_AGV_TRACE");
    const bool trace_enabled = trace_setting != nullptr && std::string(trace_setting) == "1";
    int64_t last_trace_ms = 0;
    int64_t max_send_gap_ms = 0;
    unsigned received_commands = 0;
    unsigned nonzero_moves = 0;
    unsigned stale_moves = 0;
    auto last_lift_status=g1d::LiftStatus::Ready;
    bool reported_lift_status=false;
    while (running && stdin_open) {
        pollfd fd{STDIN_FILENO, POLLIN, 0};
        const int poll_result = poll(&fd, 1, 20);
        if (poll_result < 0 && errno != EINTR) {
            failed = true;
            break;
        }
        if (poll_result > 0 && (fd.revents & (POLLERR | POLLNVAL))) {
            failed = true;
            break;
        }
        if (poll_result > 0 && (fd.revents & (POLLIN | POLLHUP))) {
            char buffer[256];
            const ssize_t n = read(STDIN_FILENO, buffer, sizeof(buffer));
            if (n <= 0) {
                stdin_open = false;
            } else {
                pending.append(buffer, static_cast<size_t>(n));
                if (pending.size() > 1024) {
                    std::cerr << "Command stream exceeded 1024 bytes.\n";
                    failed = true;
                    break;
                }
                size_t newline;
                while ((newline = pending.find('\n')) != std::string::npos) {
                    Command parsed;
                    const std::string line = pending.substr(0, newline);
                    pending.erase(0, newline + 1);
                    if (!ParseCommand(line, parsed)) {
                        std::cerr << "Invalid motion command; stopping.\n";
                        failed = true;
                        stdin_open = false;
                        break;
                    }
                    latest = parsed;
                    last_input_ms = NowMs();
                    ++received_commands;
                }
            }
        }
        if (!stdin_open) break;
        const int64_t now = NowMs();
        if (now - last_send_ms < 50) continue;
        if (last_send_ms != 0) {
            max_send_gap_ms = std::max(max_send_gap_ms, now - last_send_ms);
        }
        last_send_ms = now;
        Command output;
        const int64_t height_age = now - height_time_ms.load(std::memory_order_acquire);
        const bool height_fresh = height_time_ms.load() != 0 && height_age <= 500;
        const int64_t input_age_ms = last_input_ms == 0 ? -1 : now - last_input_ms;
        const bool input_fresh = last_input_ms != 0 && input_age_ms <= 300;
        if (!input_fresh) ++stale_moves;
        if (input_fresh) {
            output = latest;
            if (mode == Mode::Base) {
                output.vz = 0.0f;
            }
            if (mode == Mode::Lift) output.vx = output.vyaw = 0.0f;
        }
        if (mode != Mode::Base) {
            const float height=height_m.load(std::memory_order_relaxed);
            const auto lift=g1d::FilterLift(output.vz,height,height_fresh,height_min,height_max);
            output.vz=lift.speed;
            if (!reported_lift_status || lift.status!=last_lift_status) {
                std::cerr<<"LIFT "<<g1d::LiftStatusName(lift.status)<<" height="<<height
                         <<" m feedback_age_ms="<<(height_time_ms.load()?height_age:-1)
                         <<" window=["<<height_min<<','<<height_max<<"]"
                         <<" requested="<<latest.vz<<" sent="<<output.vz<<'\n';
                last_lift_status=lift.status;
                reported_lift_status=true;
            }
        }
        if (output.vx != 0.0f || output.vyaw != 0.0f) ++nonzero_moves;
        const int move_result = mode == Mode::Lift ? 0 : client.Move(output.vx, 0.0f, output.vyaw);
        const int lift_result = mode == Mode::Base ? 0 : client.HeightAdjust(output.vz);
        if (recording.enabled()) {
            // Observation only: calls above and their return values are unchanged.
            try {
                std::ostringstream row;
                row << std::setprecision(12)
                    << "{\"mode\":\"" << mode_text << "\",\"height_m\":" << height_m.load()
                    << ",\"height_received_monotonic_s\":" << height_time_ms.load()/1000.0
                    << ",\"height_fresh\":" << (height_fresh ? "true" : "false")
                    << ",\"input_fresh\":" << (input_fresh ? "true" : "false")
                    << ",\"requested\":[" << latest.vx << ',' << latest.vyaw << ',' << latest.vz << ']'
                    << ",\"submitted\":[" << output.vx << ',' << output.vyaw << ',' << output.vz << ']'
                    << ",\"move_result\":" << move_result << ",\"lift_result\":" << lift_result
                    << ",\"base_world_pose\":null,\"base_measured_velocity\":null}";
                recording.Send("agv_state_action", row.str(), NowMs()/1000.0);
            } catch (...) { /* Recording cannot change control behavior. */ }
        }
        if (trace_enabled && now - last_trace_ms >= 500) {
            std::cerr << "AGV TRACE mode=" << mode_text
                      << " rx_500ms=" << received_commands
                      << " rx_age_ms=" << input_age_ms
                      << " latest_vx=" << latest.vx
                      << " latest_yaw=" << latest.vyaw
                      << " sent_vx=" << output.vx
                      << " sent_yaw=" << output.vyaw
                      << " sent_lift=" << output.vz
                      << " height=" << height_m.load()
                      << " height_age_ms=" << height_age
                      << " nonzero_moves_500ms=" << nonzero_moves
                      << " stale_moves_500ms=" << stale_moves
                      << " max_send_gap_ms=" << max_send_gap_ms
                      << " move_ret=" << move_result << '\n';
            last_trace_ms = now;
            received_commands = nonzero_moves = stale_moves = 0;
            max_send_gap_ms = 0;
        }
        if (move_result != 0 || lift_result != 0) {
            std::cerr << "SDK2 call failed: Move=" << move_result
                      << " HeightAdjust=" << lift_result << '\n';
            failed = true;
            break;
        }
    }
    if (mode != Mode::Lift) client.Move(0.0f, 0.0f, 0.0f);
    if (mode != Mode::Base) client.HeightAdjust(0.0f);
    return failed ? 1 : 0;
}
