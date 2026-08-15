# -*- coding: utf-8 -*-
"""Run open-weight TCM models locally with vLLM on the TCMClinicalReason-Bench
test set and produce a submission-ready JSON file.

Models (the two open-weight TCM models reported in the README), loaded from
the HuggingFace Hub:
  shizhen -> FreedomIntelligence/ShizhenGPT-7B-LLM
  huatuo  -> FreedomIntelligence/HuatuoGPT-3-8B

Both run with guided JSON decoding against the 7-block schema, so the output
is structured by construction.

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
    # single model
    python inference_vllm.py --model shizhen
    # both models (one subprocess per model so GPU memory is fully released)
    python inference_vllm.py --model all

    # two GPUs in parallel - one process per GPU
    CUDA_VISIBLE_DEVICES=0 python inference_vllm.py --model shizhen &
    CUDA_VISIBLE_DEVICES=1 python inference_vllm.py --model huatuo  &
    wait

If some records come back empty, re-run with --patch to regenerate only the
records whose output is empty (merged into the existing file in place).
"""

import argparse
import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from vllm import LLM, SamplingParams

# The vLLM structured-output API has gone through three generations of
# naming; probe from newest to oldest.
StructuredOutputsParams = None  # type: ignore
GuidedDecodingParams = None  # type: ignore
try:
    from vllm.sampling_params import StructuredOutputsParams  # type: ignore
    _STRUCT_API = "structured_outputs"
except ImportError:
    try:
        from vllm.sampling_params import GuidedDecodingParams  # type: ignore
        _STRUCT_API = "guided_decoding"
    except ImportError:
        try:
            from vllm import GuidedDecodingParams  # type: ignore
            _STRUCT_API = "guided_decoding"
        except ImportError:
            _STRUCT_API = "legacy"


# -------------------------- paths --------------------------

HERE = Path(__file__).resolve().parent
DATASET = str(HERE.parent / "data" / "TCMCR-Reasoning.json")
OUTPUT_DIR = str(HERE / "outputs")


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

# Guided-decoding schema: minLength=1 pushes the model away from empty strings.
TCM_SCHEMA_VLLM: Dict[str, Any] = {
    "type": "object",
    "properties": {k: {"type": "string", "minLength": 1} for k in SECTION_KEYS},
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

_FENCE_RE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.S)
_BRACE_RE = re.compile(r"\{.*\}", re.S)


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


# --------------------- model configs ---------------------

# system_prompt:        injected system prompt (JSON output contract).
# chat_template_kwargs: passed to apply_chat_template (e.g. enable_thinking).
# max_tokens / sampling_kwargs: per-model overrides.
MODEL_CONFIGS: Dict[str, Dict[str, Any]] = {
    # ShizhenGPT-7B-LLM: Qwen2.5-7B-Instruct architecture, guided JSON.
    "shizhen": {
        "model_path": "FreedomIntelligence/ShizhenGPT-7B-LLM",
        "suffix": "_shizhen",
        "system_prompt": SYSTEM_PROMPT_JSON,
        "chat_template_kwargs": {},
        "max_tokens": 4096,
        "llm_kwargs": {},
        "sampling_kwargs": {},
    },
    # HuatuoGPT-3-8B: Qwen3-8B-Base, reasoning model. Thinking is disabled
    # (a leading <think> conflicts with the guided-JSON opening brace); the
    # thinking-mode recommended sampling parameters are kept.
    "huatuo": {
        "model_path": "FreedomIntelligence/HuatuoGPT-3-8B",
        "suffix": "_huatuo",
        "system_prompt": SYSTEM_PROMPT_JSON,
        "chat_template_kwargs": {"enable_thinking": False},
        "max_tokens": 4096,
        "llm_kwargs": {},
        "sampling_kwargs": {
            "temperature": 0.6, "top_p": 0.95, "top_k": 20, "min_p": 0.0,
        },
    },
}


# --------------------- prompt construction ---------------------

