"""
Cron job scheduler - executes due jobs.

Provides tick() which checks for due jobs and runs them. The gateway
calls this every 60 seconds from a background thread.

Uses a file-based lock (~/.hermes/cron/.tick.lock) so only one tick
runs at a time if multiple processes overlap.
"""

import asyncio
import json
import logging
import os
import sys
import traceback

# fcntl is Unix-only; on Windows use msvcrt for file locking
try:
    import fcntl
except ImportError:
    fcntl = None
    try:
        import msvcrt
    except ImportError:
        msvcrt = None
from pathlib import Path
from hermes_constants import get_hermes_home
from typing import Optional

from hermes_time import now as _hermes_now

logger = logging.getLogger(__name__)

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from cron.jobs import get_due_jobs, mark_job_run, save_job_output, advance_next_run

# Sentinel: when a cron agent has nothing new to report, it can start its
# response with this marker to suppress delivery.  Output is still saved
# locally for audit.
SILENT_MARKER = "[SILENT]"

# Resolve Hermes home directory (respects HERMES_HOME override)
_hermes_home = get_hermes_home()
_initial_hermes_home = _hermes_home


def _get_hermes_home() -> Path:
    """Resolve Hermes home lazily so tests and long-lived processes see env changes."""
    if _hermes_home != _initial_hermes_home:
        return _hermes_home
    env_home = os.getenv("HERMES_HOME")
    if env_home:
        return Path(env_home).expanduser()
    return _hermes_home


def _emit_audit_event(audit_store, session_id: Optional[str], **kwargs) -> None:
    if not audit_store or not session_id:
        return
    try:
        audit_store.append(
            session_id,
            source_platform="cron",
            source_surface="cron",
            **kwargs,
        )
    except Exception as e:
        logger.debug("Cron audit event failed: %s", e)


def _resolve_origin(job: dict) -> Optional[dict]:
    """Extract origin info from a job, preserving any extra routing metadata."""
    origin = job.get("origin")
    if not origin:
        return None
    platform = origin.get("platform")
    chat_id = origin.get("chat_id")
    if platform and chat_id:
        return origin
    return None


def _resolve_delivery_target(job: dict) -> Optional[dict]:
    """Resolve the concrete auto-delivery target for a cron job, if any."""
    deliver = job.get("deliver", "local")
    origin = _resolve_origin(job)

    if deliver == "local":
        return None

    if deliver == "origin":
        if not origin:
            return None
        return {
            "platform": origin["platform"],
            "chat_id": str(origin["chat_id"]),
            "thread_id": origin.get("thread_id"),
        }

    if ":" in deliver:
        platform_name, rest = deliver.split(":", 1)
        # Check for thread_id suffix (e.g. "telegram:-1003724596514:17")
        if ":" in rest:
            chat_id, thread_id = rest.split(":", 1)
        else:
            chat_id, thread_id = rest, None
        return {
            "platform": platform_name,
            "chat_id": chat_id,
            "thread_id": thread_id,
        }

    platform_name = deliver
    if origin and origin.get("platform") == platform_name:
        return {
            "platform": platform_name,
            "chat_id": str(origin["chat_id"]),
            "thread_id": origin.get("thread_id"),
        }

    chat_id = os.getenv(f"{platform_name.upper()}_HOME_CHANNEL", "")
    if not chat_id:
        return None

    return {
        "platform": platform_name,
        "chat_id": chat_id,
        "thread_id": None,
    }


