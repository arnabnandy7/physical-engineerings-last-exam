# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Plan-work-verify harness running three specialized agent roles per task.

1. The planner inspects the task's files and writes a plan describing each
   deliverable, its path and format, and how to verify it.
2. The worker executes the task using the plan; its answer becomes the final
   message.
3. The verifier starts with a fresh conversation containing only the task, the
   plan, and the worker's summary. It inspects the deliverables and replies
   `PASS` or lists specific gaps, which are sent back to the worker for up to
   `MAX_FIX_ROUNDS` rounds while sufficient time remains.

The planner and verifier receive only `bash` and `read_file` and are instructed
not to modify any files.
"""

import asyncio
import json
import os
import re
import secrets

import openai
import taskbridge

MODEL = os.environ["MODEL_NAME"]
# The model's highest reasoning level: max for Claude, GLM, GPT-6 and
# DeepSeek-V4, high for the others (Gemini refuses max). REASONING_EFFORT
# overrides it.
MAX_EFFORT_MODELS = ("claude", "glm", "gpt-6", "deepseek-v4")
EFFORT = os.environ.get("REASONING_EFFORT") or (
    "max" if any(m in MODEL.lower() for m in MAX_EFFORT_MODELS) else "high"
)
# a fix round wraps up this long before the time limit, and a wrap-up call may
# take as long
WRAP_UP_SEC = 240
WRAP_UP = {
    "role": "user",
    "content": "Time is up for this step. Stop and reply with your answer.",
}
RESULT_CHARS = 20_000  # a longer tool result loses its middle
# Seconds between retries once the SDK's own quick retries are exhausted.
RETRY_WAITS = (5, 15, 30, 60, 120)
# How endpoints word a 400 saying the conversation no longer fits the context
# window: Claude, OpenAI and vLLM, Gemini, xAI.
CONTEXT_FULL = re.compile(
    r"context.?(length|window)|prompt is too long|maximum prompt length"
    r"|exceed[^.]{0,40}(token|context)",
    re.I,
)
CUT_KEEP = 1000  # characters a tool result keeps once the context is full
# times in a row an empty or cut-off reply gets a "continue" before the run
# stops
MAX_NUDGES = 3
# lower it for a small context window
MAX_OUTPUT_TOKENS = int(os.environ.get("MAX_OUTPUT_TOKENS", "32000"))
# Claude caches a prompt prefix only when marked with cache_control. Each call
# marks the system prompt and the latest message (using 2 of Claude's 4 cache
# breakpoints) so each turn reads the prior conversation from cache. Other
# providers cache automatically without markers.
CACHE_MARK = "claude" in MODEL.lower()
# the planner wraps up once it has used this share of the task's time
PLAN_SHARE = 0.1
# the worker wraps up when this share is left, to keep time for checking and
# fixing
CHECK_SHARE = 0.25
# a check or a fix round starts only with this long to work before its own
# wrap-up
MIN_STEP_SEC = 120
MAX_FIX_ROUNDS = 2

# The evaluator sets OPENAI_BASE_URL and OPENAI_API_KEY for every model.
# The SDK handles quick retries; recover() handles longer backoffs. Because
# calls are non-streaming, a max-effort response can take over 15 minutes
# before any bytes arrive, so the timeout matches the egress proxy's 30-minute
# idle limit.
client = openai.AsyncOpenAI(max_retries=5, timeout=1800)

PLANNER = """You plan a real-world physical engineering task that another engineer will carry out in a Linux container with no internet access. \
Your tools are:
- bash: Execute shell commands (e.g. `ls -la`, `cat`, `grep`, `find .`, `python3`). Note: `read_file` is NOT a bash command; it is a separate tool.
- read_file: Read file contents directly by path.

PROTOCOL:
1. ALWAYS start by listing files in the working directory using `ls -la` or `find . -maxdepth 2` to discover existing files and folders. NEVER assume or hallucinate file names.
2. Carefully inspect the problem specification files (e.g. Task.md, instruction.md, agent_view.json, project documents).
3. Identify active standards vs superseded/cancelled codes (e.g. look for Rev2, latest addenda, and ignore CANCELLED or SUPERSEDED files).
4. Do not create or change any file.
5. Formulate a structured, step-by-step plan:
   - Each required deliverable with its exact path and format.
   - What each deliverable must contain.
   - Governing equations, physical constraints, and design codes to use.
   - Exact Python scripts the worker should write using `write_file` or execute inline to compute numerical values or build deliverables. DO NOT suggest running non-existent generator scripts.
   - Specific verification criteria the verifier must check on disk before sign-off."""

WORKER = """You are an expert licensed professional engineer carrying out a task in a Linux container with no internet access. Your bash tool starts in {workdir}. \
A plan for the task is provided below; follow it, and adapt if directory inspection reveals specific project structures.

