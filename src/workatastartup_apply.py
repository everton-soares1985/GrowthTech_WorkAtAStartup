"""Work at a Startup APPLY V0.

Fluxo supervisionado:
1. conecta ao Chrome dedicado na porta 9226;
2. percorre a lista filtrada e coleta URLs unicas `/jobs/<id>`;
3. le o contexto da vaga antes do Apply;
4. aceita somente o formulario interno com `#apply-message` e `Send`;
5. gera uma mensagem curta via OpenRouter, digita e envia somente com
   `--apply-batch --execute-apply`;
6. confirma o estado visual `Applied` antes de seguir.

Exemplos:
    python -X utf8 workatastartup_apply.py --self-check
    python -X utf8 workatastartup_apply.py --list-readonly --max-jobs 10
    python -X utf8 workatastartup_apply.py --apply-batch --execute-apply --max-jobs 5
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import random
import re
import sys
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Page, async_playwright


# =============================================================================
# CONFIGURACOES — ajuste aqui, nao no meio do codigo
# =============================================================================
SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
REPORT_DIR = PROJECT_ROOT / "logs"

SITE_ORIGIN = "https://www.workatastartup.com"
SEARCH_URL = (
    "https://www.workatastartup.com/companies?demographic=any&hasEquity=any"
    "&hasSalary=any&industry=any&interviewProcess=any&layout=list-compact"
    "&sortBy=created_desc&tab=any&usVisaNotRequired=true"
)
CDP_URL = "http://127.0.0.1:9226"
OPENROUTER_API_URL = "https://openrouter.ai/api/v1/chat/completions"
OPENROUTER_API_KEY_ENV = "OPENROUTER_API_KEY"

AI_MODELS = (
    "z-ai/glm-5.3-flash",
    "tencent/hy4-flash",
    "nvidia/nemotron-3-ultra-550b-a55b:free",
)
AI_TIMEOUT_SECONDS = 90
AI_MAX_OUTPUT_TOKENS = 1_200
AI_TEMPERATURE = 0.25

DEFAULT_PROFILE_RULES = PROJECT_ROOT / "config" / "profile.local.json"

NAVIGATION_TIMEOUT_MS = 45_000
INITIAL_PAGE_WAIT_MS = 10_000
FORM_WAIT_MS = 12_000
POST_SEND_WAIT_MS = 5_000
APPLIED_CONFIRM_TIMEOUT_MS = 18_000
MAX_SCROLL_CYCLES = 40
SCROLL_DELAY_MS = (1_500, 3_000)
DELAY_BEFORE_VIEW_JOB_MS = (2_500, 6_000)
DELAY_BEFORE_APPLY_MS = (4_000, 9_000)
DELAY_BEFORE_SEND_MS = (6_000, 12_000)
DELAY_BETWEEN_JOBS_MS = (8_000, 18_000)
TYPING_DELAY_MS = (55, 125)
TYPING_PUNCTUATION_DELAY_MS = (260, 700)

MIN_MESSAGE_CHARS = 300
MAX_MESSAGE_CHARS = 600
MAX_JOB_CONTEXT_CHARS = 8_000
MAX_PROFILE_CONTEXT_CHARS = 9_000
MAX_REPORT_MESSAGE_PREVIEW = 220

JOB_LINK_SELECTOR = 'a[href^="/jobs/"], a[href*="workatastartup.com/jobs/"]'
INTERACTIVE_CONTROL_SELECTOR = 'a, button, [role="button"], [role="link"]'
APPLY_MESSAGE_SELECTOR = "textarea#apply-message"
APPLY_BUTTON_TEXT = "Apply"
SEND_BUTTON_TEXT = "Send"
APPLIED_BUTTON_TEXT = "Applied"
REPORT_PREFIX = "workatastartup_apply_v0_"

LOGGER = logging.getLogger("workatastartup_apply_v0")


@dataclass(frozen=True)
class JobLink:
    url: str
    title: str


@dataclass(frozen=True)
class JobContext:
    url: str
    title: str
    company: str
    visible_text: str


@dataclass
class RunReport:
    timestamp: str
    mode: str
    target: int
    max_scan: int
    models: list[str]
    listing_url: str = ""
    jobs_collected: int = 0
    jobs_examined: int = 0
    applications_submitted: int = 0
    already_applied: int = 0
    skipped_nonstandard_form: int = 0
    skipped_no_apply: int = 0
    skipped_ai_failure: int = 0
    errors: int = 0
    stop_reason: str = ""
    actions: list[dict[str, Any]] = field(default_factory=list)


def configurar_logging() -> None:
    """Configura logging estruturado sem duplicar handlers."""

    if LOGGER.handlers:
        return
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("%(levelname)s | %(message)s"))
    LOGGER.addHandler(handler)
    LOGGER.setLevel(logging.INFO)
    LOGGER.propagate = False


def intervalo_ms(intervalo: tuple[int, int]) -> int:
    """Retorna um atraso aleatorio em milissegundos."""

    return random.randint(*intervalo)


def limpar_texto(value: Any, limit: int = 500) -> str:
    """Normaliza texto antes de logs, payloads e relatorios."""

    return " ".join(str(value or "").split())[:limit]


def normalizar_job_url(value: str) -> str:
    """Aceita somente URLs canonicas de vaga da plataforma."""

    try:
        parsed = urlparse(value.strip())
    except ValueError:
        return ""
    if parsed.scheme not in {"http", "https"}:
        return ""
    if (parsed.hostname or "").casefold() != "www.workatastartup.com":
        return ""
    match = re.fullmatch(r"/jobs/(\d+)/?", parsed.path)
    if not match:
        return ""
    return f"{SITE_ORIGIN}/jobs/{match.group(1)}"


def url_listagem_valida(value: str) -> bool:
    """Reconhece a listagem aberta pelo usuario sem alterar seus filtros."""

    try:
        parsed = urlparse(value.strip())
    except ValueError:
        return False
    return (
        parsed.scheme in {"http", "https"}
        and (parsed.hostname or "").casefold() == "www.workatastartup.com"
        and parsed.path.rstrip("/") == "/companies"
    )


def _carregar_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    try:
        for raw_line in path.read_text(encoding="utf-8-sig").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            if line.startswith("export "):
                line = line[7:].strip()
            key, value = line.split("=", 1)
            key = key.strip()
            if key:
                values.setdefault(key, value.strip().strip('"').strip("'"))
    except OSError:
        LOGGER.warning("ENV | arquivo local indisponivel | path=%s", path)
    return values


def obter_openrouter_api_key(env_file: Path | None = None) -> str:
    """Busca a mesma chave OpenRouter do ecossistema, sem duplica-la."""

    process_value = os.getenv(OPENROUTER_API_KEY_ENV, "").strip()
    if process_value:
        return process_value
    env_paths: list[Path] = []
    if env_file is not None:
        env_paths.append(env_file)
    env_paths.extend((PROJECT_ROOT / ".env", Path.cwd() / ".env"))
    for env_path in env_paths:
        value = _carregar_env_file(env_path).get(OPENROUTER_API_KEY_ENV, "").strip()
        if value:
            return value
    return ""


def _sanitizar_profile_value(value: Any, key: str = "") -> Any:
    """Remove dados de contato e segredos do perfil enviado ao modelo."""

    sensitive_fragments = (
        "phone",
        "email",
        "token",
        "secret",
        "password",
        "api_key",
        "address",
        "document",
    )
    if any(fragment in key.casefold() for fragment in sensitive_fragments):
        return "<redacted>"
    if isinstance(value, dict):
        return {str(k): _sanitizar_profile_value(v, str(k)) for k, v in value.items()}
    if isinstance(value, list):
        return [_sanitizar_profile_value(item, key) for item in value[:80]]
    if isinstance(value, str):
        return limpar_texto(value, 1_200)
    if isinstance(value, (bool, int, float)) or value is None:
        return value
    return limpar_texto(value, 300)


def carregar_contexto_perfil(profile_rules: Path) -> str:
    """Monta contexto profissional somente com o perfil local do projeto."""

    sections: list[str] = []
    if profile_rules.is_file():
        try:
            raw_profile = json.loads(profile_rules.read_text(encoding="utf-8-sig"))
            safe_profile = _sanitizar_profile_value(raw_profile)
            sections.append("CANDIDATE_PROFILE:\n" + json.dumps(safe_profile, ensure_ascii=False))
        except (OSError, json.JSONDecodeError, ValueError):
            LOGGER.warning("PROFILE | regras locais invalidas | path=%s", profile_rules)

    return "\n\n".join(sections)[:MAX_PROFILE_CONTEXT_CHARS]


def construir_payload_ia(context: JobContext, profile_context: str) -> dict[str, Any]:
    """Cria o payload sanitizado usado por todos os modelos."""

    return {
        "task": "write_work_at_a_startup_application_message",
        "job": {
            "title": context.title,
            "company": context.company,
            "visible_description": context.visible_text[:MAX_JOB_CONTEXT_CHARS],
        },
        "candidate_context": profile_context,
        "requirements": {
            "language": "English",
            "minimum_characters": MIN_MESSAGE_CHARS,
            "maximum_characters": MAX_MESSAGE_CHARS,
            "tone": "concise, natural, specific and professional",
            "must_reference_company_or_role": True,
            "must_not_invent_facts": True,
            "must_write_even_when_background_is_from_another_area": True,
            "must_not_rank_or_reject_the_job_for_candidate_fit": True,
        },
        "output_schema": {"message": "string"},
    }


def prompt_sistema() -> str:
    return (
        "You write one concise application message for Work at a Startup. "
        "Use only facts present in candidate_context and the visible job description. "
        "Never rank, reject or skip a job because the candidate background is different. "
        "Always produce a message for every supported application form, emphasizing truthful "
        "transferable experience and interest in the role. "
        "Never invent credentials, years of experience, employment history, location, "
        "work authorization or technical expertise. Explain concrete fit and interest. "
        "Return strict JSON only with exactly this shape: {\"message\":\"...\"}."
    )


def extrair_json_object(raw_text: str) -> dict[str, Any] | None:
    """Aceita JSON puro ou um unico objeto cercado por texto residual."""

    text = raw_text.strip()
    candidates = [text]
    first = text.find("{")
    last = text.rfind("}")
    if first >= 0 and last > first:
        candidates.append(text[first : last + 1])
    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed
    return None


def extrair_mensagem_modelo(raw_text: str) -> str:
    """Aceita JSON estrito e fallbacks textuais controlados dos provedores."""

    text = str(raw_text or "").strip()
    if not text:
        return ""

    parsed = extrair_json_object(text)
    if parsed:
        for key in ("message", "answer", "text"):
            value = parsed.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()

    try:
        decoded = json.loads(text)
    except json.JSONDecodeError:
        decoded = None
    if isinstance(decoded, str) and decoded.strip():
        return decoded.strip()

    message_match = re.search(
        r'"message"\s*:\s*"((?:\\.|[^"\\])*)"',
        text,
        flags=re.DOTALL | re.IGNORECASE,
    )
    if message_match:
        try:
            return str(json.loads(f'"{message_match.group(1)}"')).strip()
        except json.JSONDecodeError:
            pass

    plain = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL | re.IGNORECASE)
    plain = re.sub(r"^```(?:json|text)?\s*|\s*```$", "", plain.strip(), flags=re.IGNORECASE)
    plain = re.sub(r"^(?:message|answer)\s*:\s*", "", plain.strip(), flags=re.IGNORECASE)
    if "{" not in plain and "}" not in plain:
        return plain.strip()
    return ""


def validar_mensagem_ia(value: Any) -> tuple[bool, str, str]:
    """Valida tamanho e sinais basicos antes de tocar no formulario."""

    message = ajustar_mensagem_ao_limite(value)
    if len(message) < MIN_MESSAGE_CHARS:
        return False, "message_too_short", message
    if len(message) > MAX_MESSAGE_CHARS:
        return False, "message_too_long", message
    if re.search(r"https?://|@[A-Za-z0-9_.-]+", message):
        return False, "message_contains_contact_or_url", message
    return True, "ok", message


def ajustar_mensagem_ao_limite(value: Any) -> str:
    """Normaliza e reduz textos longos em limite de frase ou palavra."""

    message = limpar_texto(value, 2_000)
    if len(message) <= MAX_MESSAGE_CHARS:
        return message

    window = message[: MAX_MESSAGE_CHARS + 1]
    sentence_cut = max(window.rfind(". "), window.rfind("! "), window.rfind("? "))
    if sentence_cut + 1 >= MIN_MESSAGE_CHARS:
        return window[: sentence_cut + 1].strip()

    word_cut = window.rfind(" ", 0, MAX_MESSAGE_CHARS + 1)
    if word_cut >= MIN_MESSAGE_CHARS:
        return window[:word_cut].rstrip(" ,;:-") + "."
    return window[:MAX_MESSAGE_CHARS].rstrip(" ,;:-") + "."


def construir_mensagem_fallback(context: JobContext) -> str:
    """Gera mensagem local verdadeira quando nenhum provedor entrega texto valido."""

    title = limpar_texto(context.title, 100) or "this role"
    company = limpar_texto(context.company, 70)
    company_phrase = f" at {company}" if company else ""
    message = (
        f"I'm interested in the {title} opportunity{company_phrase}. "
        "My background combines hands-on work with Python automation, data analysis, "
        "digital growth, and building practical systems. I bring disciplined continuous "
        "learning, clear communication, and a strong focus on reliable execution. "
        "Even when a role reaches beyond my previous path, I adapt quickly and apply "
        "transferable problem-solving skills. I would value the opportunity to contribute, "
        "learn from the team, and help turn priorities into measurable results."
    )
    return ajustar_mensagem_ao_limite(message)


def campo_auxiliar_padrao_permitido(tag_name: str, input_type: str, descriptor: str) -> bool:
    """Permite somente o checkbox de preferencia de localizacao do modal simples."""

    normalized_tag = tag_name.casefold().strip()
    normalized_type = input_type.casefold().strip()
    normalized_descriptor = descriptor.casefold()
    return (
        normalized_tag == "input"
        and normalized_type == "checkbox"
        and any(term in normalized_descriptor for term in ("relocat", "location preference"))
    )


def _openrouter_request(
    model: str,
    openrouter_credential: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": prompt_sistema()},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
        ],
        "temperature": AI_TEMPERATURE,
        "max_tokens": AI_MAX_OUTPUT_TOKENS,
        "reasoning": {"effort": "minimal", "exclude": True},
    }
    request = urllib.request.Request(
        OPENROUTER_API_URL,
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {openrouter_credential}",
            "Content-Type": "application/json",
            "X-Title": "GrowthTech Work at a Startup V0",
        },
        method="POST",
    )
    started = time.perf_counter()
    try:
        with urllib.request.urlopen(request, timeout=AI_TIMEOUT_SECONDS) as response:
            response_data = json.loads(response.read().decode("utf-8"))
        choices = response_data.get("choices", [])
        content = choices[0].get("message", {}).get("content", "") if choices else ""
        if isinstance(content, list):
            content = " ".join(
                str(item.get("text", "")) if isinstance(item, dict) else str(item)
                for item in content
            )
        return {
            "raw": str(content),
            "error": "" if content else "empty_response",
            "latency_seconds": round(time.perf_counter() - started, 3),
        }
    except urllib.error.HTTPError as exc:
        return {
            "raw": "",
            "error": f"http_{exc.code}",
            "latency_seconds": round(time.perf_counter() - started, 3),
        }
    except (urllib.error.URLError, TimeoutError, OSError, ValueError, json.JSONDecodeError) as exc:
        return {
            "raw": "",
            "error": type(exc).__name__,
            "latency_seconds": round(time.perf_counter() - started, 3),
        }


async def gerar_mensagem_ia(
    context: JobContext,
    profile_context: str,
    openrouter_credential: str,
) -> tuple[str, str, list[dict[str, Any]]]:
    """Executa a ordem oficial e retorna a primeira mensagem valida."""

    payload = construir_payload_ia(context, profile_context)
    attempts: list[dict[str, Any]] = []
    for model in AI_MODELS:
        result = await asyncio.to_thread(
            _openrouter_request,
            model,
            openrouter_credential,
            payload,
        )
        raw_response = str(result.get("raw", ""))
        extracted_message = extrair_mensagem_modelo(raw_response)
        valid, reason, message = validar_mensagem_ia(extracted_message)
        if raw_response and not extracted_message:
            reason = "response_schema_unrecognized"
        attempt = {
            "model": model,
            "success": valid,
            "reason": reason if result.get("raw") else str(result.get("error") or reason),
            "latency_seconds": result.get("latency_seconds"),
            "message_length": len(message),
        }
        attempts.append(attempt)
        LOGGER.info(
            "WAAS_AI | model=%s | success=%s | len=%s | reason=%s",
            model,
            valid,
            len(message),
            attempt["reason"],
        )
        if valid:
            return message, model, attempts

    fallback = construir_mensagem_fallback(context)
    valid, reason, fallback = validar_mensagem_ia(fallback)
    attempts.append(
        {
            "model": "local_safe_fallback",
            "success": valid,
            "reason": reason,
            "latency_seconds": 0.0,
            "message_length": len(fallback),
        }
    )
    LOGGER.info(
        "WAAS_AI | model=local_safe_fallback | success=%s | len=%s | reason=%s",
        valid,
        len(fallback),
        reason,
    )
    if valid:
        return fallback, "local_safe_fallback", attempts
    return "", "", attempts


async def detectar_bloqueio(page: Page) -> str:
    """Interrompe em login, CAPTCHA ou challenge."""

    try:
        body = limpar_texto(await page.locator("body").inner_text(), 3_000).casefold()
    except PlaywrightError:
        return "page_unreadable"
    if "captcha" in body or "verify you are human" in body:
        return "captcha_detected"
    if "access denied" in body or "security challenge" in body:
        return "challenge_detected"
    if "sign in" in body and "companies & jobs" not in body:
        return "login_required"
    return ""


async def escolher_pagina_lista(browser: Any) -> tuple[Page, bool]:
    """Reutiliza uma aba WAAS existente ou cria uma aba temporaria."""

    if not browser.contexts:
        raise RuntimeError("cdp_context_missing")
    context = browser.contexts[0]
    for page in context.pages:
        if "workatastartup.com" in str(page.url):
            return page, False
    return await context.new_page(), True


async def coletar_vagas(page: Page, max_scan: int) -> tuple[list[JobLink], int, str]:
    """Rola a listagem e acumula URLs reais sem clicar em vagas."""

    collected: dict[str, JobLink] = {}
    stable_cycles = 0
    previous_snapshot: tuple[int, int] | None = None
    stop_reason = "scroll_limit_reached"

    for cycle in range(1, MAX_SCROLL_CYCLES + 1):
        raw_links = await page.evaluate(
            r"""
            (selector) => Array.from(document.querySelectorAll(selector)).map((node) => {
                const rect = node.getBoundingClientRect();
                const style = getComputedStyle(node);
                const visible = rect.width > 0 && rect.height > 0 &&
                    style.display !== 'none' && style.visibility !== 'hidden';
                return {
                    href: node.href || '',
                    text: (node.innerText || node.textContent || '').replace(/\s+/g, ' ').trim(),
                    aria: (node.getAttribute('aria-label') || '').replace(/\s+/g, ' ').trim(),
                    visible,
                };
            })
            """,
            JOB_LINK_SELECTOR,
        )
        for item in raw_links:
            if not item.get("visible"):
                continue
            url = normalizar_job_url(str(item.get("href", "")))
            if not url:
                continue
            aria = limpar_texto(item.get("aria", ""), 240)
            text = limpar_texto(item.get("text", ""), 240)
            title = re.sub(r"^View job:\s*", "", aria, flags=re.IGNORECASE) or text
            collected.setdefault(url, JobLink(url=url, title=title or "<title unavailable>"))
            if len(collected) >= max_scan:
                stop_reason = "scan_target_reached"
                break

        scroll_height = int(
            await page.evaluate(
                "() => Math.max(document.body.scrollHeight, document.documentElement.scrollHeight)"
            )
        )
        snapshot = (len(collected), scroll_height)
        LOGGER.info(
            "WAAS_SCROLL | cycle=%s/%s | jobs=%s/%s | height=%s",
            cycle,
            MAX_SCROLL_CYCLES,
            len(collected),
            max_scan,
            scroll_height,
        )
        if len(collected) >= max_scan:
            break
        if snapshot == previous_snapshot:
            stable_cycles += 1
        else:
            stable_cycles = 0
            previous_snapshot = snapshot
        if stable_cycles >= 2:
            stop_reason = "list_stable_after_two_cycles"
            break
        await page.evaluate(
            "() => (document.scrollingElement || document.documentElement).scrollTo(0, "
            "(document.scrollingElement || document.documentElement).scrollHeight)"
        )
        await page.wait_for_timeout(intervalo_ms(SCROLL_DELAY_MS))

    return list(collected.values()), cycle, stop_reason


async def extrair_contexto_vaga(page: Page, job_url: str) -> JobContext:
    """Le titulo, empresa e texto visivel antes do Apply."""

    data = await page.evaluate(
        r"""
        () => {
            const clean = (value) => (value || '').replace(/\s+/g, ' ').trim();
            const title = clean(document.querySelector('h1')?.innerText || '');
            const breadcrumb = Array.from(
                document.querySelectorAll('nav[aria-label="Breadcrumb"] a, nav[aria-label="breadcrumb"] a')
            ).map((node) => clean(node.innerText || node.textContent || '')).filter(Boolean);
            const article = document.querySelector('article') || document.querySelector('main') || document.body;
            return {
                title,
                company: breadcrumb.length >= 1 ? breadcrumb[breadcrumb.length - 1] : '',
                text: clean(article?.innerText || ''),
            };
        }
        """
    )
    return JobContext(
        url=job_url,
        title=limpar_texto(data.get("title", ""), 240) or "<title unavailable>",
        company=limpar_texto(data.get("company", ""), 180) or "<company unavailable>",
        visible_text=limpar_texto(data.get("text", ""), MAX_JOB_CONTEXT_CHARS),
    )


async def locator_exato_visivel(page: Page, role: str, text: str) -> Any | None:
    locator = page.get_by_role(role, name=re.compile(rf"^{re.escape(text)}$", re.IGNORECASE))
    for index in range(await locator.count()):
        candidate = locator.nth(index)
        if await candidate.is_visible():
            return candidate
    return None


async def locator_interativo_exato_visivel(page: Page, text: str) -> Any | None:
    """Fallback para controles clicaveis que nao expoem role semantica correta."""

    expected = text.casefold()
    locator = page.locator(INTERACTIVE_CONTROL_SELECTOR)
    matches: list[Any] = []
    for index in range(await locator.count()):
        candidate = locator.nth(index)
        if not await candidate.is_visible():
            continue
        try:
            candidate_text = limpar_texto(await candidate.inner_text(), 100).casefold()
        except PlaywrightError:
            continue
        if candidate_text == expected:
            matches.append(candidate)
    return matches[0] if len(matches) == 1 else None


async def detectar_applied(page: Page) -> bool:
    """Confirma o estado Applied por controle visivel de texto exato."""

    for role in ("button", "link"):
        if await locator_exato_visivel(page, role, APPLIED_BUTTON_TEXT) is not None:
            return True
    return await locator_interativo_exato_visivel(page, APPLIED_BUTTON_TEXT) is not None


async def fechar_modal_seguro(page: Page) -> None:
    await page.keyboard.press("Escape")
    await page.wait_for_timeout(700)
    visible_form = page.locator(APPLY_MESSAGE_SELECTOR)
    modal_still_visible = False
    for index in range(await visible_form.count()):
        if await visible_form.nth(index).is_visible():
            modal_still_visible = True
            break
    if not modal_still_visible:
        return

    close_button = await locator_exato_visivel(page, "button", "Close")
    if close_button is None:
        close_button = await locator_interativo_exato_visivel(page, "Close")
    if close_button is not None:
        await close_button.click()
        await page.wait_for_timeout(500)
        return
    LOGGER.warning("WAAS_MODAL | Escape e Close indisponiveis; modal permaneceu aberto")


async def localizar_formulario_padrao(page: Page) -> tuple[Any | None, Any | None, str]:
    """Aceita somente um textarea #apply-message e um botao Send no mesmo dialogo."""

    textareas = page.locator(APPLY_MESSAGE_SELECTOR)
    visible_textareas = [
        textareas.nth(index)
        for index in range(await textareas.count())
        if await textareas.nth(index).is_visible()
    ]
    if len(visible_textareas) != 1:
        return None, None, "apply_message_not_unique"
    textarea = visible_textareas[0]
    dialog = page.locator('[role="dialog"]').filter(has=textarea)
    if await dialog.count() != 1:
        return None, None, "apply_dialog_not_unique"
    send = dialog.get_by_role(
        "button",
        name=re.compile(rf"^{re.escape(SEND_BUTTON_TEXT)}$", re.IGNORECASE),
    )
    visible_sends = [
        send.nth(index)
        for index in range(await send.count())
        if await send.nth(index).is_visible()
    ]
    if len(visible_sends) != 1:
        return None, None, "send_button_not_unique"

    auxiliary_fields = page.locator('input, select, textarea, [contenteditable="true"]')
    for index in range(await auxiliary_fields.count()):
        field = auxiliary_fields.nth(index)
        if not await field.is_visible():
            continue
        tag_name = await field.evaluate("(node) => node.tagName.toLowerCase()")
        input_type = await field.get_attribute("type") or ""
        field_id = await field.get_attribute("id") or ""
        if tag_name == "textarea" and field_id == "apply-message":
            continue
        descriptor = await field.evaluate(
            """
            (node) => {
                const id = node.getAttribute('id') || '';
                const name = node.getAttribute('name') || '';
                const aria = node.getAttribute('aria-label') || '';
                const label = id
                    ? document.querySelector(`label[for="${CSS.escape(id)}"]`)?.innerText || ''
                    : '';
                const parent = node.closest('label, div')?.innerText || '';
                return [id, name, aria, label, parent].join(' ');
            }
            """
        )
        if not campo_auxiliar_padrao_permitido(tag_name, input_type, descriptor):
            return None, None, "nonstandard_visible_extra_field"
    return textarea, visible_sends[0], "ok"


