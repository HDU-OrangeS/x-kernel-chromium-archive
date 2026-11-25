#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright 2025 KylinSoft Co., Ltd. <https://www.kylinos.cn/>
# See LICENSES for license details.
"""Own one Chromium WebGL smoke on an existing Wayland display; no CDP.

Run inside the owned x-kernel guest. The parent owns QEMU and Weston.
The default bounded mode is for disposable tests; --hands-on keeps the manually
opened page alive until the user closes Chromium or requests stop.
"""
import argparse
import hashlib
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import time
import types

sys.dont_write_bytecode = True
from urllib.parse import parse_qs, urlsplit

ARTIFACT_LIMIT = 65536
ARTIFACTS = ('launch.json', 'owner.json', 'result.json', 'page-report.json', 'page-verdict.json',
             'policy.json', 'policy-after.json', 'chromium.log', 'processes-before-stop.json',
             'processes-after-stop.json', 'http.log', 'guest_observer-source.py')

POLICY = Path('/etc/chromium/policies/managed/kiosk.json')
BINARY = '/usr/lib/chromium/chromium'
POINTS = [(8, 8), (56, 8), (8, 56), (56, 56), (24, 24), (40, 24), (24, 40), (40, 40)]


def save(path, value):
    temporary = path.with_suffix(path.suffix + '.new')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')
    temporary.replace(path)


def validate_report(report, token):
    errors = []
    if report.get('schema') != 'xkernel-webgl-smoke-v1' or report.get('run') != token:
        errors.append('Wrong schema or run token')
    for key in ('context_created', 'program_linked', 'debug_renderer_available'):
        if report.get(key) is not True:
            errors.append(key + ' is not confirmed')
    renderer = report.get('unmasked_renderer')
    if not isinstance(renderer, str) or 'virgl' not in renderer.lower():
        errors.append('No explicit VirGL renderer identity; masked/unknown is inconclusive')
    identities = ' '.join(str(report.get(key, '')) for key in
                          ('vendor', 'renderer', 'unmasked_vendor', 'unmasked_renderer')).lower()
    if any(word in identities for word in ('swiftshader', 'llvmpipe', 'softpipe', 'software rasterizer')):
        errors.append('Software renderer identified')
    if report.get('context_lost') is not False or report.get('errors') != []:
        errors.append('Context loss or browser-side error')
    phases = report.get('phases')
    if not isinstance(phases, list) or len(phases) != 2:
        errors.append('Two shader phases required')
        phases = []
    for index, phase in enumerate(phases):
        if not isinstance(phase, dict):
            errors.append('Malformed phase')
            continue
        blue = (.25, .75)[index]
        if phase.get('blue') != blue or phase.get('gl_error') != 0:
            errors.append('Wrong uniform or GL error in phase ' + str(index))
        samples = phase.get('samples')
        if not isinstance(samples, list) or len(samples) != len(POINTS):
            errors.append('Eight samples required per phase')
            continue
        for sample, (x, y) in zip(samples, POINTS):
            if not isinstance(sample, dict) or (sample.get('x'), sample.get('y')) != (x, y):
                errors.append('Wrong sample coordinate')
                continue
            pixel = sample.get('rgba')
            expected = [round((x + .5) * 255 / 64), round((y + .5) * 255 / 64), round(blue * 255), 255]
            if (not isinstance(pixel, list) or len(pixel) != 4
                    or any(type(channel) is not int or not 0 <= channel <= 255 for channel in pixel)
                    or any(abs(a - b) > 2 for a, b in zip(pixel, expected))):
                errors.append('Shader pixel mismatch at phase %d, (%d,%d)' % (index, x, y))
    return {'readback_passed': not errors, 'errors': errors,
            'scope': 'WebGL1 shader/readback with VirGL identity; visible presentation and host hardware need independent evidence'}


