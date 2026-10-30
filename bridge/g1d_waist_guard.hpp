#pragma once
// SDK-independent yaw/pitch trajectories. Both arms stay relative to the torso.
#include <algorithm>
#include <array>
#include <cmath>
#include <stdexcept>

namespace g1d {
// Bitmask: an angle boundary and following-error boundary can coincide.
enum PitchLimit { PitchUnlimited=0, PitchAngleMin=1, PitchAngleMax=2, PitchFollowError=4 };
enum class PitchFollowing { Following=0, WaitCenter=1, WaitPush=2 };
using Waist = std::array<double, 2>; // lowstate[12] yaw + dedicated waistpitchstate pitch.
inline bool ValidWaist(const Waist& q) {
    return std::isfinite(q[0]) && std::isfinite(q[1]) &&
           std::abs(q[0])<=2.7053+0.02 && q[1]>=-0.04363-0.02 && q[1]<=2.3562+0.02;
}

class WaistGuard {
public:
    WaistGuard(double limit=0.5235987756, double speed=0.10, double acceleration=0.20,
               double pitch_min=0.0, double pitch_max=0.5235987756,
               double pitch_speed=0.08, double pitch_acceleration=0.16,
               double pitch_max_error=0.10, double pitch_stall_timeout=1.5,
               double pitch_min_progress=0.005)
        : limit_rad(limit), speed_rad_s(speed), acceleration_rad_s2(acceleration),
          pitch_min_rad(pitch_min), pitch_max_rad(pitch_max),
          pitch_speed_rad_s(pitch_speed), pitch_acceleration_rad_s2(pitch_acceleration),
          pitch_max_error_rad(pitch_max_error), pitch_stall_timeout_s(pitch_stall_timeout),
          pitch_min_progress_rad(pitch_min_progress) {
        if (!std::isfinite(limit) || limit<0.087 || limit>1.0472 ||
            !std::isfinite(speed) || speed<0.02 || speed>0.20 ||
            !std::isfinite(acceleration) || acceleration<0.05 || acceleration>0.50)
            throw std::runtime_error("invalid waist yaw limit/speed/acceleration");
        // Pitch model limits are asymmetric: -0.04363..2.3562 rad, not +/-yaw.
        if (!std::isfinite(pitch_min) || pitch_min< -0.02 || pitch_min>0.0 ||
            !std::isfinite(pitch_max) || pitch_max<0.087 || pitch_max>1.0472 ||
            !std::isfinite(pitch_speed) || pitch_speed<0.01 || pitch_speed>0.15 ||
            !std::isfinite(pitch_acceleration) || pitch_acceleration<0.02 || pitch_acceleration>0.30)
            throw std::runtime_error("invalid waist pitch limits/speed/acceleration");
        if (!std::isfinite(pitch_max_error) || pitch_max_error<0.05 || pitch_max_error>0.15 ||
            !std::isfinite(pitch_stall_timeout) || pitch_stall_timeout<0.5 || pitch_stall_timeout>5.0 ||
            !std::isfinite(pitch_min_progress) || pitch_min_progress<0.001 || pitch_min_progress>0.02)
            throw std::runtime_error("invalid pitch following-error/progress parameters");
    }
    const double limit_rad, speed_rad_s, acceleration_rad_s2;
    const double pitch_min_rad, pitch_max_rad, pitch_speed_rad_s, pitch_acceleration_rad_s2;
    const double pitch_max_error_rad, pitch_stall_timeout_s, pitch_min_progress_rad;
    Waist output{};
    double velocity=0, pitch_velocity=0;
    bool captured=false, limited=false, pitch_limited=false;
    int pitch_limit=PitchUnlimited;
    PitchFollowing pitch_following=PitchFollowing::Following;
    double pitch_requested_rate=0, pitch_stall_elapsed_s=0, pitch_stall_error_rad=0;

    void Capture(const Waist& measured) {
        if (!ValidWaist(measured)) throw std::runtime_error("waist feedback outside model limits");
        // Never jump to zero or a software boundary on takeover. If initially
        // outside the software window, only inward movement is permitted.
        output=measured;
        captured=true;
        Stop();
        // A/B or homing must not bypass a latched local pitch failure.
        if (pitch_following!=PitchFollowing::Following) pitch_following=PitchFollowing::WaitCenter;
    }
    void StopPitch() {
        pitch_velocity=0; pitch_limited=false; pitch_limit=PitchUnlimited;
        pitch_requested_rate=0;
        ResetProgress();
    }
    void Stop() { velocity=0; limited=false; StopPitch(); }
    void Tick(const Waist& measured, double requested_rate, bool tracking, double dt,
              double requested_pitch_rate=0.0, bool pitch_enabled=false,
              double input_timestamp=0.0) {
        if (!captured) return;
        if (!ValidWaist(measured)) throw std::runtime_error("invalid waist feedback");
        if (!std::isfinite(requested_rate) || std::abs(requested_rate)>speed_rad_s+1e-9)
            throw std::runtime_error("waist velocity exceeds configured limit");
        if (!std::isfinite(requested_pitch_rate) ||
            std::abs(requested_pitch_rate)>pitch_speed_rad_s+1e-9 ||
            (!pitch_enabled && requested_pitch_rate!=0.0))
            throw std::runtime_error("waist pitch velocity exceeds configured limit or is disabled");
        if (!tracking) { Stop(); return; }
        if (!std::isfinite(dt) || dt<0 || !std::isfinite(input_timestamp) || input_timestamp<0)
            throw std::runtime_error("invalid waist control timing");
        const double elapsed=dt; // Progress uses real elapsed time, not trajectory dt.
        dt=std::clamp(dt,0.,0.02);
        Advance(output[0],velocity,limited,measured[0],requested_rate,
                -limit_rad,limit_rad,acceleration_rad_s2,0.10,dt);
        // A mode switch stops pitch integration immediately; ordinary stick
        // release in PITCH mode still decelerates smoothly. Keep the held angle.
        if (!pitch_enabled) { StopPitch(); return; }
        pitch_requested_rate=requested_pitch_rate;
        if (!RecoverPitch(measured[1],requested_rate,input_timestamp)) return;
        AdvancePitch(measured[1],dt);
        MonitorPitchProgress(measured[1],elapsed,input_timestamp);
    }

private:
    int progress_direction=0;
    double progress_reference=0, recovery_input_timestamp=0;

