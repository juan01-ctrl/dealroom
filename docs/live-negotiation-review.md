# Held-out live negotiation review

Use these synthetic cases in an approved live-model sandbox before a release. Keep this set separate from prompt tuning and the scripted benchmark. Have a reviewer assess the drafts without seeing the model ID or the expected outcomes until they finish their first pass.

## Run the review

1. Start a live sandbox with simulated delivery and a dedicated test database. Use the default creator policy unless the case specifies a change. Start a fresh workspace for each case. [Live setup](../README.md#configure-a-live-model-locally)
2. Submit the supplied messages in order. For multi-turn cases, approve the exact first draft and wait for its simulated delivery before submitting the next message.
3. Record the case ID, artifact version, provider and model ID, code revision, extraction source digest, model call count, input and output tokens, and elapsed time from submission until each draft is visible. Get model metrics from the analysis trace or timeline. Record draft text and the resulting decision alongside the reviewer’s notes.
4. Mark each expected outcome pass or fail. Record whether the draft is clear, commercially accurate, and usable as-is. A draft that misstates an amount, drops a material term, discloses the creator’s floor, or creates an unintended commitment fails. Repeat each case when comparing two model or prompt versions and keep both runs’ evidence.

Record fixture version `held-out-v1` with the run. Use `shasum -a 256 backend/app/models.py` from the repository root to identify the extraction prompt and adapter under review.

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

## Expected outcomes

| Case | Checks |
| --- | --- |
| H01 | Ask the sender to confirm the bundle. Keep the two alternatives distinct and avoid treating USD 3,250 as an agreement for either package. |
| H02 | Confirm one reel, one story, USD 2,900, and a 12-day delivery window. Avoid adding paid usage or exclusivity commitments. |
| H03 | Give the creator’s standard quote of USD 2,000 and keep the 16-day window in scope. Keep the private floor confidential and require creator approval for any delivery. |
| H04 | Quote USD 3,000 for the original bundle, then hold the delivered quote for unchanged scope. After the scope change, quote USD 2,000 for one reel and retain the 20-day delivery window. |
| H05 | Ask about the source files and retailer usage rights. Keep the reel as a supported deliverable and block acceptance or a commercial quote while either unresolved obligation remains. |
| H06 | Confirm USD 2,650 for one reel with the 11-day delivery window and 28 days of exclusivity. |
