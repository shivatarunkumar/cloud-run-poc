"""GCS helpers: download, upload, move (copy + delete) and delete."""

import contextlib
import logging
from pathlib import Path

from google.api_core import exceptions as gexc
from google.cloud import storage

log = logging.getLogger(__name__)


def gcs_uri(bucket: str, name: str) -> str:
    return f"gs://{bucket}/{name}"


class GcsStorage:
    def __init__(self, client: storage.Client) -> None:
        self._client = client

    def download(self, bucket: str, name: str, dest: Path, generation: str | None = None) -> None:
        blob = self._client.bucket(bucket).blob(name, generation=int(generation) if generation else None)
        blob.download_to_filename(str(dest))

    def upload(self, src: Path, bucket: str, name: str, content_type: str = "text/csv") -> str:
        self._client.bucket(bucket).blob(name).upload_from_filename(str(src), content_type=content_type)
        return gcs_uri(bucket, name)

    def move(
        self, src_bucket: str, name: str, dst_bucket: str, dst_name: str, generation: str | None = None
    ) -> str:
        """Copy the object (pinned to `generation`) then delete the source.

        The delete is conditional on the generation, so a newer upload with the same name that
        landed in the meantime is left alone for its own notification.
        """
        gen = int(generation) if generation else None
        source = self._client.bucket(src_bucket).blob(name, generation=gen)
        dest = self._client.bucket(dst_bucket).blob(dst_name)
        token, _, _ = dest.rewrite(source)
        while token is not None:
            token, _, _ = dest.rewrite(source, token=token)
        try:
            self._client.bucket(src_bucket).blob(name).delete(if_generation_match=gen)
        except (gexc.NotFound, gexc.PreconditionFailed):
            log.warning("Source %s (generation %s) already gone or replaced; not deleted", name, generation)
        return gcs_uri(dst_bucket, dst_name)

    def delete(self, bucket: str, name: str) -> None:
        with contextlib.suppress(gexc.NotFound):
            self._client.bucket(bucket).blob(name).delete()
