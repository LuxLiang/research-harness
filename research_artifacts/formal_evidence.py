"""Controller-owned, portable Lean execution receipts and acceptance checks.

Receipts are committed alongside reports. They assume a trusted host filesystem;
model resource tools can write only action resources, not this reserved directory.
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any, Mapping

from .runtime_types import RuntimeValidationError, atomic_write_json, sha256_json

APPROVED_AXIOMS = {"propext", "Classical.choice", "Quot.sound"}


def scientific_hash(claim: Mapping[str, Any]) -> str:
    return sha256_json({key: claim.get(key) for key in (
        "id", "project_id", "claim_type", "statement", "assumptions", "dependencies",
    )})


def claim_inputs(root: Path, claim: Mapping[str, Any]) -> dict[str, Any]:
    sources = []
    mappings = []
    resources = (root / 'projects' / str(claim['project_id']) / 'resources').resolve()
    for resource in claim.get('verification', {}).get('resource_refs', []):
        uri = resource.get('uri', '')
        if '://' in uri:
            continue
        path = (root / uri).resolve()
        if not path.is_relative_to(resources) or not path.is_file():
            continue
        if hashlib.sha256(path.read_bytes()).hexdigest() != resource.get('sha256'):
            raise RuntimeValidationError('Formal Claim resource hash mismatch')
        if path.suffix == '.lean':
            sources.append({'uri': uri, 'sha256': resource['sha256']})
        elif path.suffix == '.json':
            value = json.loads(path.read_text())
            if isinstance(value, dict) and 'target_theorems' in value:
                mappings.append(value['target_theorems'])
    if not sources or len(mappings) != 1 or not isinstance(mappings[0], dict):
        raise RuntimeValidationError('Formal Claim needs local pinned Lean sources and one target_theorems mapping')
    if not mappings[0] or any(
        not isinstance(path, str) or Path(path).is_absolute() or '..' in Path(path).parts
        or not isinstance(names, list) or not names or not all(isinstance(name, str) and name for name in names)
        for path, names in mappings[0].items()
    ):
        raise RuntimeValidationError('Invalid formal target mapping')
    source_hashes = {}
    matched = set()
    for logical_path in mappings[0]:
        candidates = [source for source in sources if Path(source['uri']).name == Path(logical_path).name]
        if len(candidates) != 1 or candidates[0]['uri'] in matched:
            raise RuntimeValidationError('Each formal source path must identify one uniquely named pinned resource')
        matched.add(candidates[0]['uri'])
        source_hashes[logical_path] = candidates[0]['sha256']
    if len(matched) != len(sources):
        raise RuntimeValidationError('Formal mapping must cover every pinned Lean source')
    return {'source_resources': sorted(sources, key=lambda item: item['uri']), 'source_hashes': source_hashes,
            'target_theorems': mappings[0], 'claim_scientific_sha256': scientific_hash(claim)}


def check_report(report: Mapping[str, Any], inputs: Mapping[str, Any]) -> None:
    if not isinstance(report, dict):
        raise RuntimeValidationError('Lean report must be a JSON object')
    if report.get('adapter_version') != 'research-lean-tool/v0.2':
        raise RuntimeValidationError('Unsupported or missing Lean tool report version')
    hashes = report.get('source_hashes')
    if not isinstance(hashes, dict) or hashes != inputs['source_hashes']:
        raise RuntimeValidationError('Lean report sources differ from the pinned Claim sources')
    if report.get('target_theorems') != inputs['target_theorems'] or set(hashes) != set(inputs['target_theorems']):
        raise RuntimeValidationError('Lean report targets differ from the pinned semantic mapping')
    approved = report.get('allowed_axioms')
    if not isinstance(approved, list) or not set(approved).issubset(APPROVED_AXIOMS):
        raise RuntimeValidationError('Lean report attempts to expand the host axiom policy')
    if report.get('outcome') != 'PASS':
        return
    checks = report.get('source_checks')
    audits = report.get('target_axioms')
    if (type(report.get('build_returncode')) is not int or report['build_returncode'] != 0
            or report.get('findings') != [] or not isinstance(checks, list)
            or len(checks) != len(hashes) or not isinstance(audits, dict)
            or set(audits) != set(hashes)):
        raise RuntimeValidationError('Lean PASS lacks successful compilation and complete audits')
    seen = set()
    for check in checks:
        if not isinstance(check, dict):
            raise RuntimeValidationError('Invalid Lean source compilation record')
        source = check.get('source')
        if (source not in hashes or source in seen or check.get('source_sha256') != hashes[source]
                or type(check.get('returncode')) is not int or check['returncode'] != 0
                or not check.get('command') or check['command'][-1] != '--stdin'):
            raise RuntimeValidationError('Lean PASS has inconsistent source compilation results')
        seen.add(source)
    reported = set()
    for source, targets in inputs['target_theorems'].items():
        if not isinstance(audits[source], dict) or set(audits[source]) != set(targets):
            raise RuntimeValidationError('Lean PASS is missing target axiom audits')
        for axioms in audits[source].values():
            if not isinstance(axioms, list) or not set(axioms).issubset(set(approved)) or 'sorryAx' in axioms:
                raise RuntimeValidationError('Lean PASS depends on forbidden axioms')
            reported.update(axioms)
    if set(report.get('reported_axioms', [])) != reported:
        raise RuntimeValidationError('Lean axiom summary differs from per-target audits')
    stronger = report.get('stronger_checker')
    if stronger is not None and (not isinstance(stronger, dict) or stronger.get('returncode') != 0):
        raise RuntimeValidationError('Lean PASS includes a failed stronger checker')


def record_lean_execution(root: Path, *, report_path: str | Path, claim: Mapping[str, Any],
                          action_id: str, bundle_sha256: str, session_id: str,
                          synthetic: bool = False) -> list[dict[str, str]]:
    """Called by trusted runtimes after invoking Lean, never exposed as a model tool."""
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]*', action_id) or not session_id:
        raise RuntimeValidationError('Invalid Lean execution identity')
    inputs = claim_inputs(root, claim)
    raw = Path(report_path).read_bytes()
    report = json.loads(raw)
    check_report(report, inputs)
    digest = hashlib.sha256(raw).hexdigest()
    directory = root / 'projects' / str(claim['project_id']) / 'resources' / 'formal-executions' / action_id
    directory.mkdir(parents=True, exist_ok=True)
    execution_suffix = sha256_json({'session_id': session_id, 'bundle_sha256': bundle_sha256})[:16]
    stem = f'{digest}-{execution_suffix}'
    copied = directory / f'{stem}.json'
    if copied.exists() and copied.read_bytes() != raw:
        raise RuntimeValidationError('Immutable Lean report conflict')
    copied.write_bytes(raw)
    report_ref = {'uri': str(copied.relative_to(root)), 'sha256': digest,
                  'media_type': 'application/json', 'description': 'Host Lean execution report'}
    receipt = {'receipt_version': 'research-lean-execution/v0.1', 'report': report_ref,
               'claim_ref': {'id': claim['id'], 'kind': 'ScientificClaim', 'revision': claim['revision']},
               'project_id': claim['project_id'], 'action_id': action_id,
               'bundle_sha256': bundle_sha256, 'session_id': session_id,
               'execution_kind': 'SYNTHETIC' if synthetic else 'LEAN', **inputs}
    receipt_path = directory / f'{stem}.receipt.json'
    if receipt_path.exists() and json.loads(receipt_path.read_text()) != receipt:
        raise RuntimeValidationError('Immutable Lean execution receipt conflict')
    atomic_write_json(receipt_path, receipt)
    return [report_ref, {'uri': str(receipt_path.relative_to(root)),
                        'sha256': hashlib.sha256(receipt_path.read_bytes()).hexdigest(),
                        'media_type': 'application/json', 'description': 'Host Lean execution receipt'}]


def validate_formal_review(root: Path, review: Mapping[str, Any], claim: Mapping[str, Any],
                           expected_action: Mapping[str, Any] | None = None) -> None:
    """Reject PASS without one host receipt matching the target and actual report."""
    inputs = claim_inputs(root, claim)
    resources = {item['uri']: item for item in review.get('evidence_resources', [])}
    receipts = []
    for uri, resource in resources.items():
        if not uri.endswith('.receipt.json'):
            continue
        path = (root / uri).resolve()
        reserved = (root / 'projects' / str(claim['project_id']) / 'resources' / 'formal-executions').resolve()
        if not path.is_relative_to(reserved) or not path.is_file():
            continue
        raw = path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != resource['sha256']:
            raise RuntimeValidationError('Lean execution receipt hash mismatch')
        receipts.append((path, json.loads(raw)))
    if len(receipts) != 1:
        raise RuntimeValidationError('Formal PASS requires exactly one host execution receipt')
    path, receipt = receipts[0]
    if not isinstance(receipt, dict):
        raise RuntimeValidationError('Lean receipt must be a JSON object')
    if (receipt.get('receipt_version') != 'research-lean-execution/v0.1'
            or receipt.get('claim_ref') != review.get('target', {}).get('artifact_ref')
            or receipt.get('project_id') != claim['project_id']
            or any(receipt.get(key) != value for key, value in inputs.items())
            or not receipt.get('session_id') or not receipt.get('bundle_sha256')
            or receipt.get('session_id') != review.get('reviewer', {}).get('actor', {}).get('session_id')
            or receipt.get('action_id') != path.parent.name):
        raise RuntimeValidationError('Lean execution receipt does not bind this Claim and verification input')
    if expected_action and any(receipt.get(key) != value for key, value in expected_action.items()):
        raise RuntimeValidationError('Lean execution belongs to another action, context, or session')
    if receipt.get('execution_kind') == 'SYNTHETIC':
        if 'synthetic' not in claim.get('tags', []) or 'synthetic' not in review.get('tags', []):
            raise RuntimeValidationError('Synthetic Lean execution cannot verify a real Claim')
    elif receipt.get('execution_kind') != 'LEAN':
        raise RuntimeValidationError('Unknown Lean execution kind')
    report_ref = receipt.get('report', {})
    if not isinstance(report_ref, dict):
        raise RuntimeValidationError('Invalid Lean report resource reference')
    actual_ref = resources.get(report_ref.get('uri'))
    if actual_ref is None or any(actual_ref.get(key) != report_ref.get(key) for key in ('uri', 'sha256', 'media_type')):
        raise RuntimeValidationError('Formal Review must pin the receipt report resource')
    report_path = (root / report_ref['uri']).resolve()
    if report_path.parent != path.parent or path.name != report_path.stem + '.receipt.json':
        raise RuntimeValidationError('Lean report and execution receipt scope mismatch')
    raw = report_path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != report_ref['sha256']:
        raise RuntimeValidationError('Lean report hash mismatch')
    report = json.loads(raw)
    check_report(report, inputs)
    if report.get('outcome') != 'PASS':
        raise RuntimeValidationError('Formal PASS contradicts the actual tool result')
