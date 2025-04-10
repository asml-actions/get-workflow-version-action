import dataclasses
import os
import pathlib
import re

import requests
import typer
from typing_extensions import Annotated

# Do not show locals to avoid displaying `GH_TOKEN`
app = typer.Typer(pretty_exceptions_show_locals=False)


@dataclasses.dataclass(frozen=True)
class ReusableWorkflow:
    sha: str
    repository: str
    file_name: str
    ref: str

    @classmethod
    def from_github_api(cls, *, path: str, sha: str, **_):
        # Example of `path`: "canonical/data-platform-workflows/.github/workflows/build_charm.yaml@v13.1.2"
        match = re.fullmatch(
            r"(?P<repository>.*?)/\.github/workflows/(?P<file_name>.*?)@(?P<ref>.*?)",
            path,
        )
        assert match
        return cls(sha=sha, **match.groupdict())


def validate_github_repository(value: str):
    if not re.fullmatch(r"[a-zA-Z0-9\-]+/[a-zA-Z0-9.\-_]+", value):
        raise typer.BadParameter(f"'{value}' is not a valid GitHub repository name")
    return value


def parse_github_api_url(value: str):
    return value.removesuffix("/")


@app.command()
def main(
    caller_repository: Annotated[
        str,
        typer.Argument(
            callback=validate_github_repository,
            help='Caller workflow GitHub repository. (e.g. "octocat/Hello-World")',
        ),
    ],
    caller_run_id: Annotated[
        int, typer.Argument(help="GitHub workflow run ID (e.g. 8938022468)")
    ],
    reusable_workflow_repository: Annotated[
        str,
        typer.Argument(
            help='Reusable workflow GitHub repository (e.g. "canonical/data-platform-workflows")'
        ),
    ],
    reusable_workflow_file_name: Annotated[
        str,
        typer.Argument(help='Reusable workflow file name (e.g. "build_charm.yaml")'),
    ],
    github_api_url: Annotated[
        str,
        typer.Argument(
            callback=parse_github_api_url,
            help='GitHub REST API URL (e.g. "https://api.github.com"',
        ),
    ] = "https://api.github.com",
):
    """Get commit SHA that GitHub Actions reusable workflow was called with

    Workaround for https://github.com/actions/toolkit/issues/1264

    When a reusable workflow is called, the `github` context is always associated with the caller workflow

    https://docs.github.com/en/actions/using-workflows/reusing-workflows#overview

    If a reusable workflow needs to checkout files (e.g. a Python script) from its repository
    —instead of the repository of the caller workflow—
    it needs to know what version it was called with.
    """
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    token = os.environ.get("GH_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    response = requests.get(
        f"{github_api_url}/repos/{caller_repository}/actions/runs/{caller_run_id}",
        headers=headers,
    )
    try:
        response.raise_for_status()
    except requests.HTTPError as exception:
        if exception.response.status_code == 403:
            raise Exception(
                "Ensure that GitHub job has `permissions: actions: read`. GitHub API call 403 forbidden\n"
                "https://docs.github.com/en/actions/security-guides/automatic-token-authentication#modifying-the-permissions-for-the-github_token"
            )
        elif exception.response.status_code == 404:
            response = requests.get(
                f"{github_api_url}/repos/{caller_repository}", headers=headers
            )
            if response.status_code == 404:
                if token:
                    raise Exception(
                        f"{caller_repository=} not found. Check if `github-token` input has correct permissions"
                    )
                raise Exception(
                    f"{caller_repository=} not found. If repository is private, pass `github-token` input to authenticate to GitHub"
                )
            raise Exception(
                f"Workflow run not found. Check if {caller_run_id=} is valid"
            )
        raise
    all_workflows = [
        ReusableWorkflow.from_github_api(**workflow)
        for workflow in response.json()["referenced_workflows"]
    ]
    workflows = {
        workflow
        for workflow in all_workflows
        if workflow.repository == reusable_workflow_repository
        and workflow.file_name == reusable_workflow_file_name
    }
    if not workflows:
        raise ValueError(
            f"No reference found for {reusable_workflow_file_name=} {reusable_workflow_repository=}"
        )
    elif len(workflows) > 1:
        message = f"Cannot resolve multiple versions found for {reusable_workflow_file_name=} {reusable_workflow_repository=}:"
        for workflow in workflows:
            message += f"\n• {workflow.ref} ({workflow.sha})"
        raise ValueError(message)
    workflow = workflows.pop()
    print(f"Reusable workflow version: {workflow.sha} (ref: {workflow.ref})")
    if output := os.environ.get("GITHUB_OUTPUT"):
        with pathlib.Path(output).open("a") as file:
            file.write(f"sha={workflow.sha}\n")
