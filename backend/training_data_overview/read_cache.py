"""Versioned in-process read model; source catalog remains authoritative."""
from collections import OrderedDict
from copy import deepcopy
import json
import threading
from ..file_io import io_path


class OverviewReadCache:
    def __init__(self, records, catalog_namespace, conversion_namespace, summarize, limit=32):
        self.records, self.catalog_namespace = records, catalog_namespace
        self.conversion_namespace, self.summarize = conversion_namespace, summarize
        self.limit = limit
        self._lock = threading.RLock()
        self._version = None
        self._catalog = None
        self._conversions = []
        self._queries = OrderedDict()

    def query(self, filters):
        # Only this private read-cache lock is held; no publication/registry lock.
        # Concurrent identical requests share one decode and aggregation.
        with self._lock:
            version = None
            catalog_text, conversion_texts, catalog_changed = None, None, False
            if io_path(self.records.database_path).exists():
                with self.records._connection() as connection:
                    connection.execute('PRAGMA busy_timeout=250')
                    connection.execute('BEGIN')
                    exists = connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='records'").fetchone()
                    if exists:
                        catalog_meta = connection.execute('SELECT record_key,revision,updated_at FROM records WHERE namespace=? AND record_key=?', (self.catalog_namespace, 'current')).fetchone()
                        conversion_meta = connection.execute('SELECT record_key,revision,updated_at FROM records WHERE namespace=? ORDER BY record_key', (self.conversion_namespace,)).fetchall()
                        version = (catalog_meta, tuple(conversion_meta))
                        if version != self._version:
                            # A conversion status change must update warnings, but
                            # does not require decoding the large catalog again.
                            catalog_changed = not self._version or catalog_meta != self._version[0]
                            if catalog_changed:
                                row = connection.execute('SELECT payload FROM records WHERE namespace=? AND record_key=?', (self.catalog_namespace, 'current')).fetchone()
                                catalog_text = row[0] if row else None
                            conversion_texts = connection.execute('SELECT payload FROM records WHERE namespace=? ORDER BY updated_at,record_key', (self.conversion_namespace,)).fetchall()
                    connection.rollback()
            # Decode and aggregate after releasing the SQLite read transaction.
            if version is not None and version != self._version:
                if catalog_changed:
                    self._catalog = self._decode_catalog(catalog_text)
                self._conversions = [json.loads(row[0]) for row in conversion_texts or []]
                self._queries.clear()
                self._version = version
            if version is None:
                self._catalog, self._conversions, self._version = None, [], None
                self._queries.clear()
            normalized = {key: value for key, value in filters.items() if value not in ('', None) and not (key in {'app', 'source'} and value == 'all')}
            key = tuple(sorted(normalized.items()))
            if key not in self._queries:
                self._queries[key] = self.summarize(self._catalog, self._conversions, normalized)
                while len(self._queries) > self.limit:
                    self._queries.popitem(last=False)
            self._queries.move_to_end(key)
            return deepcopy(self._queries[key])

    @staticmethod
    def _decode_catalog(text):
        return json.loads(text) if text is not None else None
