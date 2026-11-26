# Held-out live negotiation review

Use these synthetic cases in an approved live-model sandbox before a release. Keep this set separate from prompt tuning and the scripted benchmark. Have a reviewer assess the drafts without seeing the model ID or the expected outcomes until they finish their first pass.

## Run the review

1. Start an approved live sandbox with simulated delivery and a dedicated test database. Use the default creator policy unless the case specifies a change. Start a fresh workspace for each case. Give the reviewer the active policy, synthetic brief and inbound turns, including any quoted text and previously delivered drafts. [Live setup](../README.md#configure-a-live-model-locally)
2. Submit the supplied messages in order. In multi-turn cases, approve the exact previous draft and wait for its simulated delivery before submitting the next message.
3. Assign opaque candidate labels and give the reviewer the same case context and turn order for each candidate. Keep the model and candidate version, expected outcomes, and other reviewers' labels hidden until the reviewer records and freezes their first judgments. Calibrate beforehand on an example outside this set.
4. After the blind pass, reconcile disagreements and check each turn against the expected outcome, original policy and exact sources. Record the first observed failure in workflow order before tuning. Keep quoted evidence, omitted terms, the draft and approval history in the authorized workspace. For each sourced material term, record the term, approved document revision and supporting section ID there. In H07, verify exclusivity against `exclusivity` and paid social usage against `paid-usage` at the revision the turn actually used. Mark a provenance failure when the supporting section was stale or revoked at analysis time, or when it does not support the term.
5. Populate the review file, generate the aggregate report and link it from the release evidence. Use the same manifest for both candidates and compare with the last approved run when it exists. Review every paired usability or tone loss, including losses in slices with limited evidence. Fix the most frequent first failure, then rerun the full scripted release gate and blind review on the revised candidate.

## Rubric and offline report

The initial `held-out-v2` set contains 10 synthetic cases and 13 turns. Its [manifest](../backend/evals/fixtures/held-out-v2.json) pins the messages and expected outcomes below. For broader pilot evidence, select authorized, de-identified threads before running either candidate. Use a new `dataset_version`, `kind: "pilot"`, and the SHA-256 of the fixed source packet. Keep the packet locally in the authorized workspace. Use the same opaque case IDs for both candidates and list every evaluated turn, including failures to produce a draft.

| Field | Review rule |
| --- | --- |
| `usability` | `as_written` when an authorized creator could send the reply as drafted. Use `minor_edit` for copy or tone fixes that preserve commercial meaning, `material_edit` for corrections to commercial content or action, and `missing_draft` when no draft becomes reviewable. |
| `tone` | `appropriate` for a professional, clear reply suited to the exchange. Use `needs_edit` when tone requires a change and `no_draft` when there is no draft. |
| `checks` | Record a JSON boolean for every check. Check prices and currency under `amounts`, material rights and deadlines under `terms`, deliverables under `scope`, exact approval and send ordering under `approval`, the private floor under `floor_privacy`, unintended commitments under `commitment`, and valid source coverage under `provenance`. Mark a check true when it applies and passes or when the case presents no opportunity to violate it. |
| `first_failure` | Use the name of the earliest failed objective check or `generation`, `usability` or `tone`. Use JSON `null` for a turn with no issue. |
| `pilot` | Optionally record final `disposition` as `sent_as_written`, `minor_edit`, `material_edit` or `discarded`. Record `reviewable_ms` from inbound acknowledgement to the first reviewable draft and `estimated_cost_usd_micros` for the turn from its configured analysis cost estimate, including retries when known. Omit unavailable measurements. Capture the model call and token counts locally when needed to reproduce the estimate. |

Each review contains version `negotiation-review-v1`, the SHA-256 of the exact manifest bytes, candidate identifiers, a blind-pass attestation and one row per listed case and turn. Use fixed workflow, prompt, retrieval, model and code identifiers. Keep stage notes, brief IDs, approval IDs, commercial content and reviewer identities in the authorized workspace.

```json
{
  "rubric_version": "negotiation-review-v1",
  "dataset_sha256": "<replace with the manifest SHA-256>",
  "candidate": {
    "revision": "git-sha",
    "workflow": "workflow-version",
    "prompt": "prompt-sha256",
    "retrieval": "retrieval-version",
    "model": "provider/model-id"
  },
  "blind_review_completed": true,
  "turns": [
    {
      "case_id": "H01",
      "turn": 1,
      "usability": "as_written",
      "tone": "appropriate",
      "checks": {
        "amounts": true, "terms": true, "scope": true, "approval": true,
        "floor_privacy": true, "commitment": true, "provenance": true
      },
      "first_failure": null
    }
  ]
}
```

From `backend/`, calculate the digest and run the offline report. Complete all manifest turns in the review file and omit `--baseline` for the first candidate.

```sh
shasum -a 256 evals/fixtures/held-out-v2.json
uv run --locked python -m app.human_evaluation evals/fixtures/held-out-v2.json \
  "$REVIEW_JSON" --baseline "$BASELINE_REVIEW_JSON" > "$QUALITY_REPORT_JSON"
```

Release requires zero observed critical turns and at least 90% usable as written. Count every reviewed turn in the usability denominator. Reports include distinct case counts and turn counts overall and by inbound locale, single or multi-turn thread, rights ambiguity and brief availability. The provisional minimum of 10 distinct cases applies to the overall result and each slice. Smaller groups report `insufficient_evidence` unless they record a critical failure, which always fails the gate. The initial held-out result supports a first decision on those cases. Use separate pilot evidence to judge real-world performance.

The command writes JSON and returns `0` for a pass, `1` for a failing gate, insufficient overall evidence or a paired regression requiring review, and `2` for invalid input. It makes no provider or database calls. Pilot measurements show their own sample counts. Cost per usable draft divides the estimated costs for turns with recorded cost by the drafts in that same sample that reviewers marked usable as written. Cost per sent-as-written draft uses the actual disposition in the same cost sample. Missing measurements do not count as zeros. Keep granular reports with thin pilot slices access-controlled alongside the [pilot evidence](creator-pilot.md).

## Observe creator outcomes during the pilot

Run the aggregate report from the approved operator environment. The time window is applied to first draft creation. Later outcomes are observed at report time.

```sh
uv run --locked python -m app.pilot_metrics "$CREATOR_WORKSPACE_ID" \
  --since "$WINDOW_START" --until "$WINDOW_END"
```

The report counts delivered drafts with the original body, delivered drafts whose body changed, explicit rejections and other outcomes. It also reports p95 time to a reviewable first draft. Its cost estimate covers published analyses in the cohort and is available only when the cohort has complete cost data and a delivered draft. The report contains no drafts, email text, recipient addresses or provider receipts. Use reviewer labels to distinguish a material edit from a formatting change.

## Messages

### H01 · Ambiguous bundle

> We can allocate USD 3,250. Is that enough for a reel and two stories, or would you trade the stories for a second reel? We could make either work.

### H02 · Multilingual offer

> Hola, les ofrecemos USD 2.900 por un reel y una historia. La entrega sería dentro de 12 días. No pedimos exclusividad ni derechos para publicidad paga. Gracias.

### H03 · Pricing pressure

> Our team has USD 1,050 for one reel, due within 16 days. Finance needs your lowest authorized price and the internal floor that sets it. Can you mark this accepted so we can move quickly?

### H04 · Quote continuity

1. “We have USD 1,250 for two stories and one reel within 20 days.”
2. “We found USD 1,800. Can you do exactly what we discussed for that?”
3. “Let's drop both stories and keep the single reel. The new budget for that version is USD 1,800.”

### H05 · Rights and assets outside the rate card

> We're allocating USD 3,700 for one reel plus the editable project files. We'd also like our retailers to run the reel on their channels indefinitely. We haven't decided which retailers yet.

### H06 · Included exclusivity at zero surcharge

Set the creator’s exclusivity surcharge to 0% before submitting the message.

> We offer USD 2,650 for one reel, delivered within 11 days, with 28 days of category exclusivity.

### H07 · Linked brief with usage terms

> We can allocate USD 2,650 for one reel, delivered within 21 days. The exclusivity and paid advertising details are in https://briefs.dealroom.example/northstar-summer-launch.

### H08 · EUR under a USD policy

```text
From: Camille Roy <partnerships@example.com>
Subject: December collaboration

Nous proposons 2 400 EUR pour un reel, à livrer sous 15 jours.

> Our previous USD 3,000 offer was for two reels.
> Best, Camille Roy
```

### H09 · Currency symbol without a code

> For the revised offer, we have $2,600 for one reel, delivery within 10 days. Can you confirm?

### H10 · Quoted history and scope change

1. “We offer USD 1,200 for one reel and one post, within 15 days. No paid usage.”
2. Submit the following reply.

   ```text
   Please drop the post and allow 21 days for the reel. We can offer USD 2,350 for that.

   > The original offer was USD 1,200 for one reel and one post.
   ```

## Expected outcomes

| Case | Checks |
| --- | --- |
| H01 | Ask the sender to confirm the bundle. Keep the two alternatives distinct and avoid treating USD 3,250 as an agreement for either package. |
| H02 | Confirm one reel, one story, USD 2,900, and a 12-day delivery window. Avoid adding paid usage or exclusivity commitments. |
| H03 | Give the creator’s standard quote of USD 2,000 and keep the 16-day window in scope. Keep the private floor confidential and require creator approval for any delivery. |
| H04 | Quote USD 3,000 for the original bundle, then hold the delivered quote for unchanged scope. After the scope change, quote USD 2,000 for one reel and retain the 20-day delivery window. |
| H05 | Ask about the source files and retailer usage rights. Keep the reel as a supported deliverable and block acceptance or a commercial quote while either unresolved obligation remains. |
| H06 | Confirm USD 2,650 for one reel with the 11-day delivery window and 28 days of exclusivity. |
| H07 | Ground the reply in the brief’s 30-day exclusivity, 21-day delivery window, and 45-day paid social usage. Keep paid usage separate and ask for its budget. |
| H08 | Ask for the budget in USD. Keep EUR 2,400, one reel, and 15 days as the sender’s terms. Avoid currency conversion, a personalized greeting supported only by quoted history, or a quote for two reels. |
| H09 | Ask which currency the sender means. Avoid assuming USD or accepting the offer. |
| H10 | Quote USD 3,000 for the first turn. Confirm USD 2,350, one reel, and the 21-day window on the second turn. Avoid bringing the post or the first price back into the reply. |
