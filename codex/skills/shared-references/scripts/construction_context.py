#!/usr/bin/env python3
"""Render existing candidate/finding records for a research handoff; never a gate.

Read-only, stdlib only. Markdown is preserved verbatim. Optional explicit
supersedes links affect interpretations in the same candidate and scope, never
the underlying experiments. No model calls, writes, scoring or status changes.
"""
import argparse
import json
from pathlib import Path


def partition_findings(records):
    """Return active, historical, warnings; ambiguous links keep both records."""
    if not isinstance(records, list) or not all(isinstance(r, dict) for r in records):
        return [], [], ["Unstructured findings: use the original record below."]
    by_id = {}
    for index, record in enumerate(records):
        ident = record.get("id")
        if isinstance(ident, str) and ident:
            by_id.setdefault(ident, []).append(index)
    links, warnings = {}, []
    for index, record in enumerate(records):
        refs = record.get("supersedes", [])
        if isinstance(refs, str):
            refs = [refs]
        if not isinstance(refs, list):
            warnings.append("Malformed correction link; retained without supersession.")
            continue
        for ref in refs:
            targets = by_id.get(ref, []) if isinstance(ref, str) else []
            own = by_id.get(record.get("id"), []) if isinstance(record.get("id"), str) else []
            if len(targets) != 1 or len(own) != 1:
                warnings.append("Unresolved/ambiguous correction link: {!r}".format(ref))
                continue
            target = targets[0]
            old = records[target]
            # Missing scope is not permission to globally retract a finding.
            same = all(record.get(k) not in (None, "", [], {}) and record.get(k) == old.get(k)
                       for k in ("candidate_id", "scope"))
            if not same or target == index:
                warnings.append("Correction scope mismatch/self-link: {!r}".format(ref))
                continue
            links.setdefault(index, set()).add(target)

    def reaches(start, goal):
        pending, seen = [start], set()
        while pending:
            item = pending.pop()
            if item == goal:
                return True
            if item not in seen:
                seen.add(item)
                pending.extend(links.get(item, ()))
        return False

    cyclic = set()
    for source, targets in links.items():
        for target in targets:
            if reaches(target, source):
                cyclic.update((source, target))
    historical = set()
    for source, targets in links.items():
        for target in targets:
            if source in cyclic or target in cyclic:
                warnings.append("Cyclic correction: retain both interpretations.")
            else:
                historical.add(target)
    return ([r for i, r in enumerate(records) if i not in historical],
            [r for i, r in enumerate(records) if i in historical], warnings)


def _dump(value):
    return json.dumps(value, ensure_ascii=False, indent=2)


def render_findings(value):
    records = value.get("findings") if isinstance(value, dict) else value
    active, history, warnings = partition_findings(records)
    if not isinstance(records, list) or not all(isinstance(r, dict) for r in records):
        return "## Original findings (no automatic interpretation)\n\n" + _dump(value)
    sections = ["## Current scoped interpretations\n\n" + _dump(active)]
    if isinstance(value, dict):
        metadata = {k: v for k, v in value.items() if k != "findings"}
        if metadata:
            sections.insert(0, "## Original findings metadata\n\n" + _dump(metadata))
    if history:
        sections.append("## Historical interpretations explicitly superseded — not current verdicts\n"
                        "Original measurements remain evidence; only the interpretation is replaced.\n\n"
                        + _dump(history))
    if warnings:
        sections.append("## Unresolved references (not an experiment blocker)\n\n" + "\n".join(warnings))
    return "\n\n".join(sections)


def render_document(text, label, findings=False):
    try:
        value = json.loads(text)
    except ValueError:
        return "## {} — original notes\n\n{}".format(label, text)
    if findings:
        return render_findings(value)
    # Preserve every candidate field, including unknown/legacy fields. Do not
    # silently choose a version or mistake predictions for observed outcomes.
    result = "## {} — candidate/source/implementation records\n\n{}".format(label, _dump(value))
    if isinstance(value, dict) and "findings" in value:
        candidate = dict(value)
        embedded = candidate.pop("findings")
        result = ("## {} — candidate/source/implementation records\n\n{}\n\n{}"
                  .format(label, _dump(candidate), render_findings(embedded)))
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("candidate", help="Existing candidate JSON or Markdown; no schema migration needed")
    parser.add_argument("--findings", help="Optional existing findings JSON or Markdown")
    args = parser.parse_args(argv)
    print("# Construction handoff\n\nUse the current candidate version and its actual code base. "
          "Source conditions and prior observations inform implementation; predictions are not results. "
          "Missing fields do not create a permission gate.")
    for path, is_findings in ((args.candidate, False), (args.findings, True)):
        if not path:
            continue
        try:
            text = Path(path).read_text(encoding="utf-8-sig")
        except (OSError, UnicodeError) as exc:
            print("\nContext unavailable at {}: {}. Use the original project notes; do not invent contents."
                  .format(path, exc))
            continue
        print("\nSource: {}\n".format(path))
        print(render_document(text, "Findings" if is_findings else "Candidate", findings=is_findings))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
