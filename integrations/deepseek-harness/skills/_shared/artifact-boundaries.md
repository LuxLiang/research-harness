# Artifact boundaries

- Treat canonical YAML plus Git as scientific truth and the session log as execution truth.
- Read only the revision-pinned ContextBundle. Never reconstruct scientific state from conversation history.
- Stage proposals through research artifact tools. Never edit canonical artifact files directly and never promote a proposal.
- Obey the context's operation, kind, ID, and field-diff rules. The narrowest rule wins.
- Preserve contradictory and negative evidence. Never remove it to simplify a narrative.
- Submit structured output through `research_action_submit`; natural-language output has no transition authority.
- Submit `gate_assessment_json` only when the pinned ContextBundle contains a non-null `gate_contract`; otherwise omit it completely.
- Leave all state transitions, artifact promotion, and status gates to the deterministic controller.
- Enforce authority as Skill × operation × artifact kind × field diff. A broad tool or filesystem capability never expands the pinned ContextBundle authority.
