"""Slash command definitions and autocomplete for the Hermes CLI.

Central registry for all slash commands. Every consumer -- CLI help, gateway
dispatch, Telegram BotCommands, Slack subcommand mapping, autocomplete --
derives its data from ``COMMAND_REGISTRY``.

To add a command: add a ``CommandDef`` entry to ``COMMAND_REGISTRY``.
To add an alias: set ``aliases=("short",)`` on the existing ``CommandDef``.
"""

from __future__ import annotations

import os
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from prompt_toolkit.auto_suggest import AutoSuggest, Suggestion
from prompt_toolkit.completion import Completer, Completion


@dataclass(frozen=True)
class CommandDef:
    """Definition of a single slash command."""

    name: str                          # canonical name without slash: "background"
    description: str                   # human-readable description
    category: str                      # "Session", "Configuration", etc.
    aliases: tuple[str, ...] = ()      # alternative names: ("bg",)
    args_hint: str = ""                # argument placeholder: "<prompt>", "[name]"
    subcommands: tuple[str, ...] = ()  # tab-completable subcommands
    cli_only: bool = False             # only available in CLI
    gateway_only: bool = False         # only available in gateway/messaging
    gateway_config_gate: str | None = None  # config dotpath; when truthy, overrides cli_only for gateway


COMMAND_REGISTRY: list[CommandDef] = [
    CommandDef("new", "Start a new session (fresh session ID + history)", "Session", aliases=("reset",)),
    CommandDef("clear", "Clear screen and start a new session", "Session", cli_only=True),
    CommandDef("history", "Show conversation history", "Session", cli_only=True),
    CommandDef("save", "Save the current conversation", "Session", cli_only=True),
    CommandDef("retry", "Retry the last message (resend to agent)", "Session"),
    CommandDef("undo", "Remove the last user/assistant exchange", "Session"),
    CommandDef("title", "Set a title for the current session", "Session", args_hint="[name]"),
    CommandDef("compress", "Manually compress conversation context", "Session"),
    CommandDef("rollback", "List or restore filesystem checkpoints", "Session", args_hint="[number]"),
    CommandDef("stop", "Kill all running background processes", "Session"),
    CommandDef("approve", "Approve a pending dangerous command", "Session",
               gateway_only=True, args_hint="[session|always]"),
    CommandDef("deny", "Deny a pending dangerous command", "Session",
               gateway_only=True),
    CommandDef("background", "Run a prompt in the background", "Session",
               aliases=("bg",), args_hint="<prompt>"),
    CommandDef("queue", "Queue a prompt for the next turn (doesn't interrupt)", "Session",
               aliases=("q",), args_hint="<prompt>"),
    CommandDef("status", "Show session info", "Session",
               gateway_only=True),
    CommandDef("sethome", "Set this chat as the home channel", "Session",
               gateway_only=True, aliases=("set-home",)),
    CommandDef("resume", "Resume a previously-named session", "Session",
               args_hint="[name]"),

    # Configuration
    CommandDef("config", "Show current configuration", "Configuration",
               cli_only=True),
    CommandDef("model", "Show or change the current model", "Configuration",
               args_hint="[name]"),
    CommandDef("provider", "Show available providers and current provider",
               "Configuration"),
    CommandDef("prompt", "View/set custom system prompt", "Configuration",
               cli_only=True, args_hint="[text]", subcommands=("clear",)),
    CommandDef("personality", "Set a predefined personality", "Configuration",
               args_hint="[name]"),
    CommandDef("statusbar", "Toggle the context/model status bar", "Configuration",
               cli_only=True, aliases=("sb",)),
    CommandDef("verbose", "Cycle tool progress display: off -> new -> all -> verbose",
               "Configuration", cli_only=True,
               gateway_config_gate="display.tool_progress_command"),
    CommandDef("reasoning", "Manage reasoning effort and display", "Configuration",
               args_hint="[level|show|hide]",
               subcommands=("none", "low", "minimal", "medium", "high", "xhigh", "show", "hide", "on", "off")),
    CommandDef("skin", "Show or change the display skin/theme", "Configuration",
               cli_only=True, args_hint="[name]"),
    CommandDef("voice", "Toggle voice mode", "Configuration",
               args_hint="[on|off|tts|status]", subcommands=("on", "off", "tts", "status")),

    # Tools & Skills
    CommandDef("tools", "Manage tools: /tools [list|disable|enable] [name...]", "Tools & Skills",
               args_hint="[list|disable|enable] [name...]", cli_only=True),
    CommandDef("toolsets", "List available toolsets", "Tools & Skills",
               cli_only=True),
    CommandDef("skills", "Search, install, inspect, or manage skills",
               "Tools & Skills", cli_only=True,
               subcommands=("search", "browse", "inspect", "install")),
    CommandDef("cron", "Manage scheduled tasks", "Tools & Skills",
               cli_only=True, args_hint="[subcommand]",
               subcommands=("list", "add", "create", "edit", "pause", "resume", "run", "remove")),
    CommandDef("reload-mcp", "Reload MCP servers from config", "Tools & Skills",
               aliases=("reload_mcp",)),
    CommandDef("browser", "Connect browser tools to your live Chrome via CDP", "Tools & Skills",
               cli_only=True, args_hint="[connect|disconnect|status]",
               subcommands=("connect", "disconnect", "status")),
    CommandDef("plugins", "List installed plugins and their status",
               "Tools & Skills", cli_only=True),

    # Info
    CommandDef("help", "Show available commands", "Info"),
    CommandDef("usage", "Show token usage for the current session", "Info"),
    CommandDef("insights", "Show usage insights and analytics", "Info", args_hint="[days]"),
    CommandDef("platforms", "Show gateway/messaging platform status", "Info", cli_only=True, aliases=("gateway",)),
    CommandDef("paste", "Check clipboard for an image and attach it", "Info", cli_only=True),
    CommandDef("update", "Update Hermes Agent to the latest version", "Info", gateway_only=True),
    CommandDef("quit", "Exit the CLI", "Exit", cli_only=True, aliases=("exit", "q")),
]


