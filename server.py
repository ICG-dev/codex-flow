#!/usr/bin/env python3
"""Minimal stdio MCP server for configurable CLI roles (Python 3.9+)."""
import json
import math
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


def build_command(template, role, task, role_name='desconocido'):
    if not isinstance(template, list) or not template or any(not isinstance(s, str) for s in template):
        raise ValueError('El proveedor necesita una plantilla con una lista de argumentos.')
    args = role.get('args', [])
    if not isinstance(args, list) or any(not isinstance(arg, str) for arg in args):
        raise ValueError('args debe ser una lista de strings.')
    values = {'prompt': task, 'close_popup': str(CLOSE_POPUP)}
    def substitute(match):
        key = match.group(1)
        if key in values:
            return values[key]
        if key not in role:
            raise ValueError("Falta '{}' en el rol {}.".format(key, role_name))
        if not isinstance(role[key], str):
            raise ValueError(key + ' debe ser un string.')
        return role[key]
    # One pass: placeholders in substituted values remain literal.
    expanded = [arg for item in template for arg in (args if item == '{args}' else [item])]
    argv = [re.sub(r'\{(prompt|model|effort|close_popup)\}', substitute, item) for item in expanded]
    if not argv or not argv[0]:
        raise ValueError('La plantilla debe indicar un programa.')
    return argv


def role_mode(role):
    if not isinstance(role.get('popup', False), bool):
        raise ValueError('popup debe ser un bool.')
    return 'popup' if role.get('popup', False) else 'background'


def role_template(config, role):
    name = role.get('provider')
    provider = config['providers'].get(name)
    if provider is None:
        raise ValueError('Proveedor inexistente: ' + str(name))
    if provider.get('type') != 'cli':
        raise ValueError('Proveedor no soportado todavía: necesita type=cli.')
    mode = role_mode(role)
    if mode not in provider:
        raise ValueError("El proveedor {} no tiene plantilla '{}'.".format(name, mode))
    return provider[mode]


def role_description(name, role):
    return '{}: {}/{} ({})'.format(name, role.get('provider', '?'), role.get('model', '?'),
        'popup' if role_mode(role) == 'popup' else 'segundo plano')


def list_tools():
    try:
        roles = load_config()['roles']
        description = 'Ejecuta un rol configurable en popup o segundo plano.\n' + '\n'.join(
            role_description(name, role) for name, role in roles.items())
    except (OSError, ValueError, TypeError):
        description = TOOLS[0]['description']
    return [{**TOOLS[0], 'description': description}, TOOLS[1]]


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
        if 'CODEX_FLOW_ROLE' in os.environ:
            raise ValueError('run_role no se puede llamar desde un rol (CODEX_FLOW_ROLE={}).'.format(
                os.environ['CODEX_FLOW_ROLE']))
        if arguments['task'].lstrip().startswith('-'):
            raise ValueError('task no puede empezar con "-": el CLI lo leería como opción.')
        cwd = arguments.get('cwd', os.getcwd())
        if not isinstance(cwd, str) or not cwd:
            raise ValueError('cwd debe ser un string no vacío.')
        cwd = str(Path(cwd).resolve())
        config = load_config()
        role = config['roles'].get(arguments['role'])
        if role is None:
            raise ValueError('Rol inexistente: ' + arguments['role'])
        template = role_template(config, role)
        popup = role_mode(role) == 'popup'
        if popup and 'TMUX' not in os.environ:
            return tool_result('Abre Claude o agy dentro de tmux para usar el popup.', True)
        argv = build_command(template, role, arguments['task'], arguments['role'])
        snapshot = role.get('snapshot', True)
        if not isinstance(snapshot, bool):
            raise ValueError('snapshot debe ser un bool.')
        timeout = role.get('timeout', 1800)
        if not popup and (isinstance(timeout, bool) or not isinstance(timeout, (int, float))
                          or not math.isfinite(timeout) or timeout <= 0):
            raise ValueError('timeout debe ser un número positivo finito de segundos.')
        if git(cwd, 'rev-parse', '--is-inside-work-tree').stdout.strip() != 'true':
            raise ValueError('cwd no es un repositorio Git de trabajo.')
        if snapshot:
            git(cwd, 'add', '-u')
            staged = git(cwd, 'diff', '--cached', '--quiet', check=False)
            if staged.returncode not in (0, 1):
                raise ValueError('No se pudieron comprobar los cambios preparados en Git.')
            if staged.returncode == 1:
                git(cwd, 'commit', '-m', 'wip')
            # An empty repository needs a HEAD for the requested diff.
            elif git(cwd, 'rev-parse', '--verify', 'HEAD', check=False).returncode:
                git(cwd, 'commit', '--allow-empty', '-m', 'wip')
        error = False
        try:
            if popup:
                result = subprocess.run(['tmux', 'display-popup', '-E', '-w', '95%', '-h', '90%',
                                         '-d', cwd, '-e', 'CODEX_FLOW_ROLE=' + arguments['role'],
                                         shlex.join(argv)], capture_output=True, text=True)
                # 129 is the SIGHUP sent by close-popup.sh; other popup codes remain informational.
                text = ('Popup cerrado al terminar el turno.' if result.returncode == 129 else
                        'Código de salida del popup: {}'.format(result.returncode))
                if result.stderr.strip():
                    text += '\n' + result.stderr.strip()
            else:
                result = subprocess.run(argv, cwd=cwd, capture_output=True,
                                        encoding='utf-8', errors='replace', timeout=timeout, stdin=subprocess.DEVNULL,
                                        env={**os.environ, 'CODEX_FLOW_ROLE': arguments['role']})
                error = result.returncode != 0
                if error:
                    text = 'Código de salida del rol {}: {}\n{}'.format(
                        arguments['role'], result.returncode, result.stderr[-4000:])
                else:
                    text = result.stdout
                    if len(text) > 20000:
                        text = 'Salida recortada a los últimos 20000 caracteres.\n' + text[-20000:]
        except subprocess.TimeoutExpired:
            text, error = 'El rol {} superó {} s.'.format(arguments['role'], timeout), True
        except FileNotFoundError:
            text, error = 'Programa no encontrado: ' + ('tmux' if popup else argv[0]), True
        except OSError as exc:
            text, error = str(exc), True
        stat = git(cwd, 'diff', '--stat', 'HEAD', check=False)
        if stat.returncode:
            # Preserve the CLI outcome, including when snapshot=false leaves an unborn HEAD.
            text += '\nNo se pudo obtener git diff --stat HEAD: ' + stat.stderr.strip()
        elif stat.stdout.strip():
            text += '\n' + stat.stdout.strip()
        elif popup:
            text += '\nSin cambios respecto a HEAD.'
        return tool_result(text, error)
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
            description = role_description(name, role)
            argv = build_command(role_template(config, role), role, '', name)
            programs.add(argv[0])
            report(True, 'Rol ' + description)
        except (ValueError, TypeError) as exc:
            report(False, 'Rol {}: proveedor={}, modelo={}, popup={}: {}'.format(
                name, role.get('provider', '?'), role.get('model', '?'), role.get('popup', False), exc))
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
        result = {'tools': list_tools()}
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
