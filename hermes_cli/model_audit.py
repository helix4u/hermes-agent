"""Helpers for auditing and filling model/provider configuration."""

from __future__ import annotations

from copy import deepcopy
from typing import Any, Dict, List, Tuple

from hermes_cli.config import load_config, save_config

_TEXT_AUX_TASKS = (
    "web_extract",
    "compression",
    "session_search",
    "skills_hub",
    "approval",
    "mcp",
    "flush_memories",
)


def _string(value: Any) -> str:
    return str(value or "").strip()


def _model_config(config: Dict[str, Any]) -> Dict[str, Any]:
    raw = config.get("model", {})
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.strip():
        return {"default": raw.strip()}
    return {}


def _main_choice(config: Dict[str, Any]) -> Tuple[str, str]:
    model_cfg = _model_config(config)
    return _string(model_cfg.get("provider")), _string(model_cfg.get("default") or model_cfg.get("model"))


def _summary_choice(config: Dict[str, Any]) -> Tuple[str, str]:
    comp = config.get("compression", {})
    if not isinstance(comp, dict):
        return "", ""
    provider = _string(comp.get("summary_provider"))
    model = _string(comp.get("summary_model"))
    if provider == "auto":
        provider = ""
    return provider, model


def _vision_choice(config: Dict[str, Any]) -> Tuple[str, str]:
    aux = config.get("auxiliary", {})
    if not isinstance(aux, dict):
        return "", ""
    vision = aux.get("vision", {})
    if not isinstance(vision, dict):
        return "", ""
    provider = _string(vision.get("provider"))
    model = _string(vision.get("model"))
    if provider == "auto":
        provider = ""
    return provider, model


def _recommended_aux_choice(config: Dict[str, Any]) -> Tuple[str, str]:
    for provider, model in (_summary_choice(config), _vision_choice(config), _main_choice(config)):
        if provider and model:
            return provider, model
    for provider, model in (_vision_choice(config), _main_choice(config)):
        if provider:
            return provider, model
    return "", ""


def build_model_audit(config: Dict[str, Any] | None = None) -> Dict[str, Any]:
    cfg = deepcopy(config if isinstance(config, dict) else load_config())
    main_provider, main_model = _main_choice(cfg)
    aux_provider, aux_model = _recommended_aux_choice(cfg)
    auxiliary = cfg.get("auxiliary", {})
    if not isinstance(auxiliary, dict):
        auxiliary = {}

    entries: List[Dict[str, Any]] = []

    def _append_entry(
        name: str,
        provider: str,
        model: str,
        recommended_provider: str,
        recommended_model: str,
    ) -> None:
        provider_missing = not provider or provider == "auto"
        model_missing = not model
        entries.append(
            {
                "name": name,
                "provider": provider,
                "model": model,
                "recommended_provider": recommended_provider,
                "recommended_model": recommended_model,
                "needs_provider": bool(recommended_provider and provider_missing),
                "needs_model": bool(recommended_model and model_missing),
            }
        )

    for task in _TEXT_AUX_TASKS:
        task_cfg = auxiliary.get(task, {})
        if not isinstance(task_cfg, dict):
            task_cfg = {}
        _append_entry(
            f"auxiliary.{task}",
            _string(task_cfg.get("provider")),
            _string(task_cfg.get("model")),
            aux_provider,
            aux_model,
        )

    vision_cfg = auxiliary.get("vision", {})
    if not isinstance(vision_cfg, dict):
        vision_cfg = {}
    _append_entry(
        "auxiliary.vision",
        _string(vision_cfg.get("provider")),
        _string(vision_cfg.get("model")),
        aux_provider,
        aux_model,
    )

    delegation = cfg.get("delegation", {})
    if not isinstance(delegation, dict):
        delegation = {}
    _append_entry(
        "delegation",
        _string(delegation.get("provider")),
        _string(delegation.get("model")),
        main_provider or aux_provider,
        main_model or aux_model,
    )

    return {
        "main_provider": main_provider,
        "main_model": main_model,
        "recommended_aux_provider": aux_provider,
        "recommended_aux_model": aux_model,
        "entries": entries,
    }


def apply_model_audit_defaults(config: Dict[str, Any] | None = None, *, persist: bool = True) -> Dict[str, Any]:
    cfg = deepcopy(config if isinstance(config, dict) else load_config())
    report = build_model_audit(cfg)
    auxiliary = cfg.setdefault("auxiliary", {})
    delegation = cfg.setdefault("delegation", {})
    changes: List[str] = []

    for entry in report["entries"]:
        name = entry["name"]
        if name.startswith("auxiliary."):
            task = name.split(".", 1)[1]
            task_cfg = auxiliary.setdefault(task, {})
            if entry["needs_provider"]:
                task_cfg["provider"] = entry["recommended_provider"]
                changes.append(f"{name}.provider={entry['recommended_provider']}")
            if entry["needs_model"]:
                task_cfg["model"] = entry["recommended_model"]
                changes.append(f"{name}.model={entry['recommended_model']}")
        elif name == "delegation":
            if entry["needs_provider"]:
                delegation["provider"] = entry["recommended_provider"]
                changes.append(f"delegation.provider={entry['recommended_provider']}")
            if entry["needs_model"]:
                delegation["model"] = entry["recommended_model"]
                changes.append(f"delegation.model={entry['recommended_model']}")

    if persist and changes:
        save_config(cfg)

    return {
        "config": cfg,
        "report": report,
        "changes": changes,
    }