CRITICAL RULES:
1. NO PRE-EXISTING HELPER SCRIPTS: There are NO scripts like `create_*.py`, `convert_*.py`, `process_*.py` installed in the workspace. NEVER attempt to run a python script unless you verify it exists with `ls` or you just created it using `write_file`. To perform calculations or generate files, either write your own script using `write_file` or execute inline Python with `python3 -c "..."`.
2. YOU MUST WRITE THE DELIVERABLES: Do not output markdown outlines, future plans, or JSON blocks explaining what to do. You must call tools directly (`write_file` or `bash`) to write the actual deliverables to disk.
3. Template Preservation: If a deliverable file or skeleton template already exists on disk (e.g. in `submission/` or `output/`), inspect it (`cat`, `head`, or `read_file`) and build upon it. NEVER overwrite a deliverable with an empty file (`""`), a dummy comment, or incorrect format.
4. Physical Deliverable Creation: Ensure all required deliverable files are written to their EXACT declared paths and file formats (e.g. `.ifc`, `.zip`, `.pdf`, `.py`, `.json`). If the deliverable path is under a project subfolder (e.g. `/filesystem/<project_folder>/output/submission.zip`), write it to that exact full path or inside `<project_folder>/output/`. For zip files, ALWAYS package them using `python3 -m zipfile -c <path_to_zip> <files...>` or `zip -r <path_to_zip> <files...>`. NEVER use `write_file` to write raw text or JSON to a `.zip` file, as it produces a corrupt archive. For Python scripts (`.py`), do NOT import uninstalled third-party packages (e.g. PIL, cv2, matplotlib) unless verified to be installed; always test run `python3 <script>` to confirm zero import or syntax errors.
5. Local Oracles & Validation: If an oracle or validation script (e.g. `oracle_client.py`, `tests/`, `validate.py`) exists in the directory, run it (`python3 <script>`) to verify your solution against the constraints before wrapping up.
6. Self-Inspection & Verification: Before wrapping up, run bash commands to verify that deliverable files exist at their exact declared paths, are non-empty, and valid (`ls -lh <path>`, `head -n 20 <path>`). For zip files, run `unzip -t <path>` to verify archive integrity. For python scripts, run `python3 <script>` to verify clean execution.
When finished, reply without calling tools with a concise engineering summary of what was delivered, where each file is located, and key numerical findings."""

VERIFIER = """You are a meticulous senior quality assurance and peer-review engineer auditing deliverables in a Linux container. \
You receive the task instructions, the engineering plan, and the worker's summary.

