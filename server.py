#!/usr/bin/env python3
"""Minimal stdio MCP server for interactive CLI roles (Python 3.9+)."""
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parent
USER_CONFIG = Path.home() / '.config/codex-flow/config.json'
CLOSE_POPUP = ROOT / 'close-popup.sh'
TOOLS = [
    {'name': 'run_role', 'description': 'Ejecuta un rol en un popup de tmux tras guardar una foto Git.',
     'inputSchema': {'type': 'object', 'properties': {
         'role': {'type': 'string'}, 'task': {'type': 'string'},
         'cwd': {'type': 'string'}}, 'required': ['role', 'task']}},
    {'name': 'doctor', 'description': 'Comprueba configuración y programas disponibles.',
     'inputSchema': {'type': 'object', 'properties': {}}},
]


def read_config(path):
    with path.open(encoding='utf-8') as stream:
        config = json.load(stream)
    if not isinstance(config, dict):
        raise ValueError('La configuración debe ser un objeto JSON.')
    for section in ('providers', 'roles'):
        entries = config.get(section, {})
        if not isinstance(entries, dict) or any(not isinstance(v, dict) for v in entries.values()):
            raise ValueError(section + ' debe contener objetos de configuración.')
    return config


def load_config(base_path=None, user_path=None):
    base = read_config(Path(base_path) if base_path is not None else ROOT / 'config.json')
    user = Path(user_path) if user_path is not None else USER_CONFIG
    override = read_config(user) if user.exists() else {}
    return {key: {**base.get(key, {}), **override.get(key, {})}
            for key in ('providers', 'roles')}


def build_command(template, role, task):
    if not isinstance(template, list) or not template or any(not isinstance(s, str) for s in template):
        raise ValueError('El proveedor necesita una plantilla popup con una lista de argumentos.')
    values = {'prompt': task, 'model': role.get('model', ''),
              'effort': role.get('effort', ''), 'close_popup': str(CLOSE_POPUP)}
    if any(not isinstance(value, str) for value in values.values()):
        raise ValueError('model y effort deben ser strings.')
    # One pass: placeholders occurring in task text remain literal.
    return [re.sub(r'\{(prompt|model|effort|close_popup)\}',
                   lambda match: values[match.group(1)], item) for item in template]


def tool_result(text, error=False):
    return {'content': [{'type': 'text', 'text': text}], 'isError': error}


def git(cwd, *args, check=True):
    result = subprocess.run(['git', *args], cwd=cwd, capture_output=True, text=True)
    if check and result.returncode:
        raise ValueError('Git: ' + (result.stderr.strip() or result.stdout.strip() or
                                   'falló ' + ' '.join(args)))
    return result


def run_role(arguments):
    try:
        if not isinstance(arguments, dict):
            raise ValueError('Los argumentos deben ser un objeto.')
        for name in ('role', 'task'):
            if not isinstance(arguments.get(name), str) or not arguments[name].strip():
                raise ValueError(name + ' es un string requerido.')
        if arguments['task'].lstrip().startswith('-'):
            raise ValueError('task no puede empezar con "-": el CLI lo leería como opción.')
        cwd =arguments.get('cwd', os.getcwd())
        if not isinstance(cwd, str) or not cwd:
            raise ValueError('cwd debe ser un string no vacío.')
        cwd = str(Path(cwd).resolve())
        config = load_config()
        role = config['roles'].get(arguments['role'])
        if role is None:
            raise ValueError('Rol inexistente: ' + arguments['role'])
        provider = config['providers'].get(role.get('provider'))
        if provider is None:
            raise ValueError('Proveedor inexistente para el rol: ' + arguments['role'])
        if role.get('popup') is not True:
            raise ValueError('Rol no soportado todavía: necesita popup=true.')
        if provider.get('type') != 'cli':
            raise ValueError('Proveedor no soportado todavía: necesita type=cli.')
        if 'TMUX' not in os.environ:
            return tool_result('Abre Claude o agy dentro de tmux para usar el popup.', True)
        command = shlex.join(build_command(provider.get('popup'), role, arguments['task']))
        if git(cwd, 'rev-parse', '--is-inside-work-tree').stdout.strip() != 'true':
            raise ValueError('cwd no es un repositorio Git de trabajo.')
        git(cwd, 'add', '-u')
        staged = git(cwd, 'diff', '--cached', '--quiet', check=False)
        if staged.returncode not in (0, 1):
            raise ValueError('No se pudieron comprobar los cambios preparados en Git.')
        if staged.returncode == 1:
            git(cwd, 'commit', '-m', 'wip')
        # An empty repository needs a HEAD for the requested diff.
        elif git(cwd, 'rev-parse', '--verify', 'HEAD', check=False).returncode:
            git(cwd, 'commit', '--allow-empty', '-m', 'wip')
        popup = subprocess.run(['tmux', 'display-popup', '-E', '-w', '95%', '-h', '90%',
                                '-d', cwd, command], capture_output=True, text=True)
        stat = git(cwd, 'diff', '--stat', 'HEAD').stdout.strip()
        # 129 is the SIGHUP sent by `tmux display-popup -C` from close-popup.sh.
        status = 'Popup cerrado al terminar el turno.' if popup.returncode == 129 else 'Código de salida del popup: {}'.format(popup.returncode)
        text = '{}\n{}'.format(status, stat or 'Sin cambios respecto a HEAD.')
        if popup.stderr.strip():
            text += '\n' + popup.stderr.strip()
        # Closing the popup with `tmux display-popup -C` can yield a non-zero code.
        return tool_result(text)
    except (OSError, ValueError, TypeError) as exc:
        return tool_result(str(exc), True)