def load_lifecycle(out):
    """Execute the verified source, bypassing stale bytecode without deleting seed files."""
    path = Path('/usr/local/libexec/xkernel-manual-test/guest_observer.py')
    source = path.read_bytes()
    (out / 'guest_observer-source.py').write_bytes(source)
    sys.path.insert(0, str(path.parent))
    module = types.ModuleType('webgl_cleanup_source')
    module.__file__ = str(path)
    exec(compile(source, str(path), 'exec'), module.__dict__)
    return module, hashlib.sha256(source).hexdigest()


def status(out):
    result = {}
    for name in ('owner', 'page-verdict', 'result'):
        path = out / (name + '.json')
        if path.is_file() and not path.is_symlink():
            if path.stat().st_size > ARTIFACT_LIMIT:
                raise ValueError('Oversized status artifact: ' + name)
            value = json.loads(path.read_text())
            if not isinstance(value, dict):
                raise ValueError('Status must be a JSON object: ' + name)
            result[name] = value
    return result


def collect(out, artifact=None):
    """Only this run's small allowlisted files; never traverse its profile."""
    result = {}
    names = ARTIFACTS + ('owner.log',)
    if artifact is not None:
        if artifact not in names:
            raise ValueError('Unknown artifact name')
        names = (artifact,)
    for name in names:
        path = out / name if name != 'owner.log' else out.with_name(out.name + '.owner.log')
        if path.is_symlink():
            result[name] = {'error': 'refuse symlink'}
            continue
        if not path.is_file():
            result[name] = {'missing': True}
            continue
        length = path.stat().st_size
        with path.open('rb') as stream:
            if length > ARTIFACT_LIMIT:
                stream.seek(length - ARTIFACT_LIMIT)
            data = stream.read(ARTIFACT_LIMIT)
        result[name] = {'bytes': length, 'truncated': length > ARTIFACT_LIMIT,
                        'captured_sha256': hashlib.sha256(data).hexdigest(),
                        'text': data.decode('utf-8', errors='replace')}
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--page', type=Path)
    parser.add_argument('--artifact', choices=ARTIFACTS + ('owner.log',))
    parser.add_argument('--action', choices=('run', 'status', 'collect'), default='run')
    parser.add_argument('--wayland-display', default='wayland-probe')
    parser.add_argument('--angle', choices=('gles', 'gl'), default='gles')
    parser.add_argument('--isolate-webgl', action='store_true', help='Diagnostic only: software browser compositor, GPU WebGL')
    parser.add_argument('--software-ui-raster', action='store_true',
                        help='Development compatibility: CPU UI raster with GPU compositing and WebGL')
    parser.add_argument('--full-ui-redraw', action='store_true',
                        help='Redraw the full browser surface while retaining GPU raster/compositing')
    parser.add_argument('--max-seconds', type=int, default=300)
    parser.add_argument('--hands-on', action='store_true',
                        help='Manual development session: no watchdog; normal window close is allowed')
    args = parser.parse_args()
    if os.uname().nodename != 'kylin-x' or os.geteuid() != 0:
        parser.error('Run only inside the owned x-kernel guest')
    if (not args.out.is_absolute() or not str(args.out).startswith('/tmp/')
            or not 30 <= args.max_seconds <= 300):
        parser.error('Use a new /tmp output directory and bounded report/max timeouts')
    if args.out.is_symlink():
        parser.error('Refuse symlink output directory')
    if args.action != 'run':
        value = status(args.out) if args.action == 'status' else collect(args.out, args.artifact)
        print(('WEBGL_STATUS=' if args.action == 'status' else 'WEBGL_ARTIFACTS=') + json.dumps(value))
        return 0
    if args.page is None:
        parser.error('--page is required when running')
    args.out.mkdir(parents=True, exist_ok=False)
    lifecycle = None
    result = {'schema': 'xkernel-owned-webgl-smoke-v1', 'finished': False,
              'readback_passed': False, 'visual_review_required': True, 'cleanup_complete': False}
    child = server = None
    policy = None
    try:
        began = time.monotonic()
        save(args.out / 'owner.json', {'pid': os.getpid(), 'state': 'starting', 'started_monotonic': began})
        policy = POLICY.read_bytes()
        result['policy_sha256'] = hashlib.sha256(policy).hexdigest()
        (args.out / 'policy.json').write_bytes(policy)
        if json.loads(policy).get('DeveloperToolsAvailability') != 2:
            raise RuntimeError('Unexpected seed policy; do not rewrite it for the probe')
        lifecycle, cleanup_hash = load_lifecycle(args.out)
        result['cleanup_source_sha256'] = cleanup_hash
        if lifecycle.browser_processes():
            raise RuntimeError('Browser already exists; refuse concurrent run')
        page = args.page.read_bytes()
        token = secrets.token_hex(16)
        result.update(policy_sha256=hashlib.sha256(policy).hexdigest(),
                      page_sha256=hashlib.sha256(page).hexdigest(), run_token=token,
                      angle=args.angle, isolate_webgl=args.isolate_webgl,
                      software_ui_raster=args.software_ui_raster,
                      full_ui_redraw=args.full_ui_redraw, hands_on=args.hands_on)
        (args.out / 'policy.json').write_bytes(policy)
        version = subprocess.run([BINARY, '--version'], capture_output=True, text=True, timeout=30)
        result['browser_version'] = {'rc': version.returncode, 'stdout': version.stdout, 'stderr': version.stderr}
        if version.returncode:
            raise RuntimeError('Cannot query actual Chromium version')

        class Handler(BaseHTTPRequestHandler):
            def setup(self):
                super().setup()
                self.connection.settimeout(3)

            def log_message(self, fmt, *values):
                with (args.out / 'http.log').open('a') as log:
                    log.write((fmt % values) + '\n')

            def reply(self, code, body, content_type):
                self.send_response(code)
                self.send_header('Content-Type', content_type)
                self.send_header('Content-Length', str(len(body)))
                self.send_header('Cache-Control', 'no-store')
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                requested = urlsplit(self.path)
                if requested.path == '/' and parse_qs(requested.query).get('run') == [token]:
                    self.reply(200, page, 'text/html; charset=utf-8')
                else:
                    self.reply(404, b'Not found', 'text/plain')

            def do_POST(self):
                if self.path != '/result' or (args.out / 'page-report.json').exists():
                    self.reply(409, b'{}', 'application/json')
                    return
                try:
                    length = int(self.headers.get('Content-Length', '0'))
                    if not 1 <= length <= 65536:
                        raise ValueError('Bad body length')
                    report = json.loads(self.rfile.read(length))
                    if not isinstance(report, dict) or report.get('run') != token:
                        raise ValueError('Wrong report token')
                    verdict = validate_report(report, token)
                    save(args.out / 'page-report.json', report)
                    save(args.out / 'page-verdict.json', verdict)
                    result['readback_passed'] = verdict['readback_passed']
                    result['report_received'] = True
                    self.reply(200, json.dumps(verdict).encode(), 'application/json')
                except (ValueError, TypeError) as error:
                    self.reply(400, json.dumps({'error': str(error)}).encode(), 'application/json')

        server = HTTPServer(('127.0.0.1', 0), Handler)
        server.timeout = 1
        address = 'http://127.0.0.1:%d/?run=%s' % (server.server_port, token)
        command = [BINARY, '--ozone-platform=wayland', '--no-sandbox',
                   '--use-gl=angle', '--use-angle=' + args.angle, '--use-cmd-decoder=passthrough',
                   '--disable-software-rasterizer', '--ipc-connection-timeout=120',
                   '--disable-background-networking', '--disable-component-update', '--no-first-run',
                   '--user-data-dir=' + str(args.out / 'profile'), '--enable-logging=stderr', '--v=1']
        if args.isolate_webgl:
            command.append('--disable-gpu-compositing')
        if args.software_ui_raster:
            command.append('--disable-gpu-rasterization')
        if args.full_ui_redraw:
            command.append('--ui-disable-partial-swap')
        command.append(address)
        environment = os.environ.copy()
        for key in ('LIBGL_ALWAYS_SOFTWARE', 'GALLIUM_DRIVER', 'MESA_LOADER_DRIVER_OVERRIDE',
                    'MESA_GL_VERSION_OVERRIDE', 'MESA_GLES_VERSION_OVERRIDE', 'MESA_EXTENSION_OVERRIDE',
                    'DRI_PRIME', 'ANGLE_GL_VENDOR', 'ANGLE_GL_RENDERER', 'ANGLE_GL_VERSION',
                    'EGL_PLATFORM', 'CHROMIUM_FLAGS', 'CHROMIUM_USER_FLAGS', 'LD_PRELOAD', 'LD_LIBRARY_PATH'):
            environment.pop(key, None)
        environment['PYTHONDONTWRITEBYTECODE'] = '1'
        environment.update(XDG_RUNTIME_DIR='/run/user/0', WAYLAND_DISPLAY=args.wayland_display)
        result['command'] = command
        started = began
        with (args.out / 'chromium.log').open('xb') as log:
            child = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT, env=environment)
            result['main_pid'] = child.pid
            save(args.out / 'owner.json', {'pid': os.getpid(), 'state': 'running', 'main_pid': child.pid})
            save(args.out / 'launch.json', result)
            while True:
                if child.poll() is not None:
                    if args.hands_on and child.returncode == 0:
                        result['explicit_stop'] = True
                        result['window_closed'] = True
                        break
                    raise RuntimeError('Browser exited before explicit stop')
                if (args.out / 'stop-request').exists() or args.out.with_name(args.out.name + '.stop-request').exists():
                    result['explicit_stop'] = True
                    break
                elapsed = time.monotonic() - started
                if not args.hands_on and elapsed >= args.max_seconds:
                    raise TimeoutError('Owner watchdog expired before explicit stop')
                server.handle_request()
    except BaseException as error:
        result['failure'] = repr(error)
    finally:
        if server is not None:
            try:
                server.server_close()
            except Exception as error:
                result['server_close_error'] = repr(error)
        if child is not None:
            try:
                save(args.out / 'owner.json', {'pid': os.getpid(), 'state': 'stopping', 'main_pid': child.pid})
            except Exception as error:
                result['owner_snapshot_error'] = repr(error)
            try:
                save(args.out / 'processes-before-stop.json', lifecycle.browser_processes())
            except Exception as error:
                result['process_snapshot_error'] = repr(error)
            try:
                lifecycle.finish_resources(result, child, None, 90, True, 'popen')
            except Exception as error:
                result['cleanup_error'] = repr(error)
                # Still request stop of this Popen child if the helper failed.
                try:
                    if child.poll() is None:
                        child.terminate()
                except Exception as stop_error:
                    result['fallback_stop_error'] = repr(stop_error)
            try:
                save(args.out / 'processes-after-stop.json', lifecycle.browser_processes())
            except Exception as error:
                result['process_after_error'] = repr(error)
        try:
            after_policy = POLICY.read_bytes()
            (args.out / 'policy-after.json').write_bytes(after_policy)
            result['policy_after_sha256'] = hashlib.sha256(after_policy).hexdigest()
            result['policy_unchanged'] = policy is not None and after_policy == policy
        except OSError as error:
            result['policy_unchanged'] = False
            result['policy_read_error'] = repr(error)
        result['finished'] = True
        result['guest_smoke_passed'] = bool(result.get('explicit_stop') and result['readback_passed']
            and result['cleanup_complete'] and result['policy_unchanged'] and not result.get('failure'))
        save(args.out / 'result.json', result)
        save(args.out / 'owner.json', {'pid': os.getpid(), 'state': 'finished'})
        os.sync()
    return 0 if result['guest_smoke_passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