def _build_command_lookup() -> dict[str, CommandDef]:
    lookup: dict[str, CommandDef] = {}
    for cmd in COMMAND_REGISTRY:
        lookup[cmd.name] = cmd
        for alias in cmd.aliases:
            lookup[alias] = cmd
    return lookup


_COMMAND_LOOKUP: dict[str, CommandDef] = _build_command_lookup()


def resolve_command(name: str) -> CommandDef | None:
    return _COMMAND_LOOKUP.get(name.lower().lstrip("/"))


def register_plugin_command(cmd: CommandDef) -> None:
    COMMAND_REGISTRY.append(cmd)
    rebuild_lookups()


def _build_description(cmd: CommandDef) -> str:
    if cmd.args_hint:
        return f"{cmd.description} (usage: /{cmd.name} {cmd.args_hint})"
    return cmd.description


COMMANDS: dict[str, str] = {}
COMMANDS_BY_CATEGORY: dict[str, dict[str, str]] = {}
SUBCOMMANDS: dict[str, list[str]] = {}
_PIPE_SUBS_RE = re.compile(r"[a-z]+(?:\|[a-z]+)+")
GATEWAY_KNOWN_COMMANDS: frozenset[str] = frozenset()


def rebuild_lookups() -> None:
    global GATEWAY_KNOWN_COMMANDS

    _COMMAND_LOOKUP.clear()
    _COMMAND_LOOKUP.update(_build_command_lookup())

    COMMANDS.clear()
    COMMANDS_BY_CATEGORY.clear()
    SUBCOMMANDS.clear()

    for cmd in COMMAND_REGISTRY:
        if not cmd.gateway_only:
            COMMANDS[f"/{cmd.name}"] = _build_description(cmd)
            cat = COMMANDS_BY_CATEGORY.setdefault(cmd.category, {})
            cat[f"/{cmd.name}"] = COMMANDS[f"/{cmd.name}"]
            for alias in cmd.aliases:
                COMMANDS[f"/{alias}"] = f"{cmd.description} (alias for /{cmd.name})"
                cat[f"/{alias}"] = COMMANDS[f"/{alias}"]

    for cmd in COMMAND_REGISTRY:
        if cmd.subcommands:
            SUBCOMMANDS[f"/{cmd.name}"] = list(cmd.subcommands)
    for cmd in COMMAND_REGISTRY:
        key = f"/{cmd.name}"
        if key in SUBCOMMANDS or not cmd.args_hint:
            continue
        m = _PIPE_SUBS_RE.search(cmd.args_hint)
        if m:
            SUBCOMMANDS[key] = m.group(0).split("|")

    GATEWAY_KNOWN_COMMANDS = frozenset(
        name
        for cmd in COMMAND_REGISTRY
        if not cmd.cli_only or cmd.gateway_config_gate
        for name in (cmd.name, *cmd.aliases)
    )


