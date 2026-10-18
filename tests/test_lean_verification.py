"""Lean acceptance boundaries, including optional real-compiler regressions."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from research_artifacts.runtime_types import RuntimeValidationError
from research_artifacts.tool_adapters import LeanToolAdapter


class LeanVerificationFixture:
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / 'Main.lean'
        self.source.write_text('theorem target : True := True.intro\n')

    def verify(self, runner, **overrides):
        arguments = dict(
            project_dir=self.root, source_paths=['Main.lean'],
            target_theorems={'Main.lean': ['target']},
            lean_version='4.19.0', mathlib_commit='a' * 40,
            report_path=self.root / 'report.json', build_command=None,
        )
        arguments.update(overrides)
        return LeanToolAdapter(runner).verify(**arguments)

    @staticmethod
    def success(command, **kwargs):
        return subprocess.CompletedProcess(command, 0, "'target' does not depend on any axioms\n", '')


class LeanVerificationTestCase(LeanVerificationFixture, unittest.TestCase):
    def test_missing_or_unrelated_target_reports_cannot_pass(self):
        for output in ['', "'other' does not depend on any axioms\n"]:
            with self.subTest(output=output):
                result = self.verify(lambda command, **kwargs: subprocess.CompletedProcess(command, 0, output, ''))
                self.assertEqual(result.outcome, 'AXIOM_AUDIT_MISSING')
                self.assertFalse(result.axiom_audit_passed)

    def test_every_target_requires_its_own_report(self):
        result = self.verify(self.success, target_theorems={'Main.lean': ['target', 'second']})
        self.assertIn('AXIOM_AUDIT_MISSING:second', result.findings)

    def test_empty_sources_and_incomplete_mapping_are_rejected(self):
        for overrides in [
            dict(source_paths=[], target_theorems={}),
            dict(target_theorems={}),
            dict(target_theorems={'Main.lean': []}),
            dict(target_theorems={'Main.lean': ['target\n#eval 1']}),
        ]:
            with self.subTest(overrides=overrides), self.assertRaises(RuntimeValidationError):
                self.verify(self.success, **overrides)

    def test_report_cannot_overwrite_proof_source(self):
        original = self.source.read_bytes()
        with self.assertRaises(RuntimeValidationError):
            self.verify(self.success, report_path=self.source)
        self.assertEqual(self.source.read_bytes(), original)

    def test_source_changes_in_build_or_stronger_checker_invalidate_result(self):
        for phase in ['build', 'stronger']:
            with self.subTest(phase=phase):
                self.source.write_text('theorem target : True := True.intro\n')
                def mutate(command, **kwargs):
                    if (phase == 'build' and command == ['prepare']) or command == ['extra-check']:
                        self.source.write_text('theorem target : False := by sorry\n')
                    return self.success(command, **kwargs)
                result = self.verify(mutate, build_command=['prepare'],
                                     stronger_checker=['extra-check'] if phase == 'stronger' else None)
                self.assertEqual(result.outcome, 'SOURCE_CHANGED')
                self.assertFalse(result.axiom_audit_passed)

    def test_stronger_checker_failure_is_recorded(self):
        for error, outcome in [(subprocess.TimeoutExpired(['extra-check'], 1, output=b'partial'), 'TOOL_TIMEOUT'),
                               (FileNotFoundError('checker unavailable'), 'TOOL_UNAVAILABLE')]:
            with self.subTest(outcome=outcome):
                def fail(command, **kwargs):
                    if command == ['extra-check']:
                        raise error
                    return self.success(command, **kwargs)
                result = self.verify(fail, stronger_checker=['extra-check'])
                self.assertEqual(result.outcome, outcome)
                self.assertFalse(result.axiom_audit_passed)
                self.assertEqual(json.loads(Path(result.report_path).read_text())['outcome'], outcome)

    def test_imported_placeholder_cannot_be_approved_as_an_axiom(self):
        def placeholder(command, **kwargs):
            return subprocess.CompletedProcess(command, 0, "'target' depends on axioms: [sorryAx]\n", '')
        result = self.verify(placeholder, allowed_axioms=['sorryAx'])
        self.assertEqual(result.outcome, 'SORRY_FOUND')


LEAN_BINARY = os.environ.get('RESEARCH_HARNESS_TEST_LEAN') or shutil.which('lean')


@unittest.skipUnless(LEAN_BINARY, 'Set RESEARCH_HARNESS_TEST_LEAN to run real Lean checks')
class RealLeanVerificationTestCase(LeanVerificationFixture, unittest.TestCase):
    """Use the real compiler: no model, mathlib download, or stubbed Lean output."""

    def setUp(self):
        super().setUp()
        self.compiler = str(Path(LEAN_BINARY).resolve())

    def compile(self, text, targets=None, **overrides):
        self.source.write_text(text)
        def run(command, **kwargs):
            return subprocess.run(command, env=dict(os.environ, LEAN_PATH=str(self.root)), **kwargs)
        return self.verify(run, lean_command=[self.compiler],
                           target_theorems={'Main.lean': targets or ['target']}, **overrides)

    def test_valid_proof_with_nested_comments_and_literals(self):
        result = self.compile('''/- sorry /- axiom secret : False -/ sorryAx -/
-- sorry
def explanation := "sorry sorryAx axiom"
def rawExplanation := r#"a quote " and sorry -- /-"#
theorem target (P Q : Prop) (h : P ∧ Q) : Q ∧ P := ⟨h.2, h.1⟩
''')
        self.assertEqual(result.outcome, 'PASS')
        report = json.loads(Path(result.report_path).read_text())
        self.assertEqual(report['target_axioms'], {'Main.lean': {'target': []}})

    def test_wrong_file_build_does_not_hide_invalid_submitted_source(self):
        other = self.root / 'Other.lean'
        other.write_text('theorem other : True := True.intro\n')
        result = self.compile('theorem target : False := by trivial\n',
                              build_command=[self.compiler, 'Other.lean'])
        self.assertFalse(result.build_passed)
        self.assertEqual(result.outcome, 'LEAN_PROOF_GAP')
        self.assertFalse(result.axiom_audit_passed)

    def test_imported_hidden_axiom_is_found_without_source_audit_commands(self):
        (self.root/'Dependency.lean').write_text('axiom hidden : False\n')
        subprocess.run([self.compiler, '-o', 'Dependency.olean', 'Dependency.lean'],
                       cwd=self.root, capture_output=True, check=True)
        result = self.compile('import Dependency\ntheorem target : False := hidden\n')
        self.assertEqual(result.outcome, 'UNAPPROVED_AXIOM')
        self.assertIn('UNAPPROVED_AXIOM:hidden', result.findings)

    def test_real_placeholder_is_rejected(self):
        result = self.compile('theorem target : False := by sorry\n')
        self.assertEqual(result.outcome, 'SORRY_FOUND')

    def test_missing_target_is_rejected(self):
        result = self.compile('theorem other : True := True.intro\n')
        self.assertNotEqual(result.outcome, 'PASS')

    def test_namespaced_target_with_prime_is_audited(self):
        result = self.compile("namespace Example\ntheorem target' : True := True.intro\nend Example\n",
                              targets=["Example.target'"])
        self.assertEqual(result.outcome, 'PASS')
