"""Regressoes offline do Work at a Startup APPLY V0."""

from __future__ import annotations

import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = PROJECT_ROOT / "src"
LAUNCHER_PATH = PROJECT_ROOT / "scripts" / "start_chrome.bat"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

import workatastartup_apply as waas  # noqa: E402


def test_model_order_is_exact() -> None:
    assert waas.AI_MODELS == (
        "z-ai/glm-5.3-flash",
        "tencent/hy4-flash",
        "nvidia/nemotron-3-ultra-550b-a55b:free",
    )
    assert waas.AI_MAX_OUTPUT_TOKENS == 1_200


def test_explicit_env_file_is_supported(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text("OPENROUTER_API_KEY=test-only-value\n", encoding="utf-8")
    assert waas.obter_openrouter_api_key(env_file) == "test-only-value"


def test_launcher_exposes_optional_headless_mode() -> None:
    launcher = LAUNCHER_PATH.read_text(encoding="utf-8")
    assert 'if /I "%~1"=="headless"' in launcher
    assert "--headless=new" in launcher
    assert ".chrome-profile" in launcher


def test_default_profile_is_project_local() -> None:
    assert waas.DEFAULT_PROFILE_RULES == PROJECT_ROOT / "config" / "profile.local.json"
    assert waas.REPORT_DIR == PROJECT_ROOT / "logs"


def test_job_url_normalization_is_fail_closed() -> None:
    assert (
        waas.normalizar_job_url("https://www.workatastartup.com/jobs/120695?source=list")
        == "https://www.workatastartup.com/jobs/120695"
    )
    assert waas.normalizar_job_url("https://example.com/jobs/120695") == ""
    assert waas.normalizar_job_url("https://www.workatastartup.com/companies") == ""


def test_current_filtered_listing_is_preserved() -> None:
    assert waas.url_listagem_valida(
        "https://www.workatastartup.com/companies?role=eng&remote=true&sortBy=created_desc"
    )
    assert waas.url_listagem_valida("https://www.workatastartup.com/companies/")
    assert not waas.url_listagem_valida("https://www.workatastartup.com/jobs/120695")
    assert not waas.url_listagem_valida("https://example.com/companies")


def test_profile_sanitizer_removes_contact_and_secret_values() -> None:
    result = waas._sanitizar_profile_value(
        {
            "phone": "+5511999999999",
            "email_address": "candidate@example.com",
            "api_key": "not-a-real-key",
            "skills": ["Python", "Automation"],
        }
    )
    assert result["phone"] == "<redacted>"
    assert result["email_address"] == "<redacted>"
    assert result["api_key"] == "<redacted>"
    assert result["skills"] == ["Python", "Automation"]


def test_message_validation_enforces_length_and_contact_boundary() -> None:
    valid, reason, _ = waas.validar_mensagem_ia(
        "I am interested in helping the team automate reliable growth workflows. "
        "My background combines Python automation, data analysis, and digital growth with "
        "hands-on work building practical systems. I learn continuously, communicate clearly, "
        "and focus on reliable execution. I would welcome the opportunity to contribute these "
        "transferable skills while learning from the team and supporting measurable results."
    )
    assert valid is True
    assert reason == "ok"

    assert waas.validar_mensagem_ia("Too short")[1] == "message_too_short"
    assert waas.validar_mensagem_ia("See https://example.com for my profile and experience.")[1] in {
        "message_too_short",
        "message_contains_contact_or_url",
    }


def test_ai_message_extraction_accepts_provider_variations() -> None:
    message = "I am interested in contributing reliable automation and analytical problem solving to this role."
    assert waas.extrair_mensagem_modelo(json.dumps({"message": message})) == message
    assert waas.extrair_mensagem_modelo(f"```json\n{json.dumps({'message': message})}\n```") == message
    assert waas.extrair_mensagem_modelo(json.dumps(message)) == message
    assert waas.extrair_mensagem_modelo(f"Message: {message}") == message
    assert waas.extrair_mensagem_modelo(f"<think>internal reasoning</think>\n{message}") == message


def test_long_ai_message_is_reduced_without_exceeding_limit() -> None:
    long_message = ("This is a truthful sentence about transferable experience. " * 20).strip()
    adjusted = waas.ajustar_mensagem_ao_limite(long_message)
    assert waas.MIN_MESSAGE_CHARS <= len(adjusted) <= waas.MAX_MESSAGE_CHARS
    assert adjusted.endswith(".")


def test_local_fallback_is_valid_and_job_specific() -> None:
    context = waas.JobContext(
        url="https://www.workatastartup.com/jobs/123",
        title="Business Operations Executive",
        company="Example Startup",
        visible_text="Example description",
    )
    message = waas.construir_mensagem_fallback(context)
    valid, reason, normalized = waas.validar_mensagem_ia(message)
    assert valid is True
    assert reason == "ok"
    assert "Business Operations Executive" in normalized
    assert "Example Startup" in normalized


def test_v0_selectors_and_submit_boundary_are_explicit() -> None:
    assert waas.APPLY_MESSAGE_SELECTOR == "textarea#apply-message"
    assert "a" in waas.INTERACTIVE_CONTROL_SELECTOR
    assert "button" in waas.INTERACTIVE_CONTROL_SELECTOR
    assert waas.SEND_BUTTON_TEXT == "Send"
    assert waas.APPLIED_BUTTON_TEXT == "Applied"
    parser = waas.criar_parser()
    args = parser.parse_args(["--apply-batch"])
    assert args.execute_apply is False
    unlimited = parser.parse_args(["--apply-batch", "--max-jobs", "1000", "--max-scan", "5000"])
    assert unlimited.max_jobs == 1000
    assert unlimited.max_scan == 5000


def test_ai_prompt_never_filters_by_candidate_fit() -> None:
    prompt = waas.prompt_sistema().casefold()
    assert "never rank, reject or skip a job" in prompt
    assert "always produce a message" in prompt


def test_only_location_preference_checkbox_is_allowed_as_auxiliary_field() -> None:
    assert waas.campo_auxiliar_padrao_permitido(
        "input",
        "checkbox",
        "I'm open to relocating — update my location preferences",
    )
    assert not waas.campo_auxiliar_padrao_permitido("input", "text", "Years of experience")
    assert not waas.campo_auxiliar_padrao_permitido("select", "", "Current location")
    assert not waas.campo_auxiliar_padrao_permitido("input", "checkbox", "Agree to terms")