def _deliver_result(job: dict, content: str, *, session_id: Optional[str] = None, audit_store=None) -> None:
    """
    Deliver job output to the configured target (origin chat, specific platform, etc.).

    Uses the standalone platform send functions from send_message_tool so delivery
    works whether or not the gateway is running.
    """
    target = _resolve_delivery_target(job)
    if not target:
        if job.get("deliver", "local") != "local":
            logger.warning(
                "Job '%s' deliver=%s but no concrete delivery target could be resolved",
                job["id"],
                job.get("deliver", "local"),
            )
            _emit_audit_event(
                audit_store,
                session_id,
                kind="delivery",
                phase="finalizing",
                status="error",
                is_error=True,
                title="Delivery target unresolved",
                preview=f"deliver={job.get('deliver', 'local')}",
                payload={"job_id": job["id"], "deliver": job.get("deliver", "local")},
            )
        return

    platform_name = target["platform"]
    chat_id = target["chat_id"]
    thread_id = target.get("thread_id")
    target_label = f"{platform_name}:{chat_id}"
    if thread_id is not None:
        target_label += f":{thread_id}"

    from tools.send_message_tool import _send_to_platform
    from gateway.config import load_gateway_config, Platform

    platform_map = {
        "telegram": Platform.TELEGRAM,
        "discord": Platform.DISCORD,
        "slack": Platform.SLACK,
        "whatsapp": Platform.WHATSAPP,
        "signal": Platform.SIGNAL,
        "matrix": Platform.MATRIX,
        "mattermost": Platform.MATTERMOST,
        "homeassistant": Platform.HOMEASSISTANT,
        "dingtalk": Platform.DINGTALK,
        "email": Platform.EMAIL,
        "sms": Platform.SMS,
    }
    platform = platform_map.get(platform_name.lower())
    if not platform:
        logger.warning("Job '%s': unknown platform '%s' for delivery", job["id"], platform_name)
        return

    logger.info(
        "Job '%s': attempting delivery to %s (deliver=%s, chars=%d)",
        job["id"],
        target_label,
        job.get("deliver", "local"),
        len(str(content or "")),
    )
    _emit_audit_event(
        audit_store,
        session_id,
        kind="delivery",
        phase="finalizing",
        status="running",
        title="Delivery attempt",
        preview=f"{platform_name} delivery",
        payload={
            "job_id": job["id"],
            "platform": platform_name,
            "deliver": job.get("deliver", "local"),
            "content_chars": len(str(content or "")),
        },
    )

    try:
        config = load_gateway_config()
    except Exception as e:
        logger.error("Job '%s': failed to load gateway config for delivery: %s", job["id"], e)
        _emit_audit_event(
            audit_store,
            session_id,
            kind="delivery",
            phase="finalizing",
            status="error",
            is_error=True,
            title="Delivery config load failed",
            preview=str(e),
            payload={"job_id": job["id"], "error": str(e)},
        )
        return

    pconfig = config.platforms.get(platform)
    if not pconfig or not pconfig.enabled:
        logger.warning(
            "Job '%s': platform '%s' not configured/enabled (enabled=%s, has_token=%s)",
            job["id"],
            platform_name,
            bool(pconfig and pconfig.enabled),
            bool(getattr(pconfig, "token", None)),
        )
        _emit_audit_event(
            audit_store,
            session_id,
            kind="delivery",
            phase="finalizing",
            status="error",
            is_error=True,
            title="Delivery platform unavailable",
            preview=platform_name,
            payload={"job_id": job["id"], "platform": platform_name},
        )
        return

    # Wrap the content so the user knows this is a cron delivery and that
    # the interactive agent has no visibility into it.
    task_name = job.get("name", job["id"])
    wrapped = (
        f"Cronjob Response: {task_name}\n"
        f"-------------\n\n"
        f"{content}\n\n"
        f"Note: The agent cannot see this message, and therefore cannot respond to it."
    )

    # Run the async send in a fresh event loop (safe from any thread)
    coro = _send_to_platform(platform, pconfig, chat_id, wrapped, thread_id=thread_id)
    try:
        result = asyncio.run(coro)
    except RuntimeError:
        # asyncio.run() checks for a running loop before awaiting the coroutine;
        # when it raises, the original coro was never started — close it to
        # prevent "coroutine was never awaited" RuntimeWarning, then retry in a
        # fresh thread that has no running loop.
        coro.close()
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(asyncio.run, _send_to_platform(platform, pconfig, chat_id, wrapped, thread_id=thread_id))
            result = future.result(timeout=30)
    except Exception as e:
        logger.error("Job '%s': delivery to %s failed: %s", job["id"], target_label, e)
        _emit_audit_event(
            audit_store,
            session_id,
            kind="delivery",
            phase="finalizing",
            status="error",
            is_error=True,
            title="Delivery send failed",
            preview=str(e),
            payload={"job_id": job["id"], "platform": platform_name, "error": str(e)},
        )
        return

    if result and result.get("error"):
        logger.error("Job '%s': delivery error to %s: %s", job["id"], target_label, result["error"])
        _emit_audit_event(
            audit_store,
            session_id,
            kind="delivery",
            phase="finalizing",
            status="error",
            is_error=True,
            title="Delivery error",
            preview=str(result["error"]),
            payload={"job_id": job["id"], "platform": platform_name, "error": result["error"]},
        )
    else:
        logger.info("Job '%s': delivered to %s", job["id"], target_label)
        _emit_audit_event(
            audit_store,
            session_id,
            kind="delivery",
            phase="done",
            status="ok",
            title="Delivered result",
            preview=f"{platform_name} delivery succeeded",
            payload={"job_id": job["id"], "platform": platform_name},
        )


