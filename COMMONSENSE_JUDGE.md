# Commonsense transition judge

The environment can ask an independent OpenAI-compatible model to judge
physical actions before committing their state transitions.

## Configuration

Add these values to the repository `.env`:

```dotenv
OWB_COMMONSENSE_JUDGE_ENABLED=true
OWB_COMMONSENSE_JUDGE_FAIL_CLOSED=true
OWB_COMMONSENSE_JUDGE_API_BASE_URL=https://api.example.com/v1/
OWB_COMMONSENSE_JUDGE_API_KEY=
OWB_COMMONSENSE_JUDGE_MODEL=gpt-5
```

If the dedicated URL, key, or model is omitted, the judge falls back to
`OPENAI_BASE_URL`, `OPENAI_API_KEY`, and `AWM_SYN_OVERRIDE_MODEL`.

`OWB_COMMONSENSE_JUDGE_FAIL_CLOSED=true` rejects a physical action when the
judge is unavailable or returns an invalid response. Set it to `false` only
when API availability is more important than benchmark integrity.

## Judged actions

- `pick_object`
- `place_object`
- `open_container`
- `close_container`
- `hang_object`
- `start_device`
- `stop_device`
- `apply_physical_tool`

Navigation remains governed by deterministic topology rules. Perception and
termination actions do not invoke the judge.

## Decision records

Each run writes `judge_decisions.jsonl` beside its `working.db`. Every entry
contains the action, parameters, decision, reason, violated constraints,
confidence, model, timestamp, error information, and the raw judge response.

Rejected actions:

- return a structured failure to the evaluated agent;
- do not modify the world state;
- are recorded in the database action log;
- are excluded from successful-action replay during subgoal verification.

## Implementation

- `ows/env/commonsense_judge.py`: state summarization, prompt, API call,
  response parsing, and JSONL audit log.
- `ows/env/server.py`: pre-transition judge gate.
- `ows/eval/verify.py`: replay only actions whose recorded tool response was
  successful.
