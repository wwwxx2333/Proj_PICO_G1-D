"""Startup joint-space homing, separate from Cartesian teleoperation.

Zero joint angles follow upstream ctrl_dual_arm_go_home. This is a joint
trajectory, not collision-free planning from an arbitrary entangled pose.
"""
import numpy as np


class HomeTrajectory:
    def __init__(self, start, goal, now, speed=0.15):
        self.start = np.asarray(start, dtype=float).copy()
        self.goal = np.asarray(goal, dtype=float).copy()
        self.started = now
        # max derivative of quintic smoothstep = 1.875.
        self.duration = max(4.0, 1.875 * float(np.max(np.abs(self.goal - self.start))) / speed)
        self.settled_since = None

    def step(self, now, measured):
        u = float(np.clip((now - self.started) / self.duration, 0, 1))
        blend = 10*u**3 - 15*u**4 + 6*u**5
        q = self.start + blend * (self.goal - self.start)
        close = u >= 1 and np.max(np.abs(measured - self.goal)) < 0.05
        self.settled_since = (now if self.settled_since is None else self.settled_since) if close else None
        done = self.settled_since is not None and now - self.settled_since >= 0.5
        if now - self.started > self.duration + 8.0:
            raise ValueError("homing timed out before measured joints reached zero")
        return q, done
