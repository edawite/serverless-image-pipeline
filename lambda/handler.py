"""
AWS Lambda entrypoint for the serverless image pipeline.

This handler is triggered by S3 events (ObjectCreated:Put) on the input
bucket. It downloads the uploaded object, generates one or more WebP
thumbnails via helper functions in :mod:`image_utils`, and writes them to
the output bucket. All metrics are published using the aws‑embedded‑metrics
SDK to provide operational visibility in CloudWatch. Logging output is
structured JSON to simplify log analysis.

If processing fails, the function attempts to send a message to a dead
letter queue (DLQ) when a DLQ URL is provided via the ``DLQ_URL``
environment variable. Asynchronous invocation failures are also caught
by the Lambda service and published to the configured DLQ automatically.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Dict

import boto3
from aws_embedded_metrics import metric_scope

from . import image_utils


# Configure structured logging. Using the standard Python logger ensures that
# logs emitted here end up in CloudWatch. Downstream consumers (e.g. Log
# Insights) can parse the JSON output into fields.
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

# Clients are created outside of the handler to take advantage of execution
# environment reuse. Creating clients on every invocation would add latency.
s3_client = boto3.client("s3")
sqs_client = boto3.client("sqs")

# Environment variables defined in the SAM template. Values are coerced to
# appropriate types in :mod:`image_utils`.
OUTPUT_BUCKET = os.environ.get("OUTPUT_BUCKET")
THUMB_SIZES = os.environ.get("THUMB_SIZES", "128,512")
WEBP_QUALITY = os.environ.get("WEBP_QUALITY", "85")
DLQ_URL = os.environ.get("DLQ_URL")


@metric_scope
def lambda_handler(event: Dict[str, Any], context: Any, metrics):  # noqa: D401
    """Handle S3 put events and generate thumbnails.

    Parameters
    ----------
    event:
        The Lambda event payload. Expected to contain an S3 event with one or
        more records.
    context:
        Lambda context object providing runtime metadata.
    metrics:
        Provided by aws‑embedded‑metrics via the ``@metric_scope`` decorator.

    Returns
    -------
    dict
        A simple HTTP‑style status code. The payload is ignored by the caller.
    """

    # Set the metric namespace and dimensions. A dimension is a key/value
    # attribute used to group metrics. Here we group by function name.
    metrics.set_namespace("ImagePipeline")
    metrics.put_dimensions({"FunctionName": context.function_name})

    records = event.get("Records", [])
    for record in records:
        # Extract bucket and key from the event record.
        s3_info = record.get("s3", {})
        bucket = s3_info.get("bucket", {}).get("name")
        key = s3_info.get("object", {}).get("key")
        if not bucket or not key:
            # Emit a structured error log for malformed events. Skip processing
            # instead of raising an exception to avoid sending partial batches
            # to the DLQ.
            logger.error(json.dumps({"error": "Malformed S3 event", "record": record}))
            continue

        logger.info(json.dumps({"action": "start", "bucket": bucket, "key": key}))
        try:
            # Process the image and gather metrics.
            result = image_utils.process_image(
                s3_client=s3_client,
                input_bucket=bucket,
                object_key=key,
                output_bucket=OUTPUT_BUCKET,
                sizes_str=THUMB_SIZES,
                quality=WEBP_QUALITY,
            )
            # Publish custom metrics for each processed object. Units are set
            # explicitly to improve dashboard readability.
            metrics.put_metric("thumbnails_count", len(result["sizes"]), "Count")
            metrics.put_metric("duration_ms", result["duration_ms"], "Milliseconds")
            metrics.put_metric("size_in_bytes", result["input_size"], "Bytes")
            metrics.put_metric("size_out_bytes", result["output_size"], "Bytes")

            logger.info(
                json.dumps(
                    {
                        "action": "complete",
                        "bucket": bucket,
                        "key": key,
                        "thumbnails": result["sizes"],
                    }
                )
            )
        except Exception as exc:
            # Log exception with stack trace. The error message is included
            # separately in the JSON payload for quick searching.
            logger.exception(
                json.dumps(
                    {
                        "action": "error",
                        "bucket": bucket,
                        "key": key,
                        "error": str(exc),
                    }
                )
            )
            # Attempt to send a message to the DLQ with context about the
            # failure. This is best effort; if it fails, let the exception
            # propagate so Lambda can retry or forward to its configured DLQ.
            if DLQ_URL:
                try:
                    sqs_client.send_message(
                        QueueUrl=DLQ_URL,
                        MessageBody=json.dumps(
                            {
                                "bucket": bucket,
                                "key": key,
                                "error": str(exc),
                            }
                        ),
                    )
                    logger.info(
                        json.dumps(
                            {
                                "action": "sent_to_dlq",
                                "bucket": bucket,
                                "key": key,
                            }
                        )
                    )
                except Exception as dlq_exc:  # pragma: no cover - DLQ rarely fails
                    logger.error(
                        json.dumps(
                            {
                                "action": "dlq_failed",
                                "bucket": bucket,
                                "key": key,
                                "error": str(dlq_exc),
                            }
                        )
                    )
            # Propagate error so that Lambda signals a failure. This will
            # trigger the retry/backup behaviour configured on the event source.
            raise
    return {"statusCode": 200}
