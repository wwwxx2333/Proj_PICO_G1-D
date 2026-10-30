#pragma once
// Position-based obstruction handling. This does not estimate contact force.
#include <algorithm>
#include <array>
#include <cmath>

namespace dex3 {
using Joints = std::array<double, 7>;

struct GraspSettings {
    // Initial position-control settings, not calibrated object-force limits.
    static constexpr double max_lead = 0.20;          // rad, requested vs measured
    static constexpr double blocked_error = 0.10;     // rad, sent vs measured
    static constexpr double blocked_speed = 0.05;     // rad/s
    static constexpr double blocked_duration = 0.15;  // s, consecutive fresh states
    static constexpr double intent_deadband = 0.02;   // rad, opening/reclosing intent
    static constexpr double feedback_gap = 0.18;      // s
    static constexpr double command_speed = 0.60;     // rad/s, previous effective cap
};

enum class GraspMode { Following, Blocked, Opening };

class GraspLimiter {
public:
    explicit GraspLimiter(bool left): direction{0, left?1.:-1., left?1.:-1.,
                                              left?-1.:1., left?-1.:1.,
                                              left?-1.:1., left?-1.:1.} {}
    std::array<GraspMode, 7> modes{};
    Joints velocity{};
    unsigned limited_mask=0;

    void Reset() {
        modes.fill(GraspMode::Following);
        Pause();
        limited_mask=0;
    }
    void Pause() {
        // Preserve a grasp anchor across a feedback gap, but never count that
        // gap as stationary contact or differentiate across it.
        candidate_since.fill(0);
        previous_time=0;
        velocity.fill(0);
    }
    unsigned Mask(GraspMode mode) const {
        unsigned result=0;
        for (size_t i=1;i<7;++i)
            if (modes[i]==mode) result|=1u<<i;
        return result;
    }
    Joints Goal(const Joints& requested, const Joints& sent, const Joints& measured,
                double feedback_time) {
        const bool new_sample=feedback_time>previous_time;
        const double dt=feedback_time-previous_time;
        const bool consecutive=new_sample && previous_time>0 &&
                               dt<=GraspSettings::feedback_gap;
        if (new_sample) {
            for (size_t i=0;i<7;++i) {
                const double raw=consecutive ? (measured[i]-previous_q[i])/dt : 0;
                const double weight=dt/(0.05+dt);
                velocity[i]=consecutive ? velocity[i]+weight*(raw-velocity[i]) : 0;
            }
            if (!consecutive) candidate_since.fill(0);
            previous_q=measured;
            previous_time=feedback_time;
        }

        Joints goal=requested;
        limited_mask=0;
        for (size_t i=1;i<7;++i) {
            // All flexion is positive in this coordinate, for either hand.
            const double request=direction[i]*requested[i];
            const double position=direction[i]*measured[i];
            const double command=direction[i]*sent[i];
            if (modes[i]==GraspMode::Blocked) {
                request_anchor[i]=std::max(request_anchor[i],request);
                if (request<request_anchor[i]-GraspSettings::intent_deadband) {
                    modes[i]=GraspMode::Opening;
                    minimum_request[i]=request;
                }
            } else if (modes[i]==GraspMode::Opening) {
                if (request>minimum_request[i]+GraspSettings::intent_deadband ||
                    request<=1e-6) {
                    modes[i]=GraspMode::Following;
                    candidate_since[i]=0;
                } else {
                    minimum_request[i]=std::min(minimum_request[i],request);
                }
            }

            if (modes[i]==GraspMode::Following && new_sample) {
                // Only a sustained, stationary, closing-direction error is an
                // obstruction candidate. Ordinary moving lag is not contact.
                const bool candidate=consecutive && request>position+GraspSettings::blocked_error &&
                    command-position>=GraspSettings::blocked_error &&
                    std::abs(velocity[i])<=GraspSettings::blocked_speed;
                if (!candidate) candidate_since[i]=0;
                else if (candidate_since[i]==0) candidate_since[i]=feedback_time;
                else if (feedback_time-candidate_since[i]>=GraspSettings::blocked_duration) {
                    modes[i]=GraspMode::Blocked;
                    // Capture once. Do not rebase on each new measurement:
                    // that would ratchet the grasp or erase its preload.
                    hold_position[i]=std::min(command,position+GraspSettings::max_lead);
                    request_anchor[i]=request;
                    candidate_since[i]=0;
                }
            }
            if (modes[i]==GraspMode::Blocked)
                goal[i]=direction[i]*hold_position[i];
            else if (modes[i]==GraspMode::Opening) {
                // A small trigger release opens from the held command, even
                // when the absolute trigger target still exceeds that command.
                const double opened=hold_position[i]+request-request_anchor[i];
                goal[i]=direction[i]*std::max(0.,opened);
            }
        }
        for (size_t i=0;i<7;++i) {
            const double bounded=std::clamp(goal[i],measured[i]-GraspSettings::max_lead,
                                                   measured[i]+GraspSettings::max_lead);
            if (std::abs(bounded-goal[i])>1e-9) limited_mask|=1u<<i;
            goal[i]=bounded;
        }
        return goal;
    }
private:
    Joints direction, previous_q{}, candidate_since{}, hold_position{}, request_anchor{}, minimum_request{};
    double previous_time=0;
};
}