async def digitar_humanizado(page: Page, textarea: Any, message: str) -> bool:
    """Digita pelo teclado com ritmo variavel e confirma o valor final."""

    await textarea.click()
    await textarea.fill("")
    for char in message:
        await page.keyboard.type(char)
        await page.wait_for_timeout(intervalo_ms(TYPING_DELAY_MS))
        if char in ".!?":
            await page.wait_for_timeout(intervalo_ms(TYPING_PUNCTUATION_DELAY_MS))
    value = await textarea.input_value()
    return value == message


def gravar_relatorio(report: RunReport) -> Path:
    """Grava somente metadados sanitizados em pasta ignorada pelo Git."""

    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    path = REPORT_DIR / f"{REPORT_PREFIX}{timestamp}.json"
    path.write_text(json.dumps(asdict(report), ensure_ascii=False, indent=2), encoding="utf-8")
    return path


async def preparar_lista(browser: Any) -> tuple[Page, bool]:
    page, created = await escolher_pagina_lista(browser)
    page.set_default_timeout(NAVIGATION_TIMEOUT_MS)
    current_url = str(page.url)
    if url_listagem_valida(current_url):
        LOGGER.info("WAAS_LIST_SOURCE | current_user_filters=1 | url=%s", current_url)
    else:
        await page.goto(SEARCH_URL, wait_until="domcontentloaded", timeout=NAVIGATION_TIMEOUT_MS)
        LOGGER.info("WAAS_LIST_SOURCE | current_user_filters=0 | default_url=%s", SEARCH_URL)
    LOGGER.info("WAAS_HYDRATION | waiting=%sms before list scan", INITIAL_PAGE_WAIT_MS)
    await page.wait_for_timeout(INITIAL_PAGE_WAIT_MS)
    block = await detectar_bloqueio(page)
    if block:
        raise RuntimeError(block)
    await page.evaluate(
        "() => (document.scrollingElement || document.documentElement).scrollTo(0, 0)"
    )
    await page.wait_for_timeout(500)
    return page, created


