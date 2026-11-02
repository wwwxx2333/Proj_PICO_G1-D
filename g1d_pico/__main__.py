"""Run PICO joystick control with the G1-D camera view."""

import argparse
from dataclasses import asdict
import math
from pathlib import Path
import time
import sys

from .bridge import AgvBridge
from .arm_config import ArmConfig
from .body_config import BodyConfig
from .config_paths import ARM_CONFIG, BODY_CONFIG
from .control import (MotionCommand, map_sticks, sticks_neutral,
                      waist_yaw_rate, waist_pitch_rate)
from .right_stick import RightStickMode
from .recording.telemetry import emit
from .recording.button import RecordingButton


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, allow_abbrev=False,
        epilog="Tuning is always read from project config/body.jsonc and config/arms.jsonc; "
               "edit those files and restart. CLI tuning overrides are not supported.")
    parser.add_argument("--full-log", action="store_true", help="Show all runtime logs; file logging is always complete")
    parser.add_argument("--robot-ip", default="127.0.0.1", help="TeleImager camera server (PC2 localhost by default)")
    parser.add_argument("--cert", help="TeleVuer HTTPS certificate (or XR_TELEOP_CERT)")
    parser.add_argument("--key", help="TeleVuer HTTPS private key (or XR_TELEOP_KEY)")
    parser.add_argument("--no-camera", action="store_true", help="PICO pass-through view; no robot camera needed")
    parser.add_argument("--live", action="store_true", help="Send commands to the SDK2 bridge")
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--base-only", action="store_true", help="AGV base movement only; disable column lift")
    modes.add_argument("--lift-only", action="store_true", help="In live mode, enable lift movement only")
    parser.add_argument("--bridge-bin", type=Path, default=Path(__file__).resolve().parents[1] / "bridge/build-arm64/g1d_agv_bridge")
    parser.add_argument("--network-interface", default="eth0", help="PC2 internal robot network interface")
    parser.add_argument("--with-hands", action="store_true", help="add left/right Dex3-1 trigger control; requires --with-arms")
    parser.add_argument("--hand-bridge-bin", type=Path, default=Path(__file__).resolve().parents[1] / "bridge/build-arm64/g1d_hand_bridge")
    parser.add_argument("--with-arms", action="store_true", help="G1-D arms and waist yaw/pitch on PC2, with base and optional lift")
    parser.add_argument("--arm-bridge-bin", type=Path, default=Path(__file__).resolve().parents[1] / "bridge/build-arm64/g1d_arm_bridge")
    parser.add_argument("--skip-arm-home", action="store_true", help="explicitly skip startup arm homing")
    parser.add_argument("--arm-urdf", type=Path, default=Path(__file__).resolve().parents[1] / "assets/g1d/g1_d.urdf")
    args = parser.parse_args()
    if (args.base_only or args.lift_only) and not args.live:
        parser.error("--base-only and --lift-only require --live")
    if args.with_hands and not args.with_arms:
        parser.error("--with-hands requires --with-arms")
    if args.with_arms and (not args.live or args.lift_only):
        parser.error("--with-arms requires --live with base or base+lift")
    # Paths come from this package, never from cwd or CLI overrides.
    args.body_config = BODY_CONFIG
    args.arm_config = ARM_CONFIG
    try:
        args.body_settings = BodyConfig.load(args.body_config)
    except (OSError, ValueError) as exc:
        parser.error(f'Invalid body config {args.body_config}: {exc}')
    args.height_min_m = args.body_settings.height_min_m
    args.height_max_m = args.body_settings.height_max_m
    return args