def build_prompt_text(tokenizer, record: Dict[str, Any], cfg: Dict[str, Any]) -> str:
    user_msg = build_user_prompt(record)
    tmpl_kwargs = cfg.get("chat_template_kwargs", {}) or {}

    messages = []
    sys_prompt = cfg.get("system_prompt")
    if sys_prompt:
        messages.append({"role": "system", "content": sys_prompt})
    messages.append({"role": "user", "content": user_msg})

    try:
        return tokenizer.apply_chat_template(
            messages, tokenize=False, add_generation_prompt=True, **tmpl_kwargs,
        )
    except TypeError:
        # The tokenizer does not accept kwargs such as enable_thinking.
        return tokenizer.apply_chat_template(
            messages, tokenize=False, add_generation_prompt=True,
        )
    except Exception:
        head = f"{sys_prompt}\n\n" if sys_prompt else ""
        return f"{head}{user_msg}\n\n"


# --------------------- per-record parse ---------------------

def parse_output(raw: str) -> Dict[str, str]:
    """Parse one guided-JSON generation into the 7-key output dict.
    Falls back to best-effort JSON extraction; unrecoverable records come
    back with empty values (re-run them with --patch)."""
    parsed: Any = None
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        parsed = extract_json(raw)
    return normalize_output(parsed)


# --------------------- main run ---------------------

