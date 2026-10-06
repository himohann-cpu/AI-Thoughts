# Deterministic decides, agent explains

A rule for where to put the model, and where not to, when an agent joins a
delivery pipeline.

When I put an agent into a pipeline, I split the work in two:

- **Code decides** anything that has to be right every time, and anything that
  takes effect in the world.
- **The model explains**: it reads, groups, prioritises, drafts and proposes.

The model is the most capable part of the system and the least predictable.
So I use it where capability matters and a miss is cheap, and keep it out of
the place where predictability matters and a miss is expensive.

## Why not let the model decide?

Because of what a decision needs that an explanation does not.

A decision must be **repeatable**: the same input gives the same result next
week, after the model is upgraded. It must be **auditable**: someone can point
to the exact rule that produced it. And it must be **available**: it still
happens when the model API is down or the budget is spent.

A language model offers none of those three by default. A function does. What
the function cannot do is read a 2,000-line build log and say, in two
sentences, what went wrong. That is the trade.

## The seven principles

**1. The control must work with the model switched off.**
In my [SOC 2 agents](../SOC2_Audit_Package/soc2-compliance-agents/) every run has two phases. A deterministic phase pulls the
data, runs the checks and writes the evidence. Then an agent phase groups the
findings and writes the report. If the model is unavailable, the run falls
back to one ticket per failed check and a template report. The audit evidence
is identical either way. If removing the model breaks the control, the model
was the control.

**2. Facts travel on the deterministic path.**
Anything a reader will rely on (a count, a commit SHA, a failing check) is
inserted by code, not restated by the model. The agent can choose which
findings to group and how to describe them. It cannot change what they say.

**3. The agent proposes; a deterministic check disposes.**
Some work is judgment all the way down: diagnosing a failed build is not
something a rule can do. There the model does decide what to propose, and the
determinism moves to the other side. A proposed patch must pass
`git apply --check`. A fix counts only if the stage that failed passes
afterwards. The general form of the rule is: put determinism on whichever
side decides whether something takes effect.

**4. Saying "I don't know" is a feature.**
An agent that always answers will sometimes answer wrongly with full
confidence. My failure agents are told to return `NONE` when the log does not
support a safe fix, and the bug-fix design caps a fix's confidence at the
confidence of the diagnosis under it. I score abstention in evals as a
correct result, because a registry timeout has no code fix and the right
patch is no patch.

**5. Autonomy is earned by measurement and lost automatically.**
What an agent may do on its own is granted per action, on evidence, and
withdrawn the moment the evidence changes. A model upgrade is a new agent
holding the old agent's permissions. The
[Agent Autonomy Ladder](../Agent-Autonomy-Ladder/README.md) covers that in detail.

**6. Guardrails live in permissions, not prompts.**
"Never write to production" in a system prompt is a request. A token that
cannot write to production is a guarantee. Input from pull requests, commits
and tickets is treated as untrusted data, because anything an attacker can
type into a PR title is something the agent will read.

**7. Cost is an output.**
Every run reports what it spent, next to what it found. An agent whose cost
per useful result nobody can state is one nobody can decide to keep.

## Where I break my own rule

Applying this to my own code turned up two problems, which is the point of
writing a rule down.

- In `outerloop-pipeline`, the PR review orchestrator fails the check when any
  agent returns `FAIL`. That is a model verdict gating a merge with no
  deterministic finding behind it. It should be advisory.
- The first run of the eval harness, before any model call, showed that a
  perfect model would have scored 75%: the response parser was corrupting
  valid patches. I had no way to know that until a deterministic check
  measured it.

## What this is not

It is not a claim that models are untrustworthy, or that humans must approve
everything. It is a claim about **where** to put each kind of component. The
systems I have built got more useful, and were allowed to do more, once the
boundary was drawn clearly.
