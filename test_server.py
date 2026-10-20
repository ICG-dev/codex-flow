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
            for role, message in [('missing', 'Rol'), ('reviewer', 'no soportado'), ('bad', 'Proveedor')]:
                result = server.run_role({'role': role, 'task': 'work'})
                self.assertTrue(result['isError'])
                self.assertIn(message, result['content'][0]['text'])
            run.assert_not_called()


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
