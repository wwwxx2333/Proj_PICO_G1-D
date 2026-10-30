#pragma once
// Pure height-window filtering for the official HeightAdjust velocity API.
#include <algorithm>
#include <cmath>

namespace g1d {
enum class LiftStatus { Ready, NoFeedback, OutsideWindow, UpperLimit, LowerLimit };
inline const char* LiftStatusName(LiftStatus status) {
    switch (status) {
        case LiftStatus::Ready: return "READY";
        case LiftStatus::NoFeedback: return "NO_FEEDBACK";
        case LiftStatus::OutsideWindow: return "OUTSIDE_WINDOW";
        case LiftStatus::UpperLimit: return "UPPER_LIMIT";
        case LiftStatus::LowerLimit: return "LOWER_LIMIT";
    }
    return "UNKNOWN";
}
struct LiftResult { float speed; LiftStatus status; };
inline LiftResult FilterLift(float requested, float height, bool fresh, float lower, float upper) {
    if (!fresh || !std::isfinite(height)) return {0,LiftStatus::NoFeedback};
    if (height<lower || height>upper) return {0,LiftStatus::OutsideWindow};
    const float margin=std::min(0.01f,(upper-lower)*0.2f);
    if (requested>0 && height>=upper-margin) return {0,LiftStatus::UpperLimit};
    if (requested<0 && height<=lower+margin) return {0,LiftStatus::LowerLimit};
    return {requested,LiftStatus::Ready};
}
}