def _run_control() -> None:
    args = parse_args()
    arm_config = ArmConfig.load(args.arm_config) if args.with_arms else None
    body_config = args.body_settings
    config = body_config  # BodyConfig includes the validated joystick mapping.
    emit("control_start", {"with_arms": args.with_arms, "with_hands": args.with_hands,
                           "base_only": args.base_only, "live": args.live,
                           "body_config_path": str(args.body_config),
                           "body_config": asdict(body_config),
                           "arm_config_path": str(args.arm_config) if arm_config else None,
                           "arm_config": asdict(arm_config) if arm_config else None})
    # Keep optional dependencies out of the pure mapping and unit-test modules.
    from .input import FreshTeleVuer

    camera = None
    viewer = None
    bridge = None
    arms = None
    hands = None
    recording_button = None
    fatal_error = None
    try:
        recording_button = RecordingButton(notice=lambda text: print(text, flush=True))
        if args.no_camera:
            image_shape = (480, 640)
            binocular = False
            display_mode = "pass-through"
        else:
            from teleimager.image_client import ImageClient

            camera = ImageClient(host=args.robot_ip, request_bgr=True)
            head = camera.get_cam_config()["head_camera"]
            if not head.get("enable_zmq"):
                raise RuntimeError("robot head-camera ZMQ stream is disabled")
            image_shape = tuple(head["image_shape"])
            binocular = bool(head["binocular"])
            display_mode = "immersive"

        viewer = FreshTeleVuer(
            binocular=binocular,
            img_shape=image_shape,
            display_fps=30.0,
            display_mode=display_mode,
            zmq=not args.no_camera,
            webrtc=False,
            cert_file=args.cert,
            key_file=args.key,
        )
        if args.live:
            mode = "base" if args.base_only else "lift" if args.lift_only else "both"
            bridge = AgvBridge(args.bridge_bin, args.network_interface,
                               args.height_min_m, args.height_max_m, mode)
        if args.with_arms:
            from .arms import ArmSession
            arms = ArmSession(args.arm_bridge_bin, args.network_interface, args.arm_urdf,
                              startup_home=not args.skip_arm_home, config=arm_config,
                              body_config=body_config)
        if args.with_hands:
            from .hands import HandSession
            hands = HandSession(args.hand_bridge_bin, args.network_interface)

        mode = "LIVE" if args.live else "DRY RUN — robot motion is disabled"
        print(f"{mode}. Open TeleVuer on PICO at the Wi-Fi IP of this computer, port 8012.")
        print("Press right A with both sticks centred to arm; right B to disarm; Ctrl+C to exit.")
        print(f"Body config: {args.body_config}; forward={config.max_forward_mps} m/s, "
              f"yaw={config.max_yaw_rps} rad/s, lift={config.max_lift_normalized}, "
              f"deadzone={config.deadzone}")
        if args.with_arms:
            if not arms.home_complete:
                print("STARTUP HOME: once PICO is ready, 3s countdown then slow joint-zero homing. B cancels.")
                print("Base is locked until homing finishes. Keep arms unloaded and path clear.")
            print("BASE + ARMS + WAIST. Release grips and centre both sticks before A. Hold each grip to move that arm.")
            print("Left Y requests arm homing; release grips/centre sticks; B cancels; A resumes after home.")
            print(f"Arm config: {args.arm_config}; speed={arm_config.joint_speed_rad_s} rad/s, "
                  f"bridge={arm_config.bridge_speed_rad_s} rad/s, loop={arm_config.control_hz} Hz")
            if hands is not None:
                print("DEX3: left/right index trigger controls each hand; grips continue to move arms.")
                print("Release both triggers before A; hands keep measured pose until trigger is used.")
            print("Right stick left/right turns the waist; arms and camera move with the torso.")
            print(f"Waist yaw: +/-{math.degrees(body_config.waist_yaw_limit_rad):.1f} deg, "
                  f"speed={body_config.waist_yaw_speed_rad_s} rad/s; centre stick decelerates to hold.")
            print("Left X toggles right stick up/down: LIFT (default) <-> PITCH. Centre right stick after switching.")
            print(f"Waist pitch: [{math.degrees(body_config.waist_pitch_min_rad):.1f}, "
                  f"{math.degrees(body_config.waist_pitch_max_rad):.1f}] deg, "
                  f"speed={body_config.waist_pitch_speed_rad_s} rad/s; stick forward leans forward.")
            print(f"Pitch following: max_error={body_config.waist_pitch_max_error_rad} rad, "
                  f"timeout={body_config.waist_pitch_stall_timeout_s}s, "
                  f"min_progress={body_config.waist_pitch_min_progress_rad} rad. "
                  "After a local pitch hold, centre the right stick in PITCH mode, then push again.")
            if args.base_only:
                print("Column lift disabled in this launch (--without-lift or --base-only).")
            else:
                lift_margin = min(0.01, (args.height_max_m - args.height_min_m) * 0.2)
                print(f"Column lift ENABLED: right stick up/down; feedback bounds "
                      f"[{args.height_min_m}, {args.height_max_m}] m of rt/hispeed_state.y.")
                print(f"Lift directional stops: [{args.height_min_m + lift_margin:.3f}, "
                      f"{args.height_max_m - lift_margin:.3f}] m; inward motion allowed in end margins.")
            print("B stops base/lift and holds arms/waist; Y homes only the 14 arm joints.")
        elif args.base_only:
            print("BASE ONLY. Left stick: forward/back and turning. Lift, arms and hands are not controlled.")
        elif args.lift_only:
            print("LIFT ONLY. Right stick up/down: lift. Base, arms and hands are not controlled.")
        else:
            print("Left stick: forward/back and turning. Right stick up/down: lift. Right X: unused.")

        armed = False
        previous_a = False
        previous_y = False
        right_stick = RightStickMode()
        last_command = MotionCommand()
        last_report = 0.0
        period = 1.0 / arm_config.control_hz if arm_config is not None else 0.05
        while True:
            cycle_start = time.monotonic()
            if camera is not None:
                frame = camera.get_head_frame().bgr
                if frame is not None:
                    viewer.render_to_xr(frame)

            sample = viewer.read_sticks()
            poses = viewer.read_poses() if arms is not None else None
            fresh = sample.is_fresh(time.monotonic(), timeout_s=0.25)
            if (arms is not None and fresh and sample.home_button and not previous_y
                    and not sample.stop_button and (arms.home_complete or arms.home_cancelled)):
                armed = False
                # Stop the base before any homing work, including its countdown.
                if bridge is not None:
                    bridge.send(MotionCommand())
                if hands is not None:
                    hands.stop()
                arms.request_home()
            previous_y = sample.home_button
            homing = arms is not None and not arms.home_complete
            if arms is not None:
                changed = right_stick.update(
                    sample.mode_button, sample.right_x, sample.right_y, fresh=fresh,
                    allow_switch=not homing and not sample.stop_button and not sample.home_button,
                    deadzone=config.deadzone)
                if changed:
                    # Stop the old vertical axis before potentially slow IK. Keep
                    # the base's last planar velocity; X changes no base mapping.
                    if bridge is not None:
                        bridge.send(MotionCommand(last_command.forward_mps, last_command.yaw_rps, 0.0)
                                    if armed else MotionCommand())
                    arms.hold_pitch()
                    print(f"Left X: RIGHT_Y={right_stick.status}; centre right stick to continue", flush=True)
            if homing:
                armed = False
                if hands is not None:
                    hands.stop()
                command = MotionCommand()
                try:
                    arms.step_home(poses,
                                   neutral=fresh and sticks_neutral(sample.left_x, sample.left_y,
                                                                    sample.right_y, config.deadzone,
                                                                    right_x=sample.right_x),
                                   stop=sample.stop_button,
                                   retry=sample.start_button and not previous_a)
                except ValueError as exc:
                    arms.cancel_home(str(exc))
                previous_a = sample.start_button
            else:
                if not fresh or sample.stop_button:
                    armed = False
                elif (sample.start_button and not previous_a and
                      sticks_neutral(sample.left_x, sample.left_y, sample.right_y, config.deadzone,
                                     right_x=sample.right_x)):
                    try:
                        if arms is not None:
                            arms.start(poses)
                        if hands is not None:
                            try:
                                hands.start(poses)
                            except ValueError as exc:
                                hands.stop()
                                print(f'Dex3 not enabled; base and arms enabled: {exc}',
                                      flush=True)
                        armed = True
                    except ValueError as exc:
                        armed = False
                        if hands is not None:
                            hands.stop()
                        if arms is not None:
                            arms.stop()
                        print(f"Cannot enable arms/hands: {exc}", flush=True)
                previous_a = sample.start_button

                command = MotionCommand()
                requested_waist_rate = 0.0
                requested_pitch_rate = 0.0
                pitch_enabled = False
                if fresh and armed:
                    try:
                        command = map_sticks(sample.left_x, sample.left_y, sample.right_x, sample.right_y, config)
                        if args.base_only or (arms is not None and not right_stick.lift_selected):
                            command = MotionCommand(command.forward_mps, command.yaw_rps, 0.0)
                        elif args.lift_only:
                            command = MotionCommand(0.0, 0.0, command.lift_normalized)
                        if arms is not None:
                            requested_waist_rate = waist_yaw_rate(sample.right_x,
                                body_config.waist_yaw_speed_rad_s, config.deadzone)
                            pitch_enabled = right_stick.pitch_selected
                            if pitch_enabled:
                                requested_pitch_rate = waist_pitch_rate(sample.right_y,
                                    body_config.waist_pitch_speed_rad_s, config.deadzone)
                    except ValueError:
                        armed = False
                        command = MotionCommand()
                        requested_waist_rate = 0.0
                        requested_pitch_rate = 0.0
                        pitch_enabled = False

                if arms is not None:
                    try:
                        if not armed:
                            arms.stop()
                        arms.step(poses, waist_rate=requested_waist_rate,
                                  stick_timestamp=sample.received_at,
                                  pitch_rate=requested_pitch_rate, pitch_enabled=pitch_enabled)
                    except ValueError as exc:
                        armed = False
                        command = MotionCommand()
                        arms.stop()
                        if hands is not None:
                            hands.stop()
                        print(f"Arms HOLD; base stopped: {exc}", flush=True)
                if hands is not None:
                    try:
                        if not armed:
                            hands.stop()
                        hands.step(poses)
                    except ValueError as exc:
                        armed = False
                        command = MotionCommand()
                        hands.stop()
                        arms.stop()
                        print(f"Dex3 HOLD; base/arms stopped: {exc}", flush=True)

            if bridge is not None:
                bridge.send(command)
            recording_button.step(
                sample, control_ready=arms is None or arms.home_complete,
                preparation='' if arms is None or arms.home_complete else
                f'手臂启动归位尚未完成：{arms.status}')
            emit("control_cycle", {
                "cycle_start_monotonic_s": cycle_start,
                "sticks": sample, "pico_poses": poses,
                "pico_fresh": fresh, "armed": armed, "homing": homing,
                "right_y_mode": right_stick.status,
                "base_requested_command": command,
                "arm_status": arms.status if arms is not None else None,
                "hand_status": hands.status if hands is not None else None,
            })
            last_command = command
            if cycle_start - last_report >= 0.5:
                state = "ARMED" if armed else "DISARMED"
                if not fresh:
                    state += "/NO FRESH PICO DATA"
                print(
                    f"{state} L=({sample.left_x:+.2f},{sample.left_y:+.2f}) "
                    f"R=({sample.right_x:+.2f},{sample.right_y:+.2f}) "
                    f"-> vx={command.forward_mps:+.3f} m/s, "
                    f"yaw={command.yaw_rps:+.3f} rad/s, lift={command.lift_normalized:+.3f}",
                    flush=True,
                )
                if arms is not None:
                    print(f"RIGHT_Y={right_stick.status}" + (" (lift disabled)" if args.base_only else ""), flush=True)
                    print(f"ARMS {arms.status}", flush=True)
                if hands is not None:
                    print(f"DEX3 {hands.status}", flush=True)
                last_report = cycle_start
            time.sleep(max(0.0, period - (time.monotonic() - cycle_start)))
    except KeyboardInterrupt:
        print("Stopping.")
    except RuntimeError as exc:
        # A failed SDK2 subprocess is a latched fault. Do not resume motion
        # on the remaining hand or let the base keep its last command.
        fatal_error = exc
        print(f"Control fault: {exc}. Stopping base, arms and hands.", flush=True)
        if bridge is not None:
            try:
                bridge.send(MotionCommand())
            except (OSError, RuntimeError):
                pass
        if hands is not None:
            try:
                hands.stop()
            except (OSError, RuntimeError):
                pass
        if arms is not None:
            try:
                arms.stop()
            except (OSError, RuntimeError):
                pass
    finally:
        if recording_button is not None:
            recording_button.close()
        # A hand teardown failure must not skip arms, video, or recording cleanup.
        for name, resource in (('base', bridge), ('hands', hands), ('arms', arms),
                               ('viewer', viewer), ('camera', camera)):
            if resource is not None:
                try:
                    resource.close()
                except Exception as exc:
                    print(f'Shutdown error ({name}): {exc}', flush=True)
                    if fatal_error is None:
                        fatal_error = exc
    if fatal_error is not None:
        raise SystemExit(1)


def main() -> None:
    # Transfer holds this lock exclusively; any control launch holds it shared.
    # The lock is acquired before creating DDS clients and released after cleanup.
    from .recording.runner import CONTROL_LOCK
    from .recording.storage import FileLock
    with FileLock(CONTROL_LOCK, shared=True):
        _run_control()


if __name__ == "__main__":
    from .runtime_logging import active
    if not active() and sys.argv[1:2] != ['--help']:
        from .runtime_logging.runner import run_logged
        raise SystemExit(run_logged([sys.executable, '-u', '-m', 'g1d_pico', *sys.argv[1:]]))
    main()
