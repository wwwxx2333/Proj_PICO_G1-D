// Offline DDS lifetime regression: loopback only, isolated domain, state-only
// synthetic topic. Never uses robot state/command topics or motor commands.
#include "../bridge/g1d_dds_channel.hpp"
#include <unitree/idl/hg/HandState_.hpp>
#include <atomic>
#include <chrono>
#include <thread>

int main() {
    using State = unitree_hg::msg::dds_::HandState_;
    unitree::robot::ChannelFactory::Instance()->Init(230,"lo");
    std::atomic<int> callbacks{0};
    for (int cycle=0; cycle<30; ++cycle) {
        g1d::ClosingChannel<State> writer,reader;
        const auto topic="rt/g1d_offline_shutdown_test_"+std::to_string(cycle);
        reader.Subscribe(topic,[&](const void*) { ++callbacks; });
        writer.Advertise(topic);
        std::atomic<bool> running{true};
        std::thread sender([&] {
            State state{}; state.motor_state().resize(7);
            while(running.load()) {
                writer.Write(state);
                std::this_thread::sleep_for(std::chrono::milliseconds(1));
            }
        });
        std::this_thread::sleep_for(std::chrono::milliseconds(50));
        reader.Close(); // Keep messages arriving while callbacks are shut down.
        reader.Close(); // Idempotent.
        running=false;
        sender.join();
        writer.Close();
    }
    std::cout<<"DDS close cycles=30 callbacks="<<callbacks.load()<<'\n';
    return callbacks.load()>0 ? 0 : 1;
}
