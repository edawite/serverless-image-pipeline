"""
Helper functions for the image processing Lambda function.

This module encapsulates S3 I/O and image conversion logic. It downloads
images from S3, validates their type, generates one or more WebP
thumbnails preserving aspect ratio, and uploads the results to an output
bucket. The functions are deliberately pure and testable; all side effects
(S3 and image operations) are injected via parameters.
"""

from __future__ import annotations

import io
import logging
import time
from typing import Dict, Iterable, List, Tuple

from PIL import Image

# Use a module‑level logger. The handler config is defined in handler.py.
logger = logging.getLogger(__name__)


def parse_sizes(sizes_str: str) -> List[int]:
    """Parse a comma‑separated string of integers into a list of ints.

    Parameters
    ----------
    sizes_str:
        A comma‑separated list such as "128,512". Whitespace is ignored.

    Returns
    -------
    list of int
        Sorted ascending list of unique sizes. Invalid entries are ignored.
    """
    sizes: List[int] = []
    for part in sizes_str.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            size = int(part)
            if size > 0:
                sizes.append(size)
        except ValueError:
            logger.warning("Ignoring non‑integer thumbnail size '%s'", part)
    return sorted(set(sizes))


def process_image(
    s3_client,
    input_bucket: str,
    object_key: str,
    output_bucket: str,
    sizes_str: str,
    quality: str | int,
) -> Dict[str, object]:
    """Download an object from S3 and generate WebP thumbnails.

    Parameters
    ----------
    s3_client:
        boto3 S3 client used for downloading and uploading objects.
    input_bucket:
        Name of the bucket containing the original image.
    object_key:
        Key of the object within the input bucket.
    output_bucket:
        Target bucket where thumbnails will be stored.
    sizes_str:
        Comma‑separated list of widths in pixels, forwarded to :func:`parse_sizes`.
    quality:
        WebP quality setting (1‑100). Strings are coerced to int.

    Returns
    -------
    dict
        A dictionary containing sizes generated, the total input and output
        bytes processed, and the duration in milliseconds. Used for metric
        publication and debugging.

    Raises
    ------
    Exception
        Reraises any underlying error to signal to the caller that the
        operation failed.
    """

    start = time.perf_counter()
    # Download the object from S3 into memory. This avoids writing to /tmp and
    # simplifies local testing. For large objects consider streaming to disk.
    try:
        resp = s3_client.get_object(Bucket=input_bucket, Key=object_key)
        body: bytes = resp["Body"].read()
        input_size = len(body)
    except Exception as exc:
        logger.error("Failed to download object %s/%s: %s", input_bucket, object_key, exc)
        raise

    # Attempt to open the image. Pillow automatically infers the format.
    try:
        img = Image.open(io.BytesIO(body))
    except Exception as exc:
        logger.error("Unsupported or corrupt image file %s/%s: %s", input_bucket, object_key, exc)
        raise

    # Convert quality to int with fallback to default
    try:
        quality_int = int(quality)
    except Exception:
        quality_int = 85

    sizes = parse_sizes(sizes_str)
    output_size_total = 0
    generated_sizes: List[int] = []

    for width in sizes:
        # Compute height while preserving aspect ratio. If the source image is
        # smaller than the target size, skip upscale to avoid blurry results.
        if img.width <= width:
            logger.info("Skipping upscale for %s (original width %d <= target %d)", object_key, img.width, width)
            continue
        ratio = width / float(img.width)
        height = int(img.height * ratio)
        resized = img.resize((width, height), Image.LANCZOS)
        # Encode as WebP into an in‑memory buffer.
        buffer = io.BytesIO()
        resized.save(buffer, format="WEBP", quality=quality_int, method=6)
        buffer.seek(0)
        data = buffer.getvalue()
        output_size_total += len(data)
        # Construct destination key by inserting width before the file extension.
        dest_key = derive_output_key(object_key, width)
        try:
            s3_client.put_object(
                Bucket=output_bucket,
                Key=dest_key,
                Body=data,
                ContentType="image/webp",
                Metadata={"source": object_key, "size": str(width)},
            )
            generated_sizes.append(width)
        except Exception as exc:
            logger.error("Failed to upload thumbnail %s: %s", dest_key, exc)
            raise

    duration_ms = int((time.perf_counter() - start) * 1000)
    return {
        "sizes": generated_sizes,
        "input_size": input_size,
        "output_size": output_size_total,
        "duration_ms": duration_ms,
    }


def derive_output_key(src_key: str, width: int) -> str:
    """Derive a destination key for a thumbnail based on the source key.

    The new filename appends ``_{width}w.webp`` before the extension of the
    original filename. For example, ``uploads/cat.jpg`` with width ``128``
    becomes ``uploads/cat_128w.webp``.

    Parameters
    ----------
    src_key:
        The original object key.
    width:
        The width of the thumbnail.

    Returns
    -------
    str
        The derived object key for the thumbnail.
    """
    if "/" in src_key:
        prefix, filename = src_key.rsplit("/", 1)
        base, *_ = filename.split(".")
        return f"{prefix}/{base}_{width}w.webp"
    base, *_ = src_key.split(".")
    return f"{base}_{width}w.webp"
