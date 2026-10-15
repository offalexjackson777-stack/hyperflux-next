# Writing Guide

Every page in this repository is written for one reader. Decide who that is
before you write a word, and keep everything that reader does not need on a
linked page.

## The Three Kinds Of Page

| Kind | Reader | Where it lives | Sounds like |
| --- | --- | --- | --- |
| Guide | Someone who wants their keyboard and mouse to work | `README.md`, `docs/user/`, release notes | A friend explaining the steps |
| Engineering | Someone who wants to know how it works or why it was built this way | `docs/architecture/`, `docs/investigations/`, `docs/developer/`, `docs/project/journey.md` | An engineer telling the story of a problem |
| Reference | Someone looking up one exact fact | `docs/generated/` | A table with a one-line introduction |

## Guides

A guide gets one person from where they are to a working result.

- Name the task in the heading: "Install HyperFlux", not "Installation
  Procedure".
- Number the steps. Give one action per step, then the command, then what the
  reader should see.
- Write "you". Keep sentences under about 20 words.
- Show one path. Put alternatives after the main path, under a heading that
  says who needs them.
- Explain what a long command block does in one sentence above it. If the
  block is long, put it inside `<details>` and keep a short manual route next
  to it.
- Move caveats to [Known limitations](../user/limitations.md). A guide
  mentions a limitation only where the reader would otherwise get stuck.
- Use the names people already know: the receiver, your keyboard, your mouse,
  OpenRazer, Polychromatic, OpenRGB.

Words that do not belong in a guide, with what to say instead:

- route, transport, generation: say "connection".
- authority, qualified, qualification: say "supported" or "tested".
- physically proven, source-qualified operational candidate: say "tested on
  real hardware" or "expected to work".
- fail closed, non-writable: say "HyperFlux will not send lighting changes".
- stock applications: say "your apps (OpenRazer, Polychromatic, OpenRGB)".
- boundary, canonical, fixture, allowlist, topology, gate, evidence: leave
  the word out, or link to an engineering page.
- bridge, connector, edition: explain once, in plain words, where the word
  first appears.

## Engineering Pages

Technical words are welcome here, but the page still has to read as an
explanation, not a list of rules.

- Open with the problem the design solves, in two or three sentences a
  newcomer can follow.
- Define each project term the first time it appears, or link to its
  definition.
- Prefer "We chose X because Y; the cost is Z" over a bare statement of X.
- Use diagrams where they replace a paragraph, not as decoration.
- Link to the code, commit, or issue that a claim rests on.

### Investigations

`docs/investigations/` keeps one page per real problem that reached users.
Each page follows the same order:

1. **What people saw.** The symptom, in the words a user would use, with the
   affected versions and the issue link.
2. **How it was tracked down.** The logs, experiments, and wrong turns that
   led to the cause.
3. **The cause.** What actually went wrong, at the level of code and kernel
   behaviour.
4. **The fix.** What changed and why that change, with commit links.
5. **How it was checked.** Tests added and hardware checks run.
6. **What is still open.** Anything not yet proven, with its issue.

## Reference Pages

Generated pages start with one plain sentence that says what the page lists
and who would need it, then go straight to the table or list. Generator code
owns that sentence; edit the generator, not the page.

## Release Notes

GitHub release notes are guides. Start with one sentence on who should update
and why. List fixes as what the user will notice, not as what the code does.
Put the upgrade command near the top and the checksum procedure at the end.