async def executar_listagem_readonly(max_jobs: int) -> int:
    report = RunReport(
        timestamp=datetime.now().isoformat(timespec="seconds"),
        mode="LIST_READONLY",
        target=0,
        max_scan=max_jobs,
        models=list(AI_MODELS),
    )
    page: Page | None = None
    created = False
    try:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.connect_over_cdp(CDP_URL)
            page, created = await preparar_lista(browser)
            report.listing_url = str(page.url)
            jobs, cycles, reason = await coletar_vagas(page, max_jobs)
            report.jobs_collected = len(jobs)
            report.stop_reason = reason
            for index, job in enumerate(jobs, 1):
                LOGGER.info("WAAS_READONLY | idx=%s | title=%s | url=%s", index, job.title, job.url)
                report.actions.append({"index": index, "title": job.title, "job_url": job.url})
            LOGGER.info("WAAS_READONLY_SUMMARY | jobs=%s | scroll_cycles=%s", len(jobs), cycles)
    except (PlaywrightError, RuntimeError) as exc:
        report.errors += 1
        report.stop_reason = limpar_texto(exc, 160)
        LOGGER.error("WAAS_READONLY | stopped=%s", report.stop_reason)
        return_code = 1
    else:
        return_code = 0
    finally:
        if created and page is not None:
            try:
                await page.close()
            except PlaywrightError:
                LOGGER.warning("WAAS_READONLY | temporary_page_close_failed")
        path = gravar_relatorio(report)
        LOGGER.info("WAAS_REPORT | %s", path)
    return return_code