def _build_job_prompt(job: dict) -> str:
    """Build the effective prompt for a cron job, optionally loading one or more skills first."""
    prompt = job.get("prompt", "")
    skills = job.get("skills")

    # Always prepend [SILENT] guidance so the cron agent can suppress
    # delivery when it has nothing new or noteworthy to report.
    silent_hint = (
        "[SYSTEM: If you have nothing new or noteworthy to report, respond "
        "with exactly \"[SILENT]\" (optionally followed by a brief internal "
        "note). This suppresses delivery to the user while still saving "
        "output locally. Only use [SILENT] when there are genuinely no "
        "changes worth reporting.]\n\n"
    )
    prompt = silent_hint + prompt
    if skills is None:
        legacy = job.get("skill")
        skills = [legacy] if legacy else []

    skill_names = [str(name).strip() for name in skills if str(name).strip()]
    if not skill_names:
        return prompt

    from tools.skills_tool import skill_view

    parts = []
    skipped: list[str] = []
    for skill_name in skill_names:
        loaded = json.loads(skill_view(skill_name))
        if not loaded.get("success"):
            error = loaded.get("error") or f"Failed to load skill '{skill_name}'"
            logger.warning("Cron job '%s': skill not found, skipping — %s", job.get("name", job.get("id")), error)
            skipped.append(skill_name)
            continue

        content = str(loaded.get("content") or "").strip()
        if parts:
            parts.append("")
        parts.extend(
            [
                f'[SYSTEM: The user has invoked the "{skill_name}" skill, indicating they want you to follow its instructions. The full skill content is loaded below.]',
                "",
                content,
            ]
        )

    if skipped:
        notice = (
            f"[SYSTEM: The following skill(s) were listed for this job but could not be found "
            f"and were skipped: {', '.join(skipped)}. "
            f"Start your response with a brief notice so the user is aware, e.g.: "
            f"'⚠️ Skill(s) not found and skipped: {', '.join(skipped)}']"
        )
        parts.insert(0, notice)

    if prompt:
        parts.extend(["", f"The user has provided the following instruction alongside the skill invocation: {prompt}"])
    return "\n".join(parts)


