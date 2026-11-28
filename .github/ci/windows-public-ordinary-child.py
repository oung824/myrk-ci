"""Expose ordinary case registration to two offline CLI probes."""

import os
import sys


def ordinary_registry(*, quality_packet=None):
    if quality_packet is not None:
        raise ValueError("The ordinary CLI probes cannot use a quality packet.")
    from tests.support.agent_acceptance import CaseRegistry
    from tests.support.agent_acceptance.catalogue import complete_catalogue
    from tests.support.agent_acceptance.ordinary import register

    registry = CaseRegistry()
    register(registry)
    complete_catalogue(registry)
    return registry


if (
    os.environ.get("GITHUB_ACTIONS") == "true"
    and os.environ.get("RUNNER_ENVIRONMENT") == "github-hosted"
    and tuple(sys.orig_argv[1:5]) in (
        ("-B", "-m", "tests.support.agent_acceptance", "verify-prepared"),
        ("-B", "-m", "tests.support.agent_acceptance.formal_cli", "execute"),
    )
):
    from tests.support import agent_acceptance

    agent_acceptance.builtin_registry = ordinary_registry
