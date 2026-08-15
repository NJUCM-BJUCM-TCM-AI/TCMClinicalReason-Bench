# -*- coding: utf-8 -*-
"""Run API models on the TCMClinicalReason-Bench test set and produce a
submission-ready JSON file.

This is the script used for the five API models reported in the README,
routed to two kinds of backends:

  OpenRouter (env OPENROUTER_API_KEY, response_format=json_schema strict):
    gpt      -> openai/gpt-5.5
    claude   -> anthropic/claude-opus-4.8
    gemini   -> google/gemini-3.1-pro-preview
  Official APIs (response_format=json_object):
    deepseek -> deepseek-v4-pro @ api.deepseek.com  (env DEEPSEEK_API_KEY),
                thinking mode; the chain of thought is taken from
                message.reasoning_content.
    qwen     -> qwen3.7-max @ DashScope compatible-mode (env DASHSCOPE_API_KEY),
                non-thinking (structured output and thinking are mutually
                exclusive); the chain of thought is taken from the JSON field.

Input:  the released test set ``data/TCMCR-Reasoning.json``
        (records of {id, instruction, input}).
Output: ``<output-dir>/submission_<model>.json`` in the submission format
        required by the repository README:

    [{"id": "TCMCR-0001",
      "output": {"思维链": "...", "病因病机分析": "...", "证候诊断": "...",
                 "治法": "...", "处方": "...", "方解": "...",
                 "症状变化与中药加减": "..."}},
     ...]

Usage:
    python inference_api.py --model gpt
    python inference_api.py --model all
    python inference_api.py --model deepseek --limit 5

Interrupted runs resume automatically: records whose output is already
non-empty in the existing output file are kept (matched by id), and only
missing or failed records are re-run. Use --overwrite to start from scratch.
"""

import argparse
import json
import os
import re
import sys
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import openai


# On Windows the default stdout/stderr encoding may not handle Chinese text.
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass


# -------------------------- paths --------------------------

HERE = Path(__file__).resolve().parent
DATASET = HERE.parent / "data" / "TCMCR-Reasoning.json"
OUT_DIR = HERE / "outputs"

OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
# Optional attribution headers for OpenRouter; they do not affect behavior.
OPENROUTER_HEADERS = {
    "HTTP-Referer": "https://github.com/NJUCM-BJUCM-TCM-AI/TCMClinicalReason-Bench",
    "X-Title": "TCMClinicalReason-Bench",
}


# -------------------------- output contract --------------------------

# The seven reasoning blocks, fixed order, chain of thought first. The key
# names are part of the benchmark contract and must not be translated or
# altered; they must match the submission format in the repository README
# byte for byte.
COT_KEY = "思维链"
ANSWER_KEYS: List[str] = [
    "病因病机分析", "证候诊断", "治法", "处方", "方解", "症状变化与中药加减",
]
SECTION_KEYS: List[str] = [COT_KEY] + ANSWER_KEYS

# Strict-mode JSON schema (no minLength: strict mode does not allow it).
TCM_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {k: {"type": "string"} for k in SECTION_KEYS},
    "required": SECTION_KEYS,
    "additionalProperties": False,
}

# Per-block guidance shared by the system prompt (kept in Chinese: the cases
# and the required output are Chinese).
_SECTION_GUIDE = (
    "1.「思维链」: 你的完整推理过程。结合患者主诉、症状、舌象脉象、病史等, 逐步推演"
    "病因病机、证候、治法及遣方思路 (先想后答, 此处写思考过程)。\n"
    "2.「病因病机分析」: 分析疾病的病因与病机演变, 说明邪正盛衰、脏腑气血津液等病理变化。\n"
    "3.「证候诊断」: 给出明确的中医证候名称 (证型), 必要时标明主证 / 兼证。\n"
    "4.「治法」: 基于证候诊断给出治则治法, 主辅分明。\n"
    "5.「处方」: 给出具体方药 (方名 / 药物及剂量), 紧扣治法。\n"
    "6.「方解」: 逐组分析处方配伍, 说明君臣佐使及各药 (组) 的作用及其对应的证候病机。\n"
    "7.「症状变化与中药加减」: 针对可能出现的症状变化, 给出随症加减的思路与药物调整。\n"
)

