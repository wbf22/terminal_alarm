#!/usr/bin/env python3
import datetime
import json
import os
import sys
import threading
import time
import math
import wave
import struct
import subprocess

ALARMS_FILE = 'alarms.json'
BEEP_FILE = 'alarm_beep.wav'

# Simple ANSI colors
RESET = '\u001b[0m'
BOLD = '\u001b[1m'
GREEN = '\u001b[32m'
YELLOW = '\u001b[33m'
CYAN = '\u001b[36m'
RED = '\u001b[31m'

# Active popup tracking
ACTIVE_POPUPS = []
POPUPS_LOCK = threading.Lock()

def ensure_beep_file(path=BEEP_FILE, duration=1.0, freq=440.0, volume=0.5, sample_rate=44100):
    if os.path.exists(path):
        return
    # create a simple sine-wave WAV file
    n_samples = int(sample_rate * duration)
    wav_file = wave.open(path, 'w')
    wav_file.setparams((1, 2, sample_rate, n_samples, 'NONE', 'not compressed'))
    max_amp = 32767 * volume
    for i in range(n_samples):
        t = float(i) / sample_rate
        val = int(max_amp * math.sin(2 * math.pi * freq * t))
        wav_file.writeframes(struct.pack('<h', val))
    wav_file.close()


class AlarmManager:
    def __init__(self, path=ALARMS_FILE):
        self.path = path
        self.lock = threading.Lock()
        self.alarms = []
        self._load()

    def _load(self):
        if os.path.exists(self.path):
            try:
                with open(self.path, 'r') as f:
                    self.alarms = json.load(f)
            except Exception:
                self.alarms = []
        else:
            self.alarms = []

    def _save(self):
        with open(self.path, 'w') as f:
            json.dump(self.alarms, f, indent=2)

    def add_alarm(self, hour, minute, description):
        alarm = {
            'hour': int(hour),
            'minute': int(minute),
            'description': str(description),
            'triggered_date': None
        }
        with self.lock:
            self.alarms.append(alarm)
            self._save()

    def list_alarms(self):
        with self.lock:
            return list(self.alarms)

    def mark_triggered(self, idx, date_str):
        with self.lock:
            if 0 <= idx < len(self.alarms):
                self.alarms[idx]['triggered_date'] = date_str
                self._save()

    def delete_alarm(self, idx):
        """Delete alarm by internal index (0-based). Returns True if deleted."""
        with self.lock:
            if 0 <= idx < len(self.alarms):
                self.alarms.pop(idx)
                self._save()
                return True
        return False

def play_sound(path=BEEP_FILE):
    # best-effort cross-platform playback; suppress subprocess output
    if sys.platform.startswith('win'):
        try:
            import winsound
            winsound.PlaySound(path, winsound.SND_FILENAME | winsound.SND_ASYNC)
            return
        except Exception:
            pass
    # macOS
    if sys.platform == 'darwin':
        for cmd in (['afplay', path], ['play', path]):
            try:
                subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                return
            except Exception:
                pass
    # Linux / others
    for cmd in (['aplay', path], ['paplay', path], ['play', path]):
        try:
            subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return
        except Exception:
            pass
    # fallback: ASCII bell
    print('\a')

class AlertPopup(threading.Thread):
    """
    Simple background "popup" that prints a highlighted alert to the terminal repeatedly
    until acknowledged. This is intentionally lightweight and avoids GUI toolkits so it
    works in simple terminal environments.

    If a GUI notification tool is available, it will try to call it as well.
    """
    def __init__(self, time_str, description, beep_path=BEEP_FILE):
        super().__init__(daemon=True)
        self.time_str = time_str
        self.description = description
        self.beep_path = beep_path
        self._ack = threading.Event()
        # register
        with POPUPS_LOCK:
            ACTIVE_POPUPS.append(self)

    def ack(self):
        self._ack.set()

    def _attempt_desktop_notify(self):
        # try notify-send (linux), osascript (mac), or fallback
        if sys.platform.startswith('linux'):
            try:
                subprocess.Popen(['notify-send', 'Alarm', f'{self.time_str} - {self.description}'])
                return True
            except Exception:
                return False
        if sys.platform == 'darwin':
            try:
                subprocess.Popen(['osascript', '-e', f'display notification "{self.description}" with title "Alarm" subtitle "{self.time_str}"'])
                return True
            except Exception:
                return False
        # Windows desktop notifications are more involved; skip for now
        return False

    def run(self):
        # try desktop notify once
        self._attempt_desktop_notify()
        # print repeated terminal alert until ack
        try:
            while not self._ack.is_set():
                try:
                    play_sound(self.beep_path)
                except Exception:
                    pass
                for _ in range(5):
                    if self._ack.is_set():
                        break
                    time.sleep(1)
        finally:
            # unregister
            with POPUPS_LOCK:
                if self in ACTIVE_POPUPS:
                    ACTIVE_POPUPS.remove(self)
            # final short beep
            try:
                play_sound(self.beep_path)
            except Exception:
                pass


class AlarmWatcher(threading.Thread):
    def __init__(self, manager, beep_path=BEEP_FILE, poll_interval=1):
        super().__init__(daemon=True)
        self.manager = manager
        self.beep_path = beep_path
        self.poll_interval = poll_interval
        self._stop = threading.Event()

    def run(self):
        while not self._stop.is_set():
            now = datetime.datetime.now()
            date_str = now.strftime('%Y-%m-%d')
            alarms = self.manager.list_alarms()
            for idx, a in enumerate(alarms):
                try:
                    ah = int(a.get('hour', 0))
                    am = int(a.get('minute', 0))
                except Exception:
                    continue
                triggered = a.get('triggered_date')
                if triggered == date_str:
                    continue
                if ah == now.hour and am == now.minute:
                    try:
                        # play sound immediately
                        play_sound(self.beep_path)
                    except Exception:
                        pass
                    # show a visual popup in the background
                    try:
                        AlertPopup(_format_12h(ah, am), a.get('description', ''), self.beep_path).start()
                    except Exception:
                        pass
                    self.manager.mark_triggered(idx, date_str)
            time.sleep(self.poll_interval)

    def stop(self):
        self._stop.set()


