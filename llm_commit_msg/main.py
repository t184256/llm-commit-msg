# SPDX-FileCopyrightText: 2026 Alexander Sosedkin <monk@unboiled.info>
# SPDX-License-Identifier: GPL-3.0

"""Main module of llm_commit_msg."""

from pathlib import Path

import click

import llm_commit_msg.generate


@click.group(invoke_without_command=True)
@click.pass_context
def main(ctx: click.Context) -> None:
    """Suggest commit messages with an LLM."""
    if ctx.invoked_subcommand is None:
        ctx.invoke(_generate_cmd)


@main.command('generate')
@click.option(
    '--commented-out',
    is_flag=True,
    help='Output as commented out lines.',
)
@click.option('--model', default='gpt-oss:20b', help='Model to use.')
@click.option(
    '--api-token-file',
    type=click.Path(path_type=Path, readable=True),
    help='Path to API token file (alternative to OPENAI_API_KEY envvar).',
)
@click.option('--api-endpoint', required=True, help='API endpoint URL.')
@click.option(
    '--show-off',
    default=0,
    type=float,
    help='Slow down the output (seconds between the chunks).'
)
def _generate_cmd(
    *,
    commented_out: bool,
    model: str,
    api_token_file: Path | None,
    api_endpoint: str,
    show_off: float,
) -> None:
    llm_commit_msg.generate.generate(
        commented_out=commented_out,
        model=model,
        api_token_file=api_token_file,
        api_endpoint=api_endpoint,
        show_off=show_off,
    )


if __name__ == '__main__':
    main()