def run_job(
    job: dict,
    *,
    tool_progress_callback=None,
    thinking_callback=None,
    platform: str = "cron",
    session_id: Optional[str] = None,
) -> tuple[bool, str, str, Optional[str]]:
    """
    Execute a single cron job.
    
    Returns:
        Tuple of (success, full_output_doc, final_response, error_message)
    """
    from run_agent import AIAgent
    
    # Initialize SQLite session store so cron job messages are persisted
    # and discoverable via session_search (same pattern as gateway/run.py).
    _session_db = None
    audit_store = None
    try:
        from hermes_state import AuditEventStore, SessionDB
        _session_db = SessionDB()
        audit_store = AuditEventStore(_session_db)
    except Exception as e:
        logger.debug("Job '%s': SQLite session store not available: %s", job.get("id", "?"), e)
    
    job_id = job["id"]
    job_name = job["name"]
    prompt = _build_job_prompt(job)
    origin = _resolve_origin(job)
    _cron_session_id = f"cron_{job_id}_{_hermes_now().strftime('%Y%m%d_%H%M%S')}"

    effective_session_id = session_id or f"cron_{job_id}_{_hermes_now().strftime('%Y%m%d_%H%M%S')}"
    job["__last_session_id"] = effective_session_id

    logger.info("Running job '%s' (ID: %s)", job_name, job_id)
    logger.info("Prompt: %s", prompt[:100])
    _emit_audit_event(
        audit_store,
        effective_session_id,
        kind="cron",
        phase="starting",
        status="running",
        title="Cron job triggered",
        preview=job_name,
        payload={
            "job_id": job_id,
            "name": job_name,
            "schedule": job.get("schedule_display"),
            "deliver": job.get("deliver", "local"),
        },
    )

    # Inject origin context so the agent's send_message tool knows the chat
    if origin:
        os.environ["HERMES_SESSION_PLATFORM"] = origin["platform"]
        os.environ["HERMES_SESSION_CHAT_ID"] = str(origin["chat_id"])
        if origin.get("chat_name"):
            os.environ["HERMES_SESSION_CHAT_NAME"] = origin["chat_name"]

    try:
        # Re-read .env and config.yaml fresh every run so provider/key
        # changes take effect without a gateway restart.
        hermes_home = _get_hermes_home()
        from dotenv import load_dotenv
        try:
            load_dotenv(str(hermes_home / ".env"), override=True, encoding="utf-8")
        except UnicodeDecodeError:
            load_dotenv(str(hermes_home / ".env"), override=True, encoding="latin-1")

        delivery_target = _resolve_delivery_target(job)
        if delivery_target:
            os.environ["HERMES_CRON_AUTO_DELIVER_PLATFORM"] = delivery_target["platform"]
            os.environ["HERMES_CRON_AUTO_DELIVER_CHAT_ID"] = str(delivery_target["chat_id"])
            if delivery_target.get("thread_id") is not None:
                os.environ["HERMES_CRON_AUTO_DELIVER_THREAD_ID"] = str(delivery_target["thread_id"])

        model = job.get("model") or os.getenv("HERMES_MODEL") or "anthropic/claude-opus-4.6"

        # Load config.yaml for model, reasoning, prefill, toolsets, provider routing
        _cfg = {}
        try:
            import yaml
            _cfg_path = str(hermes_home / "config.yaml")
            if os.path.exists(_cfg_path):
                with open(_cfg_path, encoding="utf-8") as _f:
                    _cfg = yaml.safe_load(_f) or {}
                _model_cfg = _cfg.get("model", {})
                if not job.get("model"):
                    if isinstance(_model_cfg, str):
                        model = _model_cfg
                    elif isinstance(_model_cfg, dict):
                        model = _model_cfg.get("default", model)
        except Exception as e:
            logger.warning("Job '%s': failed to load config.yaml, using defaults: %s", job_id, e)

        # Reasoning config from env or config.yaml
        from hermes_constants import parse_reasoning_effort
        effort = os.getenv("HERMES_REASONING_EFFORT", "")
        if not effort:
            effort = str(_cfg.get("agent", {}).get("reasoning_effort", "")).strip()
        reasoning_config = parse_reasoning_effort(effort)

        # Prefill messages from env or config.yaml
        prefill_messages = None
        prefill_file = os.getenv("HERMES_PREFILL_MESSAGES_FILE", "") or _cfg.get("prefill_messages_file", "")
        if prefill_file:
            import json as _json
            pfpath = Path(prefill_file).expanduser()
            if not pfpath.is_absolute():
                pfpath = hermes_home / pfpath
            if pfpath.exists():
                try:
                    with open(pfpath, "r", encoding="utf-8") as _pf:
                        prefill_messages = _json.load(_pf)
                    if not isinstance(prefill_messages, list):
                        prefill_messages = None
                except Exception as e:
                    logger.warning("Job '%s': failed to parse prefill messages file '%s': %s", job_id, pfpath, e)
                    prefill_messages = None

        # Max iterations
        max_iterations = _cfg.get("agent", {}).get("max_turns") or _cfg.get("max_turns") or 90

        # Provider routing
        pr = _cfg.get("provider_routing", {})
        smart_routing = _cfg.get("smart_model_routing", {}) or {}

        from hermes_cli.runtime_provider import (
            resolve_runtime_provider,
            format_runtime_provider_error,
        )
        try:
            runtime_kwargs = {
                "requested": job.get("provider") or os.getenv("HERMES_INFERENCE_PROVIDER"),
            }
            if job.get("base_url"):
                runtime_kwargs["explicit_base_url"] = job.get("base_url")
            try:
                runtime = resolve_runtime_provider(**runtime_kwargs, allow_device_auth=False)
            except TypeError as exc:
                if "allow_device_auth" not in str(exc):
                    raise
                runtime = resolve_runtime_provider(**runtime_kwargs)
        except Exception as exc:
            message = format_runtime_provider_error(exc)
            raise RuntimeError(message) from exc

        from agent.smart_model_routing import resolve_turn_route
        turn_route = resolve_turn_route(
            prompt,
            smart_routing,
            {
                "model": model,
                "api_key": runtime.get("api_key"),
                "base_url": runtime.get("base_url"),
                "provider": runtime.get("provider"),
                "api_mode": runtime.get("api_mode"),
                "command": runtime.get("command"),
                "args": list(runtime.get("args") or []),
            },
        )

        agent = AIAgent(
            model=turn_route["model"],
            api_key=turn_route["runtime"].get("api_key"),
            base_url=turn_route["runtime"].get("base_url"),
            provider=turn_route["runtime"].get("provider"),
            api_mode=turn_route["runtime"].get("api_mode"),
            acp_command=turn_route["runtime"].get("command"),
            acp_args=turn_route["runtime"].get("args"),
            max_iterations=max_iterations,
            reasoning_config=reasoning_config,
            prefill_messages=prefill_messages,
            providers_allowed=pr.get("only"),
            providers_ignored=pr.get("ignore"),
            providers_order=pr.get("order"),
            provider_sort=pr.get("sort"),
            disabled_toolsets=["cronjob", "messaging", "clarify"],
            quiet_mode=True,
            platform=platform,
            session_id=effective_session_id,
            session_db=_session_db,
            tool_progress_callback=tool_progress_callback,
            thinking_callback=thinking_callback,
        )
        
        result = agent.run_conversation(prompt)
        
        final_response = result.get("final_response", "") or ""
        # Use a separate variable for log display; keep final_response clean
        # for delivery logic (empty response = no delivery).
        logged_response = final_response if final_response else "(No response generated)"
        
        output = f"""# Cron Job: {job_name}

**Job ID:** {job_id}
**Run Time:** {_hermes_now().strftime('%Y-%m-%d %H:%M:%S')}
**Schedule:** {job.get('schedule_display', 'N/A')}

## Prompt

{prompt}

## Response

{logged_response}
"""
        
        logger.info("Job '%s' completed successfully", job_name)
        _emit_audit_event(
            audit_store,
            effective_session_id,
            kind="cron",
            phase="done",
            status="ok",
            title="Cron job completed",
            preview=final_response[:220] if final_response else job_name,
            payload={"job_id": job_id, "success": True},
        )
        return True, output, final_response, None
        
    except Exception as e:
        error_msg = f"{type(e).__name__}: {str(e)}"
        logger.error("Job '%s' failed: %s", job_name, error_msg)
        _emit_audit_event(
            audit_store,
            effective_session_id,
            kind="cron",
            phase="done",
            status="error",
            is_error=True,
            title="Cron job failed",
            preview=error_msg[:220],
            payload={"job_id": job_id, "error": error_msg},
        )
        
        output = f"""# Cron Job: {job_name} (FAILED)

**Job ID:** {job_id}
**Run Time:** {_hermes_now().strftime('%Y-%m-%d %H:%M:%S')}
**Schedule:** {job.get('schedule_display', 'N/A')}

## Prompt

{prompt}

## Error

```
{error_msg}

{traceback.format_exc()}
```
"""
        return False, output, "", error_msg

    finally:
        # Clean up injected env vars so they don't leak to other jobs
        for key in (
            "HERMES_SESSION_PLATFORM",
            "HERMES_SESSION_CHAT_ID",
            "HERMES_SESSION_CHAT_NAME",
            "HERMES_CRON_AUTO_DELIVER_PLATFORM",
            "HERMES_CRON_AUTO_DELIVER_CHAT_ID",
            "HERMES_CRON_AUTO_DELIVER_THREAD_ID",
        ):
            os.environ.pop(key, None)
        if _session_db:
            try:
                _session_db.end_session(_cron_session_id, "cron_complete")
            except (Exception, KeyboardInterrupt) as e:
                logger.debug("Job '%s': failed to end session: %s", job_id, e)
            try:
                _session_db.close()
            except (Exception, KeyboardInterrupt) as e:
                logger.debug("Job '%s': failed to close SQLite session store: %s", job_id, e)