def run(model_key: str, tp: int, max_tokens: int, max_model_len: int,
        patch: bool = False,
        dataset: str = DATASET, output_dir: str = OUTPUT_DIR) -> None:
    cfg = MODEL_CONFIGS[model_key]

    out_path = Path(output_dir) / f"submission{cfg['suffix']}.json"

    print(f"[{model_key}] dataset = {dataset}")
    with open(dataset, "r", encoding="utf-8") as f:
        all_records: List[Dict[str, Any]] = json.load(f)
    print(f"[{model_key}] loaded {len(all_records)} records")

    existing_results: List[Dict[str, Any]] = []
    missing_idxs: List[int] = []
    if patch:
        if not out_path.exists():
            print(f"[{model_key}] patch mode but no file at {out_path}; "
                  f"run without --patch first")
            return
        with open(out_path, "r", encoding="utf-8") as f:
            existing_results = json.load(f)
        if len(existing_results) != len(all_records):
            print(f"[{model_key}] patch: size mismatch "
                  f"({len(existing_results)} vs {len(all_records)}); aborting")
            return
        missing_idxs = [i for i, r in enumerate(existing_results)
                        if output_is_empty(r.get("output"))]
        if not missing_idxs:
            print(f"[{model_key}] patch: nothing missing in {out_path.name}")
            return
        print(f"[{model_key}] patch: re-running {len(missing_idxs)} record(s): "
              f"{missing_idxs}")
        records_to_process = [all_records[i] for i in missing_idxs]
    else:
        records_to_process = all_records

    print(f"[{model_key}] loading model: {cfg['model_path']}")
    llm_kwargs = cfg.get("llm_kwargs", {}) or {}
    effective_max_model_len = cfg.get("max_model_len") or max_model_len
    llm = LLM(
        model=cfg["model_path"],
        tensor_parallel_size=tp,
        trust_remote_code=True,
        dtype="bfloat16",
        gpu_memory_utilization=0.9,
        enable_prefix_caching=True,
        max_model_len=effective_max_model_len,
        **llm_kwargs,
    )
    tokenizer = llm.get_tokenizer()

    prompts = [build_prompt_text(tokenizer, r, cfg) for r in records_to_process]

    effective_max_tokens = cfg.get("max_tokens") or max_tokens
    sp_kwargs: Dict[str, Any] = dict(
        temperature=0.0, top_p=1.0, max_tokens=effective_max_tokens,
    )
    sp_kwargs.update(cfg.get("sampling_kwargs", {}) or {})

    schema = TCM_SCHEMA_VLLM
    if _STRUCT_API == "structured_outputs":
        sp_kwargs["structured_outputs"] = StructuredOutputsParams(json=schema)
    elif _STRUCT_API == "guided_decoding":
        sp_kwargs["guided_decoding"] = GuidedDecodingParams(json=schema)
    else:
        sp_kwargs["guided_json"] = schema
    sampling = SamplingParams(**sp_kwargs)

    sampling_log = {k: v for k, v in sp_kwargs.items()
                    if k not in ("max_tokens", "structured_outputs",
                                 "guided_decoding", "guided_json")}
    print(f"[{model_key}] generating (struct_api={_STRUCT_API}, "
          f"max_tokens={effective_max_tokens}, sampling={sampling_log})...")
    outputs = llm.generate(prompts, sampling)

    results: List[Dict[str, Any]] = []
    n_ok = n_fail = 0
    for record, out in zip(records_to_process, outputs):
        raw = out.outputs[0].text
        output = parse_output(raw)
        if output_is_empty(output):
            n_fail += 1
        else:
            n_ok += 1
        results.append({"id": record["id"], "output": output})

    if patch:
        for idx, new_result in zip(missing_idxs, results):
            existing_results[idx] = new_result
        results_to_write = existing_results
        still_missing = [i for i in missing_idxs
                         if output_is_empty(existing_results[i].get("output"))]
        print(f"[{model_key}] patch: this run ok={n_ok} fail={n_fail}")
        print(f"[{model_key}] patch: still missing after merge: {still_missing}")
    else:
        results_to_write = results
        print(f"[{model_key}] ok={n_ok}  fail={n_fail}")
        if n_fail:
            print(f"[{model_key}] re-run with --patch to regenerate the "
                  f"empty record(s)")

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(results_to_write, f, ensure_ascii=False, indent=2)
    print(f"[{model_key}] wrote -> {out_path}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", choices=list(MODEL_CONFIGS.keys()) + ["all"],
                        required=True,
                        help="which model to run; 'all' spawns one subprocess per model")
    parser.add_argument("--tp", type=int, default=1, help="tensor parallel size")
    parser.add_argument("--max-tokens", type=int, default=4096,
                        help="max new tokens (per-model override in MODEL_CONFIGS)")
    parser.add_argument("--max-model-len", type=int, default=8192,
                        help="vLLM max_model_len (context window)")
    parser.add_argument("--patch", action="store_true",
                        help="re-run only the records whose output is empty in the "
                             "existing output file (merged back in place)")
    parser.add_argument("--dataset", default=DATASET,
                        help="input dataset JSON (default: data/TCMCR-Reasoning.json)")
    parser.add_argument("--output-dir", default=OUTPUT_DIR, dest="output_dir",
                        help="output directory (default: code/outputs/)")
    args = parser.parse_args()

    if args.model == "all":
        # One subprocess per model: process exit forces the OS to reclaim GPU
        # memory, avoiding OOM when loading models back to back.
        import subprocess
        import sys

        log_dir = Path("inference_logs")
        log_dir.mkdir(parents=True, exist_ok=True)

        failed: List[str] = []
        for k in MODEL_CONFIGS:
            log_path = log_dir / f"{k}.log"
            print(f"\n{'=' * 60}\n=== running model: {k}\n=== log -> {log_path}\n"
                  f"=== (real-time: `tail -f {log_path}`)\n{'=' * 60}", flush=True)
            cmd = [
                sys.executable, __file__,
                "--model", k,
                "--tp", str(args.tp),
                "--max-tokens", str(args.max_tokens),
                "--max-model-len", str(args.max_model_len),
                "--dataset", args.dataset,
                "--output-dir", args.output_dir,
            ]
            if args.patch:
                cmd.append("--patch")
            with open(log_path, "w", encoding="utf-8", buffering=1) as logf:
                ret = subprocess.run(cmd, stdout=logf, stderr=subprocess.STDOUT)

            if ret.returncode != 0:
                print(f"!!! [{k}] FAILED (exit={ret.returncode}). Last 80 lines:")
                with open(log_path, "r", encoding="utf-8", errors="replace") as f:
                    print("".join(f.readlines()[-80:]))
                failed.append(k)
            else:
                print(f"[{k}] done.")

        if failed:
            print(f"\n=== FAILED models: {failed}")
            sys.exit(1)
        print("\n=== ALL DONE")
    else:
        run(args.model, args.tp, args.max_tokens, args.max_model_len,
            patch=args.patch, dataset=args.dataset, output_dir=args.output_dir)


if __name__ == "__main__":
    main()