def doctor():
    lines = []
    def report(ok, message):
        lines.append(('ok: ' if ok else 'falta: ') + message)
    report(shutil.which('tmux') is not None, 'tmux en PATH')
    report('TMUX' in os.environ, 'Dentro de tmux (TMUX)')
    report(CLOSE_POPUP.is_file() and os.access(CLOSE_POPUP, os.X_OK), 'close-popup.sh ejecutable')
    try:
        config = load_config()
        if USER_CONFIG.exists():
            report(True, 'Configuración del usuario: JSON válido')
    except (OSError, ValueError, TypeError) as exc:
        report(False, 'Configuración JSON: ' + str(exc))
        try:
            config = read_config(ROOT / 'config.json')
        except (OSError, ValueError):
            config = {'roles': {}, 'providers': {}}
    programs = set()
    for name, role in config.get('roles', {}).items():
        try:
            provider = config['providers'].get(role.get('provider'))
            if provider is None:
                raise ValueError('proveedor inexistente')
            argv = build_command(provider.get('popup'), role, '')
            programs.add(argv[0])
        except (ValueError, TypeError) as exc:
            report(False, 'Rol {}: {}'.format(name, exc))
    for program in sorted(programs):
        report(shutil.which(program) is not None, program + ' en PATH')
    return tool_result('\n'.join(lines))


def dispatch(request):
    if not isinstance(request, dict):
        return {'jsonrpc': '2.0', 'id': None, 'error': {'code': -32600, 'message': 'Solicitud inválida'}}
    if 'id' not in request:
        return None
    reply = {'jsonrpc': '2.0', 'id': request['id']}
    method = request.get('method')
    params = request.get('params', {})
    if not isinstance(params, dict):
        reply['error'] = {'code': -32602, 'message': 'params debe ser un objeto'}
        return reply
    if method == 'initialize':
        result = {'protocolVersion': params.get('protocolVersion', '2025-06-18'),
                  'capabilities': {'tools': {}},
                  'serverInfo': {'name': 'codex-flow', 'version': '0.1.0'}}
    elif method == 'tools/list':
        result = {'tools': TOOLS}
    elif method == 'ping':
        result = {}
    elif method == 'tools/call':
        name = params.get('name')
        if name == 'run_role':
            result = run_role(params.get('arguments', {}))
        elif name == 'doctor':
            result = doctor()
        else:
            result = tool_result('Herramienta desconocida: ' + str(name), True)
    else:
        reply['error'] = {'code': -32601, 'message': 'Método desconocido: ' + str(method)}
        return reply
    reply['result'] = result
    return reply


def serve(source=None, output=None):
    source = source if source is not None else sys.stdin
    output = output if output is not None else sys.stdout
    for line in source:
        try:
            request = json.loads(line)
        except json.JSONDecodeError:
            reply = {'jsonrpc': '2.0', 'id': None,
                     'error': {'code': -32700, 'message': 'JSON inválido'}}
        else:
            try:
                reply = dispatch(request)
            except Exception as exc:
                print('Error interno: {!r}'.format(exc), file=sys.stderr)
                reply = None
                if isinstance(request, dict) and 'id' in request:
                    reply = {'jsonrpc': '2.0', 'id': request['id'],
                             'error': {'code': -32603, 'message': str(exc)}}
        if reply is not None:
            output.write(json.dumps(reply, ensure_ascii=False) + '\n')
            output.flush()


if __name__ == '__main__':
    serve()
