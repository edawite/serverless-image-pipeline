#!/bin/bash

# Invoke the Lambda locally with SAM CLI using a sample event
sam local invoke ThumbnailFunction \
    --event tests/sample_event.json \
    --env-vars iac/params.dev.json \
    --profile default
