#!/usr/bin/env bash
# run.sh - the vLLM command line, kept in a script so the JSON arguments do not
# have to survive systemd's quoting rules. The unit loads ~/vllm/vllm.env into
# the environment first; every knob below comes from there.
#
# Thinking is DISABLED server-side: Qwen3.x thinks by default and would burn
# the whole max_tokens budget on chain-of-thought while the app expects plain
# JSON. --reasoning-parser is only a safety net that strips any stray <think>
# block out of `content`. Prefix caching pays off on the app's long fixed
# system prompts (12x TTFT measured on box A). The key is enforced from
# VLLM_API_KEY (environment), never on the command line.
set -euo pipefail
: "${HF_MODEL:?}" "${SERVED_NAME:?}" "${VLLM_API_KEY:?}"
# shellcheck disable=SC2086  # QUANT_ARGS is intentionally word-split
exec "$HOME/vllm/venv/bin/vllm" serve "$HF_MODEL" \
  --served-model-name "$SERVED_NAME" \
  --host 127.0.0.1 --port 8000 \
  ${QUANT_ARGS:-} \
  --max-model-len "${MAX_MODEL_LEN:-65536}" \
  --gpu-memory-utilization "${GPU_UTIL:-0.90}" \
  --max-num-seqs "${MAX_NUM_SEQS:-16}" \
  --enable-prefix-caching \
  --limit-mm-per-prompt "{\"image\": ${IMAGES_PER_PROMPT:-16}, \"video\": 0}" \
  --default-chat-template-kwargs '{"enable_thinking": false}' \
  --reasoning-parser "${REASONING_PARSER:-qwen3}" \
  ${EXTRA_ARGS:-}
