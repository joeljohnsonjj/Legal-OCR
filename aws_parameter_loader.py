"""
AWS Parameter Store + AssumeRole loader for Bedrock Claude.

Flow (mirrors claude.py):
  1. Read Role ARN from Parameter Store at LLM_PARAMETER_PATH
     (e.g. /ai/bedrock/claude_role_arn  -> arn:aws:iam::888491521756:role/Claudebedrock-Access-Role)
  2. Assume that role via STS to get temporary credentials
  3. Store temp credentials in environment so LiteLLM can pass them to Bedrock

LLM_MODEL in .env sets the Bedrock model id (must start with bedrock/).
"""
import logging
import os
import sys
from pathlib import Path

import boto3
from botocore.exceptions import ClientError, NoCredentialsError

try:
    from dotenv import load_dotenv
    _script_dir = Path(__file__).parent
    _env_path = _script_dir / ".env"
    if _env_path.exists():
        load_dotenv(dotenv_path=_env_path)
except ImportError:
    pass


logger = logging.getLogger(__name__)

# Set after init_llm_env() is called
config = None

_sanitize = lambda s: (s or "").replace("\r", "").replace("\n", "").strip()


def get_parameter_from_store(parameter_path, region_name=None, decrypt=True):
    """Retrieve a parameter from AWS Parameter Store.

    Returns:
        The parameter value as a string.
    """
    region = region_name or os.getenv("AWS_REGION", "us-east-1")
    if not region:
        raise ValueError("AWS_REGION must be set in .env or passed as region_name")

    try:
        ssm_client = boto3.client("ssm", region_name=region)
        response = ssm_client.get_parameter(Name=parameter_path, WithDecryption=decrypt)
        return response["Parameter"]["Value"]
    except ClientError as e:
        error_code = e.response.get("Error", {}).get("Code", "")
        if error_code == "ParameterNotFound":
            raise ValueError(
                f"Parameter '{parameter_path}' not found in AWS Parameter Store."
            ) from e
        if error_code == "AccessDeniedException":
            raise PermissionError(
                f"Access denied to parameter '{parameter_path}'. "
                "Check IAM permissions (ssm:GetParameter, kms:Decrypt)."
            ) from e
        raise ValueError(
            f"AWS Parameter Store error: {e.response.get('Error', {}).get('Message', str(e))}"
        ) from e
    except NoCredentialsError as e:
        raise NoCredentialsError(
            "AWS credentials not found. Set AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY in .env."
        ) from e


def assume_bedrock_role(role_arn, region_name=None):
    """Assume the Bedrock role and return temporary credentials dict.

    Returns:
        dict with AccessKeyId, SecretAccessKey, SessionToken
    """
    region = region_name or os.getenv("AWS_REGION", "us-east-1")
    sts = boto3.client("sts", region_name=region)
    creds = sts.assume_role(
        RoleArn=role_arn,
        RoleSessionName="legal-ocr-session",
    )["Credentials"]
    return creds


def load_llm_config(parameter_path=None, model_name=None, region_name=None):
    """Load LLM configuration: read Role ARN from Parameter Store, assume role.

    Returns:
        dict with keys: model, region, role_arn, access_key_id, secret_access_key, session_token
    """
    param_path = parameter_path or os.getenv("LLM_PARAMETER_PATH")
    region = _sanitize(region_name or os.getenv("AWS_REGION", "us-east-1"))
    model = _sanitize(model_name or os.getenv("LLM_MODEL"))

    if not param_path:
        raise ValueError("LLM_PARAMETER_PATH must be set in .env file")
    if not model:
        raise ValueError("LLM_MODEL must be set in .env file (e.g. bedrock/anthropic.claude-3-haiku-20240307-v1:0)")

    # Step 1: read Role ARN from Parameter Store
    role_arn = _sanitize(get_parameter_from_store(param_path, region_name=region))

    # Step 2: assume role to get temporary Bedrock credentials
    creds = assume_bedrock_role(role_arn, region_name=region)

    return {
        "model": model,
        "region": region,
        "role_arn": role_arn,
        "access_key_id": _sanitize(creds["AccessKeyId"]),
        "secret_access_key": _sanitize(creds["SecretAccessKey"]),
        "session_token": _sanitize(creds["SessionToken"]),
        "api_key": "",  # IAM via AssumeRole; no API key used
    }


def init_llm_env(parameter_path=None, model_name=None, region_name=None):
    """Load Bedrock config (AssumeRole) and set env vars for LiteLLM.

    After this call:
      - LITELLM_MODEL = bedrock/<model-id>
      - AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY, AWS_SESSION_TOKEN = temp role creds
      - AWS_REGION_NAME = region

    Returns:
        The config dict if successful, None if loading failed.
    """
    global config
    try:
        config = load_llm_config(
            parameter_path=parameter_path,
            model_name=model_name,
            region_name=region_name,
        )
        model = config["model"]
        region = config["region"]

        # Set model for LiteLLM
        os.environ["LITELLM_MODEL"] = model
        os.environ["LLM_MODEL"] = model
        os.environ["LLM_REGION"] = region
        os.environ["AWS_REGION_NAME"] = region
        os.environ.setdefault("AWS_REGION", region)

        # Set temporary credentials so LiteLLM/boto3 uses the assumed role
        os.environ["AWS_ACCESS_KEY_ID"] = config["access_key_id"]
        os.environ["AWS_SECRET_ACCESS_KEY"] = config["secret_access_key"]
        os.environ["AWS_SESSION_TOKEN"] = config["session_token"]

        logger.info("Bedrock role assumed, model=%s region=%s", model, region)
        return config
    except (ValueError, PermissionError, NoCredentialsError) as e:
        logger.warning("Could not load LLM configuration: %s", e)
        config = None
        return None


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    if init_llm_env() is None:
        logger.error(
            "Failed to load configuration. Check .env (LLM_PARAMETER_PATH, LLM_MODEL, "
            "AWS credentials) and AWS Parameter Store."
        )
        sys.exit(1)
    print(f"Model: {os.environ.get('LITELLM_MODEL')}")
    print(f"Region: {os.environ.get('AWS_REGION_NAME')}")
    print(f"Role assumed: {config['role_arn']}")