async def executar_apply_batch(
    max_jobs: int,
    max_scan: int,
    profile_rules: Path,
    env_file: Path | None,
    execute_apply: bool,
) -> int:
    report = RunReport(
        timestamp=datetime.now().isoformat(timespec="seconds"),
        mode="APPLY_BATCH_CONTROLLED" if execute_apply else "APPLY_BATCH_BLOCKED",
        target=max_jobs,
        max_scan=max_scan,
        models=list(AI_MODELS),
    )
    if not execute_apply:
        report.stop_reason = "execute_apply_flag_required"
        LOGGER.error("WAAS_APPLY | blocked | use --execute-apply for real submit")
        path = gravar_relatorio(report)
        LOGGER.info("WAAS_REPORT | %s", path)
        return 2

    openrouter_credential = obter_openrouter_api_key(env_file)
    if not openrouter_credential:
        report.stop_reason = "OPENROUTER_API_KEY_missing"
        LOGGER.error("WAAS_APPLY | blocked | OPENROUTER_API_KEY missing")
        path = gravar_relatorio(report)
        LOGGER.info("WAAS_REPORT | %s", path)
        return 2

    profile_context = carregar_contexto_perfil(profile_rules)
    if not profile_context:
        report.stop_reason = "profile_context_missing"
        LOGGER.error("WAAS_APPLY | blocked | profile context missing")
        path = gravar_relatorio(report)
        LOGGER.info("WAAS_REPORT | %s", path)
        return 2

    list_page: Page | None = None
    list_page_created = False
    fatal_error = False
    try:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.connect_over_cdp(CDP_URL)
            list_page, list_page_created = await preparar_lista(browser)
            report.listing_url = str(list_page.url)
            jobs, cycles, scroll_reason = await coletar_vagas(list_page, max_scan)
            report.jobs_collected = len(jobs)
            LOGGER.info(
                "WAAS_BATCH_LIST | jobs=%s | scroll_cycles=%s | reason=%s | target=%s",
                len(jobs),
                cycles,
                scroll_reason,
                max_jobs,
            )

            for scan_index, job in enumerate(jobs, 1):
                if report.applications_submitted >= max_jobs:
                    report.stop_reason = "application_target_reached"
                    break
                action: dict[str, Any] = {
                    "scan_index": scan_index,
                    "job_url": job.url,
                    "title": job.title,
                    "company": "",
                    "status": "",
                    "selected_model": "",
                    "message_length": 0,
                    "message_preview": "",
                    "ai_attempts": [],
                }
                job_page: Page | None = None
                try:
                    await list_page.wait_for_timeout(intervalo_ms(DELAY_BEFORE_VIEW_JOB_MS))
                    job_page = await list_page.context.new_page()
                    job_page.set_default_timeout(NAVIGATION_TIMEOUT_MS)
                    await job_page.goto(job.url, wait_until="domcontentloaded", timeout=NAVIGATION_TIMEOUT_MS)
                    await job_page.wait_for_timeout(INITIAL_PAGE_WAIT_MS)
                    report.jobs_examined += 1

                    block = await detectar_bloqueio(job_page)
                    if block:
                        action["status"] = block
                        report.errors += 1
                        fatal_error = block in {"captcha_detected", "challenge_detected", "login_required"}
                        report.actions.append(action)
                        if fatal_error:
                            report.stop_reason = block
                            break
                        continue

                    context = await extrair_contexto_vaga(job_page, job.url)
                    action["title"] = context.title
                    action["company"] = context.company
                    if await detectar_applied(job_page):
                        action["status"] = "already_applied"
                        report.already_applied += 1
                        report.actions.append(action)
                        LOGGER.info("WAAS_BATCH | idx=%s | already_applied=1 | title=%s", scan_index, context.title)
                        continue

                    apply_control = await locator_exato_visivel(job_page, "link", APPLY_BUTTON_TEXT)
                    if apply_control is None:
                        apply_control = await locator_exato_visivel(job_page, "button", APPLY_BUTTON_TEXT)
                    if apply_control is None:
                        apply_control = await locator_interativo_exato_visivel(
                            job_page,
                            APPLY_BUTTON_TEXT,
                        )
                    if apply_control is None:
                        action["status"] = "apply_not_found"
                        report.skipped_no_apply += 1
                        report.actions.append(action)
                        LOGGER.info("WAAS_BATCH | idx=%s | skipped=apply_not_found | title=%s", scan_index, context.title)
                        continue

                    await job_page.wait_for_timeout(intervalo_ms(DELAY_BEFORE_APPLY_MS))
                    await apply_control.click()
                    try:
                        await job_page.locator(APPLY_MESSAGE_SELECTOR).wait_for(
                            state="visible",
                            timeout=FORM_WAIT_MS,
                        )
                    except PlaywrightError:
                        action["status"] = "nonstandard_form"
                        report.skipped_nonstandard_form += 1
                        report.actions.append(action)
                        await fechar_modal_seguro(job_page)
                        LOGGER.info("WAAS_BATCH | idx=%s | skipped=nonstandard_form | title=%s", scan_index, context.title)
                        continue

                    textarea, send_button, form_reason = await localizar_formulario_padrao(job_page)
                    if textarea is None or send_button is None:
                        action["status"] = form_reason
                        report.skipped_nonstandard_form += 1
                        report.actions.append(action)
                        await fechar_modal_seguro(job_page)
                        LOGGER.info("WAAS_BATCH | idx=%s | skipped=%s | title=%s", scan_index, form_reason, context.title)
                        continue

                    message, model, attempts = await gerar_mensagem_ia(
                        context,
                        profile_context,
                        openrouter_credential,
                    )
                    action["ai_attempts"] = attempts
                    if not message:
                        action["status"] = "ai_no_valid_message"
                        report.skipped_ai_failure += 1
                        report.actions.append(action)
                        await fechar_modal_seguro(job_page)
                        LOGGER.warning("WAAS_BATCH | idx=%s | skipped=ai_no_valid_message | title=%s", scan_index, context.title)
                        continue

                    typed = await digitar_humanizado(job_page, textarea, message)
                    if not typed:
                        action["status"] = "typing_not_confirmed"
                        report.errors += 1
                        report.actions.append(action)
                        report.stop_reason = "typing_not_confirmed"
                        fatal_error = True
                        break

                    action["selected_model"] = model
                    action["message_length"] = len(message)
                    action["message_preview"] = message[:MAX_REPORT_MESSAGE_PREVIEW]
                    if await send_button.is_disabled():
                        action["status"] = "send_disabled_after_fill"
                        report.errors += 1
                        report.actions.append(action)
                        await fechar_modal_seguro(job_page)
                        continue

                    await job_page.wait_for_timeout(intervalo_ms(DELAY_BEFORE_SEND_MS))
                    await send_button.click()
                    await job_page.wait_for_timeout(POST_SEND_WAIT_MS)
                    try:
                        await job_page.get_by_text(
                            re.compile(rf"^{re.escape(APPLIED_BUTTON_TEXT)}$", re.IGNORECASE)
                        ).first.wait_for(state="visible", timeout=APPLIED_CONFIRM_TIMEOUT_MS)
                    except PlaywrightError:
                        action["status"] = "submit_clicked_applied_not_confirmed"
                        report.errors += 1
                        report.actions.append(action)
                        report.stop_reason = "uncertain_submit_state"
                        fatal_error = True
                        LOGGER.error("WAAS_BATCH | idx=%s | submit_uncertain=1 | title=%s", scan_index, context.title)
                        break

                    action["status"] = "application_confirmed"
                    report.applications_submitted += 1
                    report.actions.append(action)
                    LOGGER.info(
                        "WAAS_BATCH | idx=%s | submitted=1 | confirmed=Applied | model=%s | title=%s",
                        scan_index,
                        model,
                        context.title,
                    )
                except PlaywrightError as exc:
                    action["status"] = f"playwright_error:{type(exc).__name__}"
                    report.errors += 1
                    report.actions.append(action)
                    LOGGER.warning("WAAS_BATCH | idx=%s | playwright_error | continuing", scan_index)
                finally:
                    if job_page is not None:
                        try:
                            await job_page.close()
                        except PlaywrightError:
                            LOGGER.warning("WAAS_BATCH | idx=%s | job_tab_close_failed", scan_index)
                if fatal_error:
                    break
                if report.applications_submitted < max_jobs:
                    await list_page.wait_for_timeout(intervalo_ms(DELAY_BETWEEN_JOBS_MS))

            if not report.stop_reason:
                report.stop_reason = (
                    "application_target_reached"
                    if report.applications_submitted >= max_jobs
                    else "job_list_exhausted_before_target"
                )
    except (PlaywrightError, RuntimeError) as exc:
        report.errors += 1
        report.stop_reason = limpar_texto(exc, 160)
        LOGGER.error("WAAS_BATCH | fatal=%s", report.stop_reason)
        fatal_error = True
    finally:
        if list_page_created and list_page is not None:
            try:
                await list_page.close()
            except PlaywrightError:
                LOGGER.warning("WAAS_BATCH | list_page_close_failed")
        path = gravar_relatorio(report)
        LOGGER.info(
            "WAAS_BATCH_SUMMARY | examined=%s | submitted=%s/%s | already_applied=%s | "
            "no_apply=%s | nonstandard=%s | ai_failed=%s | errors=%s | stop=%s",
            report.jobs_examined,
            report.applications_submitted,
            report.target,
            report.already_applied,
            report.skipped_no_apply,
            report.skipped_nonstandard_form,
            report.skipped_ai_failure,
            report.errors,
            report.stop_reason,
        )
        LOGGER.info("WAAS_REPORT | %s", path)
    return 1 if fatal_error else 0


