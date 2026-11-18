"""Native physical port assembly; each operation keeps its own implementation."""
from agent_run.adapters.remote import RemoteEffects
from agent_run.adapters.remote_observation import RemoteObservation


class NativeOperations(RemoteObservation, RemoteEffects):
    """Compose read observations and authorized Effects for OperationRunner."""
