#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright 2025 KylinSoft Co., Ltd. <https://www.kylinos.cn/>
# See LICENSES for license details.
"""Manually start/stop one owned Weston GL development session in x-kernel."""
import argparse
import json
import os
from pathlib import Path
import secrets
import signal
import stat
import subprocess
import sys
import time

sys.dont_write_bytecode = True
from development_common import stale_socket
BASE = Path(__file__).resolve().parent
RUN = Path('/tmp/virgl-development')
RUNTIME = Path('/run/user/0')
NAMES = {'seatd', 'weston', 'weston-desktop-shell', 'chromium', 'chrome_crashpad_handler'}
DIRTY_ENV = ('LD_PRELOAD', 'LD_LIBRARY_PATH', 'LIBGL_ALWAYS_SOFTWARE', 'GALLIUM_DRIVER',
             'MESA_LOADER_DRIVER_OVERRIDE', 'MESA_GL_VERSION_OVERRIDE', 'MESA_GLES_VERSION_OVERRIDE',
             'MESA_EXTENSION_OVERRIDE', 'DRI_PRIME', 'EGL_PLATFORM', 'LIBGL_DRIVERS_PATH',
             'GBM_BACKEND', '__EGL_VENDOR_LIBRARY_FILENAMES', 'ANGLE_GL_VENDOR',
             'ANGLE_GL_RENDERER', 'ANGLE_GL_VERSION', 'CHROMIUM_FLAGS', 'CHROMIUM_USER_FLAGS')


def save(name, value):
    path = RUN / (name + '.json')
    temporary = path.with_suffix('.new')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')
    temporary.replace(path)


def read(name):
    return json.loads((RUN / (name + '.json')).read_text())


def identity(pid):
    try:
        p = Path('/proc') / str(pid)
        raw = (p / 'stat').read_text()
        fields = raw[raw.rfind(')') + 2:].split()
        if fields[0] == 'Z':
            return None
        return {'pid': pid, 'starttime': int(fields[19]), 'exe': os.readlink(p / 'exe'),
                'cmdline': (p / 'cmdline').read_text().split('\0')}
    except (FileNotFoundError, ProcessLookupError):
        return None


def active(record):
    return bool(record and identity(record['pid']) == record)


def graphics_processes():
    found = []
    for path in Path('/proc').iterdir():
        if path.name.isdigit():
            record = identity(int(path.name))
            if record and Path(record['exe']).name in NAMES:
                found.append(record)
    return found



def input_consumers():
    """Refuse database recovery while udevd or any event-device reader is live."""
    found = []
    for path in Path('/proc').iterdir():
        if not path.name.isdigit():
            continue
        try:
            record = identity(int(path.name))
            if not record:
                continue
            if Path(record['exe']).name in {'udevd', 'systemd-udevd'}:
                found.append(record)
                continue
            for fd in (path / 'fd').iterdir():
                try:
                    if os.readlink(fd).startswith('/dev/input/'):
                        found.append(record)
                        break
                except (FileNotFoundError, ProcessLookupError):
                    continue
        except (FileNotFoundError, ProcessLookupError):
            continue
    return found


def prepare_input(environment):
    existing = input_consumers()
    if existing:
        raise RuntimeError('udevd/input consumer already exists; do not replace its database: ' + repr(existing))
    command = [sys.executable, '/usr/local/libexec/xkernel-desktop/input-coldplug.py',
               '--apply', '--require-pair', '--recover-empty']
    with (RUN / 'input-coldplug.json').open('xb') as report, (RUN / 'input-coldplug.stderr').open('xb') as errors:
        subprocess.run(command, stdout=report, stderr=errors, env=environment, check=True, timeout=30)
    report = json.loads((RUN / 'input-coldplug.json').read_text())
    devices = report.get('devices', [])
    if (report.get('applied') is not True or report.get('recover_empty') is not True
            or not all(any(device.get('properties', {}).get(role) == '1' for device in devices)
                       for role in ('ID_INPUT_KEYBOARD', 'ID_INPUT_MOUSE'))):
        raise RuntimeError('Input coldplug did not confirm an applied keyboard/mouse pair')
    return report

def is_socket(path):
    try:
        return stat.S_ISSOCK(path.stat().st_mode)
    except FileNotFoundError:
        return False


def wait_until(test, seconds, message):
    deadline = time.monotonic() + seconds
    while True:
        if test():
            return
        if time.monotonic() >= deadline:
            raise RuntimeError(message)
        time.sleep(.25)


def retire():
    keys = ('backing_bytes', 'reserved', 'pending', 'in_flight', 'quarantined',
            'invalid_transfers', 'allocation_failed')
    latest, stable = '', 0
    deadline = time.monotonic() + 20
    while True:
        latest = Path('/proc/driver/virgl_lifecycle').read_text()
        stable = stable + 1 if all(latest.splitlines().count(k + '=0') == 1 for k in keys) else 0
        if stable >= 2 or time.monotonic() >= deadline:
            return {'passed': stable >= 2, 'raw': latest}
        time.sleep(.1)