SYSTEM_PROMPT_JSON = (
    "你是一位资深中医师, 精通中医辨证论治。请根据用户提供的患者病例, 进行完整、严谨的"
    "中医临床分析, 并严格以结构化 JSON 输出。\n"
    "输出必须是一个 JSON 对象, 严格且仅包含以下 7 个字段, 缺一不可, 顺序固定:\n"
    + _SECTION_GUIDE +
    "要求: 紧扣患者实际症状、舌脉与病史, 辨证准确、方证相应、推理具体, 避免泛泛而谈。"
    "所有字段的值均为非空字符串。"
)


def build_user_prompt(record: Dict[str, Any]) -> str:
    """User prompt = instruction (first) + case text, both from the dataset."""
    instruction = (record.get("instruction") or "").strip()
    input_text = (record.get("input") or "").strip()
    if not input_text:
        return instruction
    if not instruction:
        return input_text
    return f"{instruction}\n\n{input_text}"


def _s(v: Any) -> str:
    return v.strip() if isinstance(v, str) else ""


def normalize_output(parsed: Any) -> Dict[str, str]:
    """Normalize a parsed model response into a complete 7-key output dict
    (missing keys become empty strings)."""
    if not isinstance(parsed, dict):
        parsed = {}
    return {k: _s(parsed.get(k)) for k in SECTION_KEYS}


def output_is_empty(output: Any) -> bool:
    """True if none of the six answer blocks has content (the record needs a
    re-run; the chain of thought alone does not count as an answer)."""
    if not isinstance(output, dict):
        return True
    return all(not _s(output.get(k)) for k in ANSWER_KEYS)


# -------------------------- model registry --------------------------

# Per-key fields:
#   backend        : "openrouter" (default) | "official"; selects base_url,
#                    api key env var, and the call function.
#   model          : OpenRouter model slug, or the official model name.
#   base_url/api_key_env : endpoint and env var for the official backend.
#   provider       : OpenRouter provider routing. require_parameters prefers
#                    providers that support strict json_schema.
#   reasoning_effort / extra_body : thinking/structured-output switches for
#                    the official backends.
#   cot_from_reasoning : if True, message.reasoning_content becomes the
#                    chain-of-thought block (deepseek thinking mode).
MODELS: Dict[str, Dict[str, Any]] = {
    "gpt": {
        "backend": "openrouter",
        "model": "openai/gpt-5.5",
        "provider": {"require_parameters": True},
    },
    "claude": {
        "backend": "openrouter",
        "model": "anthropic/claude-opus-4.8",
        # The google-vertex endpoint has structured_outputs=False and does not
        # support strict json_schema, so pin the anthropic provider instead.
        "provider": {"order": ["anthropic"], "allow_fallbacks": False},
    },
    "gemini": {
        "backend": "openrouter",
        "model": "google/gemini-3.1-pro-preview",
        "provider": {"require_parameters": True},
    },
    "qwen": {
        # DashScope OpenAI-compatible endpoint. Structured output (json_object)
        # and thinking mode are mutually exclusive, so enable_thinking=False;
        # the chain of thought is produced as a JSON field instead.
        "backend": "official",
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "api_key_env": "DASHSCOPE_API_KEY",
        "model": "qwen3.7-max",
        "extra_body": {"enable_thinking": False},
        "cot_from_reasoning": False,
        "max_tokens": 4592,  # long single-case outputs; raised to avoid truncation
    },
    "deepseek": {
        # DeepSeek official API, thinking mode (reasoning_effort=high); the
        # chain of thought comes from message.reasoning_content, the JSON
        # carries the six answer blocks.
        "backend": "official",
        "base_url": "https://api.deepseek.com",
        "api_key_env": "DEEPSEEK_API_KEY",
        "model": "deepseek-v4-pro",
        "reasoning_effort": "high",
        "extra_body": {"thinking": {"type": "enabled"}},
        "cot_from_reasoning": True,
        "max_tokens": 8192,  # thinking output is long; raised to avoid truncation
    },
}


