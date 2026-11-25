---
name: rag-engineering
description: Use when designing or debugging retrieval-augmented generation (RAG), document ingestion, chunking, embeddings, search, reranking, or grounded citations.
---

# RAG Engineering

Start with a reproducible retrieval baseline and diagnose failures before changing the stack.

1. Identify the corpus, source of truth, expected freshness, access rules, representative questions, and citation requirements. Prefer a direct structured lookup when the answer comes from known fields or exact identifiers. For document Q&A, begin with one retrieval pass followed by generation. Add query rewriting, reranking, or agent-directed search when measured failures justify the extra calls.
2. Preserve stable source IDs, revision IDs, locators and access metadata on chunks. Make ingestion safe to rerun. Verify updates, deletes and access changes reach the serving index. Enforce authorization before context reaches the model or cache, and include the applicable access scope in cache keys.
3. Match the baseline to the query mix. Test lexical retrieval for names and exact identifiers, semantic retrieval for paraphrases, and hybrid retrieval when both matter. Compare changes on the same labeled cases and record retrieval depth and latency. Keep retrieved content as untrusted data. Generate citations from retrieved source metadata and abstain or ask for clarification when evidence is insufficient.
4. Measure retrieval and answers separately. Use recall at the serving cutoff when relevant sources are labeled. Check answer correctness, citation support, and appropriate abstention separately. Include unanswerable queries, stale documents, and access-boundary cases when applicable. Review misses before tuning the prompt.

For a change, report the baseline, observed failure, intervention, and measured result. If the corpus or labels are unavailable, state what is missing and identify the smallest useful evaluation set.

- [Retrieval patterns](https://docs.langchain.com/oss/python/deepagents/retrieval)
- [RAG evaluation](https://docs.langchain.com/langsmith/evaluate-rag-tutorial)