rebuild_lookups()


def _resolve_config_gates() -> set[str]:
    """Return canonical names of commands whose ``gateway_config_gate`` is truthy.

    Reads ``config.yaml`` and walks the dot-separated key path for each
    config-gated command.  Returns an empty set on any error so callers
    degrade gracefully.
    """
    gated = [c for c in COMMAND_REGISTRY if c.gateway_config_gate]
    if not gated:
        return set()
    try:
        import yaml
        config_path = os.path.join(
            os.getenv("HERMES_HOME", os.path.expanduser("~/.hermes")),
            "config.yaml",
        )
        if os.path.exists(config_path):
            with open(config_path, encoding="utf-8") as f:
                cfg = yaml.safe_load(f) or {}
        else:
            cfg = {}
    except Exception:
        return set()
    result: set[str] = set()
    for cmd in gated:
        val: Any = cfg
        for key in cmd.gateway_config_gate.split("."):
            if isinstance(val, dict):
                val = val.get(key)
            else:
                val = None
                break
        if val:
            result.add(cmd.name)
    return result


def _is_gateway_available(cmd: CommandDef, config_overrides: set[str] | None = None) -> bool:
    """Check if *cmd* should appear in gateway surfaces (help, menus, mappings).

    Unconditionally available when ``cli_only`` is False.  When ``cli_only``
    is True but ``gateway_config_gate`` is set, the command is available only
    when the config value is truthy.  Pass *config_overrides* (from
    ``_resolve_config_gates()``) to avoid re-reading config for every command.
    """
    if not cmd.cli_only:
        return True
    if cmd.gateway_config_gate:
        overrides = config_overrides if config_overrides is not None else _resolve_config_gates()
        return cmd.name in overrides
    return False


def gateway_help_lines() -> list[str]:
    """Generate gateway help text lines from the registry."""
    overrides = _resolve_config_gates()
    lines: list[str] = []
    for cmd in COMMAND_REGISTRY:
        if not _is_gateway_available(cmd, overrides):
            continue
        args = f" {cmd.args_hint}" if cmd.args_hint else ""
        alias_parts: list[str] = []
        for alias in cmd.aliases:
            if alias.replace("-", "_") == cmd.name.replace("-", "_") and alias != cmd.name:
                continue
            alias_parts.append(f"`/{alias}`")
        alias_note = f" (alias: {', '.join(alias_parts)})" if alias_parts else ""
        lines.append(f"`/{cmd.name}{args}` -- {cmd.description}{alias_note}")
    return lines


def telegram_bot_commands() -> list[tuple[str, str]]:
    """Return (command_name, description) pairs for Telegram setMyCommands.

    Telegram command names cannot contain hyphens, so they are replaced with
    underscores.  Aliases are skipped -- Telegram shows one menu entry per
    canonical command.
    """
    overrides = _resolve_config_gates()
    result: list[tuple[str, str]] = []
    for cmd in COMMAND_REGISTRY:
        if not _is_gateway_available(cmd, overrides):
            continue
        result.append((cmd.name.replace("-", "_"), cmd.description))
    return result


def slack_subcommand_map() -> dict[str, str]:
    """Return subcommand -> /command mapping for Slack /hermes handler.

    Maps both canonical names and aliases so /hermes bg do stuff works
    the same as /hermes background do stuff.
    """
    overrides = _resolve_config_gates()
    mapping: dict[str, str] = {}
    for cmd in COMMAND_REGISTRY:
        if not _is_gateway_available(cmd, overrides):
            continue
        mapping[cmd.name] = f"/{cmd.name}"
        for alias in cmd.aliases:
            mapping[alias] = f"/{alias}"
    return mapping


