"""Which image bytes a deploy runs: digests, never tags.

The deploy script builds and pushes, reads each digest back from ECR and passes it as CDK
context (`-c web_image=sha256:... -c mcp_image=sha256:...`), so every task definition names
exactly the bytes it runs and an unchanged digest is an unchanged stack. A synth without
digests (tests, `cdk diff` of the foundation stacks) uses a placeholder and says so.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
PLACEHOLDER_DIGEST = "sha256:" + "0" * 64


@dataclass(frozen=True)
class Images:
    web: str = PLACEHOLDER_DIGEST
    mcp: str = PLACEHOLDER_DIGEST

    def __post_init__(self) -> None:
        for name, digest in (("web", self.web), ("mcp", self.mcp)):
            if not DIGEST.match(digest):
                raise ValueError(f"{name}_image must be an image digest 'sha256:<64 hex>', got {digest!r}")

    @property
    def placeholders(self) -> list[str]:
        return [n for n, d in (("web", self.web), ("mcp", self.mcp)) if d == PLACEHOLDER_DIGEST]
