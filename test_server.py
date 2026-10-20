import io
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import server


class ServerTests(unittest.TestCase):
    def test_substitution_is_literal_and_shell_safe(self):
        task = "quotes '\" ; $(touch injected) {model}\nsecond line"
        argv = server.build_command([sys.executable, '-c',
            'import sys; print(sys.argv[1])', '{prompt}', 'model={model}'],
            {'model': 'example', 'effort': 'medium'}, task)
        self.assertEqual(shlex.split(shlex.join(argv)), argv)
        with tempfile.TemporaryDirectory() as directory:
            result = subprocess.run(shlex.join(argv), shell=True, cwd=directory,
                                    capture_output=True, text=True, check=True)
            self.assertEqual(result.stdout, task + '\n')
            self.assertFalse((Path(directory) / 'injected').exists())
        self.assertEqual(argv[-1], 'model=example')

    def test_configuration_replaces_whole_entries(self):
        with tempfile.TemporaryDirectory() as directory:
            base, user = Path(directory) / 'base.json', Path(directory) / 'user.json'
            base.write_text(json.dumps({'providers': {'a': {'type': 'cli', 'popup': ['a']}},
                'roles': {'implementer': {'model': 'old', 'effort': 'high'},
                          'reviewer': {'popup': False}}}))
            user.write_text(json.dumps({'roles': {'implementer': {'model': 'new'}}}))
            config = server.load_config(base, user)
            self.assertEqual(config['roles']['implementer'], {'model': 'new'})
            self.assertIn('reviewer', config['roles'])
            self.assertIn('a', config['providers'])

    def test_no_tmux_has_no_side_effects(self):
        with patch.dict(os.environ, {}, clear=True), patch('server.subprocess.run') as run:
            result = server.run_role({'role': 'implementer', 'task': 'work'})
        self.assertTrue(result['isError'])
        self.assertIn('Abre Claude o agy dentro de tmux', result['content'][0]['text'])
        run.assert_not_called()

    def test_task_starting_with_dash_is_rejected(self):
        with patch.dict(os.environ, {'TMUX': 'test'}), patch('server.subprocess.run') as run:
            for task in ('--dangerously-bypass-approvals-and-sandbox', '  \n-x'):
                result = server.run_role({'role': 'implementer', 'task': task})
                self.assertTrue(result['isError'])
                self.assertIn('task', result['content'][0]['text'])
            run.assert_not_called()

    def test_run_role_is_rejected_inside_a_role(self):
        with patch.dict(os.environ, {'TMUX': 'test', 'CODEX_FLOW_ROLE': 'claude'}), \
                patch('server.subprocess.run') as run:
            result = server.run_role({'role': 'implementer', 'task': 'work'})
        self.assertTrue(result['isError'])
        self.assertIn('run_role no se puede llamar desde un rol (CODEX_FLOW_ROLE=claude).',
                      result['content'][0]['text'])
        run.assert_not_called()

    def test_dispatch_exception_returns_internal_error(self):
        requests = [{'jsonrpc': '2.0', 'id': 7, 'method': 'ping'},
                    {'jsonrpc': '2.0', 'method': 'ping'}]
        output, errors = io.StringIO(), io.StringIO()
        with patch('server.dispatch', side_effect=RuntimeError('boom')), patch('sys.stderr', errors):
            server.serve(io.StringIO(''.join(json.dumps(r) + '\n' for r in requests)), output)
        replies = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual(len(replies), 1)
        self.assertEqual(replies[0]['id'], 7)
        self.assertEqual(replies[0]['error'], {'code': -32603, 'message': 'boom'})
        self.assertIn('boom', errors.getvalue())

    def test_protocol(self):
        requests = [
            {'id': 1, 'method': 'initialize', 'params': {'protocolVersion': '2025-06-18'}},
            {'id': 2, 'method': 'tools/list'}, {'id': 3, 'method': 'unknown'},
            {'method': 'notifications/initialized'}, {'method': 'unknown'},
            {'id': 4, 'method': 'ping'},
            {'id': 5, 'method': 'tools/call', 'params': {'name': 'absent'}},
        ]
        output = io.StringIO()
        server.serve(io.StringIO(''.join(json.dumps(dict(jsonrpc='2.0', **r)) + '\n'
                                        for r in requests)), output)
        replies = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual(len(replies), 5)
        self.assertEqual(replies[0]['result']['protocolVersion'], '2025-06-18')
        self.assertEqual({t['name'] for t in replies[1]['result']['tools']}, {'run_role', 'doctor'})
        self.assertEqual(replies[2]['error']['code'], -32601)
        self.assertEqual(replies[3]['result'], {})
        self.assertTrue(replies[4]['result']['isError'])

    def test_doctor_invalid_user_json(self):
        with tempfile.TemporaryDirectory() as directory:
            user = Path(directory) / 'config.json'
            user.write_text('{')
            with patch('server.USER_CONFIG', user):
                result = server.doctor()
        self.assertFalse(result['isError'])
        self.assertIn('falta', result['content'][0]['text'])
        self.assertIn('JSON', result['content'][0]['text'])

    def test_invalid_roles_and_non_popup(self):
        config = {'providers': {'codex': {'type': 'cli', 'popup': ['codex']}},
                  'roles': {'reviewer': {'provider': 'codex', 'popup': False},
                            'bad': {'provider': 'missing', 'popup': True}}}
        with patch('server.load_config', return_value=config), patch('server.subprocess.run') as run:
            for role, message in [('missing', 'Rol'), ('reviewer', "no tiene plantilla 'background'"), ('bad', 'Proveedor')]:
                result = server.run_role({'role': role, 'task': 'work'})
                self.assertTrue(result['isError'])
                self.assertIn(message, result['content'][0]['text'])
            run.assert_not_called()


