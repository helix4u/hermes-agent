from hermes_cli.model_audit import build_model_audit, apply_model_audit_defaults


def test_build_model_audit_recommends_existing_auxiliary_defaults():
    report = build_model_audit(
        {
            "model": {"provider": "nous", "default": "openai/gpt-5.4-mini"},
            "compression": {
                "summary_provider": "nous",
                "summary_model": "google/gemini-3-flash-preview",
            },
            "auxiliary": {
                "vision": {
                    "provider": "nous",
                    "model": "google/gemini-3-flash-preview",
                },
                "web_extract": {"provider": "auto", "model": ""},
            },
            "delegation": {"provider": "nous", "model": ""},
        }
    )

    assert report["recommended_aux_provider"] == "nous"
    assert report["recommended_aux_model"] == "google/gemini-3-flash-preview"
    web_extract = next(entry for entry in report["entries"] if entry["name"] == "auxiliary.web_extract")
    assert web_extract["needs_provider"] is True
    assert web_extract["needs_model"] is True


def test_apply_model_audit_defaults_fills_only_missing_values():
    result = apply_model_audit_defaults(
        {
            "model": {"provider": "nous", "default": "openai/gpt-5.4-mini"},
            "compression": {
                "summary_provider": "nous",
                "summary_model": "google/gemini-3-flash-preview",
            },
            "auxiliary": {
                "vision": {
                    "provider": "nous",
                    "model": "google/gemini-3-flash-preview",
                },
                "web_extract": {"provider": "auto", "model": ""},
                "approval": {"provider": "nous", "model": "custom-approval-model"},
            },
            "delegation": {"provider": "nous", "model": ""},
        },
        persist=False,
    )

    cfg = result["config"]
    assert cfg["auxiliary"]["web_extract"]["provider"] == "nous"
    assert cfg["auxiliary"]["web_extract"]["model"] == "google/gemini-3-flash-preview"
    assert cfg["auxiliary"]["approval"]["model"] == "custom-approval-model"
    assert cfg["delegation"]["model"] == "openai/gpt-5.4-mini"
