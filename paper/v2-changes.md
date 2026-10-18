# Changes for draft v2

Every item below is either a correction the code forces, or an addition agreed
in review. Section numbers refer to the v1 draft.

---

## 1. Correction: the matcher is not literal

**Sections 3.5, 6.1 (RQ2/H2).** The draft says "literal `(subject, metric)`
matching". The implementation normalises the key before comparing:

```python
def _normalize(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")
```

so `"Vendor A"` and `"vendor_a"` already match. Values are then compared with
exact string equality. The honest description is **normalised key, literal
value**. H2 must be restated: case and punctuation are already handled, and the
failure modes are different words for the same concept, and the same number
written differently.

This matters because H2 as drafted is falsified by reading one function, and a
reviewer will read it.

## 2. Correction: what counts as a conflict

**Section 3.5.** A conflict requires two distinct claimants *and* two distinct
values:

```python
contested = len(set(values.values())) > 1
if contested and len(values) > 1:
```

State this. It has a consequence the draft does not mention, addressed next.

## 3. Addition: self-revision is not currently surfaced

**New, Sections 3.5 and 6.1.** Because a conflict needs two distinct tasks, an
agent that answers differently on attempt 1 and attempt 2 of the *same* task
silently overwrites its earlier answer. Nobody sees it, and no reviewer is
asked about it.

Two reasons this belongs in v2:

- **Governance.** A claim that changed without an external cause is exactly the
  kind of thing a human gate exists to catch.
- **It is a free confidence signal.** Agreement across repeated attempts is the
  consistency-based estimator RQ4 needs, computed from runs already being
  performed rather than from extra sampling.

Proposed: record every claim an attempt produces, and emit `self_revision` when
a task changes its own value without a reconciliation having asked it to.
Report it as a distinct outcome alongside detection and resolution.

## 4. Addition: source dependence (the main novelty)

**New subsection in 3.5, new hypothesis, new ablation A12.**

Truth discovery has long held that sources which copy from one another are not
independent, so their agreement is not corroboration
(Dong, Berti-Equille and Srivastava, VLDB 2009; Li et al., survey 2016). Every
multi-agent orchestration system we are aware of, including this one as built,
treats agreement between agents as evidence regardless of where the agents got
their data.

This is not hypothetical in our own demo: the weekly summary is **generated
from** the ticket database. When the summary and the database agree, that
agreement carries no information, because it is one observation counted twice.
When they disagree, the dependent source is almost certainly the stale one.

Proposed:

- `SourceRef.derived_from` records that one source is a copy or summary of
  another. Implemented in `eval/schema.py`.
- **H7**: discounting agreement between dependent sources reduces false
  assurance, and preferring the independent source on disagreement resolves
  stale-snapshot conflicts without a re-query.
- **A12** ablation: treat dependent sources as independent, and measure the
  false-assurance rate.

This is the clearest claim to novelty in the paper and should be visible in the
abstract and the contributions list.

## 5. Addition: tie-breaking becomes RQ3b

**Section 6.1.** The re-query target is the claimant with the lowest
confidence, ties broken by the lowest task id. The tie-break is now explicit in
`Coordinator.resolution_target`, because confidences tie often and a run that
picks a different claimant each time cannot be compared with another run. That
is the baseline arm; make it a named policy with these arms:

| Policy | Rule | Expected to win on |
|---|---|---|
| `lexicographic` | lowest task id | baseline, reproducible |
| `lowest_confidence` | current behaviour | true disagreement |
| `oldest_as_of` | the stalest source | **stale snapshot** |
| `lowest_tier` | declared source precedence | scope and unit mismatch |
| `least_evidence` | fewest supporting items | thin claims |
| `learned_reliability` | per-agent accuracy over runs | long deployments |
| `requery_all` | ask every claimant | small claimant counts |

**RQ3b**: which tie-breaking policy resolves most conflicts correctly per
class, and at what cost? Report a policy-by-class matrix, not a single number.

## 6. Resolved: one supersede rule, and what `valid_to` means

**Section 3.6.** The two stores disagreed. `memory/semantic.py` set only
`superseded_by`; `platform/sql/facts.py` set `superseded_by` and `valid_to`,
so the same correction produced a different record depending on which store
was in play.

Settled as **`superseded_by` always, `valid_to` only when the fact carries an
expiry.** A restated figure is corrected, not expired, so it has no `valid_to`;
a document with a stated validity period does. `Claim.valid_to` carries the
signal, because only the claim knows whether the thing it asserts can go out of
date. Both stores now share one predicate for a current fact:

```python
ACTIVE_FACT = "superseded_by IS NULL AND (valid_to IS NULL OR valid_to > ?)"
```

This separates two windows the draft ran together. A fact is *believed* from
`valid_from` until its replacement is written, and it *asserts* something true
until its own `valid_to`. Point-in-time recall needs the first, so `as_of`
reads the replacement's `valid_from` rather than the superseded row's
`valid_to`. Say in 3.6 that the two windows are distinct; it is the difference
between "what did we think then" and "what was true then".

