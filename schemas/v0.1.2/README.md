# JSON Schema v0.1.2

`common.schema.json` defines the envelope, actor, artifact reference, and
resource reference contracts. Each of the eight artifact schemas composes that
envelope and closes the resulting object with `unevaluatedProperties: false`.

The schemas enforce local shape and state-dependent required fields. Checks
that need the complete workspace—reference existence, project ownership,
dependency cycles, target revisions, and project stage gates—belong to the
Python semantic validator.

Schema identifiers are stable under:

```text
https://research-harness.local/schemas/v0.1.2/
```

This URI is an identifier, not a network dependency. The validator registers
the local schema files and resolves all references without internet access.
