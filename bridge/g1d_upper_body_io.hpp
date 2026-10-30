#pragma once
// G1-D message conversion only: no DDS participants, sockets or robot I/O.
// Pitch uses the dedicated interface from official g1d_waist_example.cpp.
#include "g1d_arm_guard.hpp"
#include "g1d_waist_guard.hpp"
#include <cstdint>
#include <cstring>
#include <iomanip>
#include <unitree/idl/hg/LowCmd_.hpp>
#include <unitree/idl/hg/LowState_.hpp>
#include <unitree/idl/hg/MotorCmd_.hpp>
#include <unitree/idl/hg/MotorState_.hpp>

namespace g1d {
inline constexpr const char* kPitchCommandTopic = "rt/waistpitchcmd";
inline constexpr const char* kPitchStateTopic = "rt/waistpitchstate";

struct ArmFeedback {
    double received_at = 0;
    uint8_t mode = 0;
    Joints q{};
    double yaw = 0;
};
struct PitchFeedback {
    double received_at = 0; // Updated only by a valid dedicated pitch sample.
    double q = 0;
};
struct UpperBodyFeedback {
    ArmFeedback arm;
    PitchFeedback pitch;

    Waist waist() const { return {arm.yaw, pitch.q}; }
    bool fresh(double now) const {
        return Fresh(arm.received_at, now) && Fresh(pitch.received_at, now);
    }
};

inline uint32_t HgCrc(const void* message, size_t length) {
    uint32_t crc = 0xFFFFFFFF;
    for (size_t i = 0; i < length; ++i) {
        uint32_t data;
        std::memcpy(&data, static_cast<const char*>(message) + 4 * i, 4);
        for (uint32_t bit = 0x80000000; bit != 0; bit >>= 1) {
            crc = (crc & 0x80000000) ? (crc << 1) ^ 0x04c11db7 : crc << 1;
            if (data & bit) crc ^= 0x04c11db7;
        }
    }
    return crc;
}

inline bool DecodeArmState(const unitree_hg::msg::dds_::LowState_& state,
                           double now, ArmFeedback& output) {
    if (state.crc() != HgCrc(&state, (sizeof(state) >> 2) - 1)) return false;
    ArmFeedback next;
    next.received_at = now;
    next.mode = state.mode_machine();
    next.yaw = state.motor_state()[12].q();
    if (!std::isfinite(next.yaw)) return false;
    for (size_t i = 0; i < next.q.size(); ++i) {
        next.q[i] = state.motor_state()[15+i].q();
        if (!std::isfinite(next.q[i])) return false;
    }
    // lowstate[14] is intentionally not used as G1-D pitch feedback.
    output = next;
    return true;
}

inline bool DecodePitchState(const unitree_hg::msg::dds_::MotorState_& state,
                             double now, PitchFeedback& output) {
    if (!std::isfinite(state.q())) return false;
    output = {now, state.q()};
    return true;
}

inline unitree_hg::msg::dds_::LowCmd_ MakeArmCommand(const ArmGuard& guard,
                                                   double yaw, uint8_t mode) {
    unitree_hg::msg::dds_::LowCmd_ command{};
    command.mode_pr() = 0;
    command.mode_machine() = mode;
    auto& waist_yaw = command.motor_cmd()[12];
    waist_yaw.mode() = 1;
    waist_yaw.q() = yaw;
    waist_yaw.kp() = 60;
    waist_yaw.kd() = 1;
    for (size_t i = 0; i < guard.output.size(); ++i) {
        auto& motor = command.motor_cmd()[15+i];
        motor.mode() = 1;
        motor.q() = guard.output[i];
        motor.kp() = 40;
        motor.kd() = 1;
        motor.tau() = guard.torque[i];
    }
    // Slot 14, legs and waist roll remain inactive. Pitch has one dedicated
    // publisher; do not send a competing target through rt/lowcmd.
    command.crc() = HgCrc(&command, (sizeof(command) >> 2) - 1);
    return command;
}

inline unitree_hg::msg::dds_::MotorCmd_ MakePitchCommand(double q) {
    if (!ValidWaist({0, q})) throw std::runtime_error("invalid dedicated pitch target");
    unitree_hg::msg::dds_::MotorCmd_ command{};
    command.mode() = 1;
    command.q() = q;
    command.dq() = 0;
    command.tau() = 0;
    command.kp() = 500; // Official G1-D waist example; not arm kp=40.
    command.kd() = 12;
    // MotorCmd_ has no HG LowCmd CRC field.
    return command;
}

inline std::string FormatUpperBodyState(const UpperBodyFeedback& state,
                                        const ArmGuard& arm, const WaistGuard& waist) {
    // No STATE6 is emitted before a dedicated pitch sample has arrived.
    // Older samples retain their own source time so fresh lowstate cannot hide
    // a stopped waistpitchstate stream in Python.
    if (state.pitch.received_at <= 0) return {};
    std::ostringstream line;
    line << std::setprecision(12) << "STATE6 " << state.arm.received_at << ' '
         << (arm.tracking ? 1 : arm.owned ? 2 : 0);
    for (double q : state.arm.q) line << ' ' << q;
    for (double q : state.waist()) line << ' ' << q;
    line << ' ' << waist.output[0] << ' ' << waist.velocity << ' ' << (waist.limited ? 1 : 0);
    line << ' ' << waist.output[1] << ' ' << waist.pitch_velocity << ' ' << (waist.pitch_limited ? 1 : 0);
    line << ' ' << state.pitch.received_at << ' ' << waist.pitch_limit
         << ' ' << static_cast<int>(waist.pitch_following) << ' ' << waist.pitch_requested_rate
         << ' ' << waist.pitch_stall_elapsed_s << ' ' << waist.pitch_stall_error_rad << '\n';
    return line.str();
}
} // namespace g1d
