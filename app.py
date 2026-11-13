"""CDK app entry. Context:

-c config=<path>        the configuration file (default config/example.json, which only synths)
-c web_image=sha256:…   image digests for the JHE and MCP images (the deploy script passes them)
-c mcp_image=sha256:…
-c mcp_enabled=false    deploy without the MCP service this time (the deploy script passes it
                        until the MCP client is registered in JHE and its secret filled)
-c nag=off              skip the cdk-nag AWS Solutions checks
"""

from pathlib import Path

import aws_cdk as cdk
from cdk_nag import AwsSolutionsChecks

from jhe_aws.config import load_config
from jhe_aws.images import PLACEHOLDER_DIGEST, Images
from jhe_aws.stacks import build_stacks

app = cdk.App()
config_path = Path(app.node.try_get_context("config") or "config/example.json")
config = load_config(config_path)
if str(app.node.try_get_context("mcp_enabled")).lower() == "false":
    config = config.model_copy(
        update={
            "compute": config.compute.model_copy(
                update={"mcp": config.compute.mcp.model_copy(update={"enabled": False})}
            )
        }
    )
images = Images(
    web=app.node.try_get_context("web_image") or PLACEHOLDER_DIGEST,
    mcp=app.node.try_get_context("mcp_image") or PLACEHOLDER_DIGEST,
)
stacks = build_stacks(app, config, images)
if images.placeholders:
    cdk.Annotations.of(stacks.compute).add_warning(
        f"placeholder image digest for {', '.join(images.placeholders)}: synth only, do not deploy"
    )
if app.node.try_get_context("nag") != "off":
    cdk.Aspects.of(app).add(AwsSolutionsChecks(verbose=True))
app.synth()