AUDIT PROTOCOL:
1. STRICT AUDIT ROLE: You are an AUDITOR, NOT the builder. Do NOT attempt to run scripts or commands to create or fix deliverables. Only inspect and audit the existing deliverables on disk using `read_file`, `ls -lh`, or validation checks (`python3 -c "..."`).
2. Deliverable Path Verification: You MUST check that every declared deliverable file exists on disk at its EXACT relative path (e.g. `submission/<filename>`, `output/<filename>`). Do NOT check the root directory if the file is specified in a subdirectory.
3. Content & Modification Check: Verify that the deliverable contains actual populated design entities (e.g. valid IFC buildings/site geometries in STEP format, non-empty calculations, complete zip archives). If a deliverable is missing, 0 bytes, or merely an untouched boilerplate template from the starter repo, it is an AUTOMATIC FAILURE.
4. Standard Tool Verification: The container contains standard Linux utilities and Python. Do NOT assume non-standard CLI validator binaries exist unless verified with `which`. Check files using file size (`ls -lh <path>`), inspection (`head -n 30 <path>`), and python scripts (`python3 -c "..."`).
5. Verdict Rule:
   - NEVER emit PASS unless you have personally verified that non-empty, valid deliverable files exist at the exact expected paths on disk and have been modified with real engineering content.
   - If everything is verified, complete, and code-compliant, reply with the single word: PASS
   - If ANY deliverable is missing, empty, untouched from template, invalid, or violates a constraint, reply with a numbered list of specific actionable gaps citing the EXACT relative file path and what is needed for the worker to fix."""


def tool(name, description, **params):
  """Builds an OpenAI function-tool definition dictionary.

  Args:
    name: Tool function name.
    description: Human-readable tool description.
    **params: Parameter names mapped to their JSON Schema type strings.

  Returns:
    An OpenAI function-tool specification dictionary.
  """
  return {
      "type": "function",
      "function": {
          "name": name,
          "description": description,
          "parameters": {
              "type": "object",
              "properties": {k: {"type": v} for k, v in params.items()},
              "required": list(params),
          },
      },
  }


TOOLS = [
    tool(
        "bash",
        "Execute standard Linux bash commands (ls, cat, grep, find, python3, etc.). Do not call other tool names like read_file inside bash.",
        command="string",
    ),
    tool("read_file", "Read the text contents of a file given its relative or absolute path.", path="string"),
    tool(
        "write_file",
        "Create or overwrite a file with text content; parent directories are created automatically.",
        path="string",
        content="string",
    ),
]
READ_ONLY = TOOLS[:2]


def clip(text):
  """Truncates `text` in the middle if it exceeds `RESULT_CHARS` characters.

  Args:
    text: Tool output string to clip.

  Returns:
    The original string if within `RESULT_CHARS`, or a middle-truncated string.
  """
  if len(text) <= RESULT_CHARS:
    return text
  half = RESULT_CHARS // 2
  return (
      f"{text[:half]}\n[... {len(text) - RESULT_CHARS} characters cut"
      f" ...]\n{text[-half:]}"
  )


def marked(messages):
  """Returns the messages a model call sends.

  For Claude, this is a copy whose system prompt and newest message carry
  `cache_control`. The stored conversation stays as it is, so each turn marks
  its own newest message.

  Args:
    messages: The conversation so far, in OpenAI chat format.

  Returns:
    `messages` itself for other models, or the marked copy for Claude.
  """
  if not CACHE_MARK:
    return messages
  out = list(messages)
  for i in {0, len(out) - 1}:
    m, content = out[i], out[i].get("content")
    if isinstance(content, str) and content:
      out[i] = {
          **m,
          "content": [{
              "type": "text",
              "text": content,
              "cache_control": {"type": "ephemeral"},
          }],
      }
    else:
      out[i] = {**m, "cache_control": {"type": "ephemeral"}}
  return out


async def run_tool(session, fn):
  """Executes a single tool call in the task container and returns its output.

  Rewrites `fn["arguments"]` as canonical JSON (or `"{}"` if invalid) so the
  conversation history remains valid for strict endpoints. Any tool failure is
  returned as an error string for the model.

  Args:
    session: Active `taskbridge.Session` connected to the task container.
    fn: Function-call dictionary containing `name` and `arguments`.

  Returns:
    The clipped tool output or error string to send back to the model.
  """
  name = fn["name"]
  try:
    if isinstance(fn.get("arguments"), dict):
      args = fn["arguments"]
    else:
      args = json.loads(fn.get("arguments") or "{}")
    if not isinstance(args, dict):
      raise TypeError("not a JSON object")
    fn["arguments"] = json.dumps(args, allow_nan=False)
  except (TypeError, ValueError) as exc:
    fn["arguments"] = "{}"
    return (
        f"error: the arguments are not a JSON object ({exc}); send the call"
        " again"
    )
  try:
    if name == "bash":
      result = await session.exec(args["command"])
      out = f"exit code {result['exit_code']}\n{result['output']}"
      if (
          result["exit_code"] != 0
          and "No such file or directory" in result["output"]
          and re.search(r"python3?\s+([^\s]+\.py)", args.get("command", ""))
      ):
        out += (
            "\n[SYSTEM ADVICE: The python script does not exist on disk. There are no pre-installed"
            " helper scripts in the container. You must write the script using 'write_file' first,"
            " or execute python code inline using python3 -c '...']"
        )
      return clip(out)
    if name == "read_file":
      return clip(
          (await session.read_file(args["path"])).decode("utf-8", "replace")
      )
    if name == "write_file":
      if args.get("path", "").endswith(".zip"):
        return (
            f"error: cannot use 'write_file' on a .zip file ({args.get('path')}). "
            "write_file produces a raw text file which corrupts the binary zip archive. "
            "You MUST use the bash tool with 'python3 -m zipfile -c <path_to_zip> <files...>' or "
            "'zip -r <path_to_zip> <files...>' to package deliverables into a valid zip archive."
        )
      return (
          f"wrote {await session.write_file(args['path'], args['content'])}"
          f" bytes to {args['path']}"
      )
    return f"error: there is no tool named {name}"
  # ValueError: content with an unpaired surrogate
  except (taskbridge.TaskError, KeyError, ValueError) as exc:
    return f"error: {type(exc).__name__}: {exc}"


async def recover(session, messages, exc, waits, until):
  """Handles a failed model call and returns whether to retry.

  On a 400 context-overflow error, shortens the oldest tool results. On a rate
  limit, server error, or connection drop, sleeps for the next interval in
  `RETRY_WAITS` (capped at the step's wrap-up deadline). Errors are logged
  rather than added to the conversation.

  Args:
    session: Active `taskbridge.Session` for the task.
    messages: The conversation, shortened in place when the context is full.
    exc: The `openai.APIError` the call raised.
    waits: Iterator over the remaining intervals in `RETRY_WAITS`.
    until: Seconds before the time limit at which the step wraps up.

  Returns:
    `True` to retry the call, `False` to re-raise.
  """
  status = getattr(exc, "status_code", None)  # None: no HTTP answer
  if status == 400 and CONTEXT_FULL.search(str(exc)):
    return await shorten(session, messages)
  wait = next(waits, None)
  if wait is None or not (
      status is None or status in (408, 409, 429) or status >= 500
  ):
    return False
  left = session.time_left()
  if left is not None and left > until:
    wait = min(wait, left - until)
  await session.log(
      f"model call failed ({str(exc)[:200]}); again in {wait:.0f} s"
  )
  await asyncio.sleep(wait)
  return True


async def shorten(session, messages):
  """Truncates the older half of long tool results to `CUT_KEEP` characters.

  Repeated calls truncate progressively newer results. Claude's thinking blocks
  are kept intact because each block's signature is self-contained and Claude
  rejects tool loops if the latest assistant turn loses its thinking block.

  Args:
    session: Active `taskbridge.Session` for the task.
    messages: The conversation, modified in place.

  Returns:
    Whether any tool result was truncated.
  """
  long = [
      n
      for n, m in enumerate(messages)
      if m["role"] == "tool" and len(m["content"]) > 2 * CUT_KEEP
  ]
  if not long:
    return False
  cut = long[: max(1, len(long) // 2)]
  for n in cut:
    messages[n]["content"] = (
        messages[n]["content"][:CUT_KEEP]
        + "\n[... cut to save context; run the tool again if needed]"
    )
  await session.log(f"context full: cut {len(cut)} old tool results")
  return True


def conversation(system, session, prompt):
  """Creates the initial `[system, user]` message list for a role.

  Args:
    system: System prompt template containing `{workdir}`.
    session: Active `taskbridge.Session` providing `workdir`.
    prompt: Initial user message content.

  Returns:
    A two-element list of OpenAI chat message dictionaries.
  """
  return [
      {"role": "system", "content": system.format(workdir=session.workdir)},
      {"role": "user", "content": prompt},
  ]


def extract_tool_calls_from_text(text: str):
  """Finds JSON tool calls embedded in markdown code blocks or raw text."""
  if not text:
    return []
  calls = []
  code_blocks = re.findall(r"```(?:json)?\s*([\s\S]*?)\s*```", text)
  candidates = [
      b.strip()
      for b in code_blocks
      if b.strip().startswith("{") and b.strip().endswith("}")
  ]
  if not candidates:
    for match in re.finditer(r"\{\s*\"(?:name|function|tool|action)\"", text):
      start_idx = match.start()
      brace_count = 0
      in_str = False
      escape = False
      for i in range(start_idx, len(text)):
        c = text[i]
        if escape:
          escape = False
          continue
        if c == "\\":
          escape = True
          continue
        if c == '"':
          in_str = not in_str
          continue
        if not in_str:
          if c == "{":
            brace_count += 1
          elif c == "}":
            brace_count -= 1
            if brace_count == 0:
              candidates.append(text[start_idx : i + 1])
              break
  for block in candidates:
    try:
      parsed = json.loads(block)
      if isinstance(parsed, dict):
        tool_name = (
            parsed.get("name")
            or parsed.get("function")
            or parsed.get("tool")
            or parsed.get("action")
        )
        raw_args = (
            parsed.get("arguments")
            or parsed.get("action_input")
            or parsed.get("parameters")
            or {}
        )
        if tool_name in ("bash", "read_file", "write_file"):
          args_str = (
              json.dumps(raw_args)
              if isinstance(raw_args, dict)
              else str(raw_args)
          )
          calls.append({
              "id": f"call_syn_{secrets.token_hex(4)}",
              "type": "function",
              "function": {"name": tool_name, "arguments": args_str},
          })
    except Exception:
      continue
  return calls


def extract_targets(task: str) -> list[str]:
  """Extracts candidate target deliverable paths from task instructions."""
  raw = re.findall(
      r"(?:/(?:app|filesystem|mnt|home|tmp|submission|output)/[\w\-./]+\.(?:ifc|py|json|md|txt|csv|pdf|zip|yaml|yml|sh))"
      r"|(?:(?:submission|output|app|filesystem)/[\w\-./]+\.(?:ifc|py|json|md|txt|csv|pdf|zip|yaml|yml|sh))",
      task,
  )
  explicit = re.findall(
      r"(?:write|deliverable[s]?)\s+(?:to|is|are|at)?\s*[`'\"]?([/\w\-./]+\.(?:ifc|py|json|md|txt|csv|pdf|zip|yaml|yml|sh))[`'\"]?",
      task,
      re.IGNORECASE,
  )
  candidates = set(raw + explicit)
  normalized = set()
  for p in candidates:
    p = p.strip("`'\" \t\r\n")
    if p.startswith(("app/", "filesystem/", "mnt/", "home/", "tmp/")):
      p = "/" + p
    normalized.add(p)

  # Filter out short relative suffixes if a more specific path exists
  final_targets = set()
  for p in normalized:
    if not p.startswith("/") and any(
        other.endswith("/" + p) for other in normalized if other != p
    ):
      continue
    final_targets.add(p)
  return sorted(final_targets)


async def check_target_status(session, targets):
  """Checks existence, file size, and validity of target deliverables, auto-syncing if found elsewhere."""
  status = {}
  for target in targets:
    try:
      basename = os.path.basename(target)
      # Auto-sync: if target is missing, check if it was created elsewhere in the container
      sync_cmd = (
          f"if [ ! -f '{target}' ]; then "
          f"  found=$(find /filesystem /app . -name '{basename}' -type f -not -path '*/templates/*' 2>/dev/null | head -n 1); "
          f"  if [ -n \"$found\" ] && [ -f \"$found\" ]; then "
          f"    mkdir -p \"$(dirname '{target}')\" && cp \"$found\" '{target}'; "
          f"  fi; "
          f"fi; "
          f"if [ -f '{target}' ]; then "
          f"  rel_target='{target}'; "
          f"  if [[ \"$rel_target\" == /filesystem/* ]]; then "
          f"    sub_rel=\"${{rel_target#/filesystem/}}\"; "
          f"    mkdir -p \"$(dirname \"$sub_rel\")\" 2>/dev/null && cp -u '{target}' \"$sub_rel\" 2>/dev/null || true; "
          f"  fi; "
          f"fi"
      )
      await session.exec(sync_cmd)

      # Validity check based on file extension
      if target.endswith(".zip"):
        cmd = f"test -f '{target}' && unzip -t '{target}' >/dev/null 2>&1 && stat -c '%s' '{target}' || echo 'MISSING_OR_CORRUPT_ZIP'"
      elif target.endswith(".py"):
        cmd = f"test -f '{target}' && python3 -m py_compile '{target}' >/dev/null 2>&1 && stat -c '%s' '{target}' || echo 'MISSING_OR_SYNTAX_ERROR'"
      else:
        cmd = f"test -f '{target}' && stat -c '%s' '{target}' || echo 'MISSING'"

      res = await session.exec(cmd)
      out = res.get("output", "").strip()
      status[target] = out
    except Exception:
      status[target] = "ERROR"
  return status


async def agent(session, messages, tools, until, final=False, require_tools=False):
  """Runs the tool-calling loop on `messages` until the model answers.

  Prompts the model to wrap up immediately once fewer than `until` seconds
  remain on the task clock. When `final` is `True`, updates
  `session.final_message` with each non-empty response.
  When `require_tools` is `True`, prevents premature text returns if no tools
  have been executed yet.

  Args:
    session: Active `taskbridge.Session` for the task.
    messages: Mutable conversation history list.
    tools: List of OpenAI tool definitions available to this role.
    until: Remaining task time threshold (in seconds) at which this step wraps
      up.
    final: Whether to record responses in `session.final_message`.
    require_tools: Whether the agent must call tools before replying with text.

  Returns:
    The final text response produced by this agent step.
  """
  # head: the start of an answer cut off at the output limit
  nudges, head, waits, tools_used = 0, "", iter(RETRY_WAITS), 0
  while True:
    left = session.time_left()  # None when the task has no time limit
    wrap_up = left is not None and left < until
    if wrap_up and messages[-1:] != [WRAP_UP]:  # asked once, also on a retry
      messages.append(dict(WRAP_UP))
    # A call still running when the wrap-up starts is stopped. The wrap-up call
    # itself gets at most WRAP_UP_SEC and stops at the time limit; if it takes
    # longer, the step returns head, its answer so far.
    if left is None:
      timeout = None
    elif wrap_up:
      timeout = min(WRAP_UP_SEC, left)
    else:
      timeout = left - until
    try:
      reply = await asyncio.wait_for(
          client.chat.completions.create(
              model=MODEL,
              messages=marked(messages),
              tools=tools,
              tool_choice="none" if wrap_up else "auto",
              reasoning_effort=EFFORT,
              max_completion_tokens=MAX_OUTPUT_TOKENS,
          ),
          timeout,
      )
    except asyncio.TimeoutError:
      if wrap_up:
        return head
      continue
    except openai.APIError as exc:
      if await recover(session, messages, exc, waits, until):
        continue
      raise
    waits = iter(RETRY_WAITS)
    if reply.usage:
      details = getattr(reply.usage, "prompt_tokens_details", None)
      await session.usage(
          MODEL,
          reply.usage.prompt_tokens,
          reply.usage.completion_tokens,
          getattr(details, "cached_tokens", 0) or 0,
      )
    message, cut = (
        reply.choices[0].message,
        reply.choices[0].finish_reason == "length",
    )
    text = head + (message.content or "")
    if final and text:
      session.final_message = text
    # sent back as returned: Gemini refuses a tool call without its thought
    # signature
    turn = message.model_dump(exclude_none=True)
    raw_calls = message.tool_calls or []
    if not raw_calls and not wrap_up and message.content:
      raw_calls = extract_tool_calls_from_text(message.content)
      if raw_calls:
        turn["tool_calls"] = raw_calls

    if wrap_up or not turn.get("tool_calls"):
      turn.pop("tool_calls", None)  # calls made at the wrap-up are not run
      if message.content:
        # a fix round, or the request to go on after a cut-off reply, continues
        # from this answer
        messages.append(turn)
      if require_tools and not tools_used and not wrap_up and nudges < MAX_NUDGES:
        nudges, head = nudges + 1, text
        messages.append({
            "role": "user",
            "content": (
                "ACTION REQUIRED: You responded with text without calling any tools. "
                "You must call the appropriate tool ('write_file' or 'bash') to create, "
                "populate, or modify the required deliverable files on disk now."
            ),
        })
        continue
      if wrap_up or (message.content and not cut) or nudges == MAX_NUDGES:
        return text
      nudges, head = nudges + 1, text
      messages.append({
          "role": "user",
          "content": (
              "Your reply was cut off; continue where it stopped."
              if cut
              else "Continue, and finish with your answer."
          ),
      })
      continue
    tools_used += len(turn["tool_calls"])
    nudges, head = 0, ""
    messages.append(turn)
    for call in turn["tool_calls"]:
      await session.log(
          f"{call['function']['name']} {call['function']['arguments'][:200]}"
      )
      content = await run_tool(session, call["function"])
      messages.append(
          {"role": "tool", "tool_call_id": call["id"], "content": content}
      )
    if left is not None:
      messages[-1]["content"] += (
          f"\n[{int((session.time_left() - until) // 60)} minutes left for this"
          " step]"
      )


async def solve(session):
  """Executes the planner, worker, and verifier loop for `session`.

  Args:
    session: Active `taskbridge.Session` for the task.

  Returns:
    The final summary message from the worker.
  """
  total = session.time_left()

  def until(share):
    """Computes the remaining seconds when `share` of total task time is left.

    Args:
      share: Fraction of total task time remaining (`0.0` to `1.0`).

    Returns:
      Remaining seconds threshold, or `0` if the task has no time limit.
    """
    return 0 if total is None else total * share

  def time_for(wrap_up):
    """Checks whether at least `MIN_STEP_SEC` remains before `wrap_up`.

    Args:
      wrap_up: Remaining seconds threshold at which the next step wraps up.

    Returns:
      `True` if there is no deadline or at least `MIN_STEP_SEC` before
      `wrap_up`.
    """
    left = session.time_left()
    return left is None or left - wrap_up >= MIN_STEP_SEC

  task = session.instruction
  # Identify declared target deliverables from task instructions
  targets = extract_targets(task)
  target_hint = (
      f"\n\n## Declared Target Deliverables\n"
      + "\n".join(f"- `{t}`" for t in targets)
      if targets
      else ""
  )
  initial_status = await check_target_status(session, targets)

  plan = await agent(
      session,
      conversation(PLANNER, session, f"{task}{target_hint}"),
      READ_ONLY,
      until(1 - PLAN_SHARE),
  )
  await session.log(f"plan: {plan[:1000]}")
  work = conversation(
      WORKER, session, f"{task}{target_hint}\n\n## Plan\n{plan}"
  )
  summary = await agent(
      session, work, TOOLS, until(CHECK_SHARE), final=True, require_tools=True
  )
  # Deliverable modification check: ensure Worker actually created or modified declared targets
  invalid_statuses = ("MISSING", "0", "ERROR", "MISSING_OR_CORRUPT_ZIP", "MISSING_OR_SYNTAX_ERROR")
  if targets and time_for(until(CHECK_SHARE / 2)):
    curr = await check_target_status(session, targets)
    modified = any(
        curr.get(t) not in invalid_statuses
        and curr.get(t) != initial_status.get(t)
        for t in targets
    )
    if not modified:
      work.append({
          "role": "user",
          "content": (
              "CRITICAL ACTION REQUIRED: You completed your turn but have NOT created or modified "
              f"the required deliverable file(s): {', '.join(targets)} on disk.\n"
              "The deliverable file is either missing or completely unchanged from the initial template.\n"
              "You MUST call 'write_file' or execute bash commands to write valid engineering content "
              "to the deliverable file on disk now before concluding."
          ),
      })
      summary = (
          await agent(
              session,
              work,
              TOOLS,
              until(CHECK_SHARE / 2),
              final=True,
              require_tools=True,
          )
          or summary
      )
  for _ in range(MAX_FIX_ROUNDS):
    if not time_for(until(CHECK_SHARE / 2)):
      break
    review = (
        f"## Task\n{task}{target_hint}\n\n## Plan\n{plan}\n\n## The engineer's"
        f" summary\n{summary}"
    )
    verdict = await agent(
        session,
        conversation(VERIFIER, session, review),
        READ_ONLY,
        until(CHECK_SHARE / 2),
    )
    await session.log(f"verifier: {verdict[:1000]}")
    # PASS check with deliverable modification verification
    if verdict.strip("*_. \r\n\t").upper() in ("", "PASS"):
      if targets:
        curr = await check_target_status(session, targets)
        modified = any(
            curr.get(t) not in invalid_statuses
            and curr.get(t) != initial_status.get(t)
            for t in targets
        )
        if not modified:
          verdict = (
              f"AUTOMATIC AUDIT FAILURE: The declared target deliverable(s) {', '.join(targets)} "
              "are missing, 0 bytes, corrupted, or untouched from the starter template. You must write valid content to disk."
          )
        else:
          break
      else:
        break
    if not time_for(WRAP_UP_SEC):
      break
    work.append({
        "role": "user",
        "content": (
            "CRITICAL ACTION REQUIRED - A reviewer checked your deliverables and"
            f" found these gaps:\n{verdict}\n\n"
            "DO NOT reply with conversational text or empty agreements.\n"
            "DO NOT overwrite deliverables with empty strings or stub placeholders.\n"
            "You MUST invoke tools (`write_file` or `bash`) to fix these gaps and"
            " create/modify the required deliverables on disk now with valid engineering content."
            " Only after the deliverables on disk are fixed and verified, reply with"
            " your updated summary."
        ),
    })
    summary = (
        await agent(
            session, work, TOOLS, WRAP_UP_SEC, final=True, require_tools=True
        )
        or summary
    )
  if targets:
    await check_target_status(session, targets)
  return summary or session.final_message


if __name__ == "__main__":
  taskbridge.run(solve)