# -------------------------- JSON extraction --------------------------

_FENCE_RE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.S)
_BRACE_RE = re.compile(r"\{.*\}", re.S)


def extract_json(content: str) -> Optional[Dict[str, Any]]:
    """Best-effort JSON extraction: direct parse -> strip ``` fences ->
    outermost {...} span."""
    content = (content or "").strip()
    if not content:
        return None
    for candidate in (content, *(m.group(1) for m in _FENCE_RE.finditer(content)),
                      *(m.group(0) for m in _BRACE_RE.finditer(content))):
        try:
            obj = json.loads(candidate)
            if isinstance(obj, dict):
                return obj
        except (json.JSONDecodeError, TypeError):
            continue
    return None


# -------------------------- API calls --------------------------

def call_openrouter(client: openai.OpenAI, record: Dict[str, Any],
                    cfg: Dict[str, Any], max_tokens: int,
                    ) -> Tuple[Dict[str, str], Dict[str, Any]]:
    """One OpenRouter call; returns (output 7-key dict, meta)."""
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT_JSON},
        {"role": "user", "content": build_user_prompt(record)},
    ]

    extra_body: Dict[str, Any] = {"reasoning": {"effort": "low"}}
    if cfg.get("provider"):
        extra_body["provider"] = cfg["provider"]

    resp = client.chat.completions.create(
        model=cfg["model"],
        messages=messages,
        max_tokens=max_tokens,
        response_format={
            "type": "json_schema",
            "json_schema": {
                "name": "tcm_analysis",
                "strict": True,
                "schema": TCM_SCHEMA,
            },
        },
        extra_body=extra_body,
        extra_headers=OPENROUTER_HEADERS,
    )

    content = (resp.choices[0].message.content or "")
    parsed = extract_json(content)
    output = normalize_output(parsed)

    # Empty-answer guard: raise so process_one retries.
    if output_is_empty(output):
        raise ValueError("empty answer blocks")

    meta = {
        "finish_reason": getattr(resp.choices[0], "finish_reason", None),
        "provider": getattr(resp, "provider", None),
        "json_ok": isinstance(parsed, dict),
    }
    return output, meta


def call_official(client: openai.OpenAI, record: Dict[str, Any],
                  cfg: Dict[str, Any], max_tokens: int,
                  ) -> Tuple[Dict[str, str], Dict[str, Any]]:
    """One official-API call (deepseek / qwen) with json_object output.

    Same prompt contract as call_openrouter; differences are carried by cfg:
        reasoning_effort -> top-level kwarg (deepseek thinking mode);
        extra_body       -> deepseek thinking / qwen enable_thinking;
        cot_from_reasoning=True -> message.reasoning_content becomes the
        chain-of-thought block (deepseek).
    """
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT_JSON},
        {"role": "user", "content": build_user_prompt(record)},
    ]

    kwargs: Dict[str, Any] = {
        "model": cfg["model"],
        "messages": messages,
        "max_tokens": max_tokens,
        "response_format": {"type": "json_object"},
    }
    if cfg.get("reasoning_effort"):
        kwargs["reasoning_effort"] = cfg["reasoning_effort"]
    if cfg.get("extra_body"):
        kwargs["extra_body"] = cfg["extra_body"]

    resp = client.chat.completions.create(**kwargs)

    msg = resp.choices[0].message
    content = msg.content or ""
    reasoning = (getattr(msg, "reasoning_content", None) or "")
    parsed = extract_json(content)
    output = normalize_output(parsed)

    # Thinking models (deepseek): use the real reasoning_content as the
    # chain-of-thought block; keep the JSON field if reasoning is empty.
    if cfg.get("cot_from_reasoning") and reasoning.strip():
        output[COT_KEY] = reasoning.strip()

    # Empty-answer guard: the official APIs occasionally return empty content
    # in JSON mode; raise so process_one retries.
    if output_is_empty(output):
        raise ValueError("empty answer blocks")

    meta = {
        "finish_reason": getattr(resp.choices[0], "finish_reason", None),
        "provider": cfg["model"],
        "json_ok": isinstance(parsed, dict),
    }
    return output, meta


