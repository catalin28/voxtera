# Prompt flow — one guest turn through the concierge

Which prompt runs at each step of a dialog turn. All prompts live in
`src/voxtera/call_center/prompts/` and are editable live in **Admin → Prompt
Editor** (`/admin/call_center_prompts.html`) — saves hot-reload, no restart.

```mermaid
flowchart TD
    G([Guest message]) --> A & B

    subgraph parallel [runs in parallel]
      A["1 · Escalation check<br/><code>escalation_stems.json</code> (word list, 0ms)<br/>stem hit → <code>escalation_classifier.md</code> (LLM)"]
      B["2 · Understand<br/><code>query_decomposer.md</code> (LLM, every turn)<br/>reads conversation memory (Redis)"]
    end

    A -- escalates --> H([Handoff to human — fixed message, no LLM])
    B --> R{"3 · Route by query_type"}

    R -- conversational --> C["<code>concierge_converse.md</code> (LLM)<br/>greetings · recall · no retrieval"]
    R -- "scoped / broad / compound / comparison" --> K1[Qdrant retrieval + ES resolve — no prompt]
    K1 --> K2["<code>concierge_render.md</code> (LLM)<br/>writes the hotel answer"]
    R -- hybrid --> Y1[Qdrant + Web lane]
    Y1 --> Y2["<code>concierge_web_synth.md</code> (LLM)<br/>ONE combined answer"]
    R -- "web / destination" --> W1["<code>concierge_web_query.md</code> (LLM)<br/>search query built from the dialog"]
    W1 --> W2[Tavily web search — no prompt]
    W2 --> W3["<code>concierge_web_synth.md</code> (LLM)<br/>writes the web answer"]
    R -- escalate --> H
    R -. "missing info" .-> T["clarifying question<br/><code>triage_questions.md</code> (templates, no LLM)"]

    C & K2 & Y2 & W3 --> Z["4 · Answer → guest<br/>saved to Redis memory · logged to<br/><code>logs/travel_agent_consierge-&lt;date&gt;.jsonl</code>"]
```

Special jumps (handled before routing):

- A bare **"yes"** after the bot offered to look something up → runs the web
  lane on the previous question (`pending_web_offer`).
- An explicit **"search online / internette ara"** → web lane on the previous
  question.

## Persona — one file, applied everywhere

`concierge_persona.md` holds WHO the assistant is (tone, no stock openers,
spoken format, language). It is **automatically prepended** to all three answer
writers (`concierge_render`, `concierge_web_synth`, `concierge_converse`) via
`_with_persona()` in `concierge.py`. Change the persona there once; the task
prompts contain only task rules.

## The prompts, one line each

| Prompt | Type | Fires when | Controls |
|---|---|---|---|
| `concierge_persona.md` | shared prefix | every answer | tone, openings, spoken format, language — the ONE place for character |
| `query_decomposer.md` | LLM (Haiku) | **every turn** | classification (hotel/destination/web/hybrid/escalate/conversational), hotel mention, region, requirements |
| `escalation_stems.json` | word list | every turn (0ms) | whether the escalation LLM even runs |
| `escalation_classifier.md` | LLM (nano) | only on a stem hit | human-handoff verdict (booking, cancel, complaint, medical, urgent) |
| `concierge_render.md` | LLM (Haiku) | hotel-KB answers | persona + grounding (no invented amenities/locations), offer-to-check-online |
| `concierge_web_query.md` | LLM (Haiku) | before any web search | one self-contained search query from the dialog (resolves "there"/"they") |
| `concierge_web_synth.md` | LLM (Haiku) | web + hybrid answers | persona, on-site vs arranged-nearby accuracy, destination format (options + one pick) |
| `concierge_converse.md` | LLM (Haiku) | conversational turns | answers from memory; forbidden to promise actions |
| `triage_questions.md` | templates | missing critical slot | localized clarifying questions |
| `concierge_decompose_legacy.md` | LLM | legacy endpoint only | not part of this flow |

LLM-call budget per turn: minimum 2 (decompose + one answer writer); a web
turn adds the query builder; an escalation-suspect turn adds the classifier.