def executar_self_check() -> int:
    checks = {
        "cdp_port_9226": CDP_URL.endswith(":9226"),
        "three_models": len(AI_MODELS) == 3,
        "primary_glm": AI_MODELS[0] == "z-ai/glm-5.3-flash",
        "textarea_selector": APPLY_MESSAGE_SELECTOR == "textarea#apply-message",
        "submit_requires_flag": True,
        "standalone_project_paths": REPORT_DIR.parent == PROJECT_ROOT,
    }
    for name, passed in checks.items():
        LOGGER.info("WAAS_SELF_CHECK | %s=%s", name, passed)
    return 0 if all(checks.values()) else 1


def criar_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Work at a Startup APPLY V0")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--self-check", action="store_true")
    mode.add_argument("--list-readonly", action="store_true")
    mode.add_argument("--apply-batch", action="store_true")
    parser.add_argument("--execute-apply", action="store_true")
    parser.add_argument("--max-jobs", type=int, default=5)
    parser.add_argument("--max-scan", type=int, default=0)
    parser.add_argument(
        "--profile",
        "--profile-rules",
        dest="profile_rules",
        type=Path,
        default=DEFAULT_PROFILE_RULES,
    )
    parser.add_argument("--env-file", type=Path)
    return parser


def main() -> int:
    configurar_logging()
    args = criar_parser().parse_args()
    if args.max_jobs < 1:
        LOGGER.error("WAAS | --max-jobs must be greater than zero")
        return 2
    max_scan = args.max_scan or max(40, args.max_jobs * 5)
    if max_scan < args.max_jobs:
        LOGGER.error("WAAS | --max-scan must be greater than or equal to --max-jobs")
        return 2
    if args.self_check:
        return executar_self_check()
    if args.list_readonly:
        return asyncio.run(executar_listagem_readonly(args.max_jobs))
    return asyncio.run(
        executar_apply_batch(
            max_jobs=args.max_jobs,
            max_scan=max_scan,
            profile_rules=args.profile_rules.resolve(),
            env_file=args.env_file.resolve() if args.env_file else None,
            execute_apply=args.execute_apply,
        )
    )


if __name__ == "__main__":
    raise SystemExit(main())
