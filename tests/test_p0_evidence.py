"""Regressions for accepting failed proof evidence and executing unpinned code."""
import copy
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

from research_artifacts.formal_evidence import record_lean_execution, validate_formal_review
from research_artifacts.runtime_types import RuntimeValidationError
from research_artifacts.tool_adapters import ExperimentExecutionAdapter, LeanToolAdapter
from research_artifacts.workspace import ArtifactRecord
from tests.git_helpers import committed_code
from tests import test_workspace as workspace_fixtures


class FormalEvidenceTestCase(unittest.TestCase):
    def setUp(self):
        self.fixture = workspace_fixtures.WorkspaceTestCase()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.root = self.fixture.root
        self.claim = copy.deepcopy(self.fixture.workspace.get('claim-demo-recoverable-state').data)
        self.review = copy.deepcopy(self.fixture.workspace.get('review-demo-claim-001').data)
        self.review['id'] = 'review-demo-formal'
        self.review['assessment']['scheme'] = 'LEAN'
        self.review['reviewer']['reviewer_type'] = 'LEAN'
        self.review['reviewer']['actor']['session_id'] = 'session-proof-check'
        directory = self.root / 'projects/proj-demo/resources/formal-input'
        directory.mkdir(parents=True)
        self.source = directory / 'Core.lean'
        self.source.write_text('theorem core : True := True.intro\n')
        mapping = directory / 'mapping.json'
        mapping.write_text(json.dumps({'target_theorems': {'Core.lean': ['core']}}))
        for path in [self.source, mapping]:
            self.claim['verification']['resource_refs'].append({
                'uri': str(path.relative_to(self.root)), 'sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
                'media_type': 'application/json' if path.suffix == '.json' else 'text/plain',
                'description': 'Pinned proof input',
            })

    def execution(self, failed=False, compiler=None):
        def runner(command, **kwargs):
            return subprocess.CompletedProcess(command, 1 if failed else 0,
                                               'proof failed' if failed else "'core' does not depend on any axioms\n", '')
        result = LeanToolAdapter(subprocess.run if compiler else runner).verify(
            project_dir=self.source.parent, source_paths=['Core.lean'],
            target_theorems={'Core.lean': ['core']}, build_command=None,
            lean_command=[compiler] if compiler else ['lean'],
            lean_version='4.19.0', mathlib_commit='not-used-core-fixture',
            report_path=self.root / 'report.json',
        )
        self.review['evidence_resources'] = record_lean_execution(
            self.root, report_path=result.report_path, claim=self.claim,
            action_id='action-proof-check', bundle_sha256='a' * 64, session_id='session-proof-check',
        )
        return result

    def issues(self):
        return self.fixture.workspace.validate(
            overrides={self.claim['id']: self.claim}, additions=[ArtifactRecord(
                Path('projects/proj-demo/reviews/review-demo-formal.yaml'), self.review)])

    def test_failed_report_cannot_support_pass_for_either_formal_scheme(self):
        self.execution(failed=True)
        for scheme in ['LEAN', 'AXIOM_AUDIT']:
            self.review['assessment']['scheme'] = scheme
            with self.subTest(scheme=scheme):
                self.assertTrue(any(issue.code == 'formal-tool-evidence' for issue in self.issues()))

    def test_valid_report_is_accepted_and_receipts_survive_workspace_copy(self):
        self.execution()
        self.assertEqual(self.issues(), [])
        with tempfile.TemporaryDirectory() as value:
            moved = Path(value) / 'workspace'
            shutil.copytree(self.root, moved)
            validate_formal_review(moved, self.review, self.claim)

    def test_report_without_host_receipt_is_rejected(self):
        self.execution()
        self.review['evidence_resources'] = self.review['evidence_resources'][:1]
        self.assertTrue(any(issue.code == 'formal-tool-evidence' for issue in self.issues()))

    def test_receipt_cannot_be_reused_for_other_claim_revision_or_action(self):
        self.execution()
        with self.assertRaisesRegex(RuntimeValidationError, 'another action'):
            validate_formal_review(self.root, self.review, self.claim, {'action_id': 'action-other'})
        self.review['target']['artifact_ref']['revision'] += 1
        self.assertTrue(any(issue.code == 'formal-tool-evidence' for issue in self.issues()))

    def test_receipt_cannot_verify_changed_claim_or_sources(self):
        self.execution()
        self.claim['statement'] = 'A different scientific conclusion.'
        self.assertTrue(any(issue.code == 'formal-tool-evidence' for issue in self.issues()))
        self.claim['statement'] = self.fixture.workspace.get(self.claim['id']).data['statement']
        self.source.write_text('theorem core : True := by trivial\n')
        for ref in self.claim['verification']['resource_refs']:
            if ref['uri'].endswith('Core.lean'):
                ref['sha256'] = hashlib.sha256(self.source.read_bytes()).hexdigest()
        self.assertTrue(any(issue.code == 'formal-tool-evidence' for issue in self.issues()))

    def test_forged_pass_and_expanded_axiom_policy_are_rejected_at_recording(self):
        self.execution(failed=True)
        path = self.root / 'report.json'
        report = json.loads(path.read_text())
        report['outcome'] = 'PASS'
        for altered in [report, {**report, 'allowed_axioms': ['magic']}]:
            path.write_text(json.dumps(altered))
            with self.assertRaises(RuntimeValidationError):
                record_lean_execution(self.root, report_path=path, claim=self.claim,
                                      action_id='action-forged', bundle_sha256='a' * 64,
                                      session_id='session-proof-check')

    @unittest.skipUnless(os.environ.get('RESEARCH_HARNESS_TEST_LEAN'), 'Real Lean compiler not configured')
    def test_real_compiler_report_can_be_accepted_by_workspace_validator(self):
        result = self.execution(compiler=os.environ['RESEARCH_HARNESS_TEST_LEAN'])
        self.assertEqual(result.outcome, 'PASS')
        self.assertEqual(self.issues(), [])