    void ResetProgress() {
        progress_direction=0;
        if (pitch_following==PitchFollowing::Following) pitch_stall_elapsed_s=0;
    }

    bool RecoverPitch(double measured, double yaw_request, double input_timestamp) {
        if (pitch_following==PitchFollowing::Following) return true;
        pitch_velocity=0; pitch_limited=false; pitch_limit=PitchUnlimited;
        // Only a later fresh PITCH-mode command with BOTH right axes at centre
        // acknowledges a stop. Disabled/LIFT/homing zero commands never count.
        if (input_timestamp<=recovery_input_timestamp) return false;
        if (pitch_following==PitchFollowing::WaitCenter) {
            if (pitch_requested_rate==0 && yaw_request==0) {
                pitch_following=PitchFollowing::WaitPush;
                recovery_input_timestamp=input_timestamp;
            }
            return false;
        }
        if (pitch_requested_rate==0) return false;
        pitch_following=PitchFollowing::Following;
        output[1]=measured; // Resume from feedback, never a queued target.
        pitch_stall_error_rad=0;
        ResetProgress();
        return true;
    }

    void AdvancePitch(double measured, double dt) {
        pitch_velocity+=std::clamp(pitch_requested_rate-pitch_velocity,
                                   -pitch_acceleration_rad_s2*dt,pitch_acceleration_rad_s2*dt);
        const double desired=output[1]+pitch_velocity*dt;
        // If starting outside the configured window, hold the starting angle
        // and allow only inward movement; never snap to a software boundary.
        const double angle_lower=std::min(output[1],pitch_min_rad);
        const double angle_upper=std::max(output[1],pitch_max_rad);
        const double follow_lower=std::min(output[1],measured-pitch_max_error_rad);
        const double follow_upper=std::max(output[1],measured+pitch_max_error_rad);
        pitch_limit=PitchUnlimited;
        if (desired<angle_lower-1e-12) pitch_limit|=PitchAngleMin;
        if (desired>angle_upper+1e-12) pitch_limit|=PitchAngleMax;
        if (desired<follow_lower-1e-12 || desired>follow_upper+1e-12) pitch_limit|=PitchFollowError;
        output[1]=std::clamp(desired,std::max(angle_lower,follow_lower),std::min(angle_upper,follow_upper));
        pitch_limited=pitch_limit!=PitchUnlimited;
        if (pitch_limited) pitch_velocity=0;
    }

    void MonitorPitchProgress(double measured, double elapsed, double input_timestamp) {
        const int direction=(pitch_requested_rate>0)-(pitch_requested_rate<0);
        const double directed_error=direction*(output[1]-measured);
        const bool angle_stop=(direction>0 && (pitch_limit & PitchAngleMax)) ||
                              (direction<0 && (pitch_limit & PitchAngleMin));
        // A long control scheduling gap cannot count as proven lack of progress.
        // The transport independently checks both feedback streams and input age.
        if (!direction || angle_stop || elapsed>0.25 ||
            directed_error<pitch_max_error_rad-pitch_min_progress_rad) {
            ResetProgress();
            return;
        }
        if (progress_direction!=direction) {
            ResetProgress();
            if (!(pitch_limit & PitchFollowError)) return;
            progress_direction=direction;
            progress_reference=measured;
            return;
        }
        // Receipt of another sample is not motion. Only measured progress in
        // the requested direction resets the window; wrong-way motion does not.
        if (direction*(measured-progress_reference)>=pitch_min_progress_rad) {
            progress_reference=measured;
            pitch_stall_elapsed_s=0;
            return;
        }
        pitch_stall_elapsed_s+=elapsed;
        if (pitch_stall_elapsed_s+1e-9<pitch_stall_timeout_s) return;
        pitch_stall_error_rad=output[1]-measured;
        pitch_following=PitchFollowing::WaitCenter;
        recovery_input_timestamp=input_timestamp;
        output[1]=measured; // Cancel catch-up target, keep supporting current pose.
        pitch_velocity=0; pitch_limited=false; pitch_limit=PitchUnlimited;
        progress_direction=0;
    }

    static void Advance(double& position, double& speed, bool& clipped,
                        double measured, double requested, double minimum, double maximum,
                        double acceleration, double lead, double dt) {
        speed+=std::clamp(requested-speed,-acceleration*dt,acceleration*dt);
        const double desired=position+speed*dt;
        // Bound command lead if the joint lags. Start outside the software
        // window without snapping to its boundary; allow inward motion only.
        const double lower=std::max(minimum,measured-lead);
        const double upper=std::min(maximum,measured+lead);
        const double next=std::clamp(desired,std::min(position,lower),std::max(position,upper));
        clipped=std::abs(next-desired)>1e-12;
        if (clipped) speed=0;
        position=next;
    }
};
}