## 7. Dataset: name it, and say what it cannot supply

**Section 6.2.** Replace "at least one domain built from public tabular and
document corpora" with the actual source:

> **SEC Financial Statement Data Sets.** Quarterly archives of as-filed
> financial statement data, 2009Q1 onward, published by the U.S. Securities and
> Exchange Commission at
> `https://www.sec.gov/data-research/sec-markets-data/financial-statement-data-sets`.
> A United States government work. Each archive contains `sub.txt` (one row per
> filing) and `num.txt` (one row per reported figure).

The reason to use it is that **the disagreements are not authored**. When a
filer submits a 10-K and later a 10-K/A correcting it, the SEC publishes both,
flags the superseded filing with `prevrpt`, and the amended figure is the
ground truth. Conflict classes derive mechanically:

| Class | Derived from | Status |
|---|---|---|
| True disagreement | restated value between the pair | natural |
| Stale snapshot | the superseded filing still in circulation | natural |
| Scope mismatch | `qtrs` differs for the same concept and date | natural |
| Unit mismatch | `uom` differs for the same concept and date | natural |
| Paraphrased metric | the amendment uses a synonymous XBRL tag | natural |
| No-conflict control | a figure unchanged across the pair | natural |
| **Formatting-only** | **not available** | **must be synthesised** |

Formatting-only differences cannot come from this source, because the archive
stores parsed numerics rather than rendered text. Say so, and report those
scenarios as synthetic.

Consider the **Federal Audit Clearinghouse** (`https://fac.gov/data`, public
domain, Form SF-SAC plus the audit report PDF) as the document-side domain if a
second non-authored corpus is needed. A 2024 GAO report on FAC data quality is
worth citing as independent evidence that this domain needs reconciliation.

Code: `eval/datasets/sec.py`, `eval/schema.py`, `scripts/fetch_sec_data.py`.

## 8. Ablations, stated in full

**Section 6.3.** Replace the prose list with a table; each row is one switch.

| # | Switch | Off-state | Tests |
|---|---|---|---|
| A1 | plan validation | dispatch unchecked | RQ1 |
| A2 | reconciliation | first claim wins | RQ2, RQ3 |
| A3 | retry | fail on first tool error | RQ5 |
| A4 | degrade | any tool failure fails the run | RQ5 |
| A5 | replan | a blocked task ends the run | RQ5 |
| A6 | human gate | promote on confidence alone | RQ6 |
| A7 | confidence bar | promote on approval alone | RQ6 |
| A8 | supersede | overwrite instead | RQ6 |
| A9 | re-query target | the seven policies above | **RQ3b** |
| A10 | claim matcher | literal, normalised, embedding, LLM-judge | RQ2 |
| A11 | confidence estimator | verbalised, consistency, hybrid | RQ4 |
| A12 | source independence | treat dependent sources as independent | **H7** |

A6 and A7 together separate the two gates; reporting only their combination
hides which one does the work.

## 9. Naming

One term per concept, in code and in the paper. Where they differ today, the
code name wins and the paper follows it.

| Concept | Use | Not |
|---|---|---|
| Promotion threshold | `promotion_confidence` (0.8) | theta, confidence bar |
| Re-query target rule | `requery_target` | tie-break, arbitration |
| Superseded fact | `superseded_by` | overwritten, replaced |
| Claim identity | `(subject, metric)` | claim key, fact key |
| Non-critical failure | `degraded` | partial, soft-failed |
| Critical failure | `blocked` | failed, halted |
| Dependent source | `derived_from` | copied, duplicated |

## 10. Values to state in the evaluation setup

Audited from the code at the pinned commit.

| Parameter | Value |
|---|---|
| `promotion_confidence` | 0.8 |
| Promotion requires human approval | yes, in addition to the threshold |
| `max_task_attempts` | 3 |
| `retry_backoff_seconds` | 0.25, giving delays 0.25 / 0.5 / 1.0 |
| `max_conflict_attempts` | 2 |
| `max_plan_revisions` | 2 |
| `max_plan_attempts` | 3 |
| `tool_timeout_seconds` | 20.0 |
| `temperature` | 0.0 on the self-hosted backend |
| `seed` | unset by default, passed through where the backend accepts one |

Every model call records its model id, temperature and seed in the trace, so
the reported configuration is read back from the runs rather than asserted. The
hosted backend runs with extended thinking, which pins sampling temperature at
1.0 and takes no seed; runs that need a fixed seed use the self-hosted
backend.

---

## Still open

- **Formatting-only scenarios** must be synthesised; decide how many and label
  them separately in every table.
- **Whether FAC is in scope** for v2 or deferred.
- **Human study size** for the review-burden measurement in RQ6.
