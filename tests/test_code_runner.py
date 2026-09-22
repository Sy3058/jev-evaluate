import subprocess
import sys
import unittest
from unittest.mock import patch

from code_runner import run_python, bounded_run
import evaluation as rubric


class CodeRunnerTests(unittest.TestCase):
    def spec(self):
        return {'function':'add','cases':[{'args':[2,3],'expected':5}]}

    def test_unavailable_never_executes_on_host(self):
        with patch('code_runner.shutil.which',return_value=None), patch('code_runner.subprocess.run') as run:
            result=run_python('raise RuntimeError()',self.spec())
        self.assertEqual(result['status'],'unavailable')
        run.assert_not_called()

    def test_local_image_only_and_container_limits(self):
        inspected=subprocess.CompletedProcess([],0,'sha256:'+'a'*64,'')
        completed=subprocess.CompletedProcess([],0,b'[{"passed":true,"actual":"5"}]',b'')
        with patch('code_runner.shutil.which',return_value='docker'), patch('code_runner.subprocess.run',return_value=inspected) as run, patch('code_runner.bounded_run',return_value=completed) as execute:
            result=run_python('def add(a,b): return a+b',self.spec())
        self.assertEqual(result['status'],'executed')
        command=execute.call_args.args[0]
        self.assertEqual(command[command.index('--network')+1],'none')
        self.assertIn('--read-only',command)
        self.assertIn('no-new-privileges',command)
        self.assertIn('readonly',command[command.index('--mount')+1])
        self.assertTrue(run.call_args.args[0][-1].startswith('jev-test-'))
        self.assertEqual(run.call_args.args[0][1:3],['rm','--force'])

    def test_timeout_is_not_a_pass(self):
        inspected=subprocess.CompletedProcess([],0,'sha256:'+'a'*64,'')
        with patch('code_runner.shutil.which',return_value='docker'), patch('code_runner.subprocess.run',return_value=inspected), patch('code_runner.bounded_run',side_effect=subprocess.TimeoutExpired('docker',15)):
            result=run_python('while True: pass',self.spec())
        self.assertEqual(result['status'],'timeout')

    def test_code_spec_validation(self):
        good=rubric.normalize_spec({'outputFormat':'python','codeTests':self.spec()},'p')
        self.assertEqual(good['codeTests']['function'],'add')
        with self.assertRaises(ValueError): rubric.normalize_spec({'codeTests':self.spec()},'p')
        with self.assertRaises(ValueError): rubric.normalize_spec({'outputFormat':'python','codeTests':{'function':'x; command','cases':[]}},'p')

    def test_output_limit_on_trusted_test_process(self):
        with self.assertRaises(ValueError):
            bounded_run([sys.executable,'-c','print("x" * 100000)'])
