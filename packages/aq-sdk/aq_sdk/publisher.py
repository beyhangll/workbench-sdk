"""Publish an aggregate evaluation comment using separate GitHub credentials."""
from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any, Literal

import httpx

from . import __version__
from .report import _markdown, comment_marker, existing_comment_id, pr_summary

API = "https://api.github.com"
MAX_PAGES = 100
PER_PAGE = 100


@dataclass(frozen=True)
class PublicationResult:
    evaluation_exit_code: int | None
    status: Literal["created", "updated", "unchanged", "reconciled", "failed", "uncertain"]
    comment_id: int | None = None
    error: str | None = None


class _Failure(Exception):
    pass


def _positive_int(value: Any) -> bool:
    return type(value) is int and 0 < value <= 2**63 - 1


def _json(response: httpx.Response) -> Any:
    try:
        return response.json()
    except ValueError:
        raise _Failure("GitHub returned an invalid response.") from None


def _read(client: httpx.Client, path: str, **params: Any) -> httpx.Response:
    try:
        response = client.get(path, params=params)
    except httpx.TransportError:
        raise _Failure("GitHub could not be read. No publication was confirmed.") from None
    if response.status_code != 200:
        raise _Failure(f"GitHub read failed (HTTP {response.status_code}). No publication was confirmed.")
    return response


def _comment(value: Any, issue_url: str) -> dict[str, Any]:
    if (not isinstance(value, dict) or not _positive_int(value.get("id"))
            or not isinstance(value.get("body"), str) or not isinstance(value.get("user"), dict)
            or not _positive_int(value["user"].get("id"))
            or str(value.get("issue_url", "")).lower() != issue_url.lower()):
        raise _Failure("GitHub returned a comment outside the expected contract.")
    return value


def _comments(client: httpx.Client, path: str, issue_url: str) -> list[dict[str, Any]]:
    comments = []
    seen = set()
    for page in range(1, MAX_PAGES + 1):
        response = _read(client, path, per_page=PER_PAGE, page=page)
        values = _json(response)
        if not isinstance(values, list) or len(values) > PER_PAGE:
            raise _Failure("GitHub returned an invalid comment page.")
        for value in values:
            item = _comment(value, issue_url)
            if item["id"] in seen:
                raise _Failure("GitHub comment pages changed during reading. No complete list was confirmed.")
            seen.add(item["id"])
            comments.append(item)
        # Construct each page locally. Never send credentials to a URL from a Link header.
        if "next" not in response.links and len(values) < PER_PAGE:
            return comments
    raise _Failure("The comment pagination limit was reached. No complete list was confirmed.")


