// Offline SDK message regression: constructs IDL values, never starts DDS.
#include "../bridge/g1d_upper_body_io.hpp"
#include <cassert>
#include <iostream>
#include <limits>
#include <vector>

using namespace unitree_hg::msg::dds_;

void TestDedicatedFeedbackAndIndependentTimestamps() {
    LowState_ low{};
    low.mode_machine() = 3;
    low.motor_state()[12].q() = 0.2f;
    low.motor_state()[14].q() = std::numeric_limits<float>::quiet_NaN();
    low.motor_state()[15].q() = 0.1f;
    low.motor_state()[15].dq() = 0.25f;
    low.motor_state()[15].tau_est() = 1.5f;
    low.motor_state()[16].dq() = std::numeric_limits<float>::quiet_NaN();
    low.crc() = g1d::HgCrc(&low, (sizeof(low) >> 2)-1);
    g1d::UpperBodyFeedback feedback;
    assert(g1d::DecodeArmState(low, 10.0, feedback.arm));
    assert(!feedback.fresh(10.01)); // lowstate alone is not enough to take over.
    assert(feedback.pitch.received_at == 0);

    MotorState_ pitch{};
    pitch.q() = 0.3f;
    assert(g1d::DecodePitchState(pitch, 9.99, feedback.pitch));
    assert(feedback.fresh(10.01));
    assert(std::abs(feedback.waist()[0]-0.2)<1e-6);
    assert(std::abs(feedback.waist()[1]-0.3)<1e-6);
    assert(feedback.arm.mode == 3);
    assert(feedback.arm.dq[0] == .25 && feedback.arm.tau_est[0] == 1.5);
    assert(std::isnan(feedback.arm.dq[1])); // Optional dq does not reject valid q.

    // New lowstate must never refresh a stopped pitch stream or substitute [14].
    low.motor_state()[14].q() = 1.0f;
    low.crc() = g1d::HgCrc(&low, (sizeof(low) >> 2)-1);
    assert(g1d::DecodeArmState(low, 10.4, feedback.arm));
    assert(!feedback.fresh(10.4));
    assert(feedback.pitch.received_at == 9.99);
    assert(std::abs(feedback.waist()[1]-0.3)<1e-6);
    pitch.q() = std::numeric_limits<float>::quiet_NaN();
    assert(!g1d::DecodePitchState(pitch, 10.5, feedback.pitch));
    assert(feedback.pitch.received_at == 9.99);
    // Conversely, pitch messages cannot refresh missing arm/yaw feedback.
    pitch.q() = 0.4f;
    assert(g1d::DecodePitchState(pitch, 11.0, feedback.pitch));
    assert(!feedback.fresh(11.0));
    low.crc() ^= 1;
    assert(!g1d::DecodeArmState(low, 11.0, feedback.arm));
    assert(feedback.arm.received_at == 10.4);
}

void TestCommandsUseSeparateOfficialInterfaces() {
    g1d::ArmGuard arms;
    arms.output[0] = 0.1;
    arms.output[13] = -0.2;
    arms.torque[0] = 2;
    const auto arm = g1d::MakeArmCommand(arms, 0.4, 3);
    const auto pitch = g1d::MakePitchCommand(0.3);
    assert(arm.mode_machine() == 3 && arm.mode_pr() == 0);
    assert(arm.crc() == g1d::HgCrc(&arm, (sizeof(arm) >> 2)-1));
    for (int i=0; i<29; ++i) {
        const auto& motor = arm.motor_cmd()[i];
        const bool active = (i==12 || i>=15);
        assert(motor.mode() == (active ? 1 : 0));
        if (!active) {
            assert(motor.kp() == 0 && motor.kd() == 0 && motor.tau() == 0 && motor.q() == 0);
        }
    }
    assert(std::abs(arm.motor_cmd()[12].q()-0.4)<1e-6);
    assert(std::abs(arm.motor_cmd()[15].q()-0.1)<1e-6);
    assert(std::abs(arm.motor_cmd()[28].q()+0.2)<1e-6);
    assert(arm.motor_cmd()[15].tau() == 2);
    assert(std::string(g1d::kPitchCommandTopic) == "rt/waistpitchcmd");
    assert(std::string(g1d::kPitchStateTopic) == "rt/waistpitchstate");
    assert(pitch.mode() == 1 && std::abs(pitch.q()-0.3)<1e-6);
    assert(pitch.kp() == 500 && pitch.kd() == 12 && pitch.dq() == 0 && pitch.tau() == 0);
    bool rejected = false;
    try { g1d::MakePitchCommand(std::numeric_limits<double>::quiet_NaN()); }
    catch (const std::runtime_error&) { rejected = true; }
    assert(rejected);
}

std::string Telemetry(bool stalled=false) {
    g1d::UpperBodyFeedback state;
    g1d::ArmGuard arm;
    g1d::WaistGuard waist;
    state.arm.received_at = 100;
    state.arm.yaw = .2;
    assert(g1d::FormatUpperBodyState(state, arm, waist).empty());
    state.pitch = {99.98, .3};
    arm.Receive("ARM 100", state.arm.q, 100);
    waist.Capture(state.waist());
    const auto takeover = g1d::MakePitchCommand(waist.output[1]);
    assert(std::abs(takeover.q()-.3)<1e-6); // Never jump to zero on takeover.
    if (stalled) {
        for(int i=0;i<800;++i) waist.Tick(state.waist(),0,true,.005,-.08,true,100+i*.005);
        assert(waist.pitch_following==g1d::PitchFollowing::WaitCenter);
        assert(waist.output[1]==state.pitch.q);
    }
    const auto text = g1d::FormatUpperBodyState(state, arm, waist);
    std::istringstream stream(text);
    std::vector<std::string> fields;
    for (std::string field; stream >> field;) fields.push_back(field);
    assert(fields.size()==31 && fields[0]=="STATE6");
    assert(std::stod(fields[18])==.3 && std::stod(fields[25])==99.98);
    assert(std::stoi(fields[26])==0 && std::stoi(fields[27])==(stalled ? 1 : 0));
    if(stalled) {
        assert(std::stod(fields[28])==-.08 && std::stod(fields[29])>=1.5);
        assert(std::abs(std::stod(fields[30])+.1)<1e-9);
    }
    return text;
}

int main(int argc, char** argv) {
    TestDedicatedFeedbackAndIndependentTimestamps();
    TestCommandsUseSeparateOfficialInterfaces();
    const auto line = Telemetry();
    const auto stopped = Telemetry(true);
    if (argc > 1) std::cout << (std::string(argv[1])=="--emit-stalled" ? stopped : line);
}
