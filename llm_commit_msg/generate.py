# SPDX-FileCopyrightText: 2026 Alexander Sosedkin <monk@unboiled.info>
# SPDX-License-Identifier: GPL-3.0

"""Generate commit messages with LLM: actual code."""

import json
import os
from collections.abc import Iterator
from pathlib import Path

import git
import requests
import unidiff  # type: ignore[import-untyped]

MAX_TOKENS = 2**16
MAX_STAGED_DIFF_CONTEXT = 9
MAX_STAGED_DIFF_CHARS = 80 * 512
MAX_STAGED_FILES = 48
MAX_STAGED_FILES_CHARS = 80 * MAX_STAGED_FILES
MAX_LOOKBACK_COMMITS = 10
MAX_LOOKBACK_CHARS = 80 * 10 * MAX_LOOKBACK_COMMITS


def _get_repo() -> git.Repo:
    return git.Repo(search_parent_directories=True)


def _truncate(s: str, max_len: int) -> str:
    if len(s) < max_len:
        return s
    return s[: max_len - 3] + '...'


def _gather_lookback_commit_messages(repo: git.Repo) -> str:
    for count in range(MAX_LOOKBACK_COMMITS, 2, -1):
        messages = [
            f'```\n{commit.message.strip()!s}\n```\n\n'
            for commit in repo.iter_commits(max_count=count)
        ]
        result = '\n'.join(messages)
        if len(result) <= MAX_LOOKBACK_CHARS:
            break
    return _truncate(result, MAX_LOOKBACK_CHARS)  # last resort: truncation


def _get_staged_files(repo: git.Repo) -> str:
    diff = repo.index.diff('HEAD')
    staged = [d.a_path or d.b_path for d in diff if d.a_path or d.b_path]
    for max_files in range(MAX_STAGED_FILES, 2, -1):
        staged = staged[:max_files]
        result = '\n'.join(str(path) for path in staged)
        if len(result) <= MAX_STAGED_FILES_CHARS:
            break
    return _truncate(result, MAX_STAGED_FILES_CHARS)  # last resort: truncation


def _truncate_patched_file(
    patched_file: unidiff.PatchedFile,
    keep_lines: int,
) -> str:
    file_diff = str(patched_file)
    lines = file_diff.split('\n')
    if len(lines) <= 2 * keep_lines + 1:
        return file_diff
    return '\n'.join([
        *lines[:keep_lines],
        '... [truncated] ...',
        *lines[-keep_lines:],
    ])


def _get_staged_diff(repo: git.Repo) -> str:
    for context in range(MAX_STAGED_DIFF_CONTEXT, 1, -1):
        result = str(repo.git.diff('--staged', f'-U{context}'))
        if len(result) <= MAX_STAGED_DIFF_CHARS:
            return result

    # That didn't work. Perhaps there's an extremely long hunk somewhere?
    # Cut the middle parts out of individual file diffs that are too long.
    max_single_file_chars = MAX_STAGED_DIFF_CHARS // 2
    for context in range(MAX_STAGED_DIFF_CONTEXT, 1, -1):
        result = str(repo.git.diff('--staged', f'-U{context}'))
        patch_set = unidiff.PatchSet(result)
        keep_lines = 2 * context

        processed_diffs = []
        for patched_file in patch_set:
            file_diff = str(patched_file)
            if len(file_diff) > max_single_file_chars:
                file_diff = _truncate_patched_file(patched_file, keep_lines)
            processed_diffs.append(file_diff)

        result = ''.join(processed_diffs)
        if len(result) <= MAX_STAGED_DIFF_CHARS:
            return result

    return _truncate(result, MAX_STAGED_DIFF_CHARS)  # last resort: truncation


def _generate_prompt() -> str:
    repo = _get_repo()
    lookback_commit_messages = _gather_lookback_commit_messages(repo)
    staged_files = _get_staged_files(repo)
    staged_diff = _get_staged_diff(repo)

    # IDEA: if the latest commits have a semicolon:
    # Start with the scope of the changes separated with a colon,
    # make sure every file is covered; wildcards and commas are allowed.

    return f"""You are a commit message generator.
Suggest a concise commit message for the following diff changes.

A handful of recent commits from the repository, in triple backticks:
{lookback_commit_messages}

Files changed:
{staged_files}

The diff to summarize:
```
{staged_diff}
```

Output only the commit message
with the style matching the convention set by previous commits,
and nothing else.
Be concise in the commit message subject line (72 chars max).
Then elaborate more in the commit message body.
""".strip()


def _get_api_key(api_token_file: Path | None) -> str:
    if api_token_file is None:
        r = os.getenv('OPENAI_API_KEY')
        if not r:
            # a bit of a layering mixup in the message
            msg = 'OPENAI_API_KEY not set, --api-token-file not provided'
            raise RuntimeError(msg)
        return r
    return api_token_file.read_text(encoding='utf-8').strip()


def _query_llm(
    model: str,
    api_endpoint: str,
    api_token_file: Path | None,
    prompt: str,
) -> Iterator[str]:
    api_key = _get_api_key(api_token_file)
    resp = requests.post(
        f'{api_endpoint}/v1/chat/completions',
        headers={
            'authorization': f'Bearer {api_key}',
            'Content-Type': 'application/json',
        },
        json={
            'model': model,
            'messages': [{'role': 'user', 'content': prompt}],
            'max_tokens': MAX_TOKENS,
            'stream': True,
        },
        timeout=30,
        stream=True,
    )
    resp.raise_for_status()
    for line in resp.iter_lines():
        if not line:
            continue
        decoded = line.decode('utf-8')
        if not decoded.startswith('data: '):
            continue
        data = decoded.removeprefix('data: ')
        if data == '[DONE]':
            break
        chunk = json.loads(data)
        content = chunk['choices'][0]['delta'].get('content', '')
        if content:
            yield content


def generate(
    *,
    commented_out: bool,
    model: str,
    api_token_file: Path | None,
    api_endpoint: str,
) -> None:
    """Generate a commit message."""
    if commented_out:
        print('#', end='', flush=True)  # noqa: T201
    try:
        prompt = _generate_prompt()
        chunks = _query_llm(model, api_endpoint, api_token_file, prompt)
        for chunk in chunks:
            out = chunk
            if commented_out:
                out = out.replace('\n', '\n#')
            print(out, end='', flush=True)  # noqa: T201
    except Exception as e:  # noqa: BLE001
        error = f'ERROR: {e}'
        if commented_out:
            error = error.replace('\n', '\n#')
        print(error)  # noqa: T201
    print(flush=True)  # noqa: T201
