#pragma once
// SDK channels with explicit native DDS close on the owning thread.
// Retain the entity while close() waits for callbacks. Resetting the last
// shared_ptr alone can run the CycloneDDS destructor on a callback thread.
#include <iostream>
#include <unitree/robot/channel/channel_factory.hpp>

namespace g1d {
template<typename Message>
class ClosingChannel {
public:
    ClosingChannel() = default;
    ClosingChannel(const ClosingChannel&) = delete;
    ClosingChannel& operator=(const ClosingChannel&) = delete;

    ~ClosingChannel() noexcept {
        try { Close(); }
        catch (const std::exception& error) {
            std::cerr << "DDS close failed: " << error.what() << '\n';
        }
    }

    void Subscribe(const std::string& name, const std::function<void(const void*)>& callback) {
        channel_ = unitree::robot::ChannelFactory::Instance()->CreateRecvChannel<Message>(name, callback, 1);
    }

    void Advertise(const std::string& name) {
        channel_ = unitree::robot::ChannelFactory::Instance()->CreateSendChannel<Message>(name);
    }

    bool Write(const Message& message) {
        return channel_ && channel_->Write(message, 0);
    }

    void Close() {
        if (!channel_) return;
        if (auto reader = channel_->GetReader()) {
            auto native = reader->GetNative(); // Strong reference survives close().
            try { native.close(); }
            catch (const dds::core::AlreadyClosedError&) {} // Retry after a partial close.
        }
        if (auto writer = channel_->GetWriter()) {
            auto native = writer->GetNative();
            try { native.close(); }
            catch (const dds::core::AlreadyClosedError&) {}
        }
        // Native callbacks have stopped. SDK destruction now joins its queued
        // application-callback thread before its listener storage disappears.
        channel_.reset();
    }

private:
    unitree::robot::ChannelPtr<Message> channel_;
};
} // namespace g1d
