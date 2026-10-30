#pragma once
// SDK-independent validation and rate limiting. Joint order: left 15..21, right 22..28.
#include <algorithm>
#include <array>
#include <cmath>
#include <sstream>
#include <stdexcept>
#include <string>

namespace g1d {
using Joints = std::array<double, 14>;
// Official G1-D URDF limits; retain 0.02 rad margin at both ends.
inline constexpr Joints lower{-3.0892,-1.5882,-2.618,-1.0472,-1.972222054,-1.614429558,-1.614429558,
                               -3.0892,-2.2515,-2.618,-1.0472,-1.972222054,-1.614429558,-1.614429558};
inline constexpr Joints upper{2.6704,2.2515,2.618,2.0944,1.972222054,1.614429558,1.614429558,
                              2.6704,1.5882,2.618,2.0944,1.972222054,1.614429558,1.614429558};
inline bool Valid(const Joints& q) {
    for (size_t i = 0; i < q.size(); ++i)
        if (!std::isfinite(q[i]) || q[i] < lower[i] + 0.02 || q[i] > upper[i] - 0.02) return false;
    return true;
}
inline bool Fresh(double timestamp, double now) {
    return std::isfinite(timestamp) && timestamp > 0 && timestamp <= now && now - timestamp <= 0.25;
}

class ArmGuard {
public:
    explicit ArmGuard(double speed = 0.30) : speed_rad_s(speed) {
        if (!std::isfinite(speed) || speed < 0.05 || speed > 1.2)
            throw std::runtime_error("arm speed must be within 0.05..1.2 rad/s");
    }
    const double speed_rad_s;
    bool owned = false;
    bool tracking = false;
    Joints output{}, target{}, torque{}, target_torque{};
    double last_input = 0;
    double waist_rate = 0;
    double pitch_rate = 0;
    bool pitch_enabled = false;

    void HoldPitch() { pitch_rate=0; pitch_enabled=false; }

    void Hold(const Joints& measured) {
        if (owned) output = target = measured;
        target_torque = torque; // Retain the last bounded support torque in HOLD.
        tracking = false;
        waist_rate = 0;
        HoldPitch();
    }
    // Return true only on a new takeover, when the transport must capture waist/mode.
    bool Receive(const std::string& line, const Joints& measured, double now) {
        std::istringstream input(line);
        std::string verb, extra;
        double timestamp = 0;
        input >> verb;
        if (verb == "HOLD") {
            if (input >> extra) throw std::runtime_error("invalid HOLD");
            if (tracking) Hold(measured);
            return false;
        }
        if (verb == "PITCH_HOLD") {
            if (input >> extra) throw std::runtime_error("invalid PITCH_HOLD");
            HoldPitch(); // Does not enable ownership or refresh the input watchdog.
            return false;
        }
        if (!(input >> timestamp) || !Fresh(timestamp, now)) {
            Hold(measured);
            return false;
        }
        if (!Valid(measured)) throw std::runtime_error("measured joints outside limits");
        if (verb == "ARM") {
            if (input >> extra) throw std::runtime_error("invalid ARM");
            const bool takeover = !owned;
            // Duplicate ARM messages cannot reset an active trajectory.
            if (!tracking) {
                output = target = measured;
                owned = tracking = true;
                last_input = timestamp;
                waist_rate = 0;
                HoldPitch();
            }
            return takeover;
        }
        if (verb != "TARGET6") throw std::runtime_error("unknown arm command (TARGET6 required)");
        Joints requested{};
        for (double& value : requested)
            if (!(input >> value)) throw std::runtime_error("incomplete arm target");
        Joints requested_torque{};
        for (size_t i = 0; i < requested_torque.size(); ++i) {
            const double limit = (i % 7 >= 5) ? 5.0 : 20.0;
            if (!(input >> requested_torque[i]) || !std::isfinite(requested_torque[i]) ||
                std::abs(requested_torque[i]) > limit)
                throw std::runtime_error("invalid or excessive gravity torque");
        }
        double requested_waist_rate=0;
        if (!(input >> requested_waist_rate) || !std::isfinite(requested_waist_rate) ||
            std::abs(requested_waist_rate)>0.20)
            throw std::runtime_error("invalid waist yaw velocity");
        double requested_pitch_rate=0;
        int requested_pitch_enabled=0;
        if (!(input >> requested_pitch_rate >> requested_pitch_enabled) ||
            !std::isfinite(requested_pitch_rate) || std::abs(requested_pitch_rate)>0.15 ||
            (requested_pitch_enabled!=0 && requested_pitch_enabled!=1) ||
            (requested_pitch_enabled==0 && requested_pitch_rate!=0.0))
            throw std::runtime_error("invalid waist pitch velocity/enable flag");
        if ((input >> extra) || !Valid(requested)) throw std::runtime_error("invalid arm target");
        if (tracking) {
            target = requested;
            target_torque = requested_torque;
            last_input = timestamp;
            waist_rate = requested_waist_rate;
            pitch_rate = requested_pitch_rate;
            pitch_enabled = requested_pitch_enabled!=0;
        }
        return false;
    }
    void Tick(const Joints& measured, double now, double dt) {
        if (!owned) return;
        if (tracking && !Fresh(last_input, now)) Hold(measured);
        const double step = speed_rad_s * std::clamp(dt, 0.0, 0.02);
        for (size_t i = 0; i < output.size(); ++i) {
            output[i] += std::clamp(target[i] - output[i], -step, step);
            const double torque_step = 20.0 * std::clamp(dt, 0.0, 0.02);
            torque[i] += std::clamp(target_torque[i] - torque[i], -torque_step, torque_step);
        }
    }
};
} // namespace g1d