# -------------------------- record processing --------------------------

def process_one(idx: int, record: Dict[str, Any], call_fn, max_retries: int = 3,
                ) -> Tuple[int, Dict[str, Any], Optional[str], Dict[str, Any]]:
    """Run one record with exponential-backoff retries.
    Returns (idx, submission_record, error_msg, meta)."""
    last_err: Optional[str] = None
    for attempt in range(max_retries):
        try:
            output, meta = call_fn(record)
            return idx, {"id": record["id"], "output": output}, None, meta
        except Exception as e:  # noqa: BLE001 - retry any transient failure
            last_err = f"{type(e).__name__}: {e}"
            if attempt < max_retries - 1:
                time.sleep(2 ** attempt)
    # All attempts failed: keep the id with an empty output so the file stays
    # complete; a resumed run will retry it.
    return idx, {"id": record["id"], "output": normalize_output({})}, last_err, {}


# -------------------------- main runner --------------------------

def _load_done(out_path: Path) -> Dict[str, Dict[str, Any]]:
    """Read already-completed records (non-empty output) from an existing
    output file -> {id: submission_record}."""
    if not out_path.exists():
        return {}
    try:
        existing = json.loads(out_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    done: Dict[str, Dict[str, Any]] = {}
    for r in existing:
        if isinstance(r, dict) and r.get("id") and not output_is_empty(r.get("output")):
            done[r["id"]] = {"id": r["id"], "output": normalize_output(r["output"])}
    return done


def _write_results(out_path: Path, results: List[Optional[Dict[str, Any]]]) -> None:
    """Atomically write current results (skipping not-yet-finished None slots):
    write to .tmp first, then os.replace."""
    final = [r for r in results if r is not None]
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = Path(str(out_path) + ".tmp")
    tmp.write_text(json.dumps(final, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(out_path)


def run(model_key: str, records: List[Dict[str, Any]], out_path: Path,
        max_tokens: int, workers: int, resume: bool = True,
        ckpt_every: int = 50) -> None:
    """Run one model over the dataset and write the submission file.

    resume (default True): records with a non-empty output in the existing
    file are reused (matched by id) and only the rest are run; --overwrite
    disables this. ckpt_every: atomically flush every N completed records.
    """
    cfg = MODELS[model_key]
    backend = cfg.get("backend", "openrouter")
    eff_max_tokens = cfg.get("max_tokens") or max_tokens
    client = openai.OpenAI(
        base_url=cfg.get("base_url", OPENROUTER_BASE_URL),
        api_key=os.environ[cfg.get("api_key_env", "OPENROUTER_API_KEY")],
        max_retries=4,  # let the SDK absorb 429/5xx first; process_one retries on top
    )

    done_map = _load_done(out_path) if resume else {}
    results: List[Optional[Dict[str, Any]]] = [None] * len(records)
    todo: List[int] = []
    for i, rec in enumerate(records):
        hit = done_map.get(rec["id"])
        if hit is not None:
            results[i] = hit
        else:
            todo.append(i)
    n_reused = len(records) - len(todo)

    print(f"[{model_key}] model={cfg['model']}  provider={cfg.get('provider')}  "
          f"max_tokens={eff_max_tokens}  records={len(records)}  "
          f"reused={n_reused}  todo={len(todo)}  workers={workers}")

    if not todo:
        _write_results(out_path, results)
        print(f"[{model_key}] all {len(records)} records already done. "
              f"wrote -> {out_path}")
        return

    call_fn = call_openrouter if backend == "openrouter" else call_official

    def call(rec):
        return call_fn(client, rec, cfg, eff_max_tokens)

    errors: List[Tuple[int, str]] = []
    metas: List[Dict[str, Any]] = []
    n_done = 0
    last_ckpt = 0
    t0 = time.time()

    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(process_one, i, records[i], call): i for i in todo}
        for fut in as_completed(futs):
            idx, result, err, meta = fut.result()
            results[idx] = result
            if meta:
                metas.append(meta)
            n_done += 1
            if err:
                errors.append((idx, err))
                print(f"[{model_key}] ({n_done}/{len(todo)}) idx={idx} FAIL: {err}")
            elif n_done % 20 == 0 or n_done == len(todo):
                rate = n_done / max(1e-9, time.time() - t0)
                eta = (len(todo) - n_done) / rate if rate else 0.0
                print(f"[{model_key}] ({n_done}/{len(todo)}) idx={idx} ok  "
                      f"({rate:.2f}/s, ETA {eta / 60:.1f}min)")
            if n_done - last_ckpt >= ckpt_every:
                _write_results(out_path, results)
                last_ckpt = n_done
                print(f"[{model_key}] checkpoint -> {out_path.name} "
                      f"({n_done}/{len(todo)} done)")

    _write_results(out_path, results)
    n_json = sum(1 for m in metas if m.get("json_ok"))
    n_trunc = sum(1 for m in metas if m.get("finish_reason") == "length")
    providers: Counter = Counter(m["provider"] for m in metas if m.get("provider"))
    print(f"[{model_key}] wrote -> {out_path}")
    print(f"[{model_key}] responses={len(metas)}  json_ok={n_json}/{len(metas)}  "
          f"truncated(finish=length)={n_trunc}  errors={len(errors)}")
    if providers:
        prov_str = ", ".join(f"{k}x{v}" for k, v in providers.most_common())
        print(f"[{model_key}] providers: {prov_str}")
    for idx, err in errors[:20]:
        print(f"  - idx={idx}: {err}")
    if len(errors) > 20:
        print(f"  ... and {len(errors) - 20} more errors")
    if errors:
        print(f"[{model_key}] re-run the same command to retry the "
              f"{len(errors)} failed record(s) (resume keeps completed ones).")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", choices=list(MODELS.keys()) + ["all"],
                        required=True)
    parser.add_argument("--max-tokens", type=int, default=4096,
                        help="max output tokens per response "
                             "(per-model override in MODELS)")
    parser.add_argument("--workers", type=int, default=8,
                        help="concurrent in-flight requests")
    parser.add_argument("--limit", type=int, default=None,
                        help="debug: only run the first N records")
    parser.add_argument("--overwrite", action="store_true",
                        help="ignore any existing output and re-run everything "
                             "(default: resume, re-running only missing/failed records)")
    parser.add_argument("--ckpt-every", type=int, default=50,
                        help="atomically flush the output file every N completed records")
    parser.add_argument("--dataset", default=str(DATASET),
                        help="input dataset JSON (default: data/TCMCR-Reasoning.json)")
    parser.add_argument("--output-dir", default=str(OUT_DIR),
                        help="output directory (default: code/outputs/)")
    args = parser.parse_args()

    _p = Path(args.dataset)
    dataset = _p if _p.is_absolute() else (Path.cwd() / _p).resolve()
    _p = Path(args.output_dir)
    out_dir = _p if _p.is_absolute() else (Path.cwd() / _p).resolve()

    keys = list(MODELS.keys()) if args.model == "all" else [args.model]
    needed_envs = {MODELS[k].get("api_key_env", "OPENROUTER_API_KEY") for k in keys}
    missing = sorted(e for e in needed_envs if e not in os.environ)
    if missing:
        sys.exit(f"error: missing environment variable(s) {missing} "
                 f"(required by model(s) {keys})")

    with open(dataset, "r", encoding="utf-8") as f:
        records: List[Dict[str, Any]] = json.load(f)
    if not all(isinstance(r, dict) and r.get("id") for r in records):
        sys.exit("error: every dataset record must have an 'id' field "
                 "(use the released data/TCMCR-Reasoning.json)")
    if args.limit:
        records = records[: args.limit]
    print(f"loaded {len(records)} records from {dataset.name}")

    for key in keys:
        out_path = out_dir / f"submission_{key}.json"
        run(key, records, out_path, args.max_tokens, args.workers,
            resume=not args.overwrite, ckpt_every=args.ckpt_every)


if __name__ == "__main__":
    main()
