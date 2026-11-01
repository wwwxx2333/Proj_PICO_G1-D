#pragma once
// Dex3-1 limits and motor order from SDK2 g1_dex3_example.cpp.
// SDK-independent command validation, ownership and watchdog.
#include <algorithm>
#include <array>
#include <cmath>
#include <iomanip>
#include <sstream>
#include <stdexcept>
#include <string>
#include "g1d_hand_grasp.hpp"

namespace dex3 {
enum class FeedbackSource { None, Fast, LowFrequency };
struct Feedback {
    double time=0;
    Joints q{};
    FeedbackSource source=FeedbackSource::None;
    Joints dq{}, tau_est{}; // Optional recording fields, unused by hand control.
};
inline constexpr Joints left_lower{-1.05,-0.724,0,-1.57,-1.75,-1.57,-1.75};
inline constexpr Joints left_upper{1.05,1.05,1.75,0,0,0,0};
inline constexpr Joints right_lower{-1.05,-1.05,-1.75,0,0,0,0};
inline constexpr Joints right_upper{1.05,0.742,0,1.57,1.75,1.57,1.75};
inline bool Fresh(double timestamp, double now) {
    return std::isfinite(timestamp) && timestamp > 0 && timestamp <= now && now - timestamp <= 0.25;
}
inline bool CanTakeover(double feedback_time, double input_time, bool new_feedback) {
    return new_feedback && std::isfinite(input_time) && input_time > 0 &&
           feedback_time >= input_time;
}
inline Feedback SelectFeedback(const Feedback& fast, const Feedback& low_frequency, double now) {
    // Feedback age is diagnostic only. A missing packet must not revoke hand
    // ownership; the bridge freezes its target until another packet arrives.
    if (fast.time > now || !std::isfinite(fast.time)) return low_frequency.time <= now ? low_frequency : Feedback{};
    if (low_frequency.time > now || !std::isfinite(low_frequency.time)) return fast;
    return fast.time >= low_frequency.time ? fast : low_frequency;
}
inline bool Valid(const Joints& q, bool left, double margin=0) {
    const auto& lower = left ? left_lower : right_lower;
    const auto& upper = left ? left_upper : right_upper;
    for (size_t i=0; i<7; ++i)
        if (!std::isfinite(q[i]) || q[i] < lower[i]-margin || q[i] > upper[i]+margin) return false;
    return true;
}
inline std::string FeedbackFault(const Joints& q, bool left, double timestamp,
                                 double now, double margin=0.05) {
    (void)now;
    std::ostringstream message;
    message << (left ? "left" : "right") << " Dex3 ";
    if (timestamp <= 0 || !std::isfinite(timestamp)) {
        message << "feedback unavailable (no valid 7-motor state received)";
        return message.str();
    }
    const auto& lower = left ? left_lower : right_lower;
    const auto& upper = left ? left_upper : right_upper;
    for (size_t i=0; i<q.size(); ++i) {
        if (!std::isfinite(q[i]) || q[i]<lower[i]-margin || q[i]>upper[i]+margin) {
            message << "motor[" << i << "] measured q=" << q[i]
                    << " rad outside [" << lower[i]-margin << ", "
                    << upper[i]+margin << "] rad";
            return message.str();
        }
    }
    return {};
}
class Guard {
public:
    explicit Guard(bool is_left): left(is_left), grasp(is_left) {}
    bool left, owned=false, tracking=false;
    Joints output{}, target{}, requested{};
    GraspLimiter grasp;
    bool target_active=false;
    double last_input=0;
    void Hold(const Joints& measured) {
        if (owned) output=target=measured;
        tracking=false;
        target_active=false;
        grasp.Reset();
    }
    void Release() {
        // Explicit stop of hand command publication.
        owned=tracking=false;
        last_input=0;
        target_active=false;
        grasp.Reset();
    }
    void Freeze() {
        // Keep publishing the last bounded command while measurements pause.
        if (owned) target=output;
        target_active=false;
        grasp.Pause();
    }
    bool Receive(const std::string& line, const Joints& measured, double now) {
        std::istringstream in(line);
        std::string verb, extra;
        in >> verb;
        if (verb == "HOLD") {
            if (in >> extra) throw std::runtime_error("invalid hand HOLD");
            if (tracking) Hold(measured);
            return false;
        }
        if (verb == "RELEASE") {
            if (in >> extra) throw std::runtime_error("invalid hand RELEASE");
            Release();
            return false;
        }
        double timestamp=0;
        if (!(in >> timestamp) || !Fresh(timestamp,now)) { Hold(measured); return false; }
        if (!Valid(measured,left,0.05)) throw std::runtime_error("invalid measured hand joints");
        if (verb == "ARM") {
            if (in >> extra) throw std::runtime_error("invalid hand ARM");
            const bool takeover = !owned;
            if (!tracking) {
                output=target=measured;
                requested=measured;
                target_active=false;
                grasp.Reset();
                owned=tracking=true;
                last_input=timestamp;
            }
            return takeover;
        }
        if (verb == "KEEPALIVE") {
            if (in >> extra) throw std::runtime_error("invalid hand KEEPALIVE");
            // Renew fresh operator input without replaying Python's older
            // takeover pose. Never acquire ownership or leave HOLD here.
            if (tracking) last_input=timestamp;
            return false;
        }
        if (verb != "TARGET") throw std::runtime_error("unknown hand command");
        Joints requested{};
        for (double& q: requested)
            if (!(in >> q)) throw std::runtime_error("incomplete hand target");
        if ((in >> extra) || !Valid(requested,left)) throw std::runtime_error("invalid hand target");
        if (tracking) {
            this->requested=target=requested;
            target_active=true;
            last_input=timestamp;
        }
        return false;
    }
    void Tick(const Joints& measured, double now, double dt, double feedback_time=-1) {
        if (!owned) return;
        // An operator-input timeout may coincide with a feedback gap. Keep
        // the last commanded pose instead of substituting an old measurement.
        if (tracking && !Fresh(last_input,now)) Hold(output);
        if (feedback_time<0) feedback_time=now; // Standalone guard tests.
        if (now-feedback_time>=GraspSettings::feedback_gap) Freeze();
        if (tracking && target_active) {
            target=grasp.Goal(requested,output,measured,feedback_time);
            const auto& lower=left ? left_lower : right_lower;
            const auto& upper=left ? left_upper : right_upper;
            for (size_t i=0;i<7;++i) target[i]=std::clamp(target[i],lower[i],upper[i]);
        }
        // If external motion suddenly moves feedback outside the lead window,
        // return toward that window at this speed, never jump the output.
        const double step=GraspSettings::command_speed*std::clamp(dt,0.0,0.02);
        for (size_t i=0;i<7;++i)
            output[i]+=std::clamp(target[i]-output[i],-step,step);
    }
};
}