def tick(verbose: bool = True) -> int:
    """
    Check and run all due jobs.
    
    Uses a file lock so only one tick runs at a time, even if the gateway's
    in-process ticker and a standalone daemon or manual tick overlap.
    
    Args:
        verbose: Whether to print status messages
    
    Returns:
        Number of jobs executed (0 if another tick is already running)
    """
    hermes_home = _get_hermes_home()
    lock_dir = hermes_home / "cron"
    lock_file = lock_dir / ".tick.lock"
    lock_dir.mkdir(parents=True, exist_ok=True)

    # Cross-platform file locking: fcntl on Unix, msvcrt on Windows
    lock_fd = None
    try:
        lock_fd = open(lock_file, "w")
        if fcntl:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        elif msvcrt:
            msvcrt.locking(lock_fd.fileno(), msvcrt.LK_NBLCK, 1)
    except (OSError, IOError):
        logger.debug("Tick skipped — another instance holds the lock")
        if lock_fd is not None:
            lock_fd.close()
        return 0

    try:
        due_jobs = get_due_jobs()

        if verbose and not due_jobs:
            logger.info("%s - No jobs due", _hermes_now().strftime('%H:%M:%S'))
            return 0

        if verbose:
            logger.info("%s - %s job(s) due", _hermes_now().strftime('%H:%M:%S'), len(due_jobs))

        executed = 0
        for job in due_jobs:
            try:
                # For recurring jobs (cron/interval), advance next_run_at to the
                # next future occurrence BEFORE execution.  This way, if the
                # process crashes mid-run, the job won't re-fire on restart.
                # One-shot jobs are left alone so they can retry on restart.
                advance_next_run(job["id"])

                success, output, final_response, error = run_job(job)

                output_file = save_job_output(job["id"], output)
                if verbose:
                    logger.info("Output saved to: %s", output_file)

                # Deliver the final response to the origin/target chat.
                # If the agent responded with [SILENT], skip delivery (but
                # output is already saved above).  Failed jobs always deliver.
                deliver_content = final_response if success else f"⚠️ Cron job '{job.get('name', job['id'])}' failed:\n{error}"
                should_deliver = bool(deliver_content)
                if should_deliver and success and deliver_content.strip().upper().startswith(SILENT_MARKER):
                    logger.info("Job '%s': agent returned %s — skipping delivery", job["id"], SILENT_MARKER)
                    try:
                        from hermes_state import AuditEventStore, SessionDB
                        _db = SessionDB()
                        _emit_audit_event(
                            AuditEventStore(_db),
                            job.get("__last_session_id"),
                            kind="delivery",
                            phase="finalizing",
                            status="skipped",
                            title="Delivery suppressed",
                            preview=SILENT_MARKER,
                            payload={"job_id": job["id"], "reason": "silent_marker"},
                        )
                        _db.close()
                    except Exception:
                        pass
                    should_deliver = False
                elif not should_deliver:
                    logger.info(
                        "Job '%s': no delivery attempted because final response was empty",
                        job["id"],
                    )
                    try:
                        from hermes_state import AuditEventStore, SessionDB
                        _db = SessionDB()
                        _emit_audit_event(
                            AuditEventStore(_db),
                            job.get("__last_session_id"),
                            kind="delivery",
                            phase="finalizing",
                            status="skipped",
                            title="No delivery attempted",
                            preview="final response was empty",
                            payload={"job_id": job["id"], "reason": "empty_response"},
                        )
                        _db.close()
                    except Exception:
                        pass

                if should_deliver:
                    try:
                        delivery_session_id = job.get("__last_session_id")
                        audit_store = None
                        try:
                            from hermes_state import AuditEventStore, SessionDB
                            delivery_db = SessionDB()
                            audit_store = AuditEventStore(delivery_db)
                        except Exception:
                            delivery_db = None
                        try:
                            _deliver_result(job, deliver_content, session_id=delivery_session_id, audit_store=audit_store)
                        finally:
                            if 'delivery_db' in locals() and delivery_db is not None:
                                try:
                                    delivery_db.close()
                                except Exception:
                                    pass
                    except Exception as de:
                        logger.error("Delivery failed for job %s: %s", job["id"], de)

                mark_job_run(job["id"], success, error)
                executed += 1

            except Exception as e:
                logger.error("Error processing job %s: %s", job['id'], e)
                mark_job_run(job["id"], False, str(e))

        return executed
    finally:
        if fcntl:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
        elif msvcrt:
            try:
                msvcrt.locking(lock_fd.fileno(), msvcrt.LK_UNLCK, 1)
            except (OSError, IOError):
                pass
        lock_fd.close()


if __name__ == "__main__":
    tick(verbose=True)