def _user_home_dir() -> str:
    """Return the preferred home directory for path completion/display."""
    for key in ("HOME", "USERPROFILE"):
        value = os.environ.get(key, "").strip()
        if value:
            return value
    return os.path.expanduser("~")


def _expand_user_path(path: str) -> str:
    if path.startswith("~"):
        home = _user_home_dir()
        if path == "~":
            return home
        if path.startswith("~/") or path.startswith("~\\"):
            suffix = path[2:].replace("/", os.sep).replace("\\", os.sep)
            return os.path.join(home, suffix)
    return os.path.expanduser(path)


class SlashCommandCompleter(Completer):
    """Autocomplete for built-in slash commands, subcommands, and skill commands."""

    def __init__(
        self,
        skill_commands_provider: Callable[[], Mapping[str, dict[str, Any]]] | None = None,
        model_completer_provider: Callable[[], dict[str, Any]] | None = None,
    ) -> None:
        self._skill_commands_provider = skill_commands_provider
        self._model_completer_provider = model_completer_provider
        self._model_info_cache: dict[str, Any] | None = None
        self._model_info_cache_time: float = 0

    def _get_model_info(self) -> dict[str, Any]:
        import time

        now = time.monotonic()
        if self._model_info_cache is not None and now - self._model_info_cache_time < 60:
            return self._model_info_cache
        if self._model_completer_provider is None:
            return {}
        try:
            self._model_info_cache = self._model_completer_provider() or {}
            self._model_info_cache_time = now
        except Exception:
            self._model_info_cache = self._model_info_cache or {}
        return self._model_info_cache

    def _iter_skill_commands(self) -> Mapping[str, dict[str, Any]]:
        if self._skill_commands_provider is None:
            return {}
        try:
            return self._skill_commands_provider() or {}
        except Exception:
            return {}

    @staticmethod
    def _completion_text(cmd_name: str, word: str) -> str:
        return f"{cmd_name} " if cmd_name == word else cmd_name

    @staticmethod
    def _extract_path_word(text: str) -> str | None:
        if not text:
            return None
        i = len(text) - 1
        while i >= 0 and text[i] != " ":
            i -= 1
        word = text[i + 1:]
        if not word:
            return None
        if (
            word.startswith(("./", "../", "~/", "/", ".\\", "..\\", "~\\", "\\"))
            or "/" in word
            or "\\" in word
            or os.path.isabs(word)
        ):
            return word
        return None

    @staticmethod
    def _path_completions(word: str, limit: int = 30):
        expanded = _expand_user_path(word)
        if expanded.endswith(("/", "\\")):
            search_dir = expanded
            prefix = ""
        else:
            search_dir = os.path.dirname(expanded) or "."
            prefix = os.path.basename(expanded)

        try:
            entries = os.listdir(search_dir)
        except OSError:
            return

        count = 0
        prefix_lower = prefix.lower()
        for entry in sorted(entries):
            if prefix and not entry.lower().startswith(prefix_lower):
                continue
            if count >= limit:
                break
            full_path = os.path.join(search_dir, entry)
            is_dir = os.path.isdir(full_path)
            if word.startswith("~"):
                display_path = "~/" + os.path.relpath(full_path, _user_home_dir())
            elif os.path.isabs(word):
                display_path = full_path
            else:
                display_path = os.path.relpath(full_path)
            if is_dir:
                display_path += "/"
            yield Completion(
                display_path,
                start_position=-len(word),
                display=entry + ("/" if is_dir else ""),
                display_meta="dir" if is_dir else _file_size_label(full_path),
            )
            count += 1

    @staticmethod
    def _extract_context_word(text: str) -> str | None:
        if not text:
            return None
        i = len(text) - 1
        while i >= 0 and text[i] != " ":
            i -= 1
        word = text[i + 1:]
        return word if word.startswith("@") else None

    @staticmethod
    def _context_completions(word: str, limit: int = 30):
        lowered = word.lower()
        static_refs = (
            ("@diff", "Git working tree diff"),
            ("@staged", "Git staged diff"),
            ("@file:", "Attach a file"),
            ("@folder:", "Attach a folder"),
            ("@git:", "Git log with diffs (e.g. @git:5)"),
            ("@url:", "Fetch web content"),
        )
        for candidate, meta in static_refs:
            if candidate.lower().startswith(lowered) and candidate.lower() != lowered:
                yield Completion(candidate, start_position=-len(word), display=candidate, display_meta=meta)

        for prefix in ("@file:", "@folder:"):
            if word.startswith(prefix):
                path_part = word[len(prefix):] or "."
                expanded = _expand_user_path(path_part)
                if expanded.endswith(("/", "\\")):
                    search_dir, match_prefix = expanded, ""
                else:
                    search_dir = os.path.dirname(expanded) or "."
                    match_prefix = os.path.basename(expanded)
                try:
                    entries = os.listdir(search_dir)
                except OSError:
                    return
                count = 0
                prefix_lower = match_prefix.lower()
                for entry in sorted(entries):
                    if match_prefix and not entry.lower().startswith(prefix_lower):
                        continue
                    if count >= limit:
                        break
                    full_path = os.path.join(search_dir, entry)
                    is_dir = os.path.isdir(full_path)
                    display_path = os.path.relpath(full_path)
                    kind = "folder" if is_dir else "file"
                    suffix = "/" if is_dir else ""
                    yield Completion(
                        f"@{kind}:{display_path}{suffix}",
                        start_position=-len(word),
                        display=entry + suffix,
                        display_meta="dir" if is_dir else _file_size_label(full_path),
                    )
                    count += 1
                return

        query = word[1:]
        if not query:
            search_dir, match_prefix = ".", ""
        else:
            expanded = _expand_user_path(query)
            if expanded.endswith(("/", "\\")):
                search_dir, match_prefix = expanded, ""
            else:
                search_dir = os.path.dirname(expanded) or "."
                match_prefix = os.path.basename(expanded)
        try:
            entries = os.listdir(search_dir)
        except OSError:
            return
        count = 0
        prefix_lower = match_prefix.lower()
        for entry in sorted(entries):
            if match_prefix and not entry.lower().startswith(prefix_lower):
                continue
            if entry.startswith(".") or count >= limit:
                continue
            full_path = os.path.join(search_dir, entry)
            is_dir = os.path.isdir(full_path)
            display_path = os.path.relpath(full_path)
            kind = "folder" if is_dir else "file"
            suffix = "/" if is_dir else ""
            yield Completion(
                f"@{kind}:{display_path}{suffix}",
                start_position=-len(word),
                display=entry + suffix,
                display_meta="dir" if is_dir else _file_size_label(full_path),
            )
            count += 1

    def get_completions(self, document, complete_event):
        text = document.text_before_cursor
        if not text.startswith("/"):
            ctx_word = self._extract_context_word(text)
            if ctx_word is not None:
                yield from self._context_completions(ctx_word)
                return
            path_word = self._extract_path_word(text)
            if path_word is not None:
                yield from self._path_completions(path_word)
            return

        parts = text.split(maxsplit=1)
        base_cmd = parts[0].lower()
        if len(parts) > 1 or (len(parts) == 1 and text.endswith(" ")):
            sub_text = parts[1] if len(parts) > 1 else ""
            sub_lower = sub_text.lower()

            if base_cmd == "/model" and " " not in sub_text:
                info = self._get_model_info()
                if info:
                    current_prov = info.get("current_provider", "")
                    providers = info.get("providers", {})
                    models_for = info.get("models_for")
                    if ":" in sub_text:
                        prov_part, model_part = sub_text.split(":", 1)
                        model_lower = model_part.lower()
                        if models_for:
                            try:
                                prov_models = models_for(prov_part)
                            except Exception:
                                prov_models = []
                            for mid in prov_models:
                                if mid.lower().startswith(model_lower) and mid.lower() != model_lower:
                                    yield Completion(full := f"{prov_part}:{mid}", start_position=-len(sub_text), display=mid)
                    else:
                        for pid, plabel in sorted(providers.items(), key=lambda kv: (kv[0] == current_prov, kv[0])):
                            display_name = f"{pid}:"
                            if display_name.lower().startswith(sub_lower):
                                meta = f"({plabel})" if plabel != pid else ""
                                if pid == current_prov:
                                    meta = f"(current — {plabel})" if plabel != pid else "(current)"
                                yield Completion(display_name, start_position=-len(sub_text), display=display_name, display_meta=meta)
                return

            if " " not in sub_text and base_cmd in SUBCOMMANDS:
                for sub in SUBCOMMANDS[base_cmd]:
                    if sub.startswith(sub_lower) and sub != sub_lower:
                        yield Completion(sub, start_position=-len(sub_text), display=sub)
            return

        word = text[1:]
        for cmd, desc in COMMANDS.items():
            cmd_name = cmd[1:]
            if cmd_name.startswith(word):
                yield Completion(self._completion_text(cmd_name, word), start_position=-len(word), display=cmd, display_meta=desc)
        for cmd, info in self._iter_skill_commands().items():
            cmd_name = cmd[1:]
            if cmd_name.startswith(word):
                description = str(info.get("description", "Skill command"))
                short_desc = description[:50] + ("..." if len(description) > 50 else "")
                yield Completion(
                    self._completion_text(cmd_name, word),
                    start_position=-len(word),
                    display=cmd,
                    display_meta=f"⚡ {short_desc}",
                )