def _format_12h(hour24, minute):
    hour = hour24
    ampm = 'AM'
    if hour == 0:
        hour = 12
        ampm = 'AM'
    elif hour == 12:
        ampm = 'PM'
    elif hour > 12:
        hour = hour - 12
        ampm = 'PM'
    return f"{hour:02d}:{int(minute):02d} {ampm}"


def format_alarm(a):
    t = _format_12h(int(a['hour']), int(a['minute']))
    desc = a.get('description', '')
    trig = a.get('triggered_date')
    status = 'Triggered' if trig else 'Pending'
    return f"{t} - {desc} ({status})"


def clear_screen():
    # try ANSI clear first
    if os.name == 'nt':
        os.system('cls')
    else:
        sys.stdout.write('\x1b[2J\x1b[H')
        sys.stdout.flush()

def print_pending_alarms(manager):
    """Print pending alarms and return a list of their internal indices."""
    alarms_full = manager.list_alarms()
    pending = [(idx, a) for idx, a in enumerate(alarms_full) if not a.get('triggered_date')]
    if not pending:
        print(f"{YELLOW}No pending alarms.{RESET}")
        return []
    print(f"{BOLD}{GREEN}Pending Alarms:{RESET}")
    for disp_idx, (internal_idx, a) in enumerate(pending, start=1):
        print(f" {CYAN}{disp_idx}.{RESET} {format_alarm(a)}")
    return [internal_idx for internal_idx, _ in pending]

def ack_all_popups():
    """Acknowledge all active popups."""
    with POPUPS_LOCK:
        pops = list(ACTIVE_POPUPS)
    if not pops:
        print("No active alarms to acknowledge.")
        return
    for p in pops:
        try:
            p.ack()
        except Exception:
            pass
    print("Acknowledged active alarms.")
def run_simple_cli(manager):
    watcher = AlarmWatcher(manager)
    watcher.start()

    try:
        while True:
            clear_screen()
            print(f"{BOLD}{CYAN}Simple Alarm CLI{RESET}")
            print("Type 'new' to add an alarm, 'ack' to stop active alerts, 'del' to delete an alarm, 'q' to quit.\n")
            pending_indices = print_pending_alarms(manager)

            cmd = input(f"\n{GREEN}> {RESET}").strip()
            if not cmd:
                continue
            c = cmd.lower()
            if c == 'q' or c == 'quit':
                print(f"{YELLOW}Quitting...{RESET}")
                break
            elif c == 'ack':
                ack_all_popups()
                time.sleep(1.0)
                continue
            elif c == 'new':
                # input hour 1-12
                while True:
                    h_in = input('Hour (1-12, or c to cancel): ').strip().lower()
                    if h_in in ('c', 'cancel'):
                        break
                    try:
                        h = int(h_in)
                        if 1 <= h <= 12:
                            break
                        else:
                            print('Hour must be 1-12')
                    except ValueError:
                        print('Please enter a number 1-12')
                else:
                    # shouldn't reach
                    continue
                if h_in in ('c', 'cancel'):
                    continue

                # input minute
                while True:
                    m_in = input('Minute (0-59): ').strip()
                    try:
                        m = int(m_in)
                        if 0 <= m <= 59:
                            break
                        else:
                            print('Minute must be 0-59')
                    except ValueError:
                        print('Please enter a number 0-59')

                # input am/pm
                while True:
                    ap = input('AM or PM (am/pm): ').strip().lower()
                    if ap in ('am', 'pm'):
                        break
                    print("Please enter 'am' or 'pm'")

                # convert to 24-hour
                hour24 = h % 12
                if ap == 'pm':
                    hour24 += 12

                desc = input('Description (optional, enter for default "Alarm"): ').strip()
                if not desc:
                    desc = 'Alarm'

                manager.add_alarm(hour24, m, desc)
                print(f"{GREEN}Saved alarm: { _format_12h(hour24, m) } - {desc}{RESET}")
                time.sleep(1.0)
            elif c in ('del', 'delete'):
                if not pending_indices:
                    print('No pending alarms to delete.')
                    time.sleep(1.0)
                    continue
                # ask which displayed number to delete
                while True:
                    sel = input('Enter the number of the alarm to delete (or c to cancel): ').strip().lower()
                    if sel in ('c', 'cancel'):
                        break
                    try:
                        s = int(sel)
                        if 1 <= s <= len(pending_indices):
                            internal_idx = pending_indices[s-1]
                            confirmed = input(f"Are you sure you want to delete alarm {s}? (y/N): ").strip().lower()
                            if confirmed == 'y':
                                if manager.delete_alarm(internal_idx):
                                    print('Deleted.')
                                else:
                                    print('Failed to delete (index may have changed).')
                            else:
                                print('Cancelled.')
                            time.sleep(1.0)
                            break
                        else:
                            print(f'Please enter a number between 1 and {len(pending_indices)}')
                    except ValueError:
                        print('Please enter a number')
                continue
            else:
                print("Unknown command. Use 'new' to add, 'ack' to acknowledge, 'del' to delete, or 'q' to quit.")
                time.sleep(1.0)
    except KeyboardInterrupt:
        print('\nInterrupted, exiting.')
    finally:
        watcher.stop()

def main():
    ensure_beep_file()
    manager = AlarmManager()
    run_simple_cli(manager)


if __name__ == '__main__':
    main()
