# ADR-0000: Template

- **Status:** Template
- **Date:** —
- **Deciders:** —
- **Related:** —

---

## Context

What is the situation? What forces are at play — technical, operational, financial,
organisational? Describe the problem so that someone reading this in two years, with
none of the current context, understands why a decision was needed.

Be specific about constraints that are not obvious: upstream behaviour we cannot
change, hardware we must support, cost ceilings, skills available.

## Decision

What we decided, stated in the active voice, in one or two sentences. Then the detail
required to act on it.

> "We will use SQLite as the state store, with a single writer per table and no
> multi-host support."

## Consequences

### Positive

What this gets us. Be concrete — "simpler" is not a consequence, "no server to
operate or back up" is.

### Negative / accepted costs

What we give up, and what we accept as a result. **Every decision has a cost.** An ADR
with no costs listed has not been thought through.

### Neutral

Facts that are neither benefits nor costs but that a reader should know.

## Alternatives considered

| Alternative | Why not |
|---|---|
| … | … |

Include the alternatives that were genuinely plausible. An alternative listed only to
be dismissed makes the document less trustworthy, not more.

## Revisit if

The specific conditions under which this decision should be reopened. Without this
section, ADRs become permanent by inertia rather than by intention.

---

## Conventions

| Field | Values |
|---|---|
| `Status` | `Proposed` \| `Accepted` \| `Deprecated` \| `Superseded by ADR-XXXX` |
| `Date` | ISO 8601, the date the decision was accepted |
| `Deciders` | Who owns it |
| `Related` | Linked ADRs, design docs, or issues |

**Rules**

- One decision per ADR. If it contains "and", consider splitting it.
- Never edit an accepted ADR to reflect a new decision. Write a new one that supersedes
  it and update the old one's `Status`. The history is the value.
- An ADR is required for any decision that is expensive to reverse, that constrains
  future options, or that a future contributor would plausibly question.
