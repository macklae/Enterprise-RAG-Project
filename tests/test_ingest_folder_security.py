"""Security tests for folder ingestion."""
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient
from app import api


class FolderIngestionSecurity(unittest.TestCase):
    def test_unknown_document_blocks_all_jobs(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            (folder / "SOP-OPS-001.pdf").write_bytes(b"test")
            (folder / "unknown_document.pdf").write_bytes(b"test")

            submitted = []

            def fake_submit(*args, **kwargs):
                submitted.append((args, kwargs))
                return SimpleNamespace(id="test-job")

            fake_svc = SimpleNamespace(
                settings=SimpleNamespace(documents_dir=folder),
                jobs=SimpleNamespace(submit=fake_submit),
            )

            with patch.object(api, "svc", fake_svc):
                with patch.object(api, "_identity", return_value=SimpleNamespace(role="sre_ops")):
                    with TestClient(api.app) as client:
                        # Call the endpoint function directly to isolate
                        # folder validation from authentication middleware.
                        request = SimpleNamespace(
                            state=SimpleNamespace(
                                auth_user={
                                    "role": "sre_ops",
                                    "manager_name": None,
                                }
                            )
                        )

                        from fastapi import HTTPException

                        with self.assertRaises(HTTPException) as caught:
                            api.ingest_folder(request)

                        self.assertEqual(caught.exception.status_code, 400)
                        self.assertIn(
                            "unknown_document.pdf",
                            caught.exception.detail["files"],
                        )
                        self.assertEqual(submitted, [])


if __name__ == "__main__":
    unittest.main()