class PinnedExperimentTestCase(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.repository = self.root / 'repo'
        self.repository.mkdir()
        self.script = self.repository / 'run.py'
        self.script.write_text('import helper\nprint(helper.VALUE)\n')
        self.helper = self.repository / 'helper.py'
        self.helper.write_text('VALUE = "PINNED"\n')
        self.commit = committed_code(self.repository)
        self.adapter = ExperimentExecutionAdapter()
        self.experiment = {
            'id': 'exp-pinned', 'revision': 1, 'hypothesis': {}, 'method': 'fixed',
            'baselines': [], 'datasets': [], 'metrics': [], 'configuration': {'environment': 'fixture', 'parameters': {}},
            'interpretation_plan': {'seeds': [1]},
            'code_location': {'repository': str(self.repository), 'git_commit': self.commit,
                              'entrypoint': f'{sys.executable} {self.script}'}, 'runs': [],
        }
        self.lock()

    def lock(self):
        self.experiment['protocol_lock'] = {
            'revision': 1, 'sha256': self.adapter.protocol_hash(self.experiment),
            'locked_at': '2026-01-01T00:00:00Z',
        }

    def execute(self):
        return self.adapter.execute(experiment=self.experiment, seed=1, repetition=0, run_root=self.root / 'runs')

    def test_dirty_scripts_and_imports_execute_from_commit_without_changing_user_files(self):
        self.helper.write_text('VALUE = "DIRTY"\n')
        self.script.write_text('print("DIRTY_SCRIPT")\n')
        result = self.execute()
        run = result['run']
        raw = json.loads(Path(run['outputs'][0]['uri']).read_text())
        self.assertEqual(run['status'], 'SUCCEEDED')
        self.assertEqual(raw['stdout'].strip(), 'PINNED')
        self.assertIn('DIRTY_SCRIPT', self.script.read_text())
        manifest = json.loads(Path(run['outputs'][1]['uri']).read_text())
        self.assertEqual(manifest['code_commit'], self.commit)
        self.assertFalse(Path(manifest['snapshot_root']).exists())
        self.experiment['runs'] = [run]
        self.assertTrue(self.execute()['reused'])

    def test_missing_commit_is_rejected(self):
        self.experiment['code_location']['git_commit'] = 'a' * 40
        self.lock()
        with self.assertRaises(RuntimeValidationError):
            self.execute()

    def test_script_outside_pinned_repository_is_rejected(self):
        external = self.root / 'external.py'
        external.write_text('print("UNPINNED")\n')
        for options in ['', '-u', '-X dev -u']:
            self.experiment['code_location']['entrypoint'] = f'{sys.executable} {options} {external}'
            self.lock()
            with self.subTest(options=options), self.assertRaisesRegex(RuntimeValidationError, 'pinned code snapshot'):
                self.execute()

    def test_relative_script_with_interpreter_flags_uses_snapshot(self):
        self.experiment['code_location']['entrypoint'] = f'{sys.executable} -u run.py'
        self.lock()
        self.script.write_text('print("DIRTY_SCRIPT")\n')
        result = self.execute()
        raw = json.loads(Path(result['run']['outputs'][0]['uri']).read_text())
        self.assertEqual(raw['stdout'].strip(), 'PINNED')

    def test_src_layout_imports_use_the_pinned_version(self):
        package = self.repository / 'src' / 'research_module'
        package.mkdir(parents=True)
        module = package / '__init__.py'
        module.write_text('VALUE = "PINNED_SRC"\n')
        self.script.write_text('import research_module\nprint(research_module.VALUE)\n')
        self.experiment['code_location']['git_commit'] = committed_code(self.repository)
        self.lock()
        module.write_text('VALUE = "DIRTY_SRC"\n')
        result = self.execute()
        raw = json.loads(Path(result['run']['outputs'][0]['uri']).read_text())
        self.assertEqual(raw['stdout'].strip(), 'PINNED_SRC')

    def test_reused_output_tampering_is_rejected(self):
        first = self.execute()
        Path(first['run']['outputs'][0]['uri']).write_text('{}')
        with self.assertRaisesRegex(RuntimeValidationError, 'output hash'):
            self.execute()
