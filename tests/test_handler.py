"""Unit tests for the Lambda handler using moto to mock AWS services."""

from __future__ import annotations

import io
import json
import os
from typing import Any

import boto3
import pytest
from moto import mock_s3, mock_sqs
from PIL import Image

# Import the handler via importlib because ``lambda`` is a reserved keyword.
import importlib.util
import pathlib

# Dynamically load the Lambda handler from the ``lambda`` directory. We avoid
# ``import lambda`` because ``lambda`` is a reserved keyword and cannot be
# imported directly. This pattern makes the tests resilient without changing
# the deployed package name.
handler_path = pathlib.Path(__file__).resolve().parents[1] / "lambda" / "handler.py"
spec = importlib.util.spec_from_file_location("handler", str(handler_path))
_handler_module = importlib.util.module_from_spec(spec)
assert spec and spec.loader  # for mypy type checking
spec.loader.exec_module(_handler_module)  # type: ignore
lambda_handler = _handler_module.lambda_handler  # type: ignore


class ContextStub:
    """Minimal stub for the Lambda context object used in tests."""

    def __init__(self, function_name: str = "test-function") -> None:
        self.function_name = function_name


@mock_s3
@mock_sqs
def test_thumbnail_generation() -> None:
    """Uploading a valid image triggers thumbnail generation for each configured size."""
    # Create buckets in the mocked S3
    region = "us-east-1"
    s3 = boto3.client("s3", region_name=region)
    s3.create_bucket(Bucket="in-bucket")
    s3.create_bucket(Bucket="out-bucket")
    # Create an SQS DLQ
    sqs = boto3.client("sqs", region_name=region)
    q_url = sqs.create_queue(QueueName="test-dlq")['QueueUrl']

    # Generate a test image larger than the largest thumbnail size to avoid skipping
    img = Image.new("RGB", (1024, 768), color="blue")
    buf = io.BytesIO()
    img.save(buf, format="JPEG")
    buf.seek(0)
    s3.put_object(Bucket="in-bucket", Key="uploads/sample.jpg", Body=buf.getvalue())

    # Set required environment variables
    os.environ["OUTPUT_BUCKET"] = "out-bucket"
    os.environ["THUMB_SIZES"] = "128,512"
    os.environ["WEBP_QUALITY"] = "85"
    os.environ["DLQ_URL"] = q_url

    # Prepare a simple S3 event
    event = {
        "Records": [
            {
                "s3": {
                    "bucket": {"name": "in-bucket"},
                    "object": {"key": "uploads/sample.jpg"},
                }
            }
        ]
    }

    response = lambda_handler(event, ContextStub())
    assert response["statusCode"] == 200
    # Verify that thumbnails for both sizes were uploaded
    contents = s3.list_objects_v2(Bucket="out-bucket").get("Contents", [])
    keys = {obj["Key"] for obj in contents}
    assert "uploads/sample_128w.webp" in keys
    assert "uploads/sample_512w.webp" in keys


@mock_s3
@mock_sqs
def test_non_image_raises_and_sends_to_dlq() -> None:
    """Uploading a non‑image file results in a DLQ message when processing fails."""
    region = "us-east-1"
    s3 = boto3.client("s3", region_name=region)
    s3.create_bucket(Bucket="in-bucket")
    s3.create_bucket(Bucket="out-bucket")
    sqs = boto3.client("sqs", region_name=region)
    q_url = sqs.create_queue(QueueName="test-dlq")['QueueUrl']

    # Upload a plain text file that will cause Pillow to fail
    s3.put_object(Bucket="in-bucket", Key="uploads/readme.txt", Body=b"hello world")

    # Set environment variables
    os.environ["OUTPUT_BUCKET"] = "out-bucket"
    os.environ["THUMB_SIZES"] = "128"
    os.environ["WEBP_QUALITY"] = "80"
    os.environ["DLQ_URL"] = q_url

    event = {
        "Records": [
            {
                "s3": {
                    "bucket": {"name": "in-bucket"},
                    "object": {"key": "uploads/readme.txt"},
                }
            }
        ]
    }

    # The handler will raise an exception which we catch here. Lambda runtime
    # would retry or send to its DLQ automatically. We emulate this by
    # catching and ignoring the exception in the test.
    with pytest.raises(Exception):
        lambda_handler(event, ContextStub())

    # The handler should have sent a message to our DLQ
    msgs = sqs.receive_message(QueueUrl=q_url, MaxNumberOfMessages=1).get("Messages", [])
    assert msgs, "Expected a message on the DLQ"
    body = json.loads(msgs[0]["Body"])
    assert body["key"] == "uploads/readme.txt"