def stop_child(child, seconds):
    if child is None:
        return
    if child.poll() is None:
        # Our unreaped direct child cannot be replaced by an unrelated PID.
        child.terminate()
    child.wait(timeout=seconds)


def owner(token):
    signal.signal(signal.SIGHUP, signal.SIG_IGN)
    state = {'token': token, 'owner': identity(os.getpid()), 'phase': 'starting'}
    seat = weston = browser = None
    browser_dir = None
    errors = []
    save('state', state)
    def requested(name):
        try:
            return (RUN / name).read_text() == token
        except FileNotFoundError:
            return False
    environment = os.environ.copy()
    for name in DIRTY_ENV:
        environment.pop(name, None)
    environment.update(PYTHONDONTWRITEBYTECODE='1', XDG_RUNTIME_DIR=str(RUNTIME),
                       WAYLAND_DISPLAY='wayland-development', LIBSEAT_BACKEND='seatd', SEATD_VTBOUND='0')
    def launch(name, command):
        with (RUN / (name + '.log')).open('xb') as log:
            child = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=log,
                                     stderr=subprocess.STDOUT, env=environment)
        return child
    try:
        if graphics_processes():
            raise RuntimeError('Existing graphics/browser process; close it first')
        if RUNTIME.is_symlink():
            raise RuntimeError('Refuse symlink runtime directory')
        # A persisted image can contain sockets from a clean previous boot.
        # Only ECONNREFUSED on our fixed, owned socket paths permits removal.
        stale_socket(Path('/run/seatd.sock'), remove=True)
        stale_socket(RUNTIME / 'wayland-development', remove=True)
        RUNTIME.mkdir(parents=True, exist_ok=True)
        RUNTIME.chmod(0o700)
        state['input_coldplug'] = prepare_input(environment)
        save('state', state)
        seat = launch('seatd', ['seatd', '-g', 'root', '-l', 'debug'])
        def seat_ready():
            if seat.poll() is not None:
                raise RuntimeError('seatd exited; see seatd.log')
            return is_socket(Path('/run/seatd.sock'))
        wait_until(seat_ready, 15, 'seatd socket not ready')
        weston = launch('weston-stderr', ['weston', '--backend=drm', '--renderer=gl',
            '--drm-device=card0', '--continue-without-input', '--idle-time=0',
            '--socket=wayland-development', '--log=' + str(RUN / 'weston.log')])
        def desktop_ready():
            if weston.poll() is not None or seat.poll() is not None:
                raise RuntimeError('Weston/seatd exited; see logs')
            log = (RUN / 'weston.log').read_text() if (RUN / 'weston.log').exists() else ''
            renderers = [l.split('GL renderer:', 1)[1].strip() for l in log.splitlines() if 'GL renderer:' in l]
            if renderers and any(not r.lower().startswith('virgl') for r in renderers):
                raise RuntimeError('Weston did not select VirGL: ' + repr(renderers))
            return bool(renderers and "Output 'Virtual-1' enabled" in log
                        and is_socket(RUNTIME / 'wayland-development'))
        wait_until(desktop_ready, 180, 'Weston GL output did not become ready')
        state.update(phase='ready', seatd=identity(seat.pid), weston=identity(weston.pid))
        save('state', state)
        while not requested('stop-request'):
            if weston.poll() is not None or seat.poll() is not None:
                raise RuntimeError('Weston or seatd exited unexpectedly')
            if browser is not None and browser.poll() is not None:
                result = json.loads((browser_dir / 'result.json').read_text())
                state.update(webgl_result=result, webgl_running=False)
                save('state', state)
                if not result.get('cleanup_complete'):
                    raise RuntimeError('Chromium cleanup incomplete; inspect WebGL result')
                browser = None
            if requested('webgl-request'):
                (RUN / 'webgl-request').unlink()
                if browser is None:
                    browser_dir = RUN / ('webgl-' + secrets.token_hex(4))
                    # Keep GPU raster/compositing; this full-redraw path passed
                    # continuous input and maximize/restore in the development guest.
                    browser = launch(browser_dir.name + '-owner', [sys.executable,
                        str(BASE / 'guest_webgl_probe.py'), '--out', str(browser_dir),
                        '--page', str(BASE / 'webgl-smoke.html'), '--hands-on',
                        '--full-ui-redraw',
                        '--wayland-display', 'wayland-development'])
                    state.update(webgl_directory=str(browser_dir), webgl_running=True)
                    save('state', state)
            time.sleep(.25)
    except BaseException as error:
        errors.append(repr(error))
    finally:
        state['phase'] = 'stopping'
        save('state', state)
        if browser is not None:
            try:
                browser_dir.with_name(browser_dir.name + '.stop-request').touch(exist_ok=True)
                browser.wait(timeout=105)
                result = json.loads((browser_dir / 'result.json').read_text())
                state['webgl_result'] = result
                if not result.get('cleanup_complete'):
                    errors.append('Chromium cleanup incomplete')
            except Exception as error:
                errors.append('Chromium: ' + repr(error))
        # Never tear the display out from under a surviving browser.
        browsers_remain = any(Path(p['exe']).name in {'chromium', 'chrome_crashpad_handler'}
                              for p in graphics_processes())
        if (browser is None or browser.poll() is not None) and not browsers_remain:
            try:
                stop_child(weston, 20)
            except Exception as error:
                errors.append('Weston: ' + repr(error))
            if weston is None or weston.poll() is not None:
                try:
                    stop_child(seat, 10)
                except Exception as error:
                    errors.append('seatd: ' + repr(error))
            try:
                state['gpu_retirement'] = retire()
                if not state['gpu_retirement']['passed']:
                    errors.append('GPU resources/counters did not return to zero')
            except Exception as error:
                errors.append('GPU retirement: ' + repr(error))
        state['remaining_graphics_processes'] = graphics_processes()
        if state['remaining_graphics_processes']:
            errors.append('Graphics processes remain; no global kill or forced success')
        state.update(phase='stopped' if not errors else 'failed', errors=errors, webgl_running=False)
        save('state', state)
        os.sync()
    return 0 if not errors else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('start', 'webgl', 'status', 'stop'))
    parser.add_argument('--owner-token', help=argparse.SUPPRESS)
    args = parser.parse_args()
    if os.uname().nodename != 'kylin-x' or os.geteuid() != 0:
        parser.error('Run inside the x-kernel guest as root')
    if RUN.is_symlink():
        raise RuntimeError('Refuse symlink session directory')
    if args.owner_token:
        return owner(args.owner_token)
    if args.action == 'start':
        if RUN.exists():
            state = read('state')
            if active(state.get('owner')):
                raise RuntimeError('Session is already running; use status/webgl/stop')
            if state['phase'] not in ('stopped', 'failed'):
                raise RuntimeError('Previous session did not finish; inspect status and owner.log')
            if graphics_processes():
                raise RuntimeError('Previous graphics processes remain; inspect status and logs')
            RUN.rename(RUN.with_name(RUN.name + '-' + state['token']))
        RUN.mkdir(mode=0o700)
        token = secrets.token_hex(8)
        save('state', {'token': token, 'phase': 'launching'})
        with (RUN / 'owner.log').open('xb') as log:
            child = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), 'start',
                '--owner-token', token], stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                start_new_session=True)
        def ready():
            state = read('state')
            if state['phase'] in ('failed', 'stopped') or child.poll() is not None:
                raise RuntimeError('Desktop startup failed: ' + json.dumps(state, ensure_ascii=False))
            return state['phase'] == 'ready'
        wait_until(ready, 200, 'Desktop startup timed out; inspect status, then stop')
        print('Weston GL ready. Run virgl-dev webgl to open Chromium; virgl-dev stop to close everything.')
        return 0
    if not RUN.exists():
        raise RuntimeError('No development session; run virgl-dev start')
    state = read('state')
    if args.action == 'status':
        state['owner_alive'] = active(state.get('owner'))
        state['desktop_alive'] = all(active(state.get(name)) for name in ('seatd', 'weston'))
        state['current_graphics_processes'] = graphics_processes()
        directory = state.get('webgl_directory')
        if directory and (Path(directory) / 'page-verdict.json').is_file():
            state['page_verdict'] = json.loads((Path(directory) / 'page-verdict.json').read_text())
        print(json.dumps(state, ensure_ascii=False, indent=2))
        healthy = (state['phase'] == 'ready' and state['owner_alive'] and state['desktop_alive']) or (
            state['phase'] == 'stopped' and not state['current_graphics_processes'])
        return 0 if healthy else 1
    if not active(state.get('owner')):
        if args.action == 'stop' and state['phase'] == 'stopped':
            if graphics_processes() or not retire()['passed']:
                raise RuntimeError('Saved session stopped, but current process/resource checks failed')
            print('Already stopped; current process/resource checks passed.')
            return 0
        raise RuntimeError('Owner is not alive; inspect status and logs, no unrelated process was signaled')
    if args.action == 'webgl':
        if state['phase'] != 'ready':
            raise RuntimeError('Desktop is not ready')
        (RUN / 'webgl-request').write_text(state['token'])
        print('WebGL page requested. Run virgl-dev status to see the independent readback verdict.')
    else:
        (RUN / 'stop-request').write_text(state['token'])
        token = state['token']
        def stopped():
            current = read('state')
            if current['token'] != token:
                raise RuntimeError('Session changed while waiting; inspect the replacement session')
            return current['phase'] in ('stopped', 'failed')
        wait_until(stopped, 160, 'Stop incomplete; inspect status and logs')
        state = read('state')
        print(json.dumps(state, ensure_ascii=False, indent=2))
        return 0 if state['phase'] == 'stopped' else 1
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except Exception as error:
        print('virgl-dev: ' + str(error), file=sys.stderr)
        raise SystemExit(1)
