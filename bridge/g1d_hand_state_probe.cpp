// Read-only Dex3 state observer. Run beside the teleoperation program on PC2.
// It creates no DDS command publisher and never sends a motor command.
#include <array>
#include <atomic>
#include <chrono>
#include <cmath>
#include <csignal>
#include <cstdint>
#include <ctime>
#include <iomanip>
#include <iostream>
#include <memory>
#include <sstream>
#include <string>
#include <thread>
#include <unitree/idl/hg/HandState_.hpp>
#include <unitree/robot/channel/channel_factory.hpp>
#include <unitree/robot/channel/channel_subscriber.hpp>

using unitree::robot::ChannelFactory;
using unitree::robot::ChannelSubscriber;
using unitree_hg::msg::dds_::HandState_;

namespace {
constexpr std::int64_t stale_ns = 250'000'000;
constexpr auto report_period = std::chrono::milliseconds(500);
volatile std::sig_atomic_t running = 1;

void Stop(int) { running = 0; }

std::int64_t NowNs() {
    return std::chrono::duration_cast<std::chrono::nanoseconds>(
        std::chrono::steady_clock::now().time_since_epoch()).count();
}

std::string WallTime() {
    const auto now = std::chrono::system_clock::now();
    const auto seconds = std::chrono::system_clock::to_time_t(now);
    std::tm local{};
    localtime_r(&seconds, &local);
    const auto millis = std::chrono::duration_cast<std::chrono::milliseconds>(
        now.time_since_epoch()).count() % 1000;
    char date[32];
    std::strftime(date, sizeof(date), "%Y-%m-%d %H:%M:%S", &local);
    std::ostringstream out;
    out << date << '.' << std::setfill('0') << std::setw(3) << millis;
    return out.str();
}

struct Counters {
    std::atomic<std::uint64_t> received{0};
    std::atomic<std::uint64_t> valid{0};
    std::atomic<std::uint64_t> invalid{0};
    std::atomic<std::uint64_t> gaps250{0};
    std::atomic<std::int64_t> latest_ns{0};
    std::atomic<std::int64_t> longest_gap_ns{0};
};

std::array<Counters, 4> counters;
constexpr std::array<const char*, 4> labels{
    "left.fast", "left.lf", "right.fast", "right.lf"
};
constexpr std::array<const char*, 4> topics{
    "rt/dex3/left/state", "rt/lf/dex3/left/state",
    "rt/dex3/right/state", "rt/lf/dex3/right/state"
};

void OnState(std::size_t index, const void* raw) {
    const auto& state = *static_cast<const HandState_*>(raw);
    auto& item = counters[index];
    const auto now = NowNs();
    const auto previous = item.latest_ns.exchange(now, std::memory_order_relaxed);
    if (previous > 0 && now > previous) {
        const auto gap = now - previous;
        if (gap >= stale_ns)
            item.gaps250.fetch_add(1, std::memory_order_relaxed);
        auto largest = item.longest_gap_ns.load(std::memory_order_relaxed);
        while (gap > largest && !item.longest_gap_ns.compare_exchange_weak(
                                  largest, gap, std::memory_order_relaxed)) {}
    }
    item.received.fetch_add(1, std::memory_order_relaxed);

    bool valid = state.motor_state().size() == 7;
    if (valid) {
        for (const auto& motor : state.motor_state()) {
            if (!std::isfinite(motor.q())) { valid = false; break; }
        }
    }
    (valid ? item.valid : item.invalid).fetch_add(1, std::memory_order_relaxed);
}

double Milliseconds(std::int64_t nanoseconds) {
    return static_cast<double>(nanoseconds) / 1'000'000.0;
}

void PrintSource(std::size_t index, std::int64_t now) {
    const auto& item = counters[index];
    const auto latest = item.latest_ns.load(std::memory_order_relaxed);
    std::cout << ' ' << labels[index]
              << "{rx=" << item.received.load(std::memory_order_relaxed)
              << ",valid=" << item.valid.load(std::memory_order_relaxed)
              << ",bad=" << item.invalid.load(std::memory_order_relaxed)
              << ",age_ms=";
    if (latest == 0) std::cout << "never";
    else std::cout << std::fixed << std::setprecision(1)
                   << Milliseconds(now - latest);
    std::cout << ",max_gap_ms=" << std::fixed << std::setprecision(1)
              << Milliseconds(item.longest_gap_ns.load(std::memory_order_relaxed))
              << ",gaps250=" << item.gaps250.load(std::memory_order_relaxed)
              << '}';
}

void PrintSnapshot(std::int64_t now) {
    for (std::size_t side = 0; side < 2; ++side) {
        std::cout << WallTime() << " STATE ";
        PrintSource(side * 2, now);
        PrintSource(side * 2 + 1, now);
        std::cout << '\n';
    }
    std::cout.flush();
}
} // namespace

int main(int argc, char** argv) {
    if (argc != 2) {
        std::cerr << "Usage: g1d_hand_state_probe eth0\n";
        return 2;
    }
    std::signal(SIGINT, Stop);
    std::signal(SIGTERM, Stop);

    ChannelFactory::Instance()->Init(0, argv[1]);
    std::array<std::unique_ptr<ChannelSubscriber<HandState_>>, 4> subscribers;
    for (std::size_t i = 0; i < subscribers.size(); ++i) {
        subscribers[i] = std::make_unique<ChannelSubscriber<HandState_>>(topics[i]);
        // Direct SDK callback (queue length 0), independent of the hand
        // bridge's queue-length-1 callback worker.
        subscribers[i]->InitChannel([i](const void* raw) { OnState(i, raw); }, 0);
    }
    std::cout << "Dex3 read-only probe v1, interface=" << argv[1]
              << ", domain=0, four state topics, no command publisher\n";
    std::cout.flush();

    auto next_report = std::chrono::steady_clock::now();
    while (running) {
        const auto now = std::chrono::steady_clock::now();
        if (now >= next_report) {
            PrintSnapshot(NowNs());
            next_report = now + report_period;
        }
        std::this_thread::sleep_for(std::chrono::milliseconds(20));
    }
    PrintSnapshot(NowNs());
    return 0;
}