class ConfigurableRoleTests(unittest.TestCase):
    def setUp(self):
        self.config = {'providers': {'fake': {'type': 'cli',
            'background': ['fake', '{args}', '{prompt}'], 'popup': ['interactive', '{prompt}']}},
            'roles': {'traductor': {'provider': 'fake', 'snapshot': False}}}
        self.loader = patch('server.load_config', return_value=self.config)
        self.loader.start()
        self.addCleanup(self.loader.stop)

    def invoke(self, outcome=None):
        if outcome is None:
            outcome = subprocess.CompletedProcess(['fake'], 0, 'translated', '')
        def run(argv, **kwargs):
            if argv[0] == 'git':
                output = 'true' if argv[1] == 'rev-parse' else ' file | 1 +'
                return subprocess.CompletedProcess(argv, 0, output, '')
            if isinstance(outcome, Exception):
                raise outcome
            return outcome
        with patch.dict(os.environ, {}, clear=True), patch('server.subprocess.run', side_effect=run) as mock:
            result = server.run_role({'role': 'traductor', 'task': 'translate'})
        return result, mock

    def test_args_expansion_and_single_pass(self):
        role = {'model': 'm{effort}', 'effort': 'high',
                'args': ['--model', '{model}', '{close_popup}', '{prompt}']}
        self.assertEqual(server.build_command(['fake', '{args}', 'end'], role, '{model}'),
                         ['fake', '--model', 'm{effort}', str(server.CLOSE_POPUP), '{model}', 'end'])
        self.assertEqual(server.build_command(['fake', '{args}', '{prompt}'], {}, 'hi'), ['fake', 'hi'])

    def test_missing_placeholder_in_template_or_args(self):
        for template, args, missing in [(['fake', '{model}'], [], 'model'),
                                        (['fake', '{args}'], ['{effort}'], 'effort')]:
            with self.subTest(missing=missing):
                self.config['providers']['fake']['background'] = template
                self.config['roles']['traductor']['args'] = args
                result, mock = self.invoke()
                self.assertTrue(result['isError'])
                self.assertIn("Falta '{}' en el rol traductor.".format(missing), result['content'][0]['text'])
                mock.assert_not_called()

    def test_background_without_tmux_and_without_snapshot(self):
        result, mock = self.invoke()
        self.assertFalse(result['isError'], result)
        self.assertEqual(result['content'][0]['text'], 'translated\nfile | 1 +')
        provider_call = [c for c in mock.call_args_list if c.args[0][0] == 'fake'][0]
        self.assertEqual(provider_call.args[0], ['fake', 'translate'])
        self.assertEqual(provider_call.kwargs, dict(cwd=str(Path.cwd().resolve()), capture_output=True,
            encoding='utf-8', errors='replace', timeout=1800, stdin=subprocess.DEVNULL,
            env={'CODEX_FLOW_ROLE': 'traductor'}))
        self.assertFalse(any(c.args[0][1] in ('add', 'commit') for c in mock.call_args_list))

    def test_background_errors_include_diff(self):
        self.config['roles']['traductor']['timeout'] = 2.5
        cases = [(subprocess.CompletedProcess(['fake'], 7, '', 'discard' + 'e' * 4000), '7'),
                 (subprocess.TimeoutExpired(['fake'], 2.5), 'El rol traductor superó 2.5 s.'),
                 (FileNotFoundError('fake'), 'fake')]
        for outcome, message in cases:
            with self.subTest(outcome=outcome):
                result, _ = self.invoke(outcome)
                self.assertTrue(result['isError'])
                text = result['content'][0]['text']
                self.assertIn(message, text)
                self.assertIn('file | 1 +', text)
                self.assertNotIn('discard', text)
                if isinstance(outcome, subprocess.CompletedProcess):
                    self.assertIn('e' * 4000, text)

    def test_empty_stdout_is_error(self):
        result, _ = self.invoke(subprocess.CompletedProcess(['fake'], 0, ' \n', 'discard' + 'e' * 4000))
        text = result['content'][0]['text']
        self.assertTrue(result['isError'])
        self.assertIn('El rol traductor no produjo salida.', text)
        self.assertIn('e' * 4000, text)
        self.assertNotIn('discard', text)
        self.assertIn('file | 1 +', text)

    def test_stdout_truncation(self):
        result, _ = self.invoke(subprocess.CompletedProcess(['fake'], 0, 'discard' + 'x' * 20000, ''))
        text = result['content'][0]['text']
        self.assertFalse(result['isError'])
        self.assertIn('recort', text[:100].lower())
        self.assertIn('x' * 20000, text)
        self.assertNotIn('discard', text)

    def test_missing_mode_template(self):
        for popup, mode in [(False, 'background'), (True, 'popup')]:
            with self.subTest(mode=mode):
                self.config['roles']['traductor']['popup'] = popup
                self.config['providers']['fake'].pop(mode)
                result, mock = self.invoke()
                self.assertTrue(result['isError'])
                self.assertIn("El proveedor fake no tiene plantilla '{}'.".format(mode), result['content'][0]['text'])
                mock.assert_not_called()

    def test_dynamic_tools_and_fallback(self):
        def listing():
            return server.dispatch({'id': 1, 'method': 'tools/list'})['result']['tools'][0]
        self.config['roles']['traductor']['model'] = 'm'
        tool = listing()
        self.assertIn('traductor: fake/m (segundo plano)', tool['description'])
        self.assertEqual(tool['inputSchema']['properties']['role'], {'type': 'string'})
        self.config['roles']['traductor']['popup'] = True
        self.assertIn('traductor: fake/m (popup)', listing()['description'])
        with patch('server.load_config', side_effect=ValueError('invalid')):
            self.assertEqual(listing()['description'], server.TOOLS[0]['description'])

    def test_doctor_lists_roles_and_both_programs(self):
        self.config['roles']['interactive'] = {'provider': 'fake', 'popup': True, 'model': 'm'}
        self.config['roles']['missing'] = {'provider': 'absent'}
        with patch('server.shutil.which', return_value='/bin/fake') as which:
            text = server.doctor()['content'][0]['text']
        self.assertIn('traductor: fake/', text)
        self.assertIn('interactive: fake/m (popup)', text)
        self.assertIn('falta: Rol missing', text)
        which.assert_any_call('fake')
        which.assert_any_call('interactive')
        del self.config['providers']['fake']['background']
        self.assertIn("no tiene plantilla 'background'", server.doctor()['content'][0]['text'])


class GitTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.git('init', '-q')
        self.git('config', 'user.email', 'test@example.com')
        self.git('config', 'user.name', 'Test')
        self.file = self.directory / 'tracked.txt'
        self.file.write_text('initial\n')
        self.git('add', '.')
        self.git('commit', '-qm', 'initial')

    def git(self, *args):
        return subprocess.run(['git', *args], cwd=self.directory, capture_output=True,
                              text=True, check=True).stdout.strip()

    def invoke(self, change=True, code=0):
        real_run = subprocess.run
        def run(argv, **kwargs):
            if argv[0] == 'tmux':
                self.assertEqual(argv[:8], ['tmux', 'display-popup', '-E', '-w', '95%', '-h', '90%', '-d'])
                self.assertEqual(Path(argv[8]).resolve(), self.directory.resolve())
                self.assertEqual(argv[9:11], ['-e', 'CODEX_FLOW_ROLE=implementer'])
                if change:
                    self.file.write_text('popup change\n')
                return subprocess.CompletedProcess(argv, code, '', '')
            return real_run(argv, **kwargs)
        with patch.dict(os.environ, {'TMUX': 'test'}), patch('server.subprocess.run', side_effect=run):
            return server.run_role({'role': 'implementer', 'task': 'work', 'cwd': str(self.directory)})

    def test_snapshot_and_diff(self):
        self.file.write_text('before popup\n')
        result = self.invoke()
        self.assertFalse(result['isError'], result)
        self.assertEqual(self.git('log', '-1', '--format=%s'), 'wip')
        self.assertEqual(self.git('show', 'HEAD:tracked.txt'), 'before popup')
        self.assertIn('tracked.txt', result['content'][0]['text'])

    def test_clean_repo_continues_without_commit(self):
        before = self.git('rev-parse', 'HEAD')
        self.assertFalse(self.invoke(False)['isError'])
        self.assertEqual(before, self.git('rev-parse', 'HEAD'))

    def test_nonzero_popup_code_is_not_error(self):
        result = self.invoke(False, code=1)
        self.assertFalse(result['isError'])
        self.assertIn('Código de salida del popup: 1', result['content'][0]['text'])

    def test_sighup_popup_code_reports_closed_popup(self):
        result = self.invoke(False, code=129)
        self.assertFalse(result['isError'])
        text = result['content'][0]['text']
        self.assertIn('Popup cerrado al terminar el turno.', text)
        self.assertNotIn('129', text)

    def test_commit_failure_aborts(self):
        self.file.write_text('changed\n')
        hook = self.directory / '.git/hooks/pre-commit'
        hook.write_text('#!/bin/sh\nexit 1\n')
        hook.chmod(0o755)
        result = self.invoke(False)
        self.assertTrue(result['isError'])
        self.assertEqual(self.git('log', '-1', '--format=%s'), 'initial')

    def test_non_git_directory(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {'TMUX': 'test'}):
            result = server.run_role({'role': 'implementer', 'task': 'work', 'cwd': directory})
        self.assertTrue(result['isError'])


if __name__ == '__main__':
    unittest.main()
