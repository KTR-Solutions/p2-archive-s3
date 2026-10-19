"""S3 GetObject, PutObject, and presigned URLs against the archive service."""

from __future__ import annotations

import sys
import threading
import unittest
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

sys.path.insert(0, str(Path(__file__).resolve().parent))

from archive import MemoryStore, archive_path, guid_from_key  # noqa: E402
from server import serve  # noqa: E402
from sigv4 import AuthFailure, check_time  # noqa: E402

GUID = "d87eea08-a215-4e9b-b136-6155d8a0476f"
ACCESS = "AKIATEST"
SECRET = "secretsecretsecretsecret"
REGION = "us-east-1"
JPEG = b"\xff\xd8\xff\xe0hello"


class ArchiveS3Test(unittest.TestCase):
    def setUp(self) -> None:
        self.store = MemoryStore()
        self.httpd = serve(self.store, ACCESS, SECRET, REGION, "127.0.0.1", 0)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        port = self.httpd.server_address[1]
        self.client = boto3.client(
            "s3",
            aws_access_key_id=ACCESS,
            aws_secret_access_key=SECRET,
            region_name=REGION,
            endpoint_url=f"http://127.0.0.1:{port}",
            config=Config(
                signature_version="s3v4",
                s3={"addressing_style": "path"},
                retries={"max_attempts": 1},
            ),
        )

    def tearDown(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()

    def test_put_and_get_round_trip(self) -> None:
        self.client.put_object(Bucket="01003", Key=GUID, Body=JPEG, ContentType="image/jpeg")
        self.assertEqual(self.store.files[("01003", GUID)], JPEG)
        response = self.client.get_object(Bucket="01003", Key=GUID)
        self.assertEqual(response["Body"].read(), JPEG)
        self.assertEqual(response["ContentType"], "image/jpeg")

    def test_presigned_get_and_put(self) -> None:
        self.store.write("01003", GUID, JPEG)
        get_url = self.client.generate_presigned_url(
            "get_object",
            Params={"Bucket": "01003", "Key": GUID},
            ExpiresIn=60,
        )
        with urllib.request.urlopen(get_url) as response:
            self.assertEqual(response.read(), JPEG)

        put_url = self.client.generate_presigned_url(
            "put_object",
            Params={"Bucket": "01003", "Key": GUID},
            ExpiresIn=60,
        )
        request = urllib.request.Request(put_url, data=b"replaced", method="PUT")
        with urllib.request.urlopen(request) as response:
            self.assertEqual(response.status, 200)
        self.assertEqual(self.store.files[("01003", GUID)], b"replaced")

    def test_missing_key_and_bad_signature(self) -> None:
        with self.assertRaises(ClientError) as missing:
            self.client.get_object(Bucket="01003", Key=GUID)
        self.assertEqual(missing.exception.response["Error"]["Code"], "NoSuchKey")

        wrong = boto3.client(
            "s3",
            aws_access_key_id=ACCESS,
            aws_secret_access_key="not-the-secret",
            region_name=REGION,
            endpoint_url=self.client.meta.endpoint_url,
            config=Config(
                signature_version="s3v4",
                s3={"addressing_style": "path"},
                retries={"max_attempts": 1},
            ),
        )
        with self.assertRaises(ClientError) as denied:
            wrong.get_object(Bucket="01003", Key=GUID)
        self.assertEqual(denied.exception.response["Error"]["Code"], "SignatureDoesNotMatch")

    def test_health_and_path_rules(self) -> None:
        port = self.httpd.server_address[1]
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/health") as response:
            self.assertEqual(response.read(), b"ok\n")
        self.assertEqual(guid_from_key(f"Archiv/{GUID}"), GUID)
        with self.assertRaises(ValueError):
            guid_from_key("../" + GUID)
        root = Path(self.id().replace(".", "_"))
        # archive_path stays under the share root for a normal division and guid.
        path = archive_path(str(root), "01003", GUID)
        self.assertEqual(path.name, GUID)
        self.assertIn("Excellent", path.parts)

    def test_expired_presign_is_refused(self) -> None:
        old = (datetime.now(timezone.utc) - timedelta(hours=2)).strftime("%Y%m%dT%H%M%SZ")
        with self.assertRaises(AuthFailure):
            check_time(old, 60, datetime.now(timezone.utc))


if __name__ == "__main__":
    unittest.main()