def publish_pr_comment(result: dict[str, Any], *, repository: str, pull_request: int, check_key: str,
                       github_token: str, publisher_id: int, reconcile_only: bool = False,
                       transport: httpx.BaseTransport | None = None) -> PublicationResult:
    """Publish once, or reconcile an uncertain earlier write without writing again.

    Supply repository, PR and publisher ID from trusted integration context, never a
    pull-request artifact. Serialize publishers for each repository/PR/check key.
    The evaluation's exit code is retained separately from publication success.
    """
    code = result.get("exit_code") if isinstance(result, dict) else None
    if type(code) is not int or code not in (0, 1, 2, 3):
        return PublicationResult(None, "failed", error="A report with an explicit evaluation exit code is required.")
    try:
        if (not isinstance(repository, str)
                or not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?/[A-Za-z0-9_.-]{1,100}", repository)
                or repository.split("/")[1] in (".", "..") or not _positive_int(pull_request)
                or not _positive_int(publisher_id) or type(reconcile_only) is not bool):
            raise _Failure("A valid repository, pull request, trusted publisher ID and reconciliation mode are required.")
        if (not isinstance(github_token, str) or not github_token or github_token.startswith("aq_")
                or any(c.isspace() or ord(c) < 33 or ord(c) > 126 for c in github_token)):
            raise _Failure("A separate GitHub repository token is required.")
        if code in (0, 1) and (not isinstance(result.get("experiment"), dict)
                              or result["experiment"].get("status") != "completed"):
            raise _Failure("A completed experiment report is required for a judged outcome.")
        try:
            body = comment_marker(check_key) + "\n" + pr_summary(result)
        except (TypeError, ValueError, KeyError, AttributeError):
            raise _Failure("The check key or aggregate evaluation report is invalid.") from None
        if github_token in body or _markdown(github_token) in body:
            raise _Failure("The aggregate comment contains the repository credential.")
        repo_path = f"/repos/{repository}"
        issue_url = f"{API}{repo_path}/issues/{pull_request}"
        path = f"{repo_path}/issues/{pull_request}/comments"
        with httpx.Client(base_url=API, transport=transport, timeout=15, follow_redirects=False, trust_env=False,
                          headers={"Authorization": f"Bearer {github_token}", "Accept": "application/vnd.github+json",
                                   "X-GitHub-Api-Version": "2026-03-10", "User-Agent": f"aq-sdk/{__version__}"}) as client:
            pr = _json(_read(client, f"{repo_path}/pulls/{pull_request}"))
            base = pr.get("base") if isinstance(pr, dict) else None
            base_repo = base.get("repo") if isinstance(base, dict) else None
            if (not isinstance(pr, dict) or type(pr.get("number")) is not int or pr["number"] != pull_request
                    or not isinstance(base_repo, dict) or not isinstance(base_repo.get("full_name"), str)
                    or base_repo["full_name"].lower() != repository.lower()):
                raise _Failure("GitHub did not confirm the intended pull request and repository.")
            comments = _comments(client, path, issue_url)
            chosen = existing_comment_id(comments, key=check_key, author_id=publisher_id)
            if reconcile_only:
                if any(c["id"] == chosen and c["body"] == body for c in comments):
                    return PublicationResult(code, "reconciled", chosen)
                return PublicationResult(code, "uncertain", error="The earlier publication is still unconfirmed. No write was attempted.")
            if chosen is not None:
                current = _comment(_json(_read(client, f"{repo_path}/issues/comments/{chosen}")), issue_url)
                if existing_comment_id([current], key=check_key, author_id=publisher_id) != chosen:
                    raise _Failure("The selected comment's ownership or marker changed. No update was attempted.")
                if current["body"] == body:
                    return PublicationResult(code, "unchanged", chosen)
            method = "POST" if chosen is None else "PATCH"
            write_path = path if chosen is None else f"{repo_path}/issues/comments/{chosen}"
            response = None
            try:
                response = client.request(method, write_path, json={"body": body})
            except httpx.TransportError:
                pass  # A write may have arrived. Reconcile with reads, never repeat it.
            if response is not None and response.status_code in (200, 201):
                try:
                    posted = _comment(_json(response), issue_url)
                    if (existing_comment_id([posted], key=check_key, author_id=publisher_id) == posted["id"]
                            and (chosen is None or chosen == posted["id"]) and posted["body"] == body):
                        return PublicationResult(code, "created" if chosen is None else "updated", posted["id"])
                except _Failure:
                    pass
            elif response is not None and 400 <= response.status_code < 500 and response.status_code != 408:
                raise _Failure(f"GitHub refused publication (HTTP {response.status_code}).")
            try:
                comments = _comments(client, path, issue_url)
                found = existing_comment_id(comments, key=check_key, author_id=publisher_id)
                if any(c["id"] == found and c["body"] == body for c in comments):
                    return PublicationResult(code, "reconciled", found)
            except _Failure:
                pass
            return PublicationResult(code, "uncertain", chosen,
                "Publication may have occurred. Retry with reconcile_only=True. Do not recreate the comment blindly.")
    except _Failure as exc:
        return PublicationResult(code, "uncertain" if reconcile_only is True else "failed", error=str(exc))