class SlashCommandAutoSuggest(AutoSuggest):
    """Inline ghost-text suggestions for slash commands and their subcommands."""

    def __init__(
        self,
        history_suggest: AutoSuggest | None = None,
        completer: SlashCommandCompleter | None = None,
    ) -> None:
        self._history = history_suggest
        self._completer = completer

    def get_suggestion(self, buffer, document):
        text = document.text_before_cursor
        if not text.startswith("/"):
            return self._history.get_suggestion(buffer, document) if self._history else None

        parts = text.split(maxsplit=1)
        base_cmd = parts[0].lower()
        if len(parts) == 1 and not text.endswith(" "):
            word = text[1:].lower()
            for cmd in COMMANDS:
                cmd_name = cmd[1:]
                if cmd_name.startswith(word) and cmd_name != word:
                    return Suggestion(cmd_name[len(word):])
            return None

        sub_text = parts[1] if len(parts) > 1 else ""
        sub_lower = sub_text.lower()
        if base_cmd == "/model" and " " not in sub_text and self._completer:
            info = self._completer._get_model_info()
            if info:
                providers = info.get("providers", {})
                models_for = info.get("models_for")
                current_prov = info.get("current_provider", "")
                if ":" in sub_text:
                    prov_part, model_part = sub_text.split(":", 1)
                    model_lower = model_part.lower()
                    if models_for:
                        try:
                            for mid in models_for(prov_part):
                                if mid.lower().startswith(model_lower) and mid.lower() != model_lower:
                                    return Suggestion(mid[len(model_part):])
                        except Exception:
                            pass
                else:
                    for pid in sorted(providers, key=lambda p: (p == current_prov, p)):
                        candidate = f"{pid}:"
                        if candidate.lower().startswith(sub_lower) and candidate.lower() != sub_lower:
                            return Suggestion(candidate[len(sub_text):])

        if base_cmd in SUBCOMMANDS and SUBCOMMANDS[base_cmd] and " " not in sub_text:
            for sub in SUBCOMMANDS[base_cmd]:
                if sub.startswith(sub_lower) and sub != sub_lower:
                    return Suggestion(sub[len(sub_text):])

        return self._history.get_suggestion(buffer, document) if self._history else None


def _file_size_label(path: str) -> str:
    try:
        size = os.path.getsize(path)
    except OSError:
        return ""
    if size < 1024:
        return f"{size}B"
    if size < 1024 * 1024:
        return f"{size / 1024:.0f}K"
    if size < 1024 * 1024 * 1024:
        return f"{size / (1024 * 1024):.1f}M"
    return f"{size / (1024 * 1024 * 1024):.1f}G"
