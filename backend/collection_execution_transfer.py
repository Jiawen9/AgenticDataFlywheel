"""Receive physical segments with the unchanged v2 verifier, outside UI run records."""
from .data_store import RecordStore
from .collection_runs import CollectionRunStore
from .collection_transfer import CollectionTransferManager
from .remote_sync import RemoteSyncPolicy


class ExecutionRecords(RecordStore):
    @staticmethod
    def _namespace(value):
        return "collection_executions" if value == "collection_runs" else value

    def get(self, namespace, key):
        return super().get(self._namespace(namespace), key)

    def list(self, namespace):
        return super().list(self._namespace(namespace))

    def put(self, namespace, key, payload, expected_revision=None):
        return super().put(self._namespace(namespace), key, payload, expected_revision)

    def update(self, namespace, key, mutator, default=None):
        return super().update(self._namespace(namespace), key, mutator, default)


class ExecutionTransfer(CollectionTransferManager):
    def __init__(self, root, remote):
        records = ExecutionRecords(root)
        runs = CollectionRunStore(root)
        runs.records = records
        super().__init__(root, runs, remote, sync_policy=RemoteSyncPolicy(records))
        self.records = records
