"""Conservative terminal filtering: hide only recognized high-rate status lines."""
import re

ANSI = re.compile(r'\x1b\[[0-?]*[ -/]*[@-~]')


class TerminalFilter:
    def __init__(self, full=False, wait_repeat_s=10.0):
        self.full, self.wait_repeat_s = full, wait_repeat_s
        self.states = {}

    def changed(self, channel, key, now, repeat=False):
        previous, last = self.states.get(channel, (None, -float('inf')))
        if key != previous or (repeat and now-last >= self.wait_repeat_s):
            self.states[channel] = key, now
            return True
        return False

    def select(self, text, now):
        if self.full:
            return [text]
        line = text.strip()
        match = re.match(r'^(ARMED|DISARMED)(/NO FRESH PICO DATA)? L=\(', line)
        if match:
            state = match.group(0).split(' L=')[0]
            messages = {'ARMED': 'CONTROL ARMED 已启用遥操作；B 暂停。',
                        'DISARMED': 'CONTROL DISARMED 已暂停；满足接管条件后按 A。',
                        'DISARMED/NO FRESH PICO DATA': 'CONTROL WAIT_PICO 等待新鲜的 PICO 输入。'}
            if self.changed('control', state, now, repeat=state != 'ARMED'):
                return [messages.get(state, 'CONTROL ' + state)]
            return []
        if line.startswith('ARMS '):
            state = line[5:]
            if state.startswith('FOLLOW='):
                # Periodic values include a real pitch fault that must stay visible.
                match = re.search(r'PITCH_NOT_FOLLOWING/\w+', state)
                fault = match.group(0) if match else 'FOLLOW'
                changed = self.changed('arms', fault, now, repeat=bool(match))
                return ['ARMS ' + fault] if changed and match else []
            if state.startswith('HOMING '):
                match = re.search(r'HOMING (\d+)%', state)
                key = ('HOMING', int(match.group(1)) // 10) if match else state
            elif state.startswith('HOME_COUNTDOWN'):
                match = re.search(r'HOME_COUNTDOWN (\d+(?:\.\d+)?)', state)
                key = ('HOME_COUNTDOWN', int(float(match.group(1)))) if match else state
            else:
                key = state
            return [text] if self.changed('arms', key, now, repeat=True) else []
        if line.startswith('RIGHT_Y='):
            return [text] if self.changed('mode', line, now, repeat='WAIT_CENTER' in line) else []
        if line.startswith('DEX3 TRIGGER '):
            return []
        if line.startswith('DEX3 '):
            return [text] if self.changed('hands', line, now, repeat=True) else []
        if line.startswith('LIFT '):
            status = line.split()[1] if len(line.split()) > 1 else line
            return ['LIFT ' + status] if self.changed('lift', status, now, repeat=status != 'READY') else []
        if (line.startswith('AGV TRACE ') or line.startswith('Dex3 mode t=')
                or re.match(r'^(left|right) Dex3 (feedback source:|diagnostics:)', line)):
            return []
        # Unknown output, warnings and all multiline traceback lines remain visible.
        return [text]
