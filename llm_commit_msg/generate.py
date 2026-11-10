# SPDX-FileCopyrightText: 2026 Alexander Sosedkin <monk@unboiled.info>
# SPDX-License-Identifier: GPL-3.0

"""Generate commit messages with LLM: actual code."""

import json
import os
import time
from collections.abc import Iterator
from pathlib import Path

import git
import requests
from unidiff.patch import PatchedFile, PatchSet

MAX_STAGED_DIFF_CONTEXT = 9
MAX_STAGED_DIFF_CHARS = 80 * 512
MAX_STAGED_FILES = 48
MAX_STAGED_FILES_CHARS = 80 * MAX_STAGED_FILES
MAX_LOOKBACK_CHARS_PER_COMMIT = 80 * 10
ANCHOR_HASH_PREFIX = 'aa'  # encountering a new one invalidates prefix caching
ANCHOR_SEARCH_LIMIT = 2048  # but search must complete within reasonable time


def _get_repo() -> git.Repo:
    return git.Repo(search_parent_directories=True)


def _truncate(s: str, max_len: int) -> str:
    if len(s) < max_len:
        return s
    return s[: max_len - 3] + '...'


def _aligned_refs(repo: git.Repo, stickiness: int, messages: int) -> list[str]:
    # get a peek into the past hashes, limited in case repos are large
    opts = ('--first-parent', f'--max-count={ANCHOR_SEARCH_LIMIT}', 'HEAD')
    hashes: list[str] = repo.git.rev_list(*opts).split()
    if len(hashes) <= messages:
        return hashes[::-1]

    # walk into the past to search for an anchor: a relatively unusual prefix
    anchor = messages
    for sha in hashes[messages:]:
        if sha.startswith(ANCHOR_HASH_PREFIX):
            break
        anchor += 1

    # walk into the future to align the start of the lookback
    # on a grid counted from anchor
    aligned_start = anchor
    while aligned_start > messages:
        aligned_start -= stickiness
    return hashes[aligned_start::-1]


def _gather_lookback_commit_messages(
    repo: git.Repo,
    messages: int,
    stickiness: int,
) -> str:
    max_chars = MAX_LOOKBACK_CHARS_PER_COMMIT * messages
    result = ''
    for hash_ in _aligned_refs(repo, stickiness, messages):
        msg = f'```{repo.commit(hash_).message.strip()!s}```\n\n'
        if len(result) + len(msg) > max_chars:
            break
        result += msg + '\n'
    return _truncate(result, max_chars)  # last resort: truncation


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
    patched_file: PatchedFile,
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
        patch_set = PatchSet(result)
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


def _generate_prompt(lookback_messages: int, stickiness: int) -> str:
    repo = _get_repo()
    lookback_commit_messages = _gather_lookback_commit_messages(
        repo,
        lookback_messages,
        stickiness,
    )
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


def _thinking_params(max_thinking_tokens: int) -> dict[str, object]:
    if max_thinking_tokens <= 0:
        return {'chat_template_kwargs': {'enable_thinking': False}}
    return {
        'chat_template_kwargs': {'enable_thinking': True},
        'thinking_budget_tokens': max_thinking_tokens,
    }


def _query_llm(  # ruff: ignore[too-many-arguments]
    *,
    model: str,
    api_endpoint: str,
    api_token_file: Path | None,
    prompt: str,
    max_thinking_tokens: int,
    max_result_tokens: int,
) -> Iterator[str]:
    api_key = _get_api_key(api_token_file)
    with requests.post(
        f'{api_endpoint}/v1/chat/completions',
        headers={
            'authorization': f'Bearer {api_key}',
            'Content-Type': 'application/json',
        },
        json={
            'model': model,
            'messages': [{'role': 'user', 'content': prompt}],
            'max_tokens': max_thinking_tokens + max_result_tokens,
            'stream': True,
            **_thinking_params(max_thinking_tokens),
        },
        timeout=30,
        stream=True,
    ) as resp:
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


def _print_chunks(
    chunks: Iterator[str],
    *,
    commented_out: bool,
    show_off: float,
) -> None:
    for chunk in chunks:
        out = chunk
        if commented_out:
            out = out.replace('\n', '\n#')
        print(out, end='', flush=True)  # ruff: ignore[print]
        if show_off:
            time.sleep(show_off)


def generate(  # ruff: ignore[too-many-arguments]
    *,
    commented_out: bool,
    model: str,
    api_token_file: Path | None,
    api_endpoint: str,
    show_off: float,
    max_thinking_tokens: int,
    max_result_tokens: int,
    lookback_messages: int,
    lookback_messages_stickiness: int,
) -> None:
    """Generate a commit message."""
    if commented_out:
        print('#', end='', flush=True)  # ruff: ignore[print]
    try:
        prompt = _generate_prompt(
            lookback_messages,
            lookback_messages_stickiness,
        )
        chunks = _query_llm(
            model=model,
            api_endpoint=api_endpoint,
            api_token_file=api_token_file,
            prompt=prompt,
            max_thinking_tokens=max_thinking_tokens,
            max_result_tokens=max_result_tokens,
        )
        _print_chunks(chunks, commented_out=commented_out, show_off=show_off)
    except Exception as e:  # ruff: ignore[blind-except]
        error = f'ERROR: {e}'
        if commented_out:
            error = error.replace('\n', '\n#')
        print(error)  # ruff: ignore[print]
    print(flush=True)  # ruff: ignore[print]
