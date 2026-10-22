"""Minimal MCP connector for a GitHub-backed markdown vault: capture + read_file over the GitHub Contents API."""

import base64
import os

import httpx
from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse

GITHUB_REPO = os.environ["GITHUB_REPO"]  # "owner/repo"
GITHUB_BRANCH = os.environ.get("GITHUB_BRANCH", "main")
GITHUB_TOKEN = os.environ["GITHUB_TOKEN"]
BEARER_TOKEN = os.environ["BEARER_TOKEN"]
INBOX_PATH = os.environ.get("INBOX_PATH", "Inbox.md")
CAPTURE_HEADING = os.environ.get("CAPTURE_HEADING", "## To process")

GITHUB_API = "https://api.github.com"
_headers = {
    "Authorization": f"Bearer {GITHUB_TOKEN}",
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
}


async def github_get_file(path: str) -> dict:
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"{GITHUB_API}/repos/{GITHUB_REPO}/contents/{path}",
            headers=_headers,
            params={"ref": GITHUB_BRANCH},
        )
    resp.raise_for_status()
    return resp.json()


async def github_put_file(path: str, content: str, sha: str, message: str) -> None:
    async with httpx.AsyncClient() as client:
        resp = await client.put(
            f"{GITHUB_API}/repos/{GITHUB_REPO}/contents/{path}",
            headers=_headers,
            json={
                "message": message,
                "content": base64.b64encode(content.encode()).decode(),
                "sha": sha,
                "branch": GITHUB_BRANCH,
            },
        )
    resp.raise_for_status()


def insert_into_inbox(markdown: str, bullet_line: str, heading: str = CAPTURE_HEADING) -> str:
    """Insert `bullet_line` into the `heading` section, just above its trailing empty bullet.

    Keeps the heading and the trailing empty bullet intact, so the file stays ready for the
    next capture (matches the convention from killian31/second_brain_template's Inbox.md).
    """
    lines = markdown.splitlines()
    try:
        heading_idx = lines.index(heading)
    except ValueError:
        raise ValueError(f'{INBOX_PATH} has no {heading!r} heading')

    next_heading_idx = next(
        (i for i in range(heading_idx + 1, len(lines)) if lines[i].startswith("## ")),
        len(lines),
    )
    section = lines[heading_idx + 1 : next_heading_idx]

    empty_bullet_idx = next(
        (i for i in range(len(section) - 1, -1, -1) if section[i].strip() == "-"),
        None,
    )
    if empty_bullet_idx is None:
        section.append(bullet_line)
        section.append("-")
    else:
        section.insert(empty_bullet_idx, bullet_line)

    lines[heading_idx + 1 : next_heading_idx] = section
    return "\n".join(lines) + ("\n" if markdown.endswith("\n") else "")


def demo() -> None:
    """ponytail: the one runnable check for insert_into_inbox's line-splicing logic."""
    sample = (
        "# Inbox\n\n"
        "## To process\n"
        "- existing item\n"
        "-\n"
        "<!-- comment -->\n\n"
        "## For the agent\n"
        "stuff\n"
    )
    out = insert_into_inbox(sample, "- new capture", "## To process")
    expected = (
        "# Inbox\n\n"
        "## To process\n"
        "- existing item\n"
        "- new capture\n"
        "-\n"
        "<!-- comment -->\n\n"
        "## For the agent\n"
        "stuff\n"
    )
    assert out == expected, f"mismatch:\n{out!r}\nvs\n{expected!r}"

    # no trailing empty bullet present: one is re-added after the new line
    sample2 = "## To process\n- a\n## For the agent\n"
    out2 = insert_into_inbox(sample2, "- b", "## To process")
    assert out2 == "## To process\n- a\n- b\n-\n## For the agent\n", out2

    print("demo: ok")


mcp = MCPServer("second-brain")


@mcp.tool()
async def capture(text: str, actionable: bool = False) -> str:
    """Append `text` as a bullet to the vault's inbox file, under its capture heading. No
    frontmatter, no filing decision, no question — even if the text is vague. Set
    actionable=True only when the text is unambiguously a to-do, so it's written as
    `- [ ] text` instead of a plain bullet. Never use this to file into a knowledge base,
    ideas folder, or project note — that defeats the point of a zero-friction inbox."""
    bullet = f"- [ ] {text}" if actionable else f"- {text}"
    file = await github_get_file(INBOX_PATH)
    content = base64.b64decode(file["content"]).decode()
    updated = insert_into_inbox(content, bullet)
    await github_put_file(INBOX_PATH, updated, file["sha"], f"capture: {text[:60]}")
    return f"Captured to {INBOX_PATH}: {bullet}"


@mcp.tool()
async def read_file(path: str) -> str:
    """Read any file in the vault repo by its repo-relative path (e.g. "INDEX.md",
    "AGENTS.md", "Knowledge/Some Note.md"). Returns the raw file content."""
    data = await github_get_file(path)
    if isinstance(data, list):
        names = "\n".join(f"{e['type']}: {e['name']}" for e in data)
        return f"{path} is a directory:\n{names}"
    # ponytail: contents API caps at ~1MB; switch to the git blobs API if a file ever exceeds that
    return base64.b64decode(data["content"]).decode()


mcp_app = mcp.streamable_http_app(
    stateless_http=True,
    json_response=True,
    # ponytail: this is a server-to-server API behind our own bearer check, not a browser target,
    # so DNS-rebinding Host-header allowlisting (meant for browser-reachable local servers) doesn't apply
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
)


async def health(request: Request) -> PlainTextResponse:
    return PlainTextResponse("ok")


async def app(scope, receive, send):
    if scope["type"] == "http" and scope["path"] == "/health":
        resp = await health(Request(scope, receive))
        return await resp(scope, receive, send)
    if scope["type"] == "http":
        headers = dict(scope["headers"])
        auth = headers.get(b"authorization", b"").decode()
        if auth != f"Bearer {BEARER_TOKEN}":
            resp = JSONResponse({"error": "unauthorized"}, status_code=401)
            return await resp(scope, receive, send)
    await mcp_app(scope, receive, send)


if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1 and sys.argv[1] == "demo":
        demo()
    else:
        import uvicorn

        uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
