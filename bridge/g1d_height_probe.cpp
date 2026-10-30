// Read-only G1-D column-height diagnostic. No AgvClient or motion command.

#include <atomic>
#include <chrono>
#include <cmath>
#include <iomanip>
#include <iostream>
#include <thread>

#include <unitree/idl/ros2/Point32_.hpp>
#include <unitree/robot/channel/channel_subscriber.hpp>

namespace {
std::atomic<float> height_m{0.0f};
std::atomic<bool> received{false};

void OnHeight(const void* message) {
    const auto* state = static_cast<const geometry_msgs::msg::dds_::Point32_*>(message);
    const float height = state->y();
    if (std::isfinite(height)) {
        height_m.store(height, std::memory_order_relaxed);
        received.store(true, std::memory_order_release);
    }
}
}  // namespace

int main(int argc, char* argv[]) {
    if (argc != 2) {
        std::cerr << "Usage: g1d_height_probe NETWORK_INTERFACE\n";
        return 2;
    }
    unitree::robot::ChannelFactory::Instance()->Init(0, argv[1]);
    unitree::robot::ChannelSubscriberPtr<geometry_msgs::msg::dds_::Point32_> subscriber(
        new unitree::robot::ChannelSubscriber<geometry_msgs::msg::dds_::Point32_>("rt/hispeed_state"));
    subscriber->InitChannel(OnHeight, 1);
    for (int i = 0; i < 25; ++i) {
        std::this_thread::sleep_for(std::chrono::milliseconds(200));
        if (received.load(std::memory_order_acquire)) {
            std::cout << "column_height_m=" << std::fixed << std::setprecision(4)
                      << height_m.load(std::memory_order_relaxed) << '\n';
            return 0;
        }
    }
    std::cerr << "No rt/hispeed_state height feedback within 5 seconds.\n";
    return 1;
}
