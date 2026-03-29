# Tirith vs. the LiteLLM PyPI Compromise

Date: 2026-03-25

## Bottom line

The strong claim does not hold up: Tirith would not have been a reliable end-to-end control against the March 24, 2026 LiteLLM PyPI compromise.

The narrower claim is fair: Tirith can reduce risk around suspicious commands, pasted terminal content, hidden Unicode/control characters, and some repo or diff scanning use cases. That is not the same thing as proving it would have blocked a poisoned PyPI release with a Python startup hook.

## What the LiteLLM incident was

LiteLLM's own security issues describe a package supply-chain compromise, not a pasted-command trick:

- Issue [#24518](https://github.com/BerriAI/litellm/issues/24518) says PyPI versions `1.82.7` and `1.82.8` were compromised after an attacker gained access to a maintainer PyPI account. It also says those versions were uploaded directly to PyPI and were never released through the official GitHub CI/CD flow.
- The same issue says:
  - `1.82.7` embedded a payload in `litellm/proxy/proxy_server.py` and triggered on `import litellm.proxy`
  - `1.82.8` added `litellm_init.pth` and triggered on any Python startup, with no import required
- The detailed analysis in issue [#24512](https://github.com/BerriAI/litellm/issues/24512) describes `litellm_init.pth` as a startup hook that launches a credential-stealing payload via Python on interpreter start.

That matters because the main execution boundary was "install a compromised package, then start Python", not "paste an obviously suspicious shell command".

## What Tirith actually is

Tirith's upstream docs describe several related but distinct products:

- The OSS `tirith` product is the shell hook. In upstream [`TIRITH.md`](https://raw.githubusercontent.com/sheeki03/tirith/main/TIRITH.md), it is described as protecting developers "at the moment they paste and hit enter".
- The same doc describes `tirith ci` as a separate GitHub App plus CI scanner positioned as a "URL execution surface scanner" for docs, scripts, and workflows.
- The upstream README focuses on homograph URLs, ANSI/control-sequence attacks, pipe-to-shell, source-to-sink execution patterns, hidden content, and related command-shape or content-shape problems.

Hermes mirrors that framing:

- [`website/docs/user-guide/security.md`](../website/docs/user-guide/security.md) documents Tirith as "content-level command scanning before execution".
- [`tools/terminal_tool.py`](../tools/terminal_tool.py) wires the guard into terminal command execution as a pre-exec check.
- [`tools/approval.py`](../tools/approval.py) combines Tirith findings with dangerous-command approvals.

## Why the strong claim breaks

There are four separate seams where "Tirith would have stopped the LiteLLM exfiltration" overreaches.

### 1. Tirith is a command/content guard, not a PyPI artifact trust boundary

Neither Tirith's docs nor Hermes's integration describe any of the following:

- validating PyPI provenance for downloaded artifacts
- inspecting wheel contents before installation
- rejecting a package because it contains a `.pth` startup hook
- attesting that a published package version matches GitHub CI output

That is the core mismatch. The LiteLLM incident was a compromised package artifact problem. Tirith is documented as a command and content inspection layer.

### 2. The LiteLLM payload executed after installation, not because the install command itself was exotic

Issue [#24512](https://github.com/BerriAI/litellm/issues/24512) says `litellm_init.pth` executed automatically on Python startup. That bypasses the "developer pasted a suspicious command" protection model unless the original command or surrounding repo content already exposed recognizable indicators.

A normal-looking install step like:

```bash
pip install litellm==1.82.8
```

or

```bash
uv pip install -e ".[all,dev]"
```

does not, by itself, prove a terminal-side content scanner would inspect and reject the fetched wheel payload.

### 3. Hermes only invokes Tirith in terminal-tool pre-exec flows

In Hermes, Tirith is not a global package-install firewall. It is part of terminal command execution:

- [`tools/terminal_tool.py`](../tools/terminal_tool.py) runs pre-exec security checks immediately before terminal execution.
- [`tools/approval.py`](../tools/approval.py) returns early without running the external guard work unless the command is happening in an interactive CLI, gateway, or explicit ask flow.

That means CI-like or other non-interactive command execution paths are not protected by this guard layer in the same way as interactive terminal use.

### 4. Hermes defaults Tirith to fail open

Hermes's Tirith wrapper defaults to:

- `tirith_enabled: true`
- `tirith_fail_open: true`

See [`tools/tirith_security.py`](../tools/tirith_security.py) and [`website/docs/user-guide/security.md`](../website/docs/user-guide/security.md).

In that same wrapper, disabled/unavailable/timed-out/unexpected-exit conditions can all resolve to `"action": "allow"` when fail-open behavior is active.

That does not make Tirith useless, but it does make any blanket "would have stopped it" claim even harder to support.

## Where Tirith could still have helped

There are narrower paths where Tirith could plausibly reduce risk:

- A malicious pasted command with homograph domains, hidden Unicode, ANSI injection, or obvious source-to-sink execution behavior
- A malicious README or docs snippet that tries to smuggle a deceptive install command
- A repo scanning or CI workflow that explicitly looks for suspicious patterns in changed files

Those are real protections. They are just different from upstream package provenance or wheel-content validation.

## Hermes-specific notes as of this checkout

### Current repo state

This checkout currently has:

- `.github/workflows/tests.yml`
- `.github/workflows/deploy-site.yml`
- `.github/workflows/docs-site-checks.yml`

It does **not** have a local `.github/workflows/supply-chain-audit.yml`.

The current tests workflow:

- runs on Python 3.11
- installs dependencies with `uv pip install -e ".[all,dev]"`
- does not invoke Tirith in the workflow itself

### LiteLLM exposure in this repo

In this checkout:

- `litellm` is **not** a direct dependency in [`pyproject.toml`](../pyproject.toml)
- `litellm` appears in [`uv.lock`](../uv.lock) transitively via `yc-bench`
- the relevant lock entries are guarded by a Python `>= 3.12` marker
- the `all` extra in [`pyproject.toml`](../pyproject.toml) does not include `yc-bench`

So, by inspection, the default `tests.yml` path should not install `litellm`: it uses Python 3.11 and installs `.[all,dev]`, not `.[yc-bench]`.

That does not change the Tirith conclusion. It just means the current checkout's ordinary test workflow does not appear to hit the affected LiteLLM dependency path in the first place.

## Upstream-main nuance

Upstream `main` currently exposes a separate workflow at:

- [`.github/workflows/supply-chain-audit.yml`](https://raw.githubusercontent.com/NousResearch/hermes-agent/main/.github/workflows/supply-chain-audit.yml)

That workflow is much closer to the LiteLLM attack shape than Tirith is. It scans PR diffs for:

- added or modified `.pth` files
- base64 plus `exec` or `eval`
- encoded subprocess invocations
- install-hook files like `sitecustomize.py` and `usercustomize.py`
- outbound POST or PUT calls

It even references the LiteLLM attack pattern directly.

That is a meaningful mitigation for suspicious code introduced through a PR. It is still not the same thing as validating a dependency fetched from PyPI during installation, and it is separate from the Tirith shell-hook claim.

## Verdict

The clean version of the answer is:

- "Tirith could have helped in some adjacent ways" is reasonable.
- "Tirith would have stopped the LiteLLM exfiltration" is overstated.

The LiteLLM incident was a package supply-chain compromise with Python startup execution. Tirith, as documented and as integrated in Hermes, is primarily a terminal command and content guard. Those layers overlap only partially.

## Sources

External:

- LiteLLM incident summary: <https://github.com/BerriAI/litellm/issues/24518>
- LiteLLM detailed `.pth` analysis: <https://github.com/BerriAI/litellm/issues/24512>
- Tirith upstream repo/README: <https://github.com/sheeki03/tirith>
- Tirith product/spec doc: <https://raw.githubusercontent.com/sheeki03/tirith/main/TIRITH.md>
- Upstream Hermes main supply-chain audit workflow: <https://raw.githubusercontent.com/NousResearch/hermes-agent/main/.github/workflows/supply-chain-audit.yml>

Local repo:

- `website/docs/user-guide/security.md`
- `website/docs/user-guide/configuration.md`
- `tools/terminal_tool.py`
- `tools/approval.py`
- `tools/tirith_security.py`
- `.github/workflows/tests.yml`
- `pyproject.toml`
- `uv.lock`
