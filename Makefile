.PHONY: lint fmt test build deploy

# Lint code using Ruff
lint:
	@ruff lambda tests

# Format code using Black
fmt:
	@black lambda tests

# Run unit tests with pytest
# Requires local dependencies installed via pip or pipenv
 test:
	p@ytest -q

# Build the SAM application
build:
	@sam build --use-container

# Deploy the SAM application to AWS using parameters file
# The guided deploy will prompt for missing parameters
# Adjust config file or stack name as needed
 deploy:
	@sam deploy --guided --stack-name serverless-image-pipeline --template-file iac/template.yaml --parameter-overrides file://iac/params.dev.json
