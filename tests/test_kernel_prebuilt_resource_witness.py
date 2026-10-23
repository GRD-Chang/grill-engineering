"""Ordinary native resource material retains only its actual owned fields."""
from agent_run.model import ResourceInspection, ResourceWitness, inspection_wire, resource_wire


def test_ordinary_resource_wire_keeps_its_existing_material_shape():
    original = dict(subject='ticket:2', generation=1, kind='development', checkout='/private/work/ticket-2',
        owned_root='/private/work', common_git_dir='/private/repo/.git', source_ref='refs/heads/ticket-2', head_sha='1' * 40)
    witness = ResourceWitness(**original)
    assert resource_wire(witness) == original
    assert inspection_wire(ResourceInspection(witness, 'clean', '1' * 40, 'worktree_owned')) == {
        'resource': original, 'disposition': 'clean', 'head_sha': '1' * 40, 'reason': 'worktree_owned'}
