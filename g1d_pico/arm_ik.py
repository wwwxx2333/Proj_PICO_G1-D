"""G1-D adaptation of Unitree G1_29_ArmIK (Apache-2.0).

Upstream: xr_teleoperate 817fb00c63cde15e5f24a0f8fa08e1e33ed89d3b.
Copyright 2025 HangZhou YuShu TECHNOLOGY CO., LTD.
See third_party/unitree_xr for attribution and adaptation notes.
"""
from collections import deque
from pathlib import Path
import time
import casadi as ca
import numpy as np
import pinocchio as pin
from pinocchio import casadi as cpin
from .arm_math import rigid_pose

JOINT_SUFFIXES = ("shoulder_pitch", "shoulder_roll", "shoulder_yaw", "elbow",
                  "wrist_roll", "wrist_pitch", "wrist_yaw")
JOINT_NAMES = tuple(f"{side}_{name}_joint" for side in ("left", "right") for name in JOINT_SUFFIXES)


class IKTargetRejected(ValueError):
    """Optimizer failed; keep the last accepted command, never its debug iterate."""


class DualArmIK:
    def __init__(self, urdf: Path, filter_weights=(0.4, 0.3, 0.2, 0.1)):
        self.full_model = pin.buildModelFromUrdf(str(urdf))
        full = self.full_model
        if any(not full.existJointName(n) for n in JOINT_NAMES):
            raise ValueError("URDF does not contain the expected G1-D 14 arm joints")
        self.full_data = full.createData()
        self.full_q_indices = [full.joints[full.getJointId(n)].idx_q for n in JOINT_NAMES]
        self.full_v_indices = [full.joints[full.getJointId(n)].idx_v for n in JOINT_NAMES]
        # URDF names are misleading: torso_Joint axis Z = lowstate yaw(12);
        # Yaw_Joint axis Y = dedicated waistpitchstate.q, not lowstate[14].
        self.waist_indices = [full.joints[full.getJointId(n)].idx_q
                              for n in ("torso_Joint", "Yaw_Joint")]
        locked = [i for i in range(1, full.njoints) if full.names[i] not in JOINT_NAMES]
        self.model = pin.buildReducedModel(full, locked, pin.neutral(full))
        self.indices = np.array([self.model.joints[self.model.getJointId(n)].idx_q for n in JOINT_NAMES])
        # G1-D model order is required to match the SDK's 15..28 order.
        if not np.array_equal(self.indices, np.arange(14)):
            raise ValueError("unexpected G1-D joint ordering")
        self.torso_id = self.model.getFrameId("torso_link")
        self.wrist_ids = []
        for side in ("left", "right"):
            # Same 5 cm operational-frame offset as upstream, not a calibrated TCP.
            frame = pin.Frame(f"{side}_ee", self.model.getJointId(f"{side}_wrist_yaw_joint"),
                              pin.SE3(np.eye(3), np.array([0.05, 0., 0.])), pin.OP_FRAME)
            self.wrist_ids.append(self.model.addFrame(frame))
        self.data = self.model.createData()
        self.lower = self.model.lowerPositionLimit + 0.02
        self.upper = self.model.upperPositionLimit - 0.02
        self.torque_limits = np.minimum(full.effortLimit[self.full_v_indices], 20.0)
        pin.framesForwardKinematics(self.model, self.data, pin.neutral(self.model))
        self.world_torso = self.data.oMf[self.torso_id].copy()
        self.filter_weights = np.asarray(filter_weights, dtype=float)
        self._history = deque(maxlen=len(self.filter_weights))
        self.last_solve_ms = 0.0
        self.last_residuals = [(0., 0.), (0., 0.)]
        self._build_optimizer()
        # Load IPOPT and allocate its first solve before any DDS process starts.
        zero = self.checked_q(np.zeros(14))
        self.solve(self.poses(zero), zero)
        self.reset(zero)

    def _build_optimizer(self):
        cmodel = cpin.Model(self.model)
        cdata = cmodel.createData()
        q = ca.SX.sym("q", 14)
        cpin.framesForwardKinematics(cmodel, cdata, q)
        targets = [ca.SX.sym(f"target_{i}", 4, 4) for i in range(2)]
        pos = ca.vertcat(*[cdata.oMf[f].translation - t[:3, 3]
                          for f, t in zip(self.wrist_ids, targets)])
        rot = ca.vertcat(*[cpin.log3(cdata.oMf[f].rotation @ t[:3, :3].T)
                          for f, t in zip(self.wrist_ids, targets)])
        error = ca.Function("arm_errors", [q, *targets], [pos, rot])
        self.opti = ca.Opti()
        self.var_q = self.opti.variable(14)
        self.previous = self.opti.parameter(14)
        self.targets = [self.opti.parameter(4, 4) for _ in range(2)]
        self.bound_lower = self.opti.parameter(14)
        self.bound_upper = self.opti.parameter(14)
        pos, rot = error(self.var_q, *self.targets)
        # Upstream G1_29_ArmIK objective and solver settings.
        self.opti.minimize(50 * ca.sumsqr(pos) + ca.sumsqr(rot)
                           + 0.02 * ca.sumsqr(self.var_q)
                           + 0.1 * ca.sumsqr(self.var_q - self.previous))
        self.opti.subject_to(self.opti.bounded(self.bound_lower, self.var_q, self.bound_upper))
        self.opti.solver("ipopt", {
            "expand": True, "detect_simple_bounds": True, "calc_lam_p": False,
            "print_time": False, "ipopt.sb": "yes", "ipopt.print_level": 0,
            "ipopt.max_iter": 30, "ipopt.tol": 1e-4,
            "ipopt.acceptable_tol": 5e-4, "ipopt.acceptable_iter": 5,
            "ipopt.warm_start_init_point": "yes", "ipopt.derivative_test": "none",
            "ipopt.jacobian_approximation": "exact", "ipopt.max_cpu_time": 0.08,
        })

    def checked_q(self, q):
        q = np.asarray(q, dtype=float)
        if q.shape != (14,) or not np.isfinite(q).all():
            raise ValueError("arm state must have 14 finite joint positions")
        if np.any(q < self.lower) or np.any(q > self.upper):
            raise ValueError("arm state lies outside URDF joint limits with margin")
        return q.copy()

    def reset(self, measured):
        # No history from a previous clutch, home or disarm may move an idle arm.
        q = self.checked_q(measured)
        self._history.clear()
        self._history.extend(q.copy() for _ in range(len(self.filter_weights)))

    def poses(self, q):
        pin.framesForwardKinematics(self.model, self.data, self.checked_q(q))
        return [(self.world_torso.inverse() * self.data.oMf[f]).homogeneous.copy()
                for f in self.wrist_ids]

    def solve(self, targets, seed, enabled=(True, True)):
        seed = self.checked_q(seed)
        targets = [rigid_pose(t) for t in targets]
        if not any(enabled):
            return seed
        lower, upper = self.lower.copy(), self.upper.copy()
        for side in range(2):
            if not enabled[side]:
                lower[side*7:side*7+7] = upper[side*7:side*7+7] = seed[side*7:side*7+7]
        self.opti.set_initial(self.var_q, seed)
        self.opti.set_value(self.previous, seed)
        self.opti.set_value(self.bound_lower, lower)
        self.opti.set_value(self.bound_upper, upper)
        for parameter, target in zip(self.targets, targets):
            self.opti.set_value(parameter, (self.world_torso * pin.SE3(target)).homogeneous)
        started = time.perf_counter()
        try:
            result = self.opti.solve()
            q = self.checked_q(np.asarray(result.value(self.var_q)).reshape(14))
        except (RuntimeError, ValueError) as exc:
            status = self.opti.stats().get("return_status", "unknown")
            raise IKTargetRejected(f"IPOPT {status}; last valid target retained") from exc
        finally:
            self.last_solve_ms = (time.perf_counter() - started) * 1000
        if np.max(np.abs(q - seed)) > 0.6:
            raise IKTargetRejected("IK joint change exceeds 0.60 rad")
        actual = self.poses(q)
        self.last_residuals = [(float(np.linalg.norm(a[:3, 3] - t[:3, 3])),
                               float(np.linalg.norm(pin.log3(t[:3, :3] @ a[:3, :3].T))))
                              for a, t in zip(actual, targets)]
        # Upstream accepts the optimized compromise; no 15 mm binary cutoff.
        self._history.append(q.copy())
        weights = self.filter_weights[::-1][-len(self._history):]
        filtered = np.average(np.array(self._history), axis=0, weights=weights)
        for side in range(2):
            if not enabled[side]:
                filtered[side*7:side*7+7] = seed[side*7:side*7+7]
        return self.checked_q(filtered)

    def gravity(self, q, waist):
        """Upstream static RNEA, evaluated with the G1-D measured waist angles."""
        waist = np.asarray(waist, dtype=float)
        if waist.shape != (2,) or not np.isfinite(waist).all():
            raise ValueError("invalid waist feedback for gravity compensation")
        full_q = pin.neutral(self.full_model)
        full_q[self.full_q_indices] = self.checked_q(q)
        full_q[self.waist_indices] = waist
        zeros = np.zeros(self.full_model.nv)
        tau = pin.rnea(self.full_model, self.full_data, full_q, zeros, zeros)[self.full_v_indices]
        if not np.isfinite(tau).all() or np.any(np.abs(tau) > self.torque_limits):
            raise ValueError("model gravity torque exceeds configured/URDF limits")
        return tau.copy()
