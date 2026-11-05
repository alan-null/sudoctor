#!/usr/bin/env python3
"""Restore configured tmux windows and named panes without restarting live apps."""
import json
import pathlib
import re
import socket
import subprocess
import sys
import time


LAYOUTS = {'even-horizontal', 'even-vertical', 'main-horizontal', 'main-vertical', 'tiled'}


def run(*args):
    return subprocess.run(args, capture_output=True, text=True, check=False)


def tmux(*args):
    result = run('tmux', *args)
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or 'tmux command failed')
    return result.stdout.strip()


def validate(cfg):
    if not re.fullmatch(r'[a-z][a-z0-9-]{0,49}', cfg['name']):
        raise ValueError('Invalid session name')
    windows = cfg['windows']
    if not isinstance(windows, list) or not windows:
        raise ValueError('At least one window is required')
    names = set()
    for window in windows:
        name = window['name']
        if not re.fullmatch(r'[a-zA-Z0-9_-]+', name) or name in names:
            raise ValueError('Invalid or duplicate window name: ' + name)
        names.add(name)
        if ('command' in window) == ('panes' in window):
            raise ValueError('Each window needs either command or panes')
        if window.get('layout', 'tiled') not in LAYOUTS:
            raise ValueError('Invalid window layout')
        panes = window.get('panes', [window])
        if not isinstance(panes, list) or not panes:
            raise ValueError('At least one pane is required')
        pane_names = set()
        for pane in panes:
            name = pane['name']
            if not re.fullmatch(r'[a-zA-Z0-9_-]+', name) or name in pane_names:
                raise ValueError('Invalid or duplicate pane name: ' + name)
            pane_names.add(name)
            if not isinstance(pane.get('command'), str) or not pane['command'].strip():
                raise ValueError('Each pane needs a nonempty command')
        for item in [cfg, window, *panes]:
            if 'cwd' in item and (not isinstance(item['cwd'], str) or not item['cwd'].startswith('/')):
                raise ValueError('cwd must be an absolute path')


def wait_for_tcp(probe):
    if not probe:
        return
    deadline = time.monotonic() + int(probe.get('timeout', 180))
    while True:
        try:
            with socket.create_connection((probe['host'], int(probe['port'])), timeout=3):
                return
        except OSError:
            if time.monotonic() >= deadline:
                raise RuntimeError('VPN/dependency readiness probe timed out')
            time.sleep(3)


def pane_state(target):
    rows = tmux('list-panes', '-t', target, '-F', '#{pane_id}\t#{@sudoctor_name}\t#{pane_dead}')
    return [row.split('\t') for row in rows.splitlines()]


def restore(cfg):
    validate(cfg)
    wait_for_tcp(cfg.get('ready_tcp'))
    session = cfg['name']
    exists = run('tmux', 'has-session', '-t', '=' + session).returncode == 0
    actual = {}
    if exists:
        rows = tmux('list-windows', '-t', '=' + session, '-F', '#{window_name}\t#{window_id}')
        actual = dict(row.split('\t') for row in rows.splitlines())
    for window in cfg['windows']:
        panes = window.get('panes', [window])
        target = actual.get(window['name'])
        initial = None
        if target is None:
            cwd = panes[0].get('cwd', window.get('cwd', cfg.get('cwd', str(pathlib.Path.home()))))
            # Start a temporary shell so options and identity are set before the app starts.
            if not exists:
                target = tmux('new-session', '-d', '-P', '-F', '#{window_id}', '-s', session,
                              '-n', window['name'], '-c', cwd, '/bin/bash', '--noprofile', '--norc')
                exists = True
            else:
                target = tmux('new-window', '-d', '-P', '-F', '#{window_id}', '-t', '=' + session,
                              '-n', window['name'], '-c', cwd, '/bin/bash', '--noprofile', '--norc')
            initial = pane_state(target)[0][0]
        tmux('set-window-option', '-t', target, 'automatic-rename', 'off')
        tmux('set-window-option', '-t', target, 'allow-rename', 'off')
        tmux('set-window-option', '-t', target, 'remain-on-exit', 'on')
        states = {name: (pid, dead) for pid, name, dead in pane_state(target)}
        # Adopt the single pane of a legacy command window, retaining its running process.
        if 'command' in window and panes[0]['name'] not in states:
            pid, _, dead = pane_state(target)[0]
            tmux('set-option', '-p', '-t', pid, '@sudoctor_name', panes[0]['name'])
            states[panes[0]['name']] = (pid, dead)
        changed = False
        for pane in panes:
            cwd = pane.get('cwd', window.get('cwd', cfg.get('cwd', str(pathlib.Path.home()))))
            state = states.get(pane['name'])
            if state and state[1] == '0' and state[0] != initial:
                continue
            if state:
                pid = state[0]
            elif initial:
                pid = initial
            else:
                pid = tmux('split-window', '-d', '-P', '-F', '#{pane_id}', '-t', target,
                           '-c', cwd, '/bin/bash', '--noprofile', '--norc')
            tmux('set-option', '-p', '-t', pid, '@sudoctor_name', pane['name'])
            tmux('select-pane', '-t', pid, '-T', pane['name'])
            tmux('respawn-pane', '-k', '-t', pid, '-c', cwd, '/bin/bash', '-c', pane['command'])
            initial = None
            changed = True
            # Redistribute after every split so many panes do not exhaust one small pane.
            tmux('select-layout', '-t', target, window.get('layout', 'tiled'))
        if changed or 'layout' in window:
            tmux('select-layout', '-t', target, window.get('layout', 'tiled'))
        if 'panes' in window:
            tmux('set-window-option', '-t', target, 'pane-border-status', 'top')
            tmux('set-window-option', '-t', target, 'pane-border-format', '#{@sudoctor_name} | #{pane_current_command}')
    time.sleep(2)
    for window in cfg['windows']:
        target = '=' + session + ':=' + window['name']
        states = {name: dead for _, name, dead in pane_state(target)}
        for pane in window.get('panes', [window]):
            if states.get(pane['name']) != '0':
                raise RuntimeError('Pane missing or exited: ' + window['name'] + '/' + pane['name'])
    print('OK', session)


def main():
    if len(sys.argv) == 3 and sys.argv[1] == '--validate':
        validate(json.loads(pathlib.Path(sys.argv[2]).read_text()))
    elif len(sys.argv) == 2:
        restore(json.loads(pathlib.Path(sys.argv[1]).read_text()))
    else:
        raise SystemExit('Usage: sudoctor-tmux.py [--validate] config.json')


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        print('ERROR:', exc, file=sys.stderr)
        sys.exit(1)
