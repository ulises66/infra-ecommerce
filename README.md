# Infra Ecommerce CDK Stack

This project provisions an end-to-end environment for a simple ecommerce
application using AWS CDK (Python). It creates the following resources:

- A VPC with public subnets for the application load balancer and private
  isolated subnets for the database.
- An Amazon ECS cluster with two Fargate services (frontend and backend).
- An Application Load Balancer that routes `/api` paths to the backend service
  and all other requests to the frontend service.
- An Amazon RDS MySQL database with credentials stored in AWS Secrets Manager.

## Container images

The stack now builds placeholder container images automatically during
deployment so that the ECS tasks can start without requiring you to push images
manually. The Docker contexts live under `container_images/`:

- `container_images/frontend` serves a static HTML page on port `3000` using the
  Python `http.server` module.
- `container_images/backend` provides a lightweight JSON API on port `4000` and
  echoes the database connection information from its environment variables.

To swap in your own application code, replace the contents of these directories
with your Dockerfile and supporting files, then deploy again. The `Frontend` and
`Backend` outputs in the CloudFormation stack list the relative paths so that
other team members can easily locate the build contexts.

> **Note:** The CDK stack builds both images for the `linux/amd64` platform so
> that they run on the default AWS Fargate architecture. If you need to target
> a different runtime (for example Graviton/ARM), adjust the `Platform`
> parameter in `infra_ecommerce_stack.py` accordingly.

## Getting started

1. Create and activate a virtual environment (optional but recommended):

   ```bash
   python3 -m venv .venv
   source .venv/bin/activate
   ```

2. Install the dependencies:

   ```bash
   pip install -r requirements.txt
   ```

3. (Optional) Run the unit tests:

   ```bash
   pytest
   ```

4. Synthesize the CloudFormation template:

   ```bash
   cdk synth
   ```

5. Deploy the stack to your AWS account:

   ```bash
   cdk deploy
   ```

After a successful deployment the CDK output will show the public load balancer
URL. Browse to the URL to view the placeholder frontend. Paths that start with
`/api` are served by the backend container and return JSON data.
