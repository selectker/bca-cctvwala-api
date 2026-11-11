"""In-memory stand-in for the boto3 S3 client (R2) used by the tests."""
from datetime import datetime, timezone


class FakeS3:
    class exceptions:
        class NoSuchKey(Exception):
            pass

    def __init__(self):
        self.objects: dict[str, dict] = {}
        self.buckets: dict[str, set] = {}
        self._n = 0

    def put_object(self, Bucket, Key, Body, ContentType=None, CacheControl=None):
        self._n += 1
        self.objects[Key] = {"body": Body, "type": ContentType, "cache": CacheControl, "n": self._n}
        self.buckets.setdefault(Bucket, set()).add(Key)

    def get_object(self, Bucket, Key):
        if Key not in self.objects:
            raise self.exceptions.NoSuchKey()
        body = self.objects[Key]["body"]
        return {"Body": type("B", (), {"read": lambda self_: body})()}

    def delete_object(self, Bucket, Key):
        self.objects.pop(Key, None)

    def upload_file(self, path, Bucket, Key, ExtraArgs=None):
        self._n += 1
        with open(path, "rb") as f:
            self.objects[Key] = {"body": f.read(), "type": (ExtraArgs or {}).get("ContentType"), "n": self._n}
        self.buckets.setdefault(Bucket, set()).add(Key)

    def download_file(self, Bucket, Key, path):
        if Key not in self.objects:
            raise Exception("An error occurred (404) when calling the HeadObject operation: Not Found")
        with open(path, "wb") as f:
            f.write(self.objects[Key]["body"])

    def get_paginator(self, name):
        outer = self

        class P:
            def paginate(self, Bucket, Prefix):
                yield {"Contents": [
                    {"Key": k, "Size": len(v["body"]), "LastModified": datetime.fromtimestamp(1_700_000_000 + v["n"], timezone.utc)}
                    for k, v in outer.objects.items() if k.startswith(Prefix)
                ]}
        return P()
