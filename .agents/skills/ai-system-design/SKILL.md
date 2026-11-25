---
name: ai-system-design
description: Use when designing an AI application or agent system, deciding between a pipeline and an agent, or reviewing workflow state, reliability, latency, and cost.
---

# AI System Design

Choose the simplest execution model that satisfies the user goal and its failure constraints.

1. Define the primary user flow, supported actions, data boundaries, and success criteria. Identify which latency, cost, availability, and quality constraints are requirements and which need measurement. Preserve existing project choices and verify SDK details against installed versions and official documentation.
2. Use deterministic code for rules and known transitions. Use a single model call for a bounded judgment or transformation, and a fixed workflow when the application knows the sequence. Use an agent when evidence at runtime must determine the next tool or step. Choose explicit graph orchestration when branch behavior, restart, or approval and resume require explicit control.
3. Diagram the request path and label where data and state live. Distinguish temporary run state, durable workflow checkpoints, and business records. Define tool inputs, authorization, and outcomes. For actions with side effects, use idempotency or reconciliation at the external boundary and verify what happens on timeout or replay. When human approval is required, bind it to the exact action and payload, then recheck authorization before execution.
4. Set bounds on model calls, tool calls, retries, and elapsed time where those can grow. Define failure paths for unavailable dependencies, partial completion, invalid output, and exhausted budgets. Validate the happy path and the highest-impact failure path with observable outcomes.

For an architecture proposal, give the chosen execution model, the main request flow, state ownership, the tradeoff that drove the choice, and how to verify it. Use the LangChain build skill for framework-specific implementation decisions.

- [LangGraph workflows and agents](https://docs.langchain.com/oss/python/langgraph/workflows-agents)
- [LangGraph persistence](https://docs.langchain.com/oss/python/langgraph/persistence)
